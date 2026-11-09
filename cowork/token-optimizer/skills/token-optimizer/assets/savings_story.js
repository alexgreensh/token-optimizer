// Savings story (U9): the Savings tab, top to bottom.
//   1. One hero card: the 30-day total ($ and tokens), the measured/estimated
//      bar, and one sentence on the typical session. The only percentage above
//      the fold. The full row list and the method sit in its fold.
//   2. One 2x2 block: compounding, setup cleanup, limits, model routing.
//   3. One coverage line.
//   4. One row of folds (the caller adds opportunity, ledger and diagnostics).
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
      if (a > 0 && a < 1) return (v < 0 ? '-' : '') + 'under $1';
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

function savingsStoryHtml(data) {
  if (!savingsStoryOn(data)) return '';
  var f = savingsStoryFmt(), num = f.num, has = f.has, usd = f.usd, tok = f.tok, pct = f.pct, count = f.count;
  var s = data.savings || {};
  var se = s.session_effect || {};
  var ps = se.per_session || {};
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
  var trimLedger = sd.method === 'trim_ledger';
  var trims = trimLedger ? (sd.trims || []).filter(function(t) { return t && !t.closed_at && num(t.tokens_per_call) > 0; }) : [];
  function trimName(tier) {
    var t = trims.filter(function(x) { return (x.source === 'attested' || x.tier === 'estimated') === (tier === 'estimated'); })
      .sort(function(a, b) { return String(b.ts).localeCompare(String(a.ts)); })[0];
    var when = t ? f.day(t.ts) : '';
    return 'Setup cleanup' + (when ? ', ' + when : '') + (tier === 'estimated' ? ' (your figure)' : ' (recorded)');
  }
  var trimMeasuredUsd = trimLedger ? num(sd.cost_saved_usd) : 0;
  var trimMeasuredTok = trimLedger ? num(sd.tokens_saved) : 0;
  var trimEst = (trimLedger && sd.estimated) || {};
  var est = as.estimates_usd || {};
  var resumeUsd = num(est.resume_lean_estimated);
  var outputInEst = num(est.output_reread);
  var reloadTok = num(th.reload_avoided_tokens);
  var removedTok = has(th.context_removed_tokens)
    ? num(th.context_removed_tokens) + num(th.output_not_generated_tokens)
    : num(th.removed_tokens) - reloadTok;
  var measured = [
    { key: 'context', label: 'Context removed', usd: num(as.logged_usd) - trimMeasuredUsd,
      tok: Math.max(0, removedTok - trimMeasuredTok), on: has(as.logged_usd) },
    { key: 'rereads', label: 'Re-reads avoided', usd: as.reread_usd, tok: th.reread_tokens,
      on: as.reread_available !== false && has(as.reread_usd) },
    { key: 'premium', label: 'Cheaper cache writes', usd: as.class_premium_usd, tok: 0, on: num(as.class_premium_usd) > 0,
      why: 'Removed context would have been written to the cache on the next call, at the higher write rate.' },
    { key: 'setup_measured', label: trimName('measured'), usd: trimMeasuredUsd, tok: trimMeasuredTok, on: trimLedger && trimMeasuredUsd > 0 }
  ];
  var estimated = [
    { key: 'setup_estimated', label: trimName('estimated'), usd: trimEst.cost_saved_usd, tok: trimEst.tokens_saved,
      on: num(trimEst.cost_saved_usd) > 0 },
    { key: 'output_reread', label: 'Shorter answers, not re-read', usd: orr.cost_saved_usd, tok: orr.tokens_saved,
      on: num(orr.cost_saved_usd) > 0,
      why: orr.control_net == null && orr.available ? 'Not yet compared against sessions without the nudge.' : '' },
    { key: 'resumes', label: 'Lean resumes', usd: resumeUsd, tok: reloadTok, on: resumeUsd > 0 },
    { key: 'other', label: 'Other estimates', usd: num(as.estimated_usd) - resumeUsd - outputInEst, tok: 0,
      on: num(as.estimated_usd) - resumeUsd - outputInEst >= 0.5,
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
  var med = has(ps.net_median) ? ps.net_median : ps.median;
  var p75 = has(ps.net_p75) ? ps.net_p75 : ps.p75;
  if (measured.length || estimated.length) {
    var mPct = totalUsd > 0 ? Math.max(0, Math.min(100, mUsd / totalUsd * 100)) : 0;
    function top(rows) {
      return '<ul class="st-rows">' + rows.slice(0, 2).map(function(r) {
        return '<li><span>' + esc(r.label) + '</span><span class="st-v">' + usd(r.usd) + '</span></li>';
      }).join('') + '</ul>';
    }
    function table(title, rows) {
      if (!rows.length) return '';
      return '<p><strong>' + title + ', ' + usd(sum(rows, 'usd')) + '</strong></p><ul>' + rows.map(function(r) {
        return '<li>' + esc(r.label) + ': ' + usd(r.usd) + (num(r.tok) > 0 ? ', ' + tok(r.tok) + ' tokens' : '') +
          (r.why ? '. ' + esc(r.why) : '') + '</li>';
      }).join('') + '</ul>';
    }
    var overhead = se.overhead || {};
    var carry = s.lean_resume_carry || {};
    var incomplete = cp.pricing_complete === false || as.reread_pricing_complete === false;
    var method = table('Measured, counted event by event', measured) +
      table('Estimated, sized by a stated method', estimated) +
      '<p>Measured rows come from the ledger: each removal, the cache rate the next call would have paid, and every later turn that would have re-read it, up to the session&rsquo;s next compaction.</p>' +
      (num(ps.sessions) > 0 && has(p75) ? '<p>The typical session is the median of ' + count(ps.sessions) + ' sessions with ledger entries; in the top quarter it is ' +
        pct(p75) + ' or more. Both are after subtracting the notes Token Optimizer itself adds to context' +
        (num(overhead.usd) > 0 ? ' (' + tok(overhead.reread_tokens) + ' tokens re-read, about ' + usd(overhead.usd) + ')' : '') + '.</p>' : '') +
      (incomplete ? '<p>Some older history has no exact price, so this total understates.</p>' : '') +
      (carry.available && num(carry.carry_cost_usd) > 0 ? '<p>Diagnostic, not counted' + ': lean resumes also keep the resumed session lighter. Followed to each session&rsquo;s first compaction that is ' +
        tok(carry.carry_tokens) + ' tokens, about ' + usd(carry.carry_cost_usd) + '. It stays out of every total until the method is reviewed.</p>' : '') +
      '<p>Valued at API rates. On a subscription this is room inside your limits; nothing is refunded. Model routing and opportunity are never part of this total.</p>';
    out += '<div class="card st-hero">' +
      '<h2 class="st-h">Saved in the last ' + days + ' days</h2>' +
      '<div class="st-total__row"><span class="st-total__usd">' + usd(totalUsd) + '</span>' +
        (totalTok > 0 ? '<span class="st-total__tok">' + tok(totalTok) + ' tokens kept out of context</span>' : '') + '</div>' +
      (totalUsd > 0 ? '<div class="st-bar" role="img" aria-label="Measured ' + usd(mUsd) + ', estimated ' + usd(eUsd) + '">' +
        '<span class="st-bar__m" style="width:' + mPct.toFixed(1) + '%"></span>' +
        '<span class="st-bar__e" style="width:' + (100 - mPct).toFixed(1) + '%"></span></div>' : '') +
      '<div class="st-bands">' +
        (measured.length ? '<div class="st-band"><p class="st-legend"><span class="st-sw st-m" aria-hidden="true"></span>Measured <span class="st-v">' + usd(mUsd) + '</span></p>' + top(measured) + '</div>' : '') +
        (estimated.length ? '<div class="st-band"><p class="st-legend"><span class="st-sw st-e" aria-hidden="true"></span>Estimated <span class="st-v">' + usd(eUsd) + '</span></p>' + top(estimated) + '</div>' : '') +
      '</div>' +
      (num(ps.sessions) > 0 && has(med)
        ? '<p class="st-hero__line">In a typical session, <span class="st-num">' + pct(med) + '</span> of your context never gets sent.</p>' : '') +
      (measuringNames.length ? '<p class="st-lead">Still measuring, not in the total yet: ' + measuringNames.join(' and ') + '.</p>' : '') +
      f.fold('How this total is built', method) +
      '</div>';
  }

  // ---- 2. One 2x2 block ----
  var cards = [];
  var removedOnce = num(cp.removed_tokens), rereads = num(cp.reread_tokens);
  if (removedOnce > 0 && rereads > 0) {
    var carryN = rereads / removedOnce;
    var oncePct = Math.max(1.2, 100 / (1 + carryN));
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">Removed once, saved on every later turn</h2>' +
      '<div class="st-ratio" role="img" aria-label="' + tok(removedOnce) + ' removed once, ' + tok(rereads) + ' re-reads avoided">' +
        '<div class="st-ratio__row"><span>Removed once</span><span class="st-ratio__track"><span class="st-ratio__once" style="width:' + oncePct.toFixed(2) + '%"></span></span><span class="st-v">' + tok(removedOnce) + '</span></div>' +
        '<div class="st-ratio__row"><span>Re-reads avoided</span><span class="st-ratio__track"><span class="st-ratio__carry" style="width:100%"></span></span><span class="st-v">' + tok(rereads) + '</span></div>' +
      '</div>' +
      '<p class="st-lead">Each removed token was spared about <strong>' + Math.round(carryN) + ' more reads</strong> before its session compacted.</p>' +
      '</div>');
  }
  if (!trimLedger) {
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">Setup cleanup <span class="st-tag">Measuring</span></h2>' +
      '<p class="st-lead">Each cleanup is being recorded with the date it happened and the tokens it removes from every call. It joins the total once that record exists.</p>' +
      '</div>');
  } else {
    var rows = trims.slice(0, 4).map(function(t) {
      var attested = t.source === 'attested' || t.tier === 'estimated';
      return '<li><span>' + esc(t.label || t.item || t.component || 'Setup cleanup') +
        '<span class="st-sub">' + esc(f.day(t.ts) || 'undated') + ', ' + (attested ? 'your figure' : 'recorded by the audit') + '</span></span>' +
        '<span class="st-v">' + usd(t.cost_saved_usd) + '</span></li>';
    }).join('');
    var opts = sd.attested_options || [];
    var formula = trims.map(function(t) {
      return '<li>' + esc(t.label || t.item || 'Setup cleanup') + ': ' + count(t.tokens_per_call) + ' tokens per call &times; ' + count(t.requests) +
        ' calls since' + (num(t.projected_30d_usd) > 0 ? '. At current volume, about ' + usd(t.projected_30d_usd) + ' per 30 days.' : '.') + '</li>';
    }).join('');
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">Setup cleanup' + '</h2>' +
      (rows
        ? '<p class="st-lead">Trimming what loads on every call pays again on each call. These amounts are already in the total.</p><ul class="st-rows st-rows--card">' + rows + '</ul>' +
          f.fold('How each one is priced', '<ul>' + formula + '</ul>' + (opts.length ? '<p>Your figure can be one of these sizes, priced the same way:</p><ul>' + opts.map(function(o) {
            return '<li>' + count(o.tokens_per_call) + ' tokens per call: about ' + usd(o.projected_30d_usd) + ' per 30 days' + (o.chosen ? ' (shown)' : '') + '</li>';
          }).join('') + '</ul>' : ''))
        : '<p class="st-lead">No setup cleanup recorded yet. When an audit archives or rewrites part of your setup, it appears here with its value.</p>') +
      '</div>');
  }
  var actual = num(mr.actual_cost_usd);
  var stretch = (actual > 0 && totalUsd > 0) ? totalUsd / actual : null;
  var ledgerShare = has(fleet.cost_share_net) ? num(fleet.cost_share_net) : (has(fleet.cost_share) ? num(fleet.cost_share) : null);
  if (stretch != null) {
    var windows = ((data.runway || {}).windows || []).filter(function(w) {
      return isFinite(Number(w.headroom_pct)) && isFinite(Number(w.without_headroom_pct));
    });
    var names = { five_hour: '5-hour limit', seven_day: 'Weekly limit' };
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">Your limits <span class="st-tag">Estimated</span></h2>' +
      '<p class="st-big">Your limits stretch about <span class="st-num">' + pct(stretch) + '</span> further.</p>' +
      (windows.length ? '<ul class="st-rows st-rows--card">' + windows.map(function(w) {
          return '<li><span>' + esc(names[w.key] || ((w.label || 'Window') + ' limit')) + '</span><span class="st-v">' +
            num(w.headroom_pct) + '% left</span></li>';
        }).join('') + '</ul>' : '') +
      f.fold('How this is estimated', '<p>The saved total divided by what the same ' + days + ' days actually cost, both at API rates. ' +
        'Providers do not publish how they weight models and cache reads inside a limit; price is the closest public stand-in, so this stays an estimate.</p>' +
        (ledgerShare != null ? '<p>Counted ledger alone, in sessions where Token Optimizer ran: ' + pct(ledgerShare) + ' of the would-be cost.</p>' : '')) +
      '</div>');
  }
  var routingUsd = has(as.routing_usd) ? num(as.routing_usd) : num(mr.realized_cost_usd);
  if (!has(mr.measured_baseline_opus_share)) {
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">Model routing <span class="st-tag">Measuring</span></h2>' +
      '<p class="st-lead">Routing is being re-based on your earliest tracked month, so it is shown here once that comparison exists. It is never part of the total.</p>' +
      '</div>');
  } else if (has(mr.baseline_opus_share)) {
    var base = Math.round(num(mr.baseline_opus_share) * 100) + '%';
    var handEntered = mr.baseline_source === 'pretool_baseline';
    cards.push('<div class="card st-card">' +
      '<h2 class="st-h">Model routing <span class="st-tag">Assumption</span></h2>' +
      '<p class="st-big"><span class="st-num">' + usd(routingUsd) + '</span>' +
        (handEntered ? ' if ' + base + ' of your work ran on Opus before Token Optimizer.' : ' against a measured ' + base + ' Opus baseline.') + '</p>' +
      '<p class="st-lead">' + (handEntered ? 'That starting share was entered by hand, so this stays out of the total.' : 'Shown on its own, outside the total.') + '</p>' +
      f.fold('Measured alternative', (has(mr.current_opus_share) ? '<p>You now run ' + Math.round(num(mr.current_opus_share) * 100) + '% of your work on Opus.</p>' : '') +
        (has(mr.measured_baseline_opus_share)
          ? '<p>Your earliest tracked month ran ' + Math.round(num(mr.measured_baseline_opus_share) * 100) + '% Opus' +
            '. Against that, routing saved ' + usd(mr.measured_baseline_realized_cost_usd) + '.</p>'
          : '<p>No measured starting month is on record yet.</p>')) +
      '</div>');
  }
  if (cards.length) out += '<div class="st-grid">' + cards.join('') + '</div>';

  // ---- 3. Coverage: one line ----
  if (num(cov.sessions) > 0 && num(cov.ran_sessions) > 0) {
    var missing = num(cov.ran_no_ledger_sessions);
    out += '<p class="st-cov">Token Optimizer ran in ' + count(cov.ran_sessions) + ' of your ' + count(cov.sessions) + ' sessions.' +
      (missing > 0 ? ' ' + count(missing) + ' of them ' + (missing === 1 ? 'is' : 'are') + ' not in the ledger yet, so the total understates.' : '') + '</p>';
  }

  return out ? '<section class="st" aria-label="What Token Optimizer saved">' + out + '</section>' : '';
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
    return '<li>' + c[1] + ': ' + f.count(k.sessions) + ' sessions, ' + f.pct(k.traffic_share) + ' of traffic</li>';
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
