const fs = require('node:fs');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { runAllDetectors } = require('./waste-detectors.ts');
const { getPricing } = require('./pricing.ts');

const run = (i, model, input, output = 1000, family = model) => ({system:'openclaw', sessionId:String(i), agentName:'a', project:'', timestamp:new Date('2026-10-01T00:00:00Z'), durationSeconds:1,
  tokens:{input, output, cacheRead:0, cacheWrite:0}, costUsd:1, model:family, rawModel:model, runType:'heartbeat', outcome:'success', messageCount:3, toolsUsed:[], sourcePath:''});
const heartbeat = (runs) => runAllDetectors(runs).find(f => f.wasteType === 'heartbeat_model_waste');

test('a long-context OpenAI card is not selected by aggregate input tokens', () => {
  // gpt-5.4 has no lighter Claude tier, so it contributes no routing figure at all.
  assert.equal(heartbeat([0,1,2].map(i => run(i, 'gpt-5.4', 400000))), undefined);
});

test('Sonnet work is priced against base Haiku cards however many tokens the window adds up to', () => {
  const p = getPricing();
  const haiku = Object.keys(p).filter(k => /^haiku-\d+(-\d+)?$/.test(k)).sort((a, b) => { const [x, y] = [a, b].map(k => k.split('-').slice(1).map(Number)); return (x[0] - y[0]) || ((x[1] ?? 0) - (y[1] ?? 0)); }).pop();
  const input = 400000, output = 5000;
  const f = heartbeat([0,1,2].map(i => run(i, 'sonnet', input, output)));
  const expected = 3 * (input * (p.sonnet.input - p[haiku].input) + output * (p.sonnet.output - p[haiku].output)) * .2 * 30;
  assert.ok(f);
  assert.ok(Math.abs(f.monthlyWasteUsd - expected) < 1e-9, `${f.monthlyWasteUsd} != ${expected}`);
  assert.match(f.recommendation, /ran on Haiku/);
  assert.match(f.fixSnippet, /"model": "haiku"/);
});

test('Opus work routes to Sonnet, not Haiku', () => {
  const f = heartbeat([0,1,2].map(i => run(i, 'claude-opus-5-5', 100000, 1000, 'opus')));
  assert.ok(f);
  assert.match(f.recommendation, /ran on Sonnet/);
  assert.match(f.fixSnippet, /"model": "sonnet"/);
  const p = getPricing();
  const expected = 3 * (100000 * (p['opus-5-5'].input - p['sonnet-5-5'].input) + 1000 * (p['opus-5-5'].output - p['sonnet-5-5'].output)) * .2 * 30;
  assert.ok(Math.abs(f.monthlyWasteUsd - expected) < 1e-9);
});
