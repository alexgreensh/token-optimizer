const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { loadSource } = require('./test-source-loader.cjs');
const { runAllDetectors } = loadSource('./waste-detectors.ts');
const runs = [0,1,2].map(i => ({system:'openclaw',sessionId:String(i),agentName:'a',project:'',timestamp:new Date('2026-10-01T00:00:00Z'),durationSeconds:1,tokens:{input:100000,output:1000,cacheRead:500000,cacheWrite:100000},costUsd:50,model:'opus',rawModel:'claude-opus-5-5',runType:'heartbeat',outcome:'success',messageCount:3,toolsUsed:[],sourcePath:''}));
const routing = runAllDetectors(runs).filter(f => ['heartbeat_model_waste','overpowered_model'].includes(f.wasteType));
const concrete = {...routing[0], wasteType:'concrete_test', monthlyWasteUsd:7, recommendation:'test'};

test('public audit and generateDashboard exclude both routing values from subtotal', () => {
  assert.equal(routing.length,2);
  // A preceding test may have loaded the real entry point already.
  const warmIndex = loadSource('./index.ts');
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
  const entry = require.resolve('./index.ts');
  const index = loadSource('./index.ts', (request, filename) => {
    if (filename === entry) {
      if (request === 'fs') return {...fs, existsSync:()=>false};
      if (stubs[request]) return stubs[request];
      if (request.startsWith('./') && request !== './models') return {};
    }
  });
  assert.notEqual(index, warmIndex);
  assert.equal(index.audit(30).monthlySavingsUsd, 7);
  assert.equal(index.generateDashboard(30), '/virtual/dashboard.html');
  assert.equal(captured.monthlySavingsUsd, 7);
  assert.equal(captured.findings.length, 3);
  assert.equal(require.cache[entry], undefined);
});

test('actual dashboard renders both complete routing assumption labels', () => {
  const dashboard = loadSource('./dashboard.ts');
  const report = {scannedAt:new Date(),daysScanned:30,agentsFound:[],totalSessions:0,totalCostUsd:0,totalTokens:0,findings:routing,monthlySavingsUsd:0};
  const data = dashboard.buildDashboardData([],report);
  const html = dashboard.generateDashboardHtml(data);
  for (const finding of routing) {
    assert.ok(html.includes(finding.recommendation), finding.wasteType);
    assert.match(finding.recommendation,/20%.*Sonnet.*input and output.*API rates/);
  }
});
