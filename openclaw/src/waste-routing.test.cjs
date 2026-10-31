const fs = require('node:fs');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { runAllDetectors } = require('./waste-detectors.ts');
const make = (i, type = 'heartbeat') => ({system:'openclaw', sessionId:String(i), agentName:'a', project:'', timestamp:new Date('2026-10-01T00:00:00Z'), durationSeconds:1, tokens:{input:100000,output:1000,cacheRead:9000000,cacheWrite:9000000}, costUsd:9999, model:i === 0 ? 'opus' : 'sonnet', rawModel:i === 0 ? 'claude-opus-5-5' : 'sonnet', runType:type, outcome:'success', messageCount:3, toolsUsed:[], sourcePath:''});
const find = (runs, type) => runAllDetectors(runs).find(f => f.wasteType === type);
for (const [kind, runType] of [['heartbeat_model_waste', 'heartbeat'], ['overpowered_model', 'manual']]) {
  test(`${kind} uses 20% fresh input and output, mixed known cards`, () => {
    delete process.env.TOKEN_OPTIMIZER_ROUTABLE_OPUS_FRACTION;
    const runs = [0,1,2].map(i => make(i,runType));
    const f = find(runs,kind);
    // Opus 5.5 $4/$20 and two Sonnet $2/$10, Haiku $1/$5 per million.
    const expected = ((.4+.02-.105)+2*(.2+.01-.105))*.2*30;
    assert.ok(f); assert.ok(Math.abs(f.monthlyWasteUsd-expected)<1e-9, `${f.monthlyWasteUsd} != ${expected}`);
    assert.equal(f.monthlyWasteTokens, 3*101000*.2*30);
    assert.match(f.recommendation, /20%/); assert.match(f.recommendation, /assumption/i); assert.match(f.recommendation, /input and output/i);
    const unchanged = runs.map(r => ({...r, costUsd:1, tokens:{...r.tokens,cacheRead:0,cacheWrite:0}}));
    assert.equal(find(unchanged,kind).monthlyWasteUsd, f.monthlyWasteUsd);
  });
  test(`${kind} respects override`, () => {
    process.env.TOKEN_OPTIMIZER_ROUTABLE_OPUS_FRACTION='0.4';
    const f = find([0,1,2].map(i=>make(i,runType)),kind);
    assert.match(f.recommendation, /40%/);
    assert.ok(Math.abs(f.monthlyWasteUsd - 6.3) < 1e-9);
    assert.equal(f.monthlyWasteTokens, 3636000);
    delete process.env.TOKEN_OPTIMIZER_ROUTABLE_OPUS_FRACTION;
  });
}
test('mixed model buckets use generation cards and omit unknown models', () => {
  const runs = [0,1,2].map(i => make(i));
  runs[0].modelTokenBuckets = {
    'claude-opus-5-5': {input:100000,output:1000,cacheRead:1e9,cacheWrite:1e9,cacheWrite1h:0,cacheWrite5m:0},
    'claude-opus-99-99': {input:1e9,output:1e9,cacheRead:0,cacheWrite:0,cacheWrite1h:0,cacheWrite5m:0}
  };
  const f = find(runs,'heartbeat_model_waste');
  assert.ok(Math.abs(f.monthlyWasteUsd-3.15)<1e-9);
  assert.equal(f.monthlyWasteTokens, 1818000);
});

test('monthly token and dollar estimates scale consistently for 15 and 30 days', () => {
  const runs = [0,1,2].map(i => make(i));
  runs[1].timestamp = new Date('2026-10-08T12:00:00Z');
  runs[2].timestamp = new Date('2026-10-16T00:00:00Z');
  const fifteen = find(runs,'heartbeat_model_waste');
  runs[2].timestamp = new Date('2026-10-31T00:00:00Z');
  const thirty = find(runs,'heartbeat_model_waste');
  assert.equal(fifteen.monthlyWasteTokens, thirty.monthlyWasteTokens * 2);
  assert.equal(fifteen.monthlyWasteUsd, thirty.monthlyWasteUsd * 2);
});
test('unknown generation aliases do not inherit a family price', () => {
  for (const raw of ['opus-99', 'anthropic/claude-opus-99-99', 'anthropic/claude-opus-5-99']) {
    const runs = [0,1,2].map(i => ({...make(i), rawModel:raw}));
    assert.equal(find(runs,'heartbeat_model_waste'), undefined, raw);
  }
});

test('custom card resolution and pricing share the same cached table', () => {
  const pricing = require('./pricing.ts');
  const { resolvePricedModel } = require('./event-pricing.ts');
  const table = pricing.getPricing();
  table['custom-card'] = {input:8e-6,output:40e-6,cacheRead:0,cacheWrite:0};
  try {
    assert.equal(pricing.getPricing(''), table);
    assert.equal(resolvePricedModel('custom-card',''), 'custom-card');
    const runs = [0,1,2].map(i => ({...make(i), rawModel:'custom-card'}));
    const f = find(runs,'heartbeat_model_waste');
    assert.ok(Math.abs(f.monthlyWasteUsd - (3*(.8+.04-.105)*.2*30)) < 1e-9);
  } finally { delete table['custom-card']; }
});
