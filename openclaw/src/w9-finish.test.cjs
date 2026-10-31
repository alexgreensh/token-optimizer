const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const {priceSavingsEvent} = require('./event-pricing.ts');
const {readSavingsEventsByCategory, allocateCents, computeRealizedSavings} = require('./savings.ts');

test('event pricing honors a stored billed class at the exact generation', () => {
  const dir = '';
  assert.ok(Math.abs(priceSavingsEvent(1e6, 'resume_lean', {model:'sonnet'}, dir, 'cacheWrite').cost - 2.5) < 1e-9);
  assert.ok(Math.abs(priceSavingsEvent(1e6, 'resume_lean', {model:'claude-opus-5-5'}, dir, 'cacheWrite').cost - 5.0) < 1e-9);
  // A class the provider does not bill falls back to the input rate.
  assert.ok(Math.abs(priceSavingsEvent(1e6, 'resume_lean', {model:'gpt-5.4'}, dir, 'cacheWrite').cost - 2.5) < 1e-9);
  // No class evidence -> input-rate floor, disclosed per priced token.
  const floor = priceSavingsEvent(1e6, 'checkpoint_restore', {model:'sonnet'}, dir);
  assert.ok(Math.abs(floor.cost - 2.0) < 1e-9);
  assert.equal(floor.floorTokens, 1e6);
  // Output-class events keep their evidenced class (not a floor).
  const out = priceSavingsEvent(1e6, 'verbosity_steer', {model:'sonnet'}, dir);
  assert.ok(Math.abs(out.cost - 10) < 1e-9);
  assert.equal(out.floorTokens, 0);
});

test('writer stores the billed class and the reader reprices it', () => {
  const {logSavingsEvent} = require('./read-cache.ts');
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-native', 'events-'));
  const state = path.join(dir, 'token-optimizer'); fs.mkdirSync(state);
  fs.writeFileSync(path.join(state, 'session-history.json'), JSON.stringify([{sessionId:'s',model:'sonnet'}]));
  logSavingsEvent('resume_lean', 1e6, 's', 'lean resume', {openclawDir:dir, tokenClass:'cacheWrite'});
  const row = JSON.parse(fs.readFileSync(path.join(state, 'savings-events.jsonl'), 'utf8').split('\n')[0]);
  assert.equal(row.token_class, 'cacheWrite');
  assert.ok(Math.abs(row.cost_saved_usd - 2.5) < 1e-9);
  // A historical row with stored class but missing coverage reprices at that class.
  fs.appendFileSync(path.join(state, 'savings-events.jsonl'), JSON.stringify({
    timestamp:'2026-10-01T00:00:00Z', session_id:'s', event_type:'checkpoint_restore',
    tokens_saved:1e6, detail:'old', token_class:'cacheWrite', model:'sonnet',
    cost_saved_usd:0, unpriced_tokens:1e6, pricing_status:'partial_or_unavailable'}) + '\n');
  const result = readSavingsEventsByCategory(dir);
  assert.ok(Math.abs(result.totalCostSavedUsd - 5) < 1e-9);
  assert.equal(result.inputRateFloorTokens, 0);
  assert.equal(result.inputRateFloorUsd, 0);
});

test('class-less priced savings disclose the input-rate floor', () => {
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-native', 'floor-'));
  const state = path.join(dir, 'token-optimizer'); fs.mkdirSync(state);
  fs.writeFileSync(path.join(state, 'session-history.json'), JSON.stringify([{sessionId:'s',model:'sonnet'}]));
  const rows = [
    {timestamp:'2026-10-01T00:00:00Z', session_id:'s', event_type:'checkpoint_restore', tokens_saved:1e6, cost_saved_usd:2, detail:'stored'},
    {timestamp:'2026-10-01T00:00:01Z', session_id:'s', event_type:'tool_archive', tokens_saved:1e6, detail:'new'},
    {timestamp:'2026-10-01T00:00:02Z', session_id:'s', event_type:'verbosity_steer', tokens_saved:1e6, cost_saved_usd:10, detail:'out'},
    {timestamp:'2026-10-01T00:00:03Z', session_id:'s', event_type:'checkpoint_restore', tokens_saved:1e6, cost_saved_usd:2.5, token_class:'cacheWrite', detail:'classed'},
  ];
  fs.writeFileSync(path.join(state, 'savings-events.jsonl'), rows.map(JSON.stringify).join('\n'));
  const result = readSavingsEventsByCategory(dir);
  // Floor covers the two class-less non-output rows (stored $2 + repriced $2).
  assert.ok(Math.abs(result.inputRateFloorUsd - 4) < 1e-9);
  assert.equal(result.inputRateFloorTokens, 2e6);
  assert.equal(result.unpricedEvents, 0);
});

