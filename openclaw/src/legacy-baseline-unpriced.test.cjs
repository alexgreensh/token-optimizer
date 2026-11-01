const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const {computeRealizedSavings} = require('./savings.ts');

const DAY = 86400000, T0 = Date.parse('2026-01-01T00:00:00Z'), NOW = T0 + 60 * DAY;

// A baseline frozen by an older release: shares only, no per-class mix.
function legacyBaseline(shares) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'legacy-baseline-'));
  const state = path.join(dir, 'token-optimizer'); fs.mkdirSync(state);
  const row = (id, ts, model) => ({sessionId: id, ts, model, input: 100000, output: 10000, cacheRead: 0, cacheWrite: 0, costUsd: 0, durationSec: 300});
  const rows = [row('install', T0, 'opus')];
  for (let i = 0; i < 12; i++) rows.push(row('after' + i, NOW - (i + 1) * DAY, 'sonnet'));
  fs.writeFileSync(path.join(state, 'session-history.json'), JSON.stringify(rows));
  const zero = {fi: 0, cr: 0, cw: 0, out: 0};
  fs.writeFileSync(path.join(state, 'baseline-state.json'), JSON.stringify({
    version: 2, frozenAt: T0 + 40 * DAY, installTs: T0, windowStart: T0 + DAY, windowEnd: T0 + 31 * DAY, proxy: 'opus',
    stats: {n: 35, costPerSession: 1, meanTokens: {fi: 100000, cr: 0, cw: 0, out: 10000}, effRate: zero, shares},
  }));
  return computeRealizedSavings(dir, 30, NOW);
}

test('unpriced shares of a frozen baseline are dropped, the rest renormalized, and the drop is disclosed', () => {
  const clean = legacyBaseline({opus: 1});
  const mixed = legacyBaseline({opus: 0.97, 'llama3.1:8b': 0.03});
  assert.equal(clean.ready, true);
  assert.ok(clean.monthlySavingsUsd > 0);
  assert.equal(mixed.ready, true);
  assert.equal(mixed.monthlySavingsUsd, clean.monthlySavingsUsd);
  assert.equal(mixed.baselineUnpricedShare, 0.03);
  assert.deepEqual(mixed.baselineUnpricedModels, ['llama3.1:8b']);
  assert.equal(clean.baselineUnpricedShare, 0);
});

test('a frozen baseline with no priced model at all stays unavailable and says why', () => {
  const r = legacyBaseline({'llama3.1:8b': 1});
  assert.equal(r.ready, false);
  assert.equal(r.status, 'baseline includes unpriced models');
  assert.equal(r.baselineUnpricedShare, 1);
  assert.equal(r.monthlySavingsUsd, 0);
});
