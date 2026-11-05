const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const { readSavingsEventsByCategory } = require('./savings.ts');

function ledger(rows, readEvents = readSavingsEventsByCategory) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'input-rate-floor-'));
  const state = path.join(dir, 'token-optimizer'); fs.mkdirSync(state);
  fs.writeFileSync(path.join(state, 'session-history.json'), JSON.stringify([{sessionId:'s', model:'sonnet'}]));
  const base = {session_id:'s', detail:'x'};
  fs.writeFileSync(path.join(state, 'savings-events.jsonl'), rows.map((r, i) => JSON.stringify({...base, timestamp:`2026-10-01T00:00:${String(i * 5).padStart(2, '0')}Z`, ...r})).join('\n'));
  return readEvents(dir);
}

test('the input-rate floor is the net counted archive amount, not gross re-expansions', () => {
  // Sonnet reads fresh input at $2/MTok: $1.00 archived, $0.80 re-expanded, $0.20 counted.
  const result = ledger([
    {event_type:'tool_archive', tokens_saved:500000},
    {event_type:'tool_archive_reexpand', tokens_saved:400000},
  ]);
  assert.ok(Math.abs(result.totalCostSavedUsd - 0.2) < 1e-9);
  assert.ok(Math.abs(result.inputRateFloorUsd - 0.2) < 1e-9, `${result.inputRateFloorUsd}`);
  assert.equal(result.inputRateFloorTokens, 100000);
});

test('estimated-tier events are not part of the counted input-rate floor', () => {
  const result = ledger([
    {event_type:'checkpoint_restore', tokens_saved:1000000},
    {event_type:'hint_followed', tokens_saved:1000000},
    {event_type:'setup_optimization', tokens_saved:1000000},
  ]);
  assert.equal(result.inputRateFloorUsd, 0);
  assert.equal(result.inputRateFloorTokens, 0);
});

test('re-expansions that exceed the archive leave no floor', () => {
  const result = ledger([
    {event_type:'tool_archive', tokens_saved:100000},
    {event_type:'tool_archive_reexpand', tokens_saved:300000},
  ]);
  assert.equal(result.inputRateFloorUsd, 0);
  assert.equal(result.inputRateFloorTokens, 0);
});

test('packaged runtime labels restores as estimates and reports turn economics', () => {
  const built = require('../dist/savings.js');
  const result = ledger([
    {event_type:'checkpoint_restore', tokens_saved:1000000},
    {event_type:'resume_lean', tokens_saved:1000000},
  ], built.readSavingsEventsByCategory);
  assert.equal(result.inputRateFloorUsd, 0);
  assert.equal(built.measuredSavingsFromSummary(result), 0);
  assert.equal(result.categories.length, 2);
  assert.ok(result.categories.every((c) => c.label.endsWith('[est]')));
  const quality = require('../dist/quality.js');
  assert.equal(quality.freshSessionSavingsUsd, undefined);
  const message = quality.buildFreshSessionNudgeMessage(49, 60, true, 'sonnet', 1000000, 1000);
  assert.ok(message.includes('per turn') && message.includes('pays back after') && message.includes('est.'));
  assert.ok(!message.includes('rebuilt for free'));
});