test('largest-remainder cent allocation keeps signed and clamped sums', () => {
  assert.deepEqual(allocateCents(10.02, [5.004, 5.006, 0.01]), [5.0, 5.01, 0.01]);
  assert.deepEqual(allocateCents(-3.02, [-1.004, -2.016]), [-1.0, -2.02]);
  const flat = allocateCents(0, [-5, 3]);
  assert.equal(Math.round(flat.reduce((a, b) => a + b, 0) * 100), 0);
  const zero = allocateCents(0, []);
  assert.deepEqual(zero, []);
  // Adversarial fractions: parts always re-add to the displayed total.
  for (const parts of [[0.004, 0.006], [50.499, 0.006], [1.005, 1.005, 1.005]]) {
    const total = parts.reduce((a, b) => a + b, 0);
    const out = allocateCents(total, parts);
    assert.equal(Math.round(out.reduce((a, b) => a + b, 0) * 100), Math.round(total * 100), JSON.stringify(parts));
  }
});

test('displayed levers add to the displayed headline while raw values stay exact', () => {
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-native', 'cents-'));
  const state = path.join(dir, 'token-optimizer'); fs.mkdirSync(state);
  const day = 86400000, t = Date.parse('2026-01-01T00:00:00Z'), now = t + 60 * day;
  const row = (id, ts, model, mult) => ({sessionId:id, ts, model, input:100000*mult, output:10000*mult, cacheRead:0, cacheWrite:0, costUsd:0, durationSec:300});
  const rows = [row('install', t, 'opus', 1)];
  for (let i = 0; i < 35; i++) rows.push(row('before'+i, t + (2 + i / 2) * day, 'opus', 1));
  for (let i = 0; i < 12; i++) rows.push(row('after'+i, now - (i + 1) * day, 'sonnet', 1.003));
  fs.writeFileSync(path.join(state, 'session-history.json'), JSON.stringify(rows));
  const result = computeRealizedSavings(dir, 30, now);
  assert.equal(result.ready, true);
  const display = result.breakdown.map((b) => b.monthlyUsdDisplay);
  assert.ok(display.every((x) => typeof x === 'number' && Number.isFinite(x)));
  const raw = result.breakdown.map((b) => b.monthlyUsd);
  assert.ok(raw.every((x) => typeof x === 'number' && Number.isFinite(x)));
  assert.equal(Math.round(display.reduce((a, b) => a + b, 0) * 100), Math.round(result.monthlySavingsUsd * 100));
});

test('lean resume credits the avoided reload at the cache-write class', () => {
  const oldHome = process.env.HOME;
  const dir = fs.mkdtempSync(path.join(process.cwd(), '.scratch-native', 'lean-'));
  process.env.HOME = dir;
  const {logResumeLeanSavings} = require('./continuity.ts');
  process.env.HOME = oldHome;
  const cp = path.join(dir, 'checkpoint.md');
  fs.writeFileSync(cp, 'x'.repeat(3300));
  const calls = [];
  logResumeLeanSavings({sessionDirName:'target-session', path:cp, trigger:'test', createdAt:0}, 'lean block', (...args) => calls.push(args));
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'resume_lean');
  assert.deepEqual(calls[0][4], {tokenClass:'cacheWrite'});
});
