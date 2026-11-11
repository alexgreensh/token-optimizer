// Savings story (U9): the Savings tab, top to bottom. Savings are big and
// confident; the measured / estimated breakdown lives only in the folds.
//   1. Hero band: three huge numbers (30-day total with routing included,
//      saved since install with routing included, tokens kept out of context).
//   2. Transformation (when the payload says ok) and "Your limits go further".
//   3. Still on the table: computable opportunities per period, warm accent,
//      each with one action. Never part of a savings total.
//   4. The re-read card, cost per message only when it fell at least 10%, then
//      the caller's row of folds.
// healthNotesHtml() renders the parts that moved to the Health tab.
//
// Inlined into dashboard.html at generation by scripts/dashboard_assets.py.
// Every reader is fail-open: a missing or malformed field drops its row or
// card, never throws. With no session_effect it returns '' and the Savings
// tab keeps its previous layout.
function savingsStoryOn(data) {
  var se = data && data.savings && data.savings.session_effect;
  return !!(se && se.available);
}

function savingsStoryFmt() {
  function num(x) { var v = Number(x); return isFinite(v) ? v : 0; }
  return {
    num: num,
    has: function(x) { return x != null && x !== '' && isFinite(Number(x)); },
    // Whole dollars everywhere on the tab; cents stay in the ledger fold.
    usd: function(v) {
      v = num(v);
      var a = Math.abs(v);
      if (a > 0 && a < 1) return 'under $1';  // sign is carried by the label
      return (v < 0 ? '-' : '') + '$' + Math.round(a).toLocaleString('en-US');
    },
    tok: function(v) {
      v = num(v);
      if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
      if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
      if (v >= 1e3) return Math.round(v / 1e3) + 'K';
      return String(Math.round(v));
    },
    pct: function(f) { return (num(f) * 100).toFixed(1).replace(/\.0$/, '') + '%'; },
    count: function(v) { return Math.round(num(v)).toLocaleString('en-US'); },
    day: function(iso) {
      if (!iso) return '';
      var d = new Date(String(iso).length <= 10 ? iso + 'T00:00:00' : iso);
      if (isNaN(d.getTime())) return '';
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    },
    fold: function(summary, body, cls) {
      return '<details class="fold' + (cls ? ' ' + cls : '') + '"><summary>' + esc(summary) +
        '</summary><div class="fold__body">' + body + '</div></details>';
    }
  };
}

// The hero rows: measured and estimated, each {key,label,usd,tok,why}. Shared
// by the hero card and the ledger fold so the two can never disagree.
function savingsStoryBands(data) {
  var f = savingsStoryFmt(), num = f.num, has = f.has;
  var s = (data && data.savings) || {};
  var as = s.action_savings || {}, th = s.token_headline || {}, sd = s.structural_detail || {}, orr = s.output_reread || {};
  var measuringNames = [];
  var trimLedger = sd.method === 'trim_ledger';
  var trims = trimLedger ? (sd.trims || []).filter(function(t) { return t && !t.closed_at && num(t.tokens_per_call) > 0; }) : [];
  function trimName(tier) {
    var group = trims.filter(function(x) { return (x.source === 'attested' || x.tier === 'estimated') === (tier === 'estimated'); })
      .sort(function(a, b) { return String(b.ts).localeCompare(String(a.ts)); });
    var t = group[0], when = t && group.every(function(x) { return String(x.ts).slice(0, 10) === String(t.ts).slice(0, 10); }) ? f.day(t.ts) : '';
    var stepped = group.some(function(x) { return x.step_measured; });
    return 'Setup cleanup' + (when ? ', ' + when : '') + (tier === 'estimated' ? (t && t.source === 'dated_archive' ? ' (realized, archive estimate)' : ' (realized, your figure)') : stepped ? ' (realized, measured)' : ' (realized, recorded)');
  }
  var trimMeasuredUsd = trimLedger ? num(sd.cost_saved_usd) : 0;
  var trimMeasuredTok = trimLedger ? num(sd.tokens_saved) : 0;
  var trimEst = (trimLedger && sd.estimated) || {};
  var est = as.estimates_usd || {};
  var outputInEst = num(est.verbosity_output_estimated);
  var trimInEst = num(est.structural_estimated);
  var reloadTok = num(th.reload_avoided_tokens);
  var removedTok = has(th.context_removed_tokens)
    ? num(th.context_removed_tokens) + num(th.output_not_generated_tokens)
    : num(th.removed_tokens) - reloadTok;
  var measured = [
    { key: 'context', label: 'Context removed', usd: num(as.logged_usd) - trimMeasuredUsd + num(as.class_premium_usd),
      tok: Math.max(0, removedTok - trimMeasuredTok), on: has(as.logged_usd) },
    { key: 'rereads', label: 'Re-reads avoided', usd: as.reread_usd, tok: th.reread_tokens,
      on: as.reread_available !== false && has(as.reread_usd) },
    { key: 'setup_measured', label: trimName('measured'), usd: trimMeasuredUsd, tok: trimMeasuredTok, on: trimLedger && trimMeasuredUsd > 0 }
  ];
  var estimated = [
    { key: 'setup_estimated', label: trimName('estimated'), usd: trimInEst, tok: trimEst.tokens_saved,
      on: trimInEst > 0 },
    { key: 'output_reread', label: 'Shorter answers, not re-read', usd: outputInEst, tok: orr.tokens_saved,
      on: outputInEst > 0,
      why: orr.control_net == null && orr.available ? 'Not yet compared against sessions without the nudge.' : '' },
    { key: 'other', label: 'Other estimates', usd: num(as.estimated_usd) - outputInEst - trimInEst, tok: reloadTok,
      on: Math.abs(num(as.estimated_usd) - outputInEst - trimInEst) > 0,
      why: 'Loops stopped, sub-agent output, handovers and restores.' }
  ];
  function live(rows) {
    return rows.filter(function(r) { return r.on && num(r.usd) !== 0; })
      .sort(function(a, b) { return num(b.usd) - num(a.usd); });
  }
  measured = live(measured); estimated = live(estimated);
  if (!trimLedger) measuringNames.push('setup cleanup');
  if (!orr.available && !has(orr.cost_saved_usd)) measuringNames.push('shorter answers that are not re-read');
  function sum(rows, k) { return rows.reduce(function(a, r) { return a + num(r[k]); }, 0); }
  var mUsd = sum(measured, 'usd'), eUsd = sum(estimated, 'usd');
  var totalUsd = mUsd + eUsd, totalTok = sum(measured, 'tok') + sum(estimated, 'tok');
  return { measured: measured, estimated: estimated, measuringNames: measuringNames, trimLedger: trimLedger, trims: trims,
           mUsd: mUsd, eUsd: eUsd, totalUsd: totalUsd, totalTok: totalTok };
}

