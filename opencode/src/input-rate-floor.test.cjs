const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const Module = require('node:module');
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
  if (/^\.{1,2}\//.test(request) && request.endsWith('.js') && parent?.filename.endsWith('.ts')) {
    const candidate = path.resolve(path.dirname(parent.filename), request.slice(0, -3) + '.ts');
    if (fs.existsSync(candidate)) return candidate;
  }
  return resolve.call(this, request, parent, ...rest);
};
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const {TrendsStore} = require('./storage/trends.ts');

function storeWithSession() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'input-rate-floor-'));
  const store = new TrendsStore(dir);
  store.recordSession({sessionId:'s', project:null, model:'claude-opus-5-5', tokensInput:1, tokensOutput:0,
    tokensCacheRead:0, tokensCacheWrite:0, costUsd:0, resourceHealth:null, sessionEfficiency:null,
    toolCalls:0, compactions:0, mode:null, durationSeconds:1});
  return store;
}

test('the input-rate floor is the net counted archive amount, not gross re-expansions', () => {
  const store = storeWithSession();
  // Opus 5.5 reads fresh input at $4/MTok: $1.00 archived, $0.80 re-expanded, $0.20 counted.
  store.logSavingsEvent('tool_archive', 250000, 's', 'archived', 'claude-opus-5-5');
  store.logSavingsEvent('tool_archive_reexpand', 200000, 's', 're-expanded', 'claude-opus-5-5');
  const result = store.getCompressionSavings();
  assert.ok(Math.abs(result.totalCostSavedUsd - 0.2) < 1e-9);
  assert.ok(Math.abs(result.inputRateFloorUsd - 0.2) < 1e-9, `${result.inputRateFloorUsd}`);
  assert.equal(result.inputRateFloorTokens, 50000);
  store.close();
});

test('re-expansions that exceed the archive leave no floor', () => {
  const store = storeWithSession();
  store.logSavingsEvent('tool_archive', 100000, 's', 'archived', 'claude-opus-5-5');
  store.logSavingsEvent('tool_archive_reexpand', 300000, 's', 're-expanded', 'claude-opus-5-5');
  const result = store.getCompressionSavings();
  assert.equal(result.totalCostSavedUsd, 0);
  assert.equal(result.inputRateFloorUsd, 0);
  assert.equal(result.inputRateFloorTokens, 0);
  store.close();
});

test('classed archive credits are not part of the floor even when re-expansions lack a class', () => {
  const store = storeWithSession();
  store.logSavingsEvent('tool_archive', 250000, 's', 'archived', 'claude-opus-5-5', 'input');
  store.logSavingsEvent('tool_archive_reexpand', 200000, 's', 're-expanded', 'claude-opus-5-5');
  const result = store.getCompressionSavings();
  assert.equal(result.inputRateFloorUsd, 0);
  store.close();
});
