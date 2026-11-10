// Savings story (U9): the Savings tab, top to bottom.
//   1. Hero band: three huge numbers (30-day total, measured since install
//      with its estimate line, tokens kept out of context), then the 30-day
//      measured/estimated split and its folds.
//   2. Still on the table: computable opportunities per period, warm accent,
//      each with one action. Never part of a savings total.
//   3. One grid: compounding, model routing (its own estimated card, never
//      added to any total), setup cleanup. Cost per message only when it fell
//      at least 10%.
//   4. One coverage line, then the caller's row of folds.
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
    return 'Setup cleanup' + (when ? ', ' + when : '') + (tier === 'estimated' ? (t && t.source === 'dated_archive' ? ' (realized, archive estimate)' : ' (realized, your figure)') : ' (realized, recorded)');
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
  // Pools whose ledger is still being built show "Measuring", never a number:
  // per-item setup cleanup (trim ledger), output re-reads, and the routing line
  // with its measured alternative.
  var measuringNames = [];

  var out = '';

  // ---- 1. Hero: one total, one bar, one sentence ----
  // Setup cleanup recorded by the trim ledger sits inside logged_usd (the
  // structural pool always has), so it is its own row, taken out of
  // "Context removed" once.
  var B = savingsStoryBands(data);
  var trimLedger = B.trimLedger, trims = B.trims, measured = B.measured, estimated = B.estimated;
  var mUsd = B.mUsd, eUsd = B.eUsd, totalUsd = B.totalUsd, totalTok = B.totalTok;
  measuringNames = B.measuringNames;
  function sum(rows, k) { return rows.reduce(function(a, r) { return a + num(r[k]); }, 0); }
  if (measured.length || estimated.length) {
    var mPct = totalUsd > 0 ? Math.max(0, Math.min(100, mUsd / totalUsd * 100)) : 0;
    function cents(rows) { return rows.reduce(function(n, r) { return n + Math.round(num(r.usd) * 100); }, 0); }
    function moneyCents(n) { return (n < 0 ? '-$' : '$') + (Math.abs(n) / 100).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
    function top(rows) {
      return '<ul class="st-rows">' + rows.map(function(r) {
        return '<li><span>' + esc(r.label) + '</span><span class="st-v">' + usd(r.usd) + '</span></li>';
      }).join('') + '</ul>';
    }
    function table(title, rows) {
      if (!rows.length) return '';
      return '<p><strong>' + title + ', ' + moneyCents(cents(rows)) + '</strong></p><ul>' + rows.map(function(r) {
        return '<li>' + esc(r.label) + ': ' + moneyCents(Math.round(num(r.usd) * 100)) + (num(r.tok) > 0 ? ', ' + tok(r.tok) + ' tokens' : '') +
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
    var method = (archive ? '<p>Tool archive, net of re-expands: ' + usd(archive.cost_saved_usd) + ' once, before repeat-reads.</p>' : '') +
      (bashExpand ? '<p>Bash compression, net of re-expands: ' + usd(bashNet) + ' once, before repeat-reads.</p>' : '') +
      (num((as.estimates_usd || {}).expand_overhead) ? '<p>Estimated expand-command overhead: ' + usd(as.estimates_usd.expand_overhead) + '.</p>' : '') +
      table('Measured, counted event by event', measured) +
      table('Estimated, sized by a documented method', estimated) +
      '<p>Measured rows come from the ledger: each removal, the cache rate the next call would have paid, and every later turn that would have re-read it, up to the session&rsquo;s next compaction.</p>' +
      savingsPerSessionHtml(ps, overhead) +
      (measuringNames.length ? '<p>Still measuring, not in the total yet: ' + measuringNames.join(' and ') + '.</p>' : '') +
      (incomplete ? '<p>Some older history has no exact price, so this total understates.</p>' : '') +
      (carry.available && num(carry.carry_cost_usd) > 0 ? '<p>Diagnostic, not counted' + ': the older target-session proxy suggested ' +
        tok(carry.carry_tokens) + ' tokens, about ' + usd(carry.carry_cost_usd) + '. That proxy stays out of totals; receiver-linked estimates are counted separately.</p>' : '') +
      '<p>Valued at API rates. On a subscription this is room inside your limits; nothing is refunded. Model routing and opportunity are never part of this total.</p>';
    var SIh = savingsSinceInstall(data);
    var sinceEst = SIh ? num(((s.since_install || {}).all_in || {}).estimated_usd) : 0;
    var sinceMeas = SIh ? SIh.total - sinceEst : 0;
    out += '<div class="card st-hero">' +
      '<h2 class="st-h">What Token Optimizer saved</h2>' +
      '<div class="st-mega__row st-mega__row--hero">' +
        '<div class="st-mega__item"><span class="st-mega st-mega--usd">' + usd(totalUsd) + '</span><span class="st-mega__unit">saved in the last ' + days + ' days</span></div>' +
        (SIh && sinceMeas > 0 ? '<div class="st-mega__item"><span class="st-mega st-mega--usd">' + usd(sinceMeas) + '</span><span class="st-mega__unit">measured since ' + (SIh.date ? esc(SIh.date) : 'you installed') + '</span>' +
          (sinceEst > 0 ? '<span class="st-mega__note">+ ' + usd(sinceEst) + ' estimated</span>' : '') + '</div>' : '') +
        (totalTok > 0 ? '<div class="st-mega__item"><span class="st-mega st-mega--tok">' + tok(totalTok) + '</span><span class="st-mega__unit">tokens kept out of context in ' + days + ' days</span></div>' : '') +
      '</div>' +
      savingsPerSessionHeadline(ps) +
      (totalUsd > 0 ? '<p class="st-bar__title">The last ' + days + ' days, measured and estimated</p>' : '') +
      (totalUsd > 0 ? '<div class="st-bar" role="img" aria-label="Measured ' + usd(mUsd) + ', estimated ' + usd(eUsd) + '">' +
        '<span class="st-bar__m" style="width:' + mPct.toFixed(1) + '%"></span>' +
        '<span class="st-bar__e" style="width:' + (100 - mPct).toFixed(1) + '%"></span></div>' : '') +
      '<div class="st-bands">' +
        (measured.length ? '<div class="st-band"><p class="st-legend"><span class="st-sw st-m" aria-hidden="true"></span>Measured <span class="st-v">' + usd(mUsd) + '</span></p>' + top(measured) + '</div>' : '') +
        (estimated.length ? '<div class="st-band"><p class="st-legend"><span class="st-sw st-e" aria-hidden="true"></span>Estimated <span class="st-v">' + usd(eUsd) + '</span></p>' + top(estimated) + '</div>' : '') +
      '</div>' +
      '<p class="st-sub">Whole-dollar rows are rounded individually; exact amounts are in the breakdown.</p>' +
      f.fold('How this total is built', method) +
      (SIh && SIh.total > 0 ? f.fold('How since-install is valued', savingsSinceInstallBody(data, SIh)) : '') +
      '</div>';
  }

  // ---- 1b. Still on the table: opportunity, never part of a savings total ----
  out += savingsOnTableHtml(data);

  // ---- 2. One 2x2 block ----
  var cards = [];
  var removedOnce = num(cp.removed_tokens), rereads = num(cp.reread_tokens);
  if (removedOnce > 0 && rereads > 0) {
    var carryN = rereads / removedOnce;
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">' + (as.reread_available !== false && has(as.reread_usd) && num(as.reread_usd) > 0 && totalUsd > 0 ? usd(as.reread_usd) + ' of your ' + usd(totalUsd) + ' came from re-reads avoided' : 'Removed once, saved on every later turn') + '</h2>' +
      '<p class="st-mega__item st-mega__item--card"><span class="st-mega st-mega--tok st-mega--card">' + Math.round(carryN) + '&times;</span><span class="st-mega__unit">re-reads avoided per token removed</span></p>' +
      '<p class="st-lead"><strong>' + tok(removedOnce) + ' priced tokens removed</strong> became <strong>' + tok(rereads) + ' tokens not sent</strong> on later reads' + (has(cp.reread_usd) ? ', worth ' + usd(cp.reread_usd) + ' at API rates' : '') + '.</p>' +
      '</div>');
  }
  if (!trimLedger) {
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">Setup cleanup <span class="st-tag">Measuring</span></h2>' +
      '<p class="st-lead">Each cleanup is being recorded with the date it happened and the tokens it removes from every call. It joins the total once that record exists.</p>' +
      '</div>');
  } else {
    var valuedTrims = trims.filter(function(t) { return t.tier !== 'unattributed' && (num(t.cost_saved_usd) > 0 || num(t.projected_30d_usd) > 0); })
      .sort(function(a, b) { return num(b.projected_30d_usd) - num(a.projected_30d_usd); });
    var rows = valuedTrims.slice(0, 4).map(function(t) {
      var attested = t.source === 'attested' || t.tier === 'estimated';
      return '<li><span>' + esc(t.label || t.item || t.component || 'Setup cleanup') +
        '<span class="st-sub">' + esc(f.day(t.ts) || 'undated') + ', ' + (t.source === 'dated_archive' ? 'estimated from archive' : attested ? 'your figure' : 'recorded by the audit') + '</span></span>' +
        '<span class="st-v">' + usd(t.cost_saved_usd) + '</span></li>';
    }).join('');
    var opts = sd.attested_options || [];
    var formula = valuedTrims.map(function(t) {
      return '<li>' + esc(t.label || t.item || 'Setup cleanup') + ': ' + count(t.tokens_per_call) + ' tokens per call &times; ' + count(t.requests) +
        ' calls since' + (num(t.projected_30d_usd) > 0 ? '. Estimated next 30 days at the recent rate: ' + (num(t.projected_30d_usd) < 1 ? '' : 'about ') + usd(t.projected_30d_usd) + '.' : '.') + '</li>';
    }).join('');
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">Setup cleanup' + '</h2>' +
      (rows
        ? '<p class="st-lead">Trimming setup reduces repeated context on calls that load it. These amounts are already in the total.</p><ul class="st-rows st-rows--card">' + rows + '</ul>' +
          f.fold('How each one is priced', '<ul>' + formula + '</ul>' + (opts.length ? '<p>Your figure can be one of these sizes, priced the same way:</p><ul>' + opts.map(function(o) {
            return '<li>' + count(o.tokens_per_call) + ' tokens per call: estimated ' + usd(o.projected_30d_usd) + ' per 30 days at the recent rate' + (o.chosen ? ' (shown)' : '') + '</li>';
          }).join('') + '</ul>' : ''))
        : '<p class="st-lead">No setup cleanup recorded yet. When an audit archives or rewrites part of your setup, it appears here with its value.</p>') +
      '</div>');
  }
  // Model routing gets its own card, before setup cleanup (setup is always
  // pushed last, so with three cards the list spans the full row).
  var routeCard = savingsRoutingCardHtml(data);
  if (routeCard) cards.splice(Math.max(0, cards.length - 1), 0, routeCard);
  if (cards.length) out += '<div class="st-grid">' + cards.join('') + '</div>';
  out += savingsCostTrendHtml(s.cost_per_message_trend);


  // ---- 3. Coverage: one line ----
  if (num(cov.sessions) > 0 && num(cov.ran_sessions) > 0) {
    var missing = num(cov.ran_no_ledger_sessions);
    out += '<p class="st-cov">Token Optimizer ran in ' + count(cov.ran_sessions) + ' of your ' + count(cov.sessions) + ' sessions.' +
      (missing > 0 ? ' ' + count(missing) + ' of them ' + (missing === 1 ? 'is' : 'are') + ' not in the ledger yet, so the total understates.' : '') + '</p>';
  }

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

// Hero fold: how the since-install figures are valued. Routing has its own card.
function savingsSinceInstallBody(data, SI) {
  var f = savingsStoryFmt(), num = f.num, has = f.has, usd = f.usd, pct = f.pct, esc_ = esc;
  var s = (data && data.savings) || {};
  var fleet = (s.session_effect || {}).fleet || {};
  var ledgerShare = has(fleet.cost_share_net) ? num(fleet.cost_share_net) : (has(fleet.cost_share) ? num(fleet.cost_share) : null);
  var windows = ((data.runway || {}).windows || []).filter(function(w) { return w && isFinite(Number(w.headroom_pct)); });
  var names = { five_hour: '5-hour limit', seven_day: 'Weekly limit' };
  var routed = SI.routing > 0;
  return '<p>Measured and separately estimated savings are summed since ' + (SI.date ? esc_(SI.date) : 'install') +
    '. Providers do not publish how they weight models and cache reads inside a limit; API price is the closest public stand-in, so this is an estimate of room, not a refund.</p>' +
    (SI.excluded ? '<p>Excluded since install: ' + (routed ? '' : 'routing' + (has(SI.excluded.routing) ? ' ' + usd(SI.excluded.routing) : '') + ', ') + 'unverified lean-resume proxy ' + (has(SI.excluded.lean_resumes) ? usd(SI.excluded.lean_resumes) : 'not priced') + ', and verbosity ' + (has(SI.excluded.verbosity) ? usd(SI.excluded.verbosity) : 'not priced') + '.</p>' : '<p>Unverified lean-resume and verbosity estimates are excluded.</p>') +
    (routed ? '<p>Model routing has its own card and is never added here.</p>' : '') +
    (windows.length ? '<p>Right now: ' + windows.map(function(w) {
      return esc_(names[w.key] || ((w.label || 'Window') + ' limit')) + ' ' + num(w.headroom_pct) + '% left';
    }).join(', ') + ' (share of each limit window).</p>' : '') +
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

// Model routing, its own card: the since-install estimate against the stated
// or install-time baseline. Never added to any other total.
function savingsRoutingCardHtml(data) {
  var f = savingsStoryFmt(), num = f.num, has = f.has, usd = f.usd;
  var s = (data && data.savings) || {};
  var ai = ((s.since_install || {}).all_in) || {};
  var mr = s.model_routing || {};
  var routing = num((ai.components_usd || {}).routing_estimated);
  if (!(routing > 0)) return '';
  var basis = savingsRoutingBasis(data);
  var period = savingsMonthRange(ai.routing_majority_period, ' to ');
  var days = Number(s.period_days) || 30;
  var body = (basis.kind === 'measured'
      ? '<p>Your current work priced at the model mix Token Optimizer recorded when you installed' + (basis.pct ? ' (' + basis.pct + ' Opus)' : '') + ', against what it actually cost.'
      : '<p>Your current work priced at the ' + (basis.pct ? basis.pct + ' ' : '') + 'Opus share you gave Token Optimizer, against what it actually cost.') +
      ' It is an estimate, so it is never added to any savings total.</p>' +
    (basis.kind === 'stated' && has(ai.routing_measured_baseline_opus_share) && has(ai.routing_measured_baseline_realized_cost_usd) ? '<p>Using the measured starting mix (' + (num(ai.routing_measured_baseline_opus_share) * 100).toFixed(2) + '% Opus), the routing estimate is ' + usd(ai.routing_measured_baseline_realized_cost_usd) + '.</p>' : '') +
    (num(mr.realized_cost_usd) > 0 ? '<p>Last ' + days + ' days: ' + usd(mr.realized_cost_usd) + ' of it.</p>' : '');
  return '<div class="card st-card st-card--route">' +
    '<h2 class="st-h">Model routing <span class="st-tag">Estimated</span></h2>' +
    '<p class="st-mega__item st-mega__item--card"><span class="st-mega st-mega--route st-mega--card">' + usd(routing) + '</span><span class="st-mega__unit">saved by routing since you installed</span></p>' +
    '<p class="st-lead">' + esc(basis.lead) + (period ? ' Most of it from ' + esc(period) + '.' : '') + '</p>' +
    f.fold('How this is estimated', body) +
    '</div>';
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

// Per-session shares for the hero fold: each named with its base (that
// session's own context), split by where the session ran. Never above the fold.
function savingsPerSessionHeadline(ps) {
  var f = savingsStoryFmt(), has = f.has, bp = (ps || {}).by_place || {}, local = bp.local || {}, cloud = bp.cloud || {}, lines = [];
  if (f.num(local.sessions) > 0 && has(local.net_median)) lines.push('On this computer: typical ' + f.pct(local.net_median) + (has(local.net_p75) ? ', top quarter ' + f.pct(local.net_p75) + '+' : '') + (local.best && has(local.best.net_max) ? ', best ' + f.pct(local.best.net_max) : ''));
  if (f.num(cloud.sessions) > 0 && has(cloud.net_median)) lines.push('Cloud: typical ' + f.pct(cloud.net_median));
  return lines.length ? '<p class="st-lead">Per session, share of its own context not sent. ' + lines.join('; ') + '.</p>' : '';
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

// Since-install total from the same components as the hero, routing excluded.
function savingsSinceInstall(data) {
  var f = savingsStoryFmt(), num = f.num;
  var si = ((data && data.savings) || {}).since_install || {};
  var ai = si.all_in || {};
  var c = ai.components_usd || {};
  if (!(num(si.days) > 0)) return null;
  var parts = [['Context removed', c.logged], ['Cheaper cache writes', c.class_premium], ['Re-reads avoided', c.reread], ['Estimates', ai.estimated_usd]]
    .filter(function(p) { return num(p[1]) !== 0; });
  if (!parts.length) return null;
  var date = si.install_date ? new Date(si.install_date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '';
  return { parts: parts, total: parts.reduce(function(a, p) { return a + num(p[1]); }, 0), routing: num(c.routing_estimated), days: num(si.days), date: date, excluded: si.excluded_usd || null };
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
    '<li><span><strong>Total</strong></span><span class="st-v"><strong>' + cents(B.totalUsd) + '</strong></span></li></ul>';
  var SI = savingsSinceInstall(data);
  if (SI) {
    out += '<h3 class="ts-section-label st-gap">Since you installed' + (SI.date ? ' on ' + esc(SI.date) : '') + ' (' + f.count(SI.days) + ' days)</h3><ul class="st-rows">' +
      SI.parts.map(function(p) { return row(p[0], p[1], 0); }).join('') +
      '<li><span><strong>Total</strong></span><span class="st-v"><strong>' + cents(SI.total) + '</strong></span></li></ul>' +
      (SI.routing ? '<ul class="st-rows">' + row(savingsRoutingBasis(data).ledger, SI.routing, 0) + '</ul>' : '');
  }
  var cp = s.counted_period || {};
  if (cp.pricing_note) out += '<p>' + esc(cp.pricing_note) + '</p>';
  return out;
}