function savingsStoryHtml(data) {
  if (!savingsStoryOn(data)) return '';
  var f = savingsStoryFmt(), num = f.num, has = f.has, usd = f.usd, tok = f.tok, pct = f.pct, count = f.count;
  var s = data.savings || {};
  var se = s.session_effect || {};
  var ps = se.per_session || {};
  var places = ps.by_place || {}, local = places.local || {}, cloud = places.cloud || {};
  var cov = se.coverage || {};
  var fleet = se.fleet || {};
  var as = s.action_savings || {};
  var th = s.token_headline || {};
  var cp = s.counted_period || {};
  var sd = s.structural_detail || {};
  var orr = s.output_reread || {};
  var mr = s.model_routing || {};
  var days = Number(s.period_days) || 30;
  var out = '';

  // ---- 1. Hero: three huge numbers. The breakdown lives only in the folds. ----
  // Measured and estimated savings, plus the routing estimate, are one number.
  // Setup cleanup recorded by the trim ledger sits inside logged_usd (the
  // structural pool always has), so it is its own row, taken out of
  // "Context removed" once.
  var B = savingsStoryBands(data);
  var trimLedger = B.trimLedger, trims = B.trims, measured = B.measured, estimated = B.estimated;
  var mUsd = B.mUsd, eUsd = B.eUsd, totalUsd = B.totalUsd, totalTok = B.totalTok;
  var routing30 = has(mr.realized_cost_usd) ? num(mr.realized_cost_usd) : num(as.routing_usd);
  if (!(routing30 > 0)) routing30 = 0;
  var bigTotal = totalUsd + routing30;
  function sum(rows, k) { return rows.reduce(function(a, r) { return a + num(r[k]); }, 0); }
  if (measured.length || estimated.length) {
    function cents(rows) { return rows.reduce(function(n, r) { return n + Math.round(num(r.usd) * 100); }, 0); }
    function table(title, rows) {
      if (!rows.length) return '';
      return '<p><strong>' + title + ', ' + savingsCents(cents(rows) / 100) + '</strong></p><ul>' + rows.map(function(r) {
        return '<li>' + esc(r.label) + ': ' + savingsCents(r.usd) + (num(r.tok) > 0 ? ', ' + tok(r.tok) + ' tokens' : '') +
          (r.why ? '. ' + esc(r.why) : '') + '</li>';
      }).join('') + '</ul>';
    }
    var overhead = se.overhead || {};
    var carry = s.lean_resume_carry || {};
    var incomplete = cp.pricing_complete === false || as.reread_pricing_complete === false;
    var archive = (s.by_category || {}).tool_archive;
    var bashExpand = (s.by_category || {}).bash_archive_reexpand;
    var bashNet = Object.keys(s.by_category || {}).reduce(function(n, k) {
      return n + ((/^bash_/.test(k) || k === 'build_output_compress' || k === 'crossturn_dedup') ? num((s.by_category[k] || {}).cost_saved_usd) : 0);
    }, 0);
    var basis = savingsRoutingBasis(data);
    var routingRow = routing30 > 0 ? '<p><strong>Estimated, model routing, ' + savingsCents(routing30) + '</strong></p><ul><li>Model routing: ' + savingsCents(routing30) + '. ' +
      esc(basis.lead) + ' Your current work priced at that mix, against what it actually cost.</li></ul>' : '';
    var allCents = cents(measured) + cents(estimated) + Math.round(routing30 * 100);
    var missing = num(cov.ran_no_ledger_sessions);
    var coverage = num(cov.sessions) > 0 && num(cov.ran_sessions) > 0
      ? '<p>Token Optimizer ran in ' + count(cov.ran_sessions) + ' of your ' + count(cov.sessions) + ' sessions.' +
        (missing > 0 ? ' ' + count(missing) + ' of them ' + (missing === 1 ? 'is' : 'are') + ' not in the ledger yet, so the total understates.' : '') + '</p>' : '';
    var setupDetail = '';
    if (trimLedger) {
      var valuedTrims = trims.filter(function(t) { return t.tier !== 'unattributed' && (num(t.cost_saved_usd) > 0 || num(t.projected_30d_usd) > 0); })
        .sort(function(a, b) { return num(b.projected_30d_usd) - num(a.projected_30d_usd); });
      var setupRows = setupCleanupGroups(sd).map(function(g) {
        return '<li><span>' + esc(g.label) + '<span class="st-sub">' + esc(g.sub) + '</span></span><span class="st-v">' + savingsCents(g.usd) + '</span></li>';
      }).join('');
      var opts = sd.attested_options || [];
      var formula = valuedTrims.map(function(t) {
        return '<li>' + esc(t.label || t.item || 'Setup cleanup') + ': ' + count(num(t.tokens_per_call) * (t.step_measured ? num(t.step_share) : 1)) + ' tokens per call &times; ' + count(t.requests) +
          (t.step_measured ? ' calls; priced on main-thread calls since the trim, at each call&rsquo;s cache-read rate' : ' calls since') + (num(t.projected_30d_usd) > 0 ? '. Estimated next 30 days at the recent rate: ' + (num(t.projected_30d_usd) < 1 ? '' : 'about ') + usd(t.projected_30d_usd) + '.' : '.') + '</li>';
      }).join('');
      if (setupRows) {
        setupDetail = '<p><strong>Setup cleanup, item by item</strong></p><ul class="st-rows">' + setupRows + '</ul>' +
          '<p>Trimming setup reduces repeated context on calls that load it. These amounts are already in the total.</p><ul>' + formula + '</ul>' +
          (opts.length ? '<p>Your figure can be one of these sizes, priced the same way:</p><ul>' + opts.map(function(o) {
            return '<li>' + count(o.tokens_per_call) + ' tokens per call: estimated ' + usd(o.projected_30d_usd) + ' per 30 days at the recent rate' + (o.chosen ? ' (shown)' : '') + '</li>';
          }).join('') + '</ul>' : '');
      }
    }
    var method = '<p><strong>Last ' + days + ' days, ' + savingsCents(allCents / 100) + '</strong>: measured ' + savingsCents(cents(measured) / 100) +
        ', estimated ' + savingsCents(cents(estimated) / 100) + (routing30 > 0 ? ', routing ' + savingsCents(routing30) : '') + '.</p>' +
      table('Measured, counted event by event', measured) +
      table('Estimated, sized by a documented method', estimated) +
      routingRow +
      setupDetail +
      (num((s.progressive_disclosure || {}).full_bash_fetches) > 0 ? '<p>Full command output fetched from the archive: ' + count(s.progressive_disclosure.full_bash_fetches) + ' outputs, about ' + tok(s.progressive_disclosure.full_bash_tokens) + ' tokens. This does not establish whether a command would otherwise have been re-run.</p>' : '') +
      (num((s.keepwarm_estimated || {}).manual_pings) ? '<p>Manual warm-ups from the Claude Code mod: ' + count(s.keepwarm_estimated.manual_pings) + '. Their cost is included in the estimated keep-warm net.</p>' : '') +
      (num((s.mod_compaction_actions || {}).events) + num(((s.by_category || {}).mod_clean || {}).events) + num(((s.by_category || {}).mod_clean_first || {}).events) ? '<p>Clean-up from the Claude Code mod: ' + count(num((s.mod_compaction_actions || {}).events) || (num(((s.by_category || {}).mod_clean || {}).events) + num(((s.by_category || {}).mod_clean_first || {}).events))) + ' actions. Compaction cost is deducted; later reads are counted through the next compaction, with one observed cache rebuild for clean-up first' + (num((s.mod_compaction_actions || {}).unpriced_events) ? '; ' + count(s.mod_compaction_actions.unpriced_events) + ' actions are not yet priced' : '') + '.</p>' : '') +
      (num((s.mod_fresh_estimated || {}).events) ? '<p>Start fresh from the Claude Code mod: ' + count(s.mod_fresh_estimated.events) + ' actions. Estimated continuation value (old context less a rough handoff estimate; persistent setup may remain; median of comparable earlier sessions, capped at 10 turns): ' + (num(s.mod_fresh_estimated.unpriced_events) === num(s.mod_fresh_estimated.events) ? 'not yet available' : usd(s.mod_fresh_estimated.cost_saved_usd || 0)) + (num((as.estimates_usd || {}).mod_fresh_estimated) === 0 && num(s.mod_fresh_estimated.cost_saved_usd) > 0 ? ', shown separately to avoid overlap with handovers' : '') + (num(s.mod_fresh_estimated.unpriced_events) ? '; some actions need more history' : '') + '.</p>' : '') +
      (archive ? '<p>Tool archive, net of re-expands: ' + usd(archive.cost_saved_usd) + ' once, before repeat-reads.</p>' : '') +
      (bashExpand ? '<p>Bash compression, net of re-expands: ' + usd(bashNet) + ' once, before repeat-reads.</p>' : '') +
      (num((as.estimates_usd || {}).expand_overhead) ? '<p>Estimated expand-command overhead: ' + usd(as.estimates_usd.expand_overhead) + '.</p>' : '') +
      (num((as.estimates_usd || {}).handover_rerun) > 0 ? '<p>Continuity handovers, ' + usd(as.estimates_usd.handover_rerun) + ' (estimated): sessions resumed from a saved checkpoint re-read fewer stale files than sessions that started cold, and that gap is counted here.</p>' : '') +
      (totalTok > 0 ? '<p>The ' + tok(totalTok) + ' tokens kept out of context are measured and estimated' + (num(th.reload_avoided_tokens) > 0 ? ': ' + tok(th.reload_avoided_tokens) + ' of them are estimated reloads avoided' : '') + '.</p>' : '') +
      '<p>Measured rows come from the ledger: each removal, the cache rate the next call would have paid, and every later turn that would have re-read it, up to the session&rsquo;s next compaction.</p>' +
      (s.token_type_breakdown && Object.keys(s.token_type_breakdown).length ? '<p>Token types cover only ledger events with recorded turn classes. Repeat reads are modeled as cache reads except for detected cache rebuilds; write totals include detected rebuilds and initial removals recorded at a write rate. Unclassified tokens are excluded.</p>' : '') +
      savingsPerSessionHtml(ps, overhead) +
      (incomplete ? '<p>Some older history has no exact price, so this total understates.</p>' : '') +
      coverage +
      (carry.available && num(carry.carry_cost_usd) > 0 ? '<p>Diagnostic, not counted' + ': the older target-session proxy suggested ' +
        tok(carry.carry_tokens) + ' tokens, about ' + usd(carry.carry_cost_usd) + '. That proxy stays out of totals; receiver-linked estimates are counted separately.</p>' : '') +
      '<p>Valued at API rates. On a subscription this is room inside your limits; nothing is refunded. Opportunity is never part of this total.</p>';
    var SIh = savingsSinceInstall(data);
    // While an upgrade re-reads older sessions the totals are partial: say so
    // on each number it affects, with how many sessions are left.
    var aiP = ((s.since_install || {}).all_in) || {};
    var soFar30 = cp.remeasuring === true || mr.evidence === 'pending';
    var soFarAll = aiP.remeasuring === true || aiP.routing_pending === true;
    var left = Math.max(num(cp.remeasuring_sessions), num(aiP.remeasuring_sessions), num(aiP.routing_pending_sessions), mr.evidence === 'pending' ? num(mr.pending_sessions) : 0);
    var soFar = function(on) { return on ? ', so far' : ''; };
    out += '<div class="card st-hero">' +
      '<h2 class="st-h">What Token Optimizer saved</h2>' +
      '<div class="st-mega__row st-mega__row--hero">' +
        '<div class="st-mega__item"><span class="st-mega st-mega--usd">' + usd(bigTotal) + '</span><span class="st-mega__unit">saved in the last ' + days + ' days' + soFar(soFar30) + '</span></div>' +
        (SIh && SIh.all > 0 ? '<div class="st-mega__item"><span class="st-mega st-mega--usd">' + usd(SIh.all) + '</span><span class="st-mega__unit">saved since you installed' + soFar(soFarAll) + '</span></div>' : '') +
        (totalTok > 0 ? '<div class="st-mega__item"><span class="st-mega st-mega--tok">' + tok(totalTok) + '</span><span class="st-mega__unit">tokens kept out of context in ' + days + ' days' + soFar(cp.remeasuring === true) + '</span></div>' : '') +
      '</div>' +
      (soFar30 || soFarAll ? '<p class="st-lead st-progress">Updating your history after an upgrade' + (left > 0 ? ' (' + count(left) + ' session' + (left === 1 ? '' : 's') + ' left)' : '') + '. These totals are in progress and will change when it finishes.</p>' : '') +
      f.fold('How this is calculated', method + (SIh && SIh.all > 0 ? savingsSinceInstallBody(data, SIh) : '')) +
      '</div>';
  }

  // ---- 1b. Transformation and limits: both big, both with their method in a fold ----
  out += savingsTransformationHtml(data);
  out += savingsLimitsHtml(data);

  // ---- 1c. Still on the table: opportunity, never part of a savings total ----
  out += savingsOnTableHtml(data);

  // ---- 2. The re-read card ----
  var cards = [];
  var removedOnce = num(cp.removed_tokens), rereads = num(cp.reread_tokens);
  if (removedOnce > 0 && rereads > 0) {
    var carryN = rereads / removedOnce;
    cards.push('<div class="card st-card st-reread">' +
      '<h2 class="st-h">' + (as.reread_available !== false && has(as.reread_usd) && num(as.reread_usd) > 0 && bigTotal > 0 ? usd(as.reread_usd) + ' of your ' + usd(bigTotal) + ' came from re-reads avoided' : 'Removed once, saved on every later turn') + '</h2>' +
      '<div class="st-reread__body"><p class="st-mega__item st-mega__item--card"><span class="st-mega st-mega--tok st-mega--card">' + Math.round(carryN) + '&times;</span><span class="st-mega__unit">re-reads avoided per token removed</span></p>' +
      '<p class="st-lead"><strong>' + tok(removedOnce) + ' priced tokens removed</strong> became <strong>' + tok(rereads) + ' tokens not sent</strong> on later reads' + (has(cp.reread_usd) ? ', worth ' + usd(cp.reread_usd) + ' at API rates' : '') + '.</p></div>' +
      '</div>');
  }
  if (cards.length) out += '<div class="st-grid">' + cards.join('') + '</div>';
  out += savingsCostTrendHtml(s.cost_per_message_trend);

  return out ? '<section class="st" aria-label="What Token Optimizer saved">' + out + '</section>' : '';
}

