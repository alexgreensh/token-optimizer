const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const Module = require('node:module');
const {execFileSync} = require('node:child_process');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, ...rest) {
  if (/^\.{1,2}\//.test(request) && request.endsWith('.js') && parent?.filename.endsWith('.ts')) {
    const candidate = path.resolve(path.dirname(parent.filename), request.slice(0, -3) + '.ts');
    if (fs.existsSync(candidate)) return candidate;
  }
  return resolve.call(this, request, parent, ...rest);
};
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const {knownModelRate} = require('./pricing.ts');

const SCRIPTS = path.resolve(__dirname, '..', '..', 'skills', 'token-optimizer', 'scripts');
const PRICED = [
  'us.anthropic.claude-sonnet-4-5-20250929-v1:0', 'global.anthropic.claude-sonnet-4-5-20250929-v1:0',
  'eu.anthropic.claude-opus-4-1-20250805-v1:0', 'amazon-bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0',
  'anthropic.claude-opus-4-1-20250805-v1:0', 'claude-opus-4-6[1m]', 'claude-opus-4-6-thinking', 'claude-sonnet-4-5-thinking[1m]',
  'github-copilot/claude-sonnet-4.5', 'openrouter/anthropic/claude-opus-4.5', 'claude-sonnet-4-5@20250929',
  'claude-opus-4-1@20250805', 'anthropic/claude-sonnet-4-5', 'claude-3-5-sonnet-20241022', 'claude-3-7-sonnet-latest',
  'claude-opus-5-5', 'gpt-5.4', 'openai/gpt-5.4-2026-03-05', 'google/gemini-2.5-pro',
];
const UNPRICED = [
  'gpt-5.99', 'claude-opus-99', 'claude-opus-5-99', 'us.anthropic.claude-opus-99-v1:0', 'claude-opus-5-99[1m]',
  'github-copilot/claude-opus-99', 'openrouter/anthropic/claude-opus-5-99', 'gpt-5.4-unlisted-variant', 'deepseek-v99',
  'llama3.1:8b', 'claude-opus-99@20260101',
];
const PROBE = `
import sys, json, importlib.util, os
scripts = sys.argv[1]; ids = json.loads(sys.argv[2])
sys.path.insert(0, scripts)
s = importlib.util.spec_from_file_location("measure", os.path.join(scripts, "measure.py"))
m = importlib.util.module_from_spec(s); sys.modules["measure"] = m; s.loader.exec_module(m)
out = {}
for i in ids:
    r = m._routing_class_rates(i)
    out[i] = None if r is None else {"input": r["fresh_input"], "output": r["output"]}
print(json.dumps(out))
`;
const python = (() => { try { execFileSync('python3.14', ['--version'], {stdio: 'pipe'}); return 'python3.14'; } catch { return null; } })();
function pythonRates(ids) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'route-res-py-'));
  return JSON.parse(execFileSync(python, ['-I', '-c', PROBE, SCRIPTS, JSON.stringify(ids)], {
    env: {...process.env, HOME: home, TOKEN_OPTIMIZER_SNAPSHOT_DIR: path.join(home, 'snap')}, encoding: 'utf8',
  }));
}

test('documented provider routes resolve to a priced card', () => {
  for (const id of PRICED) assert.notEqual(knownModelRate(id, 'input'), null, id);
  assert.equal(knownModelRate('us.anthropic.claude-sonnet-4-5-20250929-v1:0', 'input'), knownModelRate('claude-sonnet-4-5', 'input'));
  assert.equal(knownModelRate('claude-opus-4-6[1m]', 'input'), knownModelRate('claude-opus-4-6', 'input'));
  assert.equal(knownModelRate('github-copilot/claude-sonnet-4.5', 'input'), knownModelRate('claude-sonnet-4-5', 'input'));
});

test('unknown generations and models stay unpriced behind any route', () => {
  for (const id of UNPRICED) assert.equal(knownModelRate(id, 'input'), null, id);
});

test('route resolution agrees with the Python resolver on priced status and rates', { skip: !python }, () => {
  const py = pythonRates([...PRICED, ...UNPRICED]);
  for (const id of PRICED) {
    assert.ok(py[id], `python prices ${id}`);
    assert.ok(Math.abs(knownModelRate(id, 'input') * 1e6 - py[id].input) < 1e-6, `${id}: ${knownModelRate(id, 'input') * 1e6} vs ${py[id].input}`);
    assert.ok(Math.abs(knownModelRate(id, 'output') * 1e6 - py[id].output) < 1e-6, `${id} output`);
  }
  for (const id of UNPRICED) assert.equal(py[id], null, `python leaves ${id} unpriced`);
});
