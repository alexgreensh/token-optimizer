const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const ts = require(process.env.W8Q_TYPESCRIPT_PATH || 'typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename,'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const { runAllDetectors } = require('./waste-detectors.ts');
const runs = [0,1,2].map(i => ({system:'openclaw',sessionId:String(i),agentName:'a',project:'',timestamp:new Date('2026-10-01T00:00:00Z'),durationSeconds:1,tokens:{input:100000,output:1000,cacheRead:500000,cacheWrite:100000},costUsd:50,model:'opus',rawModel:'claude-opus-5-5',runType:'heartbeat',outcome:'success',messageCount:3,toolsUsed:[],sourcePath:''}));
const routing = runAllDetectors(runs).filter(f => ['heartbeat_model_waste','overpowered_model'].includes(f.wasteType));
const concrete = {...routing[0], wasteType:'concrete_test', monthlyWasteUsd:7, recommendation:'test'};

test('public audit and generateDashboard exclude both routing values from subtotal', () => {
  assert.equal(routing.length,2);
  let captured;
  const stubs = {
    './session-parser': {findOpenClawDir:()=>'/virtual',scanAllSessions:()=>runs,classifyCronRuns:()=>{},extractCostlyPrompts:()=>[]},
    './pricing': {resetPricingCache:()=>{}},
    './waste-detectors': {runAllDetectors:()=>[...routing,concrete],detectUnusedSkills:()=>[]},
    './dashboard': {buildDashboardData:(_r, report)=>{captured=report; return {};},writeDashboard:()=>'/virtual/dashboard.html',buildAgentCostBreakdown:()=>[]},
    './context-audit': {auditContext:()=>({skills:[]}),getSkillUsageHistory:()=>({})},
    './quality': {scoreQuality:()=>null}, './coach': {generateCoachData:()=>null},
    './savings': {computeRealizedSavings:()=>null},
  };
  const original = Module._load;
  Module._load = function(request, parent, isMain) {
    if (parent?.filename.endsWith('/openclaw/src/index.ts')) {
      if (request === 'fs') return {...fs, existsSync:()=>false};
      if (stubs[request]) return stubs[request];
      if (request.startsWith('./') && request !== './models') return {};
    }
    return original.apply(this, arguments);
  };
  try {
    const index = require('./index.ts');
    assert.equal(index.audit(30).monthlySavingsUsd, 7);
    assert.equal(index.generateDashboard(30), '/virtual/dashboard.html');
    assert.equal(captured.monthlySavingsUsd, 7);
    assert.equal(captured.findings.length, 3);
  } finally { Module._load = original; }
});

test('actual dashboard renders both complete routing assumption labels', () => {
  const dashboard = require('./dashboard.ts');
  const report = {scannedAt:new Date(),daysScanned:30,agentsFound:[],totalSessions:0,totalCostUsd:0,totalTokens:0,findings:routing,monthlySavingsUsd:0};
  const data = dashboard.buildDashboardData([],report);
  const html = dashboard.generateDashboardHtml(data);
  for (const finding of routing) {
    assert.ok(html.includes(finding.recommendation), finding.wasteType);
    assert.match(finding.recommendation,/20%.*Haiku.*input and output.*API rates/);
  }
});
