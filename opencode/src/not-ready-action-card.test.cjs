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
    if (/^\.{1,2}\//.test(request) && request.endsWith('.js') && parent?.filename.endsWith('.ts')) {
      const candidate = path.resolve(path.dirname(parent.filename), request.slice(0, -3) + '.ts');
      if (fs.existsSync(candidate)) return candidate;
    }
    return resolve.call(this, request, parent, ...rest);
  };
  require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
}
const {TrendsStore} = require('./storage/trends.ts');

function seededStore() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'not-ready-card-'));
  const store = new TrendsStore(dir);
  store.recordSession({sessionId:'s', project:null, model:'claude-opus-5-5', tokensInput:1, tokensOutput:0,
    tokensCacheRead:0, tokensCacheWrite:0, costUsd:0, resourceHealth:null, sessionEfficiency:null,
    toolCalls:0, compactions:0, mode:null, durationSeconds:1});
  return {dir, store};
}
const cents = (text) => Math.round(parseFloat(text.replace('$', '')) * 100);

test('the not-ready action card parts add up to its total', () => {
  const {dir, store} = seededStore();
  // Opus 5.5: fresh input $4/MTok and output $20/MTok, so each event is worth half a cent.
  store.logSavingsEvent('tool_archive', 1250, 's', 'logged', 'claude-opus-5-5');
  store.logSavingsEvent('verbosity_steer', 250, 's', 'estimated', 'claude-opus-5-5');
  store.close();
  const {generateDashboard} = require('./dashboard/generator.ts');
  const html = generateDashboard({dataDir: dir});
  const card = /data-card="action-savings"[\s\S]*?<strong>(\$[\d.]+)<\/strong>\s*<div>Logged actions (\$[\d.]+) &middot; Other estimates (\$[\d.]+)<\/div>/.exec(html);
  assert.ok(card, 'not-ready action card is rendered');
  assert.equal(cents(card[1]), 1);
  assert.equal(cents(card[2]) + cents(card[3]), cents(card[1]));
});

test('the not-ready action card keeps whole-cent parts unchanged', () => {
  const {dir, store} = seededStore();
  store.logSavingsEvent('tool_archive', 2500000, 's', 'logged', 'claude-opus-5-5');
  store.logSavingsEvent('verbosity_steer', 500000, 's', 'estimated', 'claude-opus-5-5');
  store.close();
  const {generateDashboard} = require('./dashboard/generator.ts');
  const card = /data-card="action-savings"[\s\S]*?<strong>(\$[\d.]+)<\/strong>\s*<div>Logged actions (\$[\d.]+) &middot; Other estimates (\$[\d.]+)<\/div>/.exec(generateDashboard({dataDir: dir}));
  assert.deepEqual(card.slice(1), ['$20.00', '$10.00', '$10.00']);
});

test('restore and resume credits appear only in Other estimates', () => {
  const {dir, store} = seededStore();
  store.logSavingsEvent('checkpoint_restore', 1000000, 's', 'restore', 'claude-opus-5-5');
  store.logSavingsEvent('resume_lean', 1000000, 's', 'resume', 'claude-opus-5-5');
  const savings = store.getCompressionSavings();
  assert.equal(savings.totalCostSavedUsd, 0);
  store.close();
  const {computeRealizedSavings} = require('./savings.ts');
  assert.equal(computeRealizedSavings(dir).counterfactualEstimatedWindowUsd, 8);
  const {generateDashboard} = require('./dashboard/generator.ts');
  const card = /data-card="action-savings"[\s\S]*?<strong>(\$[\d.]+)<\/strong>\s*<div>Logged actions (\$[\d.]+) &middot; Other estimates (\$[\d.]+)<\/div>/.exec(generateDashboard({dataDir: dir}));
  assert.ok(card);
  assert.deepEqual(card.slice(1), ['$8.00', '$0.00', '$8.00']);
});