// "Apr to Jul" (years added when they differ or are needed), or ''.
function savingsMonthRange(period, joiner) {
  period = period || {};
  if (!(/^\d{4}-\d{2}$/.test(period.start || '') && /^\d{4}-\d{2}$/.test(period.end || ''))) return '';
  function m(v) { return new Date(v + '-01T00:00:00').toLocaleDateString('en-US', { month: 'short' }); }
  var sameYear = period.start.slice(0, 4) === period.end.slice(0, 4);
  return m(period.start) + (sameYear ? '' : ' ' + period.start.slice(0, 4)) + (joiner || ' to ') + m(period.end) + (sameYear ? '' : ' ' + period.end.slice(0, 4));
}

// Exact dollars and cents, for the folds.
function savingsCents(v) {
  v = savingsStoryFmt().num(v);
  return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Since-install fold: the measured / estimated / routing split and its baseline.
function savingsSinceInstallBody(data, SI) {
  var f = savingsStoryFmt(), num = f.num, has = f.has, usd = f.usd, pct = f.pct, esc_ = esc;
  var s = (data && data.savings) || {};
  var ai = ((s.since_install || {}).all_in) || {};
  var mr = s.model_routing || {};
  var fleet = (s.session_effect || {}).fleet || {};
  var ledgerShare = has(fleet.cost_share_net) ? num(fleet.cost_share_net) : (has(fleet.cost_share) ? num(fleet.cost_share) : null);
  var basis = savingsRoutingBasis(data);
  var period = savingsMonthRange(ai.routing_majority_period, ' to ');
  var since = SI.date ? esc_(SI.date) : 'install';
  var split = '<p><strong>Since ' + since + ', ' + savingsCents(SI.all) + '</strong></p><ul>' +
    (SI.measured > 0 ? '<li>Measured: ' + savingsCents(SI.measured) + '. Context removed, cheaper cache writes and re-reads avoided, counted event by event.</li>' : '') +
    (SI.estimated > 0 ? '<li>Estimated: ' + savingsCents(SI.estimated) + '. Setup cleanup from archives, handovers and other documented estimates.</li>' : '') +
    (SI.routing > 0 ? '<li>Model routing, estimated: ' + savingsCents(SI.routing) + '. ' + esc_(basis.lead) + (period ? ' Most of it from ' + esc_(period) + '.' : '') + '</li>' : '') +
    '</ul>';
  var alt = SI.routing > 0 && basis.kind === 'stated' && has(ai.routing_measured_baseline_opus_share) && has(ai.routing_measured_baseline_realized_cost_usd)
    ? '<p>Using the measured starting mix (' + (num(ai.routing_measured_baseline_opus_share) * 100).toFixed(2) + '% Opus), the routing estimate is ' + usd(ai.routing_measured_baseline_realized_cost_usd) + '.</p>' : '';
  return split +
    '<p>Providers do not publish how they weight models and cache reads inside a limit; API price is the closest public stand-in, so this is room inside your limits, not a refund.</p>' +
    alt +
    (SI.excluded ? '<p>Excluded since install: unverified lean-resume proxy ' + (has(SI.excluded.lean_resumes) ? usd(SI.excluded.lean_resumes) : 'not priced') + ', and verbosity ' + (has(SI.excluded.verbosity) ? usd(SI.excluded.verbosity) : 'not priced') + '.</p>' : '<p>Unverified lean-resume and verbosity estimates are excluded.</p>') +
    (ledgerShare != null ? '<p>Counted ledger alone, as a share of the API-rate cost of the sessions where Token Optimizer ran: ' + pct(ledgerShare) + '.</p>' : '');
}

// Which baseline the since-install routing estimate compares against:
// 'stated' (the user's own pre-TO share) or 'measured' (the mix Token
// Optimizer recorded at install). Older payloads only carried a stated one.
function savingsRoutingBasis(data) {
  var f = savingsStoryFmt(), has = f.has, num = f.num;
  var s = (data && data.savings) || {};
  var ai = ((s.since_install || {}).all_in) || {};
  var mr = s.model_routing || {};
  var src = ai.routing_baseline_source || mr.baseline_source;
  var kind = (src === 'measured_at_install' || src === 'snapshot' || src === 'history') ? 'measured' : 'stated';
  var share = has(ai.routing_baseline_opus_share) ? num(ai.routing_baseline_opus_share) : (has(mr.baseline_opus_share) ? num(mr.baseline_opus_share) : null);
  var pct = share != null ? Math.round(share * 100) + '%' : '';
  // Newer payloads say where the baseline came from and whether Token
  // Optimizer was already running in it; that sentence replaces the fixed one.
  var prov = ai.routing_baseline_provenance || mr.baseline_provenance;
  if (prov && prov.lead && prov.ledger) return { kind: prov.kind === 'stated' ? 'stated' : 'measured', pct: pct, lead: String(prov.lead), ledger: String(prov.ledger) };
  return {
    kind: kind, pct: pct,
    lead: kind === 'measured'
      ? 'Compared with the model mix Token Optimizer recorded when you installed' + (pct ? ' (' + pct + ' Opus)' : '') + '.'
      : 'At your stated ' + (pct ? pct + ' ' : '') + 'Opus baseline.',
    ledger: kind === 'measured'
      ? 'Estimated routing, against the model mix recorded at install' + (pct ? ' (' + pct + ' Opus)' : '')
      : 'Estimated routing, at your stated ' + (pct ? pct + ' ' : '') + 'Opus baseline'
  };
}

// No baseline yet: one line saying how far along it is, so the missing
// before-and-after card is explained. Nothing for any other state.
function savingsBaselineProgressHtml(ba) {
  var f = savingsStoryFmt(), num = f.num, has = f.has, count = f.count;
  var b = ba && ba.reason === 'insufficient_history' ? ba.baseline_building : null;
  if (!b || typeof b !== 'object' || b.ready === true || !has(b.sessions_in_window) || !(num(b.sessions_needed) > 0)) return '';
  var got = Math.max(0, num(b.sessions_in_window)), need = num(b.sessions_needed), left = Math.max(0, num(b.days_left));
  var span = num(b.early_window_days) > 0 ? num(b.early_window_days) : 30;
  var line;
  if (left > 0) {
    line = 'Building your baseline: ' + count(Math.min(got, need)) + ' of ' + count(need) + ' sessions, ' + count(left) + ' day' + (left === 1 ? '' : 's') + ' left. ' +
      'Your before-and-after comparison appears here when it is ready.';
  } else if (got < need) {
    line = 'Your first ' + count(span) + ' days on record had ' + count(got) + ' session' + (got === 1 ? '' : 's') + '. A stable baseline needs ' + count(need) + ', so the before-and-after comparison is not shown. Everything above is still counted.';
  } else {
    return '';
  }
  return '<div class="card st-card st-baseline-progress"><h2 class="st-h">Your transformation</h2><p class="st-lead">' + line + '</p></div>';
}

// Transformation: what this month would cost without Token Optimizer, against
// the user's own baseline from before install. Hidden unless the payload says ok.
function savingsTransformationHtml(data) {
  var f = savingsStoryFmt(), num = f.num, has = f.has, usd = f.usd;
  var ba = ((data && data.savings) || {}).before_after || {};
  if (ba.transformation_state !== 'ok' || !(num(ba.monthly_savings_usd) > 0)) return savingsBaselineProgressHtml(ba);
  var share = has(ba.transformation_pct) && num(ba.transformation_pct) > 0 ? Math.round(num(ba.transformation_pct) * 100) : 0;
  // Where the "before" came from. Older payloads carry no provenance and keep the fixed sentence.
  var prov = ba.baseline_provenance || {};
  // The lead states the baseline; what it understates is spelled out in the fold.
  var against = prov.lead ? esc(prov.lead) : 'compared with your own baseline from before you installed.';
  // A work-normalized figure prices both periods at one fixed rate card, so its
  // "now" side is not the user's spend: name the card and show the exact-card cost.
  var wp = ba.session_weight_pool || {}, fr = wp.flat_rates || null;
  var fixed = fr && has(fr.input) && has(fr.output) && has(fr.cache_read) && has(fr.cache_write);
  var own = fixed && wp.price_basis === 'own_prices';
  var rate = function(v) { return '$' + Number(num(v).toFixed(2)); };
  var method = '<ul><li>' + (prov.versus_label ? esc(prov.versus_label) : 'Without Token Optimizer') + (own ? ', at your current prices' : fixed ? ', at the fixed price card' : '') + ': ' + savingsCents(ba.counterfactual_monthly_usd) + ' a month</li>' +
    '<li>' + (own ? 'This period, at your current prices' : fixed ? 'This period, at the same fixed price card' : 'What you actually spent') + ': ' + savingsCents(ba.actual_monthly_usd) + ' a month</li>' +
    '<li>Difference: ' + savingsCents(ba.monthly_savings_usd) + ' a month</li></ul>' +
    (own ? '<p>Both sides are priced at your own current prices (per million tokens: ' + rate(fr.input) + ' input, ' + rate(fr.output) + ' output, ' + rate(fr.cache_read) + ' cache read, ' + rate(fr.cache_write) + ' cache write, averaged over the models you ran this period), so a price change between the two periods cannot count as a saving.</p>'
    : fixed ? '<p>Both sides are priced at one fixed card (per million tokens: $' + num(fr.input) + ' input, $' + num(fr.output) + ' output, $' + num(fr.cache_read) + ' cache read, $' + num(fr.cache_write) + ' cache write), so a price change between the two periods cannot count as a saving. These are dollars at that card, not your bill.' +
      (num(wp.engine_priced_actual_usd) > 0 ? ' At the prices of the models you actually used, this period comes to ' + savingsCents(wp.engine_priced_actual_usd) + ' at API rates.' : '') + '</p>' : '') +
    (prov.note ? '<p>' + esc(prov.note) + '</p>' : '') +
    (ba.before_mix_label ? '<p>' + (prov.mix_note ? 'Baseline model mix: ' + esc(ba.before_mix_label) + ', ' + esc(prov.mix_note) : 'Baseline mix before you installed: ' + esc(ba.before_mix_label)) + '. Now: ' + esc(ba.after_mix_label || 'your current mix') + '.</p>' : '') +
    (ba.breakdown_caveat ? '<p>' + esc(ba.breakdown_caveat) + '</p>' : '');
  return '<div class="card st-card st-transform">' +
    '<h2 class="st-h">Your transformation</h2>' +
    '<p class="st-mega__item st-mega__item--card"><span class="st-mega st-mega--usd st-mega--card">' + usd(ba.monthly_savings_usd) + '<span class="st-mega__per"> a month</span></span>' +
      '</p><p class="st-lead">' + (prov.claim ? esc(prov.claim) : 'Without Token Optimizer, working the way you work now would cost you this much more, ' + against) +
    (share > 0 ? ' That is ' + share + '% of what this work would have cost.' : '') + '</p>' +
    f.fold('How this is calculated', method) +
    '</div>';
}

// Limits with and without Token Optimizer: per window, the used share and what
// it would be without. Live windows first, then the last stored meter reading;
// with neither, the window grid is left out.
function savingsLimitWindows(rw, ratio, versus) {
  // What the second number is measured against: a time without Token
  // Optimizer, or (when it already ran in the baseline month) that month.
  var vs = versus ? esc(versus) : 'without Token Optimizer';
  var f = savingsStoryFmt(), num = f.num;
  var names = { five_hour: '5-hour limit', seven_day: 'Weekly limit' };
  function ok(w) {
    return w && w.used_pct != null && isFinite(Number(w.used_pct)) && Number(w.used_pct) >= 0 && Number(w.used_pct) <= 100;
  }
  var live = (Array.isArray(rw.windows) ? rw.windows : []).filter(ok), rows = live, stored = false;
  if (!rows.length) { rows = (Array.isArray(rw.last_known_windows) ? rw.last_known_windows : []).filter(ok); stored = rows.length > 0; }
  if (!rows.length) return '';
  // One row per window: name, a bar (solid = used now, striped = without
  // Token Optimizer) and the two numbers as stat blocks.
  var html = '<div class="st-limits">' + rows.map(function(w) {
    var used = num(w.used_pct), rawWithout = used * ratio;
    var name = names[w.key] || ((w.label || 'window') + ' limit');
    name = name.charAt(0).toUpperCase() + name.slice(1);
    var over = ratio > 0 && rawWithout > 100;
    var without = ratio > 0 ? Math.min(100, Math.round(rawWithout)) : 0;
    var label = Math.round(used) + '% used now' + (ratio > 0 ? (over ? ', over the limit ' + vs : ', ' + without + '% ' + vs) : '');
    var stat = ratio > 0
      ? (over
        ? '<span class="st-limit__stat st-limit__stat--over">over the limit ' + vs + '</span>'
        : '<span class="st-limit__stat"><span class="st-limit__num">' + without + '%</span> ' + vs + '</span>')
      : '';
    return '<div class="st-limit"><span class="st-limit__name">' + esc(name) + '</span>' +
      '<span class="st-limit__meter" role="img" aria-label="' + esc(label) + '">' +
      (ratio > 0 ? '<span class="st-limit__ghost" style="width:' + without + '%"></span>' : '') +
      '<span class="st-limit__fill" style="width:' + Math.round(used) + '%"></span></span>' +
      '<span class="st-limit__stat"><span class="st-limit__used">' + Math.round(used) + '%</span> used now</span>' + stat + '</div>';
  }).join('') + '</div>';
  var ts = Number(rw.meter_ts);
  if (stored && isFinite(ts) && ts > 0) {
    var when = new Date(ts * 1000).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    html += '<p class="st-sub">Last reading, ' + esc(when) + '.</p>';
  }
  return html;
}

function savingsLimitsHtml(data) {
  var f = savingsStoryFmt(), num = f.num;
  var rw = (data && data.runway) || null;
  if (!rw || typeof rw !== 'object') return '';
  var ba = ((data && data.savings) || {}).before_after || {};
  var actual = num(ba.actual_monthly_usd), before = num(ba.counterfactual_monthly_usd);
  var ratio = ba.transformation_state === 'ok' && actual > 0 && before > actual ? before / actual : 0;
  var extra = ratio > 1 ? Math.round((1 - 1 / ratio) * 100) : 0;
  var versus = (ba.baseline_provenance || {}).versus || '';
  var windows = savingsLimitWindows(rw, ratio, versus);
  if (!extra && !windows) return '';
  var direct = num(rw.extra_work_pct);
  var method = '<p>Your usage is a meter reading. The ' + (versus ? 'comparison ' + esc(versus) : 'without-Token-Optimizer comparison') + ' is an estimate using the same before-and-after basis as your transformation: counterfactual monthly cost divided by actual monthly cost, multiplied by the used percentage and capped at 100%. Providers do not publish the exact weighting of usage limits.</p>' +
    (ratio ? '<p>' + savingsCents(before) + ' divided by ' + savingsCents(actual) + ' = &times;' + ratio.toFixed(2) + '.</p>' : '') +
    (direct > 0 ? '<p>Separately, directly measured savings imply about ' + direct + '% more work per token.</p>' : '');
  return '<div class="card st-card st-limits-card">' +
    '<div class="st-limits__top"><h2 class="st-h">Your limits go further</h2>' + windows + '</div>' +
    '<div class="st-limits__bottom">' + (extra ? '<p>&asymp;' + extra + '% more room in your limits</p>' : '') +
    f.fold('How this is calculated', method) + '</div></div>';
}

// Still on the table: computable opportunities per period, each with one
// action. A part the data cannot size is left out. Never part of any
// savings total.
function savingsOnTableHtml(data) {
  var f = savingsStoryFmt(), num = f.num, has = f.has, usd = f.usd, tok = f.tok, count = f.count;
  var s = (data && data.savings) || {};
  var days = Number(s.period_days) || 30;
  var mr = s.model_routing || {};
  var lb = s.skill_listing_budget;
  var rows = [], how = [];
  if (num(mr.potential_cost_usd) > 0) {
    var target = String(mr.target_model || '');
    var name = /sonnet/i.test(target) ? 'Sonnet' : /haiku/i.test(target) ? 'Haiku' : 'a lighter model';
    var frac = has(mr.routable_fraction) ? Math.round(num(mr.routable_fraction) * 100) : null;
    rows.push({ label: 'Model routing', action: 'Send routine work to ' + name, usd: num(mr.potential_cost_usd) });
    how.push('<li>Model routing: if ' + (frac != null ? 'about ' + frac + '% of' : 'part of') + ' your Opus work ran on ' + name + ', new input and output priced at API rates. An assumption about your own work.</li>');
  }
  // The skill-listing row appears only when the user's own history sizes it;
  // with no earlier listing to compare, it is left out (never "Measuring").
  if (lb && typeof lb === 'object' && lb.available && num(lb.cost_usd) > 0) {
    var bm = /^\d{4}-\d{2}$/.test(lb.baseline_month || '') ? new Date(lb.baseline_month + '-01T00:00:00').toLocaleDateString('en-US', { month: 'short', year: 'numeric' }) : '';
    rows.push({ label: 'Skill listing', action: 'Trim the skill list budget', usd: num(lb.cost_usd) });
    how.push('<li>Skill listing: your skill list grew from ' + count(lb.baseline_chars) + ' to ' + count(lb.current_chars) + ' characters per message' + (bm ? ' since ' + esc(bm) : '') +
      '. Trimming it back is worth the ' + tok(lb.growth_tokens) + '-token difference &times; ' + count(lb.calls) + ' calls on this computer in the last ' + count(lb.period_days || days) + ' days &times; each call&rsquo;s cache-read price.</li>');
  }
  if (!rows.length) return '';
  var total = rows.reduce(function(a, r) { return a + r.usd; }, 0);
  return '<div class="card st-table">' +
    '<h2 class="st-h">Still on the table</h2>' +
    '<div class="st-table__body"><p class="st-mega__item st-mega__item--card">' +
      '<span class="st-mega st-mega--warm st-mega--card">' + usd(total) + '</span><span class="st-mega__unit">more every ' + days + ' days, yours to claim</span></p>' +
    '<div class="st-table__rows"><ul class="st-rows st-rows--card">' + rows.map(function(r) {
      return '<li><span>' + esc(r.label) + '<span class="st-sub">' + esc(r.action) + '</span></span><span class="st-v">' + usd(r.usd) + '</span></li>';
    }).join('') + '</ul>' +
    f.fold('How each one is sized', '<ul>' + how.join('') + '</ul><p>Valued at API rates, per ' + days + ' days. None of this is in any savings total.</p>') +
    '</div></div></div>';
}

// Cost per message: shown only when it fell at least 10% from the first month.
function savingsCostTrendHtml(raw) {
  var trend = (raw || []).filter(function(p) {
    return p && /^\d{4}-\d{2}$/.test(String(p.month)) && isFinite(Number(p.index)) && Number(p.index) >= 0;
  });
  if (trend.length < 2) return '';
  var start = Number(trend[0].index), latest = Number(trend[trend.length - 1].index);
  if (!(start > 0) || latest > start * 0.9) return '';
  var down = Math.round((1 - latest / start) * 100);
  var first = new Date(trend[0].month + '-01T00:00:00'), last = new Date(trend[trend.length - 1].month + '-01T00:00:00');
  var since = first.toLocaleDateString('en-US', first.getFullYear() === last.getFullYear() ? { month: 'long' } : { month: 'long', year: 'numeric' });
  var maxIndex = Math.max.apply(null, trend.map(function(p) { return Number(p.index); })) || 1;
  var points = trend.map(function(p, i) { return (i * 500 / (trend.length - 1)).toFixed(1) + ',' + (90 - Number(p.index) / maxIndex * 80).toFixed(1); }).join(' ');
  var ticks = trend.map(function(p, i) { if (i !== trend.length - 1 && i % Math.ceil(trend.length / 6)) return ''; var d = new Date(p.month + '-01T00:00:00'), x = 20 + i * 460 / (trend.length - 1); return '<text x="' + x.toFixed(1) + '" y="115" text-anchor="middle" fill="currentColor" font-size="11">' + esc(d.toLocaleDateString('en-US', {month:'short'})) + '</text>'; }).join('');
  return '<div class="card wide st-card"><h2 class="st-h">Cost per message down ' + down + '% since ' + esc(since) + '</h2>' +
    '<p class="st-lead">Indexed to the first month, at constant Opus prices for every model. This trend is not attributed to Token Optimizer.</p>' +
    '<p>Start ' + Math.round(start) + ' &middot; Latest ' + Math.round(latest) + '</p>' +
    '<svg viewBox="0 0 500 120" role="img" aria-label="Monthly cost per message index" style="width:100%;height:140px">' +
    '<polyline fill="none" stroke="currentColor" stroke-width="3" points="' + points + '"></polyline>' + ticks + '</svg></div>';
}

function savingsPerSessionHtml(ps, overhead) {
  var f = savingsStoryFmt(), num = f.num, has = f.has;
  ps = ps || {};
  var bp = ps.by_place || {};
  var rows = [['On this computer', bp.local], ['Cloud sessions', bp.cloud]].filter(function(r) {
    return r[1] && num(r[1].sessions) > 0 && has(r[1].net_median);
  });
  if (!rows.length && num(ps.sessions) > 0 && has(ps.net_median)) rows = [['All sessions', ps]];
  if (!rows.length) return '';
  overhead = overhead || {};
  return '<p><strong>Per session, share of that session&rsquo;s own context that never got sent</strong></p><ul>' + rows.map(function(r) {
      var v = r[1];
      return '<li>' + r[0] + ': typical ' + f.pct(v.net_median) + (has(v.net_p75) ? ', top quarter ' + f.pct(v.net_p75) + ' or more' : '') +
        ' (' + f.count(v.sessions) + ' sessions)</li>';
    }).join('') + '</ul><p>Medians of sessions with ledger entries, after subtracting the notes Token Optimizer itself adds' +
    (num(overhead.usd) > 0 ? ' (' + f.tok(overhead.reread_tokens) + ' tokens re-read, about ' + f.usd(overhead.usd) + ')' : '') +
    '. Cloud means the session&rsquo;s events were rebuilt from its transcript because its hooks ran on another machine.</p>' +
    (bp.local && bp.local.best && bp.local.best.filtered ? '<p>Best local session is selected among sessions with at least 20 turns because the unfiltered best was small.</p>' : '');
}

// Since-install total: every component plus the routing estimate. `all` is the
// big number; measured, estimated and routing are its split.
function savingsSinceInstall(data) {
  var f = savingsStoryFmt(), num = f.num, has = f.has;
  var si = ((data && data.savings) || {}).since_install || {};
  var ai = si.all_in || {};
  var c = ai.components_usd || {};
  if (!(num(si.days) > 0)) return null;
  var parts = [['Context removed', c.logged], ['Cheaper cache writes', c.class_premium], ['Re-reads avoided', c.reread], ['Estimates', ai.estimated_usd]]
    .filter(function(p) { return num(p[1]) !== 0; });
  if (!parts.length) return null;
  var date = si.install_date ? new Date(si.install_date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '';
  var base = parts.reduce(function(a, p) { return a + num(p[1]); }, 0);
  var routing = has(ai.routing_usd) ? num(ai.routing_usd) : num(c.routing_estimated) + num(c.routing_measured);
  if (!(routing > 0)) routing = 0;
  var all = has(ai.total_usd) ? num(ai.total_usd) : base + routing;
  var estimated = num(ai.estimated_usd);
  return { parts: parts, total: base, all: all, routing: routing, estimated: estimated, measured: all - estimated - routing,
           days: num(si.days), date: date, excluded: si.excluded_usd || null };
}

// "Where your sessions ran" fold body, for the caller's row of folds.
function savingsCoverageFold(data) {
  if (!savingsStoryOn(data)) return '';
  var f = savingsStoryFmt();
  var cov = data.savings.session_effect.coverage || {};
  var classes = cov.classes || {};
  var items = [
    ['ledger', 'Counted in the ledger'], ['logged_no_ledger', 'Logged, no repeat-read record'],
    ['ran_unlogged', 'Ran, not in the ledger yet'], ['not_ran', 'Token Optimizer did not run'],
    ['unknown', 'Not scanned yet (transcript missing)']
  ].filter(function(c) { return classes[c[0]] && f.num(classes[c[0]].sessions) > 0; });
  if (!items.length) return '';
  return f.fold('Where your sessions ran', '<ul>' + items.map(function(c) {
    var k = classes[c[0]];
    return '<li>' + c[1] + ': ' + f.count(k.sessions) + ' sessions, ' + f.pct(k.traffic_share) + ' of all your traffic</li>';
  }).join('') + '</ul><p>A session counts as covered only on evidence: a ledger row, a logged event, or Token Optimizer&rsquo;s own note in its transcript.</p>');
}

// Health tab: context quality and hook time-outs, moved off the Savings tab.
function healthNotesHtml(data) {
  if (!savingsStoryOn(data)) return '';
  var f = savingsStoryFmt();
  var out = '';
  var qt = (data.trends && data.trends.quality_trend) || [];
  var wsum = 0, wn = 0;
  qt.forEach(function(r) {
    var q = Number(r.avg_quality) || 0, n = Number(r.sessions) || 0;
    if (q > 0 && n > 0) { wsum += q * n; wn += n; }
  });
  if (wn > 0) {
    var avg = Math.round(wsum / wn);
    var vals = qt.map(function(r) { return Number(r.avg_quality) || 0; }).filter(function(v) { return v > 0; });
    var trend = '';
    if (vals.length >= 6) {
      var h = Math.floor(vals.length / 2);
      var d = vals.slice(-h).reduce(function(a, b) { return a + b; }, 0) / h - vals.slice(0, h).reduce(function(a, b) { return a + b; }, 0) / h;
      trend = d > 3 ? 'up over the period' : (d < -3 ? 'down over the period' : 'steady over the period');
    }
    out += '<div class="card wide st-health">' +
      '<h2 class="st-h">Context quality</h2>' +
      '<p class="st-big">Your context scored <span class="st-num">' + avg + '</span> out of 100 on average' + (trend ? ', ' + trend : '') + '.</p>' +
      '<div class="st-ratio__track st-quality" role="img" aria-label="' + avg + ' out of 100"><span class="st-ratio__carry" style="width:' + Math.min(100, Math.max(0, avg)) + '%"></span></div>' +
      f.fold('What this means', '<p>Averaged across your last ' + f.count(wn) + ' sessions. The score drops as context fills with stale reads, repeated output and old tool results; tighter context gives sharper answers and fewer compactions.</p>' +
        '<p>The session table above scores single sessions.</p>') +
      '</div>';
  }
  var hs = (data.savings || {}).hook_skips || {};
  if (f.num(hs.overruns) > 0) {
    out += '<div class="card wide st-health">' +
      '<h2 class="st-h">Hook time limits</h2>' +
      '<p class="st-lead">Hooks ran out of time ' + f.count(hs.overruns) + ' times' + (hs.where ? ' ' + esc(hs.where) : '') +
        (f.num(hs.skipped_compressions) > 0 ? ' and skipped about ' + f.count(hs.skipped_compressions) + ' compressions (' + f.pct(hs.skip_rate) + ' of compressible output)' : '') +
        '.</p>' +
      '</div>';
  }
  return out;
}

// "Full ledger and history" fold body in story mode: the hero's own rows to
// the cent, then the since-install sums from the same components. Model
// routing is never added to either total.
function savingsLedgerHtml(data) {
  if (!savingsStoryOn(data)) return '';
  var f = savingsStoryFmt(), num = f.num;
  var s = data.savings || {};
  var B = savingsStoryBands(data);
  var days = Number(s.period_days) || 30;
  var mr = s.model_routing || {};
  var routing30 = f.has(mr.realized_cost_usd) ? num(mr.realized_cost_usd) : num((s.action_savings || {}).routing_usd);
  if (!(routing30 > 0)) routing30 = 0;
  function cents(v) {
    v = num(v);
    return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function row(label, v, tokens) {
    return '<li><span>' + esc(label) + (num(tokens) > 0 ? '<span class="st-sub">' + f.tok(tokens) + ' tokens</span>' : '') +
      '</span><span class="st-v">' + cents(v) + '</span></li>';
  }
  var out = '<h3 class="ts-section-label">Last ' + days + ' days, to the cent</h3><ul class="st-rows">' +
    B.measured.map(function(r) { return row(r.label + ' (measured)', r.usd, r.tok); }).join('') +
    B.estimated.map(function(r) { return row(r.label + ' (estimated)', r.usd, r.tok); }).join('') +
    (routing30 > 0 ? row('Model routing (estimated)', routing30, 0) : '') +
    '<li><span><strong>Total</strong></span><span class="st-v"><strong>' + cents(B.totalUsd + routing30) + '</strong></span></li></ul>';
  var SI = savingsSinceInstall(data);
  if (SI) {
    out += '<h3 class="ts-section-label st-gap">Since you installed' + (SI.date ? ' on ' + esc(SI.date) : '') + ' (' + f.count(SI.days) + ' days)</h3><ul class="st-rows">' +
      SI.parts.map(function(p) { return row(p[0], p[1], 0); }).join('') +
      (SI.routing ? row(savingsRoutingBasis(data).ledger, SI.routing, 0) : '') +
      '<li><span><strong>Total</strong></span><span class="st-v"><strong>' + cents(SI.all) + '</strong></span></li></ul>';
  }
  var cp = s.counted_period || {};
  if (cp.pricing_note) out += '<p>' + esc(cp.pricing_note) + '</p>';
  return out;
}


// Setup cleanup rows: every valued trim grouped by day and evidence, plus the
// observed listing/connector reductions, so the rows sum exactly to the
// measured and estimated "Setup cleanup" lines of the 30-day breakdown.
function setupCleanupGroups(sd) {
  var f = savingsStoryFmt(), num = f.num, groups = {}, order = [];
  (sd.trims || []).forEach(function(t) {
    if (!t || t.tier === 'unattributed' || !(num(t.cost_saved_usd) > 0)) return;
    var kind = t.step_measured ? 'step' : t.source === 'dated_archive' ? 'archive' : (t.source === 'attested' || t.tier === 'estimated') ? 'figure' : 'audit';
    var key = (f.day(t.ts) || 'undated') + '|' + kind;
    if (!groups[key]) { groups[key] = {day: f.day(t.ts) || 'undated', kind: kind, tier: t.tier, items: [], usd: 0}; order.push(key); }
    groups[key].items.push(t); groups[key].usd += num(t.cost_saved_usd);
  });
  var words = {step: 'measured by the first-call step', archive: 'estimated from archive', figure: 'your figure', audit: 'recorded by the audit'};
  var out = order.map(function(k) {
    var g = groups[k], one = g.items.length === 1, comps = g.items.map(function(t) { return t.component; });
    var same = comps.every(function(c) { return c === comps[0]; });
    return {label: one ? (g.items[0].label || g.items[0].item || g.items[0].component || 'Setup cleanup')
                       : g.items.length + ' ' + (same && comps[0] ? comps[0] : 'items'),
            sub: g.day + ', ' + words[g.kind], usd: g.usd, tier: g.tier};
  });
  [['budget_observed', 'Skill listing reduction', 'observed on later calls'], ['connector_observed', 'Disconnected connectors', 'observed on later calls']].forEach(function(b) {
    var o = sd[b[0]] || {};
    if (num(o.usd) > 0) out.push({label: b[1], sub: b[2], usd: num(o.usd), tier: 'measured'});
  });
  return out.sort(function(a, b) { return b.usd - a.usd; });
}
