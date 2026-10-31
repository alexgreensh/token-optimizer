const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const Module = require('node:module');
const {execFileSync} = require('node:child_process');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
// savings.ts imports the sqlite-backed store; the rows below bypass the database, so a stub module is enough.
const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bun-sqlite-stub-'));
const stub = path.join(stubDir, 'bun-sqlite.cjs');
fs.writeFileSync(stub, 'class Database {}\nmodule.exports = { Database };\n');
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, ...rest) {
  if (request === 'bun:sqlite') return stub;
  if (/^\.{1,2}\//.test(request) && request.endsWith('.js') && parent?.filename.endsWith('.ts')) {
    const candidate = path.resolve(path.dirname(parent.filename), request.slice(0, -3) + '.ts');
    if (fs.existsSync(candidate)) return candidate;
  }
  return resolve.call(this, request, parent, ...rest);
};
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const {computeRealizedSavings} = require('./savings.ts');

const SCRIPTS = path.resolve(__dirname, '..', '..', 'skills', 'token-optimizer', 'scripts');
const DAY = 86400000, T0 = Date.parse('2026-01-01T00:00:00Z'), NOW = T0 + 60 * DAY;
const PROBE = `
import sys, json, importlib.util, os
scripts = sys.argv[1]; spec = json.loads(sys.argv[2])
sys.path.insert(0, scripts)
s = importlib.util.spec_from_file_location("measure", os.path.join(scripts, "measure.py"))
m = importlib.util.module_from_spec(s); sys.modules["measure"] = m; s.loader.exec_module(m)
m._pretool_baseline_mix = lambda: spec["baseline"]
m._model_mix_shares = lambda days=30, since=None: {"shares": spec["mix"], "total_tokens": spec["total"]}
m._routing_usage_by_model = lambda days=30, since=None: spec["usage"]
print(json.dumps(m._compute_model_routing_savings(days=30)))
`;
const python = (() => { try { execFileSync('python3.14', ['--version'], {stdio: 'pipe'}); return 'python3.14'; } catch { return null; } })();

function scenario(beforeModel, afterModel) {
  const row = (ts, model) => ({created_at: ts / 1000, date: new Date(ts).toISOString().slice(0, 10), model,
    tokens_input: 100000, tokens_output: 10000, tokens_cache_read: 0, tokens_cache_write: 0, cost_usd: 0, duration_seconds: 300});
  const rows = [row(T0, beforeModel)];
  for (let i = 0; i < 35; i++) rows.push(row(T0 + (2 + i / 2) * DAY, beforeModel));
  for (let i = 0; i < 12; i++) rows.push(row(NOW - (i + 1) * DAY, afterModel));
  return computeRealizedSavings(os.tmpdir(), 30, NOW, rows);
}
const routing = (r) => r.breakdown.find(x => x.key === 'routing').monthlyUsd;

function pythonRouting(native, afterModel, baselineFamily) {
  if (!python) return null;
  const sessions = native.sessionsPerMonth;
  const family = /opus/.test(afterModel) ? 'opus' : /sonnet/.test(afterModel) ? 'sonnet' : afterModel;
  const spec = {
    baseline: {[baselineFamily]: 1.0}, mix: {[family]: 1.0}, total: Math.round(110000 * sessions),
    usage: {[afterModel]: {fresh_input: 100000 * sessions, output: 10000 * sessions}},
  };
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'routing-gen-py-'));
  const out = execFileSync(python, ['-I', '-c', PROBE, SCRIPTS, JSON.stringify(spec)], {
    env: {...process.env, HOME: home, TOKEN_OPTIMIZER_SNAPSHOT_DIR: path.join(home, 'snap')}, encoding: 'utf8',
  });
  return JSON.parse(out);
}

test('a vendor price change between generations is not routing savings', () => {
  const r = scenario('claude-opus-4-1', 'claude-opus-4-6');
  assert.equal(r.ready, true);
  assert.ok(Math.abs(routing(r)) < 1e-9, `routing ${routing(r)}`);
  assert.equal(r.monthlySavingsUsd, 0);
  assert.ok(r.generationPriceDeltaUsd > 0);
  const py = pythonRouting(r, 'claude-opus-4-6', 'opus');
  if (py) {
    assert.equal(py.realized_cost_usd, 0);
    assert.ok(Math.abs(py.generation_price_delta_usd - r.generationPriceDeltaUsd) < 0.011, `${py.generation_price_delta_usd} vs ${r.generationPriceDeltaUsd}`);
  }
});

test('moving work from Opus to Sonnet still counts, matching the standardized comparison', () => {
  const r = scenario('claude-opus-4-1', 'claude-sonnet-4-5');
  assert.equal(r.ready, true);
  assert.ok(routing(r) > 0);
  assert.ok(r.monthlySavingsUsd > 0);
  const py = pythonRouting(r, 'claude-sonnet-4-5', 'opus');
  if (py) {
    assert.ok(py.realized_cost_usd > 0);
    assert.ok(Math.abs(py.realized_cost_usd - routing(r)) < 0.011, `${py.realized_cost_usd} vs ${routing(r)}`);
    assert.ok(Math.abs(py.generation_price_delta_usd - r.generationPriceDeltaUsd) < 0.011, `${py.generation_price_delta_usd} vs ${r.generationPriceDeltaUsd}`);
  }
});

test('a sonnet generation change alone is not routing savings', () => {
  const r = scenario('claude-sonnet-4-5', 'claude-sonnet-5');
  assert.equal(r.ready, true);
  assert.ok(Math.abs(routing(r)) < 1e-9);
  assert.equal(r.monthlySavingsUsd, 0);
});
