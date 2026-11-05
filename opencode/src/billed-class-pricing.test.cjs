const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const Module = require('node:module');
// Under bun, the real bun:sqlite and bun's own TypeScript loader are used.
// Under node, bun:sqlite is stood in for by node:sqlite and .ts is transpiled.
if (typeof Bun === 'undefined') {
  const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
  // bun:sqlite is stood in for by node:sqlite so the store runs under node.
  const shimDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bun-sqlite-shim-'));
  const shim = path.join(shimDir, 'bun-sqlite.cjs');
  fs.writeFileSync(shim, `const { DatabaseSync } = require('node:sqlite');
  class Database {
    constructor(file) { this.db = new DatabaseSync(file); }
    exec(sql) { return this.db.exec(sql); }
    run(sql, params) { return this.db.prepare(sql).run(...(params ?? [])); }
    prepare(sql) { return this.db.prepare(sql); }
    query(sql) { const s = this.db.prepare(sql); return { all: (...p) => s.all(...p), get: (...p) => s.get(...p), run: (...p) => s.run(...p) }; }
    close() { return this.db.close(); }
  }
  module.exports = { Database };
  `);
  const resolve = Module._resolveFilename;
  Module._resolveFilename = function(request, parent, ...rest) {
    if (request === 'bun:sqlite') return shim;
    if (request.startsWith('./') && request.endsWith('.js') && parent?.filename.endsWith('.ts')) {
      const candidate = path.resolve(path.dirname(parent.filename), request.slice(0, -3) + '.ts');
      if (fs.existsSync(candidate)) return candidate;
    }
    if (request.startsWith('../') && request.endsWith('.js') && parent?.filename.endsWith('.ts')) {
      const candidate = path.resolve(path.dirname(parent.filename), request.slice(0, -3) + '.ts');
      if (fs.existsSync(candidate)) return candidate;
    }
    return resolve.call(this, request, parent, ...rest);
  };
  require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
}
const {Database} = require('bun:sqlite');  // the node stand-in, via the hook above, under node
const {TrendsStore} = require('./storage/trends.ts');
const {computeRealizedSavings, allocateCents} = require('./savings.ts');

const mkdir = (name) => fs.mkdtempSync(path.join(os.tmpdir(), 'to-native-' + name));

test('savings events store and honor billed class evidence', () => {
  const dir = mkdir('class-');
  const store = new TrendsStore(dir);
  store.recordSession({sessionId:'s', project:null, model:'claude-opus-5-5', tokensInput:1, tokensOutput:0,
    tokensCacheRead:0, tokensCacheWrite:1e6, costUsd:0, resourceHealth:null, sessionEfficiency:null,
    toolCalls:0, compactions:0, mode:null, durationSeconds:1});
  store.logSavingsEvent('resume_lean', 1e6, 's', 'lean resume', null, 'cacheWrite');
  const db = new Database(path.join(dir, 'trends.db'));
  assert.equal(db.query('SELECT token_class FROM savings_events').get().token_class, 'cacheWrite');
  const result = store.getCompressionSavings();
  // The avoided reload reprices at the session's cache-write card ($5/MTok), not the $4 input floor.
  assert.ok(Math.abs(result.totalCostSavedUsd - 5) < 1e-9);
  assert.equal(result.inputRateFloorUsd, 0);
  assert.equal(result.inputRateFloorTokens, 0);
  store.close(); db.close();
});

test('class-less priced savings disclose the input-rate floor', () => {
  const dir = mkdir('floor-');
  const store = new TrendsStore(dir);
  store.recordSession({sessionId:'s', project:null, model:'claude-opus-5-5', tokensInput:1, tokensOutput:0,
    tokensCacheRead:0, tokensCacheWrite:0, costUsd:0, resourceHealth:null, sessionEfficiency:null,
    toolCalls:0, compactions:0, mode:null, durationSeconds:1});
  store.logSavingsEvent('checkpoint_restore', 1e6, 's', 'no class', 'claude-opus-5-5');
  store.logSavingsEvent('resume_lean', 1e6, 's', 'classed', 'claude-opus-5-5', 'cacheWrite');
  const result = store.getCompressionSavings();
  assert.ok(Math.abs(result.totalCostSavedUsd - 9) < 1e-9);
  assert.ok(Math.abs(result.inputRateFloorUsd - 4) < 1e-9);
  assert.equal(result.inputRateFloorTokens, 1e6);
  store.close();
});

test('displayed levers add to the displayed headline while raw values stay exact', () => {
  const dir = mkdir('cents-');
  const day = 86400, t = 1750000000, now = t + 60 * day;
  const row = (id, ts, model) => ({session_id:id, created_at:ts, date:new Date(ts * 1000).toISOString(), model, tokens_input:100000,
    tokens_output:10000, tokens_cache_read:0, tokens_cache_write:0, cost_usd:0, duration_seconds:300});
  const rows = [row('install', t, 'opus')];
  for (let i = 0; i < 35; i++) rows.push(row('before'+i, t + (2 + i / 2) * day, 'opus'));
  for (let i = 0; i < 12; i++) rows.push(row('after'+i, now - (i + 1) * day, 'sonnet'));
  const result = computeRealizedSavings(dir, 30, now * 1000, rows, 0.0049, 1);
  assert.equal(result.ready, true);
  const display = result.breakdown.map((b) => b.monthlyUsdDisplay);
  assert.ok(display.every((x) => typeof x === 'number' && Number.isFinite(x)));
  assert.equal(Math.round(display.reduce((a, b) => a + b, 0) * 100), Math.round(result.monthlySavingsUsd * 100));
  assert.ok(result.breakdown.every((b) => typeof b.monthlyUsd === 'number' && Number.isFinite(b.monthlyUsd)));
});

test('opencode cent allocation matches the openclaw helper', () => {
  assert.deepEqual(allocateCents(10.02, [5.004, 5.006, 0.01]), [5.0, 5.01, 0.01]);
  assert.deepEqual(allocateCents(-3.02, [-1.004, -2.016]), [-1.0, -2.02]);
  const flat = allocateCents(0, [-5, 3]);
  assert.equal(Math.round(flat.reduce((a, b) => a + b, 0) * 100), 0);
});

test('dashboard discloses the input-rate floor beside the measured dollars', () => {
  const dir = mkdir('dash-');
  const store = new TrendsStore(dir);
  store.recordSession({sessionId:'s', project:null, model:'claude-opus-5-5', tokensInput:1, tokensOutput:0,
    tokensCacheRead:0, tokensCacheWrite:0, costUsd:0, resourceHealth:null, sessionEfficiency:null,
    toolCalls:0, compactions:0, mode:null, durationSeconds:1});
  store.logSavingsEvent('checkpoint_restore', 1e6, 's', 'no class', 'claude-opus-5-5');
  store.close();
  const {generateDashboard} = require('./dashboard/generator.ts');
  const html = generateDashboard({dataDir:dir});
  assert.ok(html.includes('input rate, a minimum'));
});
