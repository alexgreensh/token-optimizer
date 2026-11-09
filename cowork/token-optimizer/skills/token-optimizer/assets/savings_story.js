// Savings story (U9): the top of the Savings tab, top to bottom.
//   1. Typical-session hero      (savings.session_effect.per_session)
//   2. One total, two bands      (measured / estimated; $ and tokens; routing never in it)
//   3. Setup trims               (savings.structural_detail, trim ledger)
//   4. Compounding               (removed once, re-read avoided N times)
//   5. Coverage line             (session_effect.coverage, transcript_backfill, hook_skips)
//   6. Limit share               (cost-weighted, estimated, no throughput multiplier)
//   7. Model routing             (separate line, its baseline assumption stated)
// Opportunity is rendered collapsed by the caller.
//
// Inlined into dashboard.html at generation (scripts/dashboard_assets.py, include
// marker). Every reader is fail-open: a missing or malformed field drops its
// row or card, never throws. With no session_effect the function returns ''
// and the Savings tab keeps its previous layout.
function savingsStoryHtml(data) {
  var s = (data && data.savings) || {};
  var se = s.session_effect || {};
  if (!se.available) return '';
  var ps = se.per_session || {};
  var cov = se.coverage || {};
  var fleet = se.fleet || {};
  var as = s.action_savings || {};
  var th = s.token_headline || {};
  var cp = s.counted_period || {};
  var sd = s.structural_detail || {};
  var tb = s.transcript_backfill || {};
  var orr = s.output_reread || {};
  var mr = s.model_routing || {};
  var hs = s.hook_skips || {};
  var days = Number(s.period_days) || 30;

  function num(x) { var v = Number(x); return isFinite(v) ? v : 0; }
  function has(x) { return x != null && isFinite(Number(x)); }
  function usd(v) {
    v = num(v);
    var a = Math.abs(v), sign = v < 0 ? '-' : '';
    if (a >= 1000) return sign + '$' + Math.round(a).toLocaleString('en-US');
    if (a >= 100) return sign + '$' + Math.round(a);
    return sign + '$' + a.toFixed(2);
  }
  function tok(v) {
    v = num(v);
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
    if (v >= 1e3) return Math.round(v / 1e3) + 'K';
    return String(Math.round(v));
  }
  function pct(f, digits) {
    var p = num(f) * 100;
    return (p >= 10 || digits === 0 ? p.toFixed(digits == null ? 1 : digits) : p.toFixed(1)).replace(/\.0$/, '') + '%';
  }
  function count(v) { return Math.round(num(v)).toLocaleString('en-US'); }
  function day(iso) {
    if (!iso) return '';
    var d = new Date(String(iso).length <= 10 ? iso + 'T00:00:00' : iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }
  function foldHtml(summary, body, cls) {
    return '<details class="fold' + (cls ? ' ' + cls : '') + '"><summary>' + esc(summary) +
      '</summary><div class="fold__body">' + body + '</div></details>';
  }
  var warnIcon = '<svg class="st-warn__icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false">' +
    '<path d="M10 2.8 18 17H2z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>' +
    '<path d="M10 8v4.2M10 14.4v.1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

  var out = '';

  // ---- 1. Hero: what Token Optimizer does in one session ----
  var med = has(ps.net_median) ? ps.net_median : ps.median;
  var p75 = has(ps.net_p75) ? ps.net_p75 : ps.p75;
  if (num(ps.sessions) > 0 && has(med)) {
    var win = se.window || {};
    var span = (day(win.start) && day(win.end)) ? day(win.start) + ' to ' + day(win.end) : 'last ' + days + ' days';
    out += '<div class="card st-hero">' +
      '<h2 class="st-hero__line">In a typical session, <span class="st-num">' + pct(med) +
        '</span> of your context never gets sent.</h2>' +
      (has(p75) ? '<p class="st-hero__sub">In your top quarter of sessions, ' + pct(p75) + ' or more.</p>' : '') +
      '<p class="st-meta"><span>' + count(ps.sessions) + ' sessions measured</span> &middot; <span>' + esc(span) + '</span>' +
        (has(ps.net_median) ? ' &middot; <span>net of Token Optimizer&rsquo;s own notes</span>' : '') + '</p>' +
      '</div>';
  }

  // ---- 2. One total, measured and estimated bands ----
  // Setup trims recorded by the trim ledger sit inside logged_usd (the
  // structural pool has always been part of the logged total), so they are
  // shown as their own row and taken out of "Context removed" once.
  var trimLedger = sd.method === 'trim_ledger';
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
    { label: 'Context removed', usd: num(as.logged_usd) - trimMeasuredUsd,
      tok: Math.max(0, removedTok - trimMeasuredTok), on: has(as.logged_usd) },
    { label: 'Write-class premium', usd: as.class_premium_usd, tok: 0,
      note: 'the next call would have paid the higher cache-write rate for it', on: num(as.class_premium_usd) > 0 },
    { label: 'Re-reads avoided', usd: as.reread_usd, tok: th.reread_tokens,
      on: as.reread_available !== false && has(as.reread_usd) },
    { label: 'Cloud sessions recovered', usd: tb.measured_usd, tok: tb.measured_tokens,
      note: count(tb.sessions) + ' sessions rebuilt from their transcripts', on: num(tb.measured_usd) > 0 },
    { label: 'Setup trims, audit-recorded', usd: trimMeasuredUsd, tok: trimMeasuredTok,
      on: trimLedger && trimMeasuredUsd > 0 }
  ];
  var estimated = [
    { label: 'Setup trims, attested', usd: trimEst.cost_saved_usd, tok: trimEst.tokens_saved,
      on: num(trimEst.cost_saved_usd) > 0 },
    { label: 'Output re-reads', usd: orr.cost_saved_usd, tok: orr.tokens_saved,
      note: orr.control_net == null && orr.available ? 'no un-nudged control yet' : '', on: num(orr.cost_saved_usd) > 0 },
    { label: 'Lean resumes', usd: resumeUsd, tok: reloadTok, on: resumeUsd > 0 },
    { label: 'Other estimates', usd: num(as.estimated_usd) - resumeUsd - outputInEst, tok: 0,
      note: 'loops, sub-agents, handovers, restores', on: num(as.estimated_usd) - resumeUsd - outputInEst >= 0.005 },
    { label: 'Cloud rows sized by ratio', usd: tb.estimated_usd, tok: tb.estimated_tokens,
      on: num(tb.estimated_usd) > 0 }
  ];
  function live(rows) { return rows.filter(function(r) { return r.on && num(r.usd) !== 0; }); }
  measured = live(measured); estimated = live(estimated);
  function sum(rows, k) { return rows.reduce(function(a, r) { return a + num(r[k]); }, 0); }
  var mUsd = sum(measured, 'usd'), eUsd = sum(estimated, 'usd');
  var totalUsd = mUsd + eUsd, totalTok = sum(measured, 'tok') + sum(estimated, 'tok');
  if (measured.length || estimated.length) {
    var mPct = totalUsd > 0 ? Math.max(0, Math.min(100, mUsd / totalUsd * 100)) : 0;
    function band(title, rows, sub, cls) {
      if (!rows.length) return '';
      return '<div class="st-band' + cls + '"><h3><span class="st-sw' + cls + '" aria-hidden="true"></span>' +
        '<span class="st-band__name">' + title + '</span><span class="st-v">' + usd(sum(rows, 'usd')) + '</span></h3>' +
        '<p class="st-band__sub">' + sub + '</p><ul class="st-rows">' + rows.map(function(r) {
          return '<li><span class="st-rows__k">' + esc(r.label) +
            (r.note ? '<span class="st-rows__n">' + esc(r.note) + '</span>' : '') + '</span>' +
            '<span class="st-v">' + usd(r.usd) + (num(r.tok) > 0 ? '<span class="st-rows__n">' + tok(r.tok) + ' tokens</span>' : '') +
            '</span></li>';
        }).join('') + '</ul></div>';
    }
    var incomplete = cp.pricing_complete === false || as.reread_pricing_complete === false;
    var overhead = se.overhead || {};
    var carry = s.lean_resume_carry || {};
    var method = '<p>Measured rows come from the ledger: each removal, the cache class the next call billed, and every later turn that would have re-read it, up to the session&rsquo;s next compaction.</p>' +
      '<p>Estimated rows rest on a stated method: an attested setup size, output that stays in history, lean resumes, and cloud rows with no recorded size. Each one is valid while its trigger holds.</p>' +
      (num(overhead.usd) > 0 ? '<p>Token Optimizer&rsquo;s own notes ride the context too: ' + tok(overhead.reread_tokens) +
        ' tokens re-read, about ' + usd(overhead.usd) + '. The typical-session figure above is net of them.</p>' : '') +
      (incomplete ? '<p>Some older history has no exact price, so this total understates.</p>' : '') +
      (carry.available && num(carry.carry_cost_usd) > 0 ? '<p><strong>Diagnostic, not counted:</strong> lean resumes also keep the resumed session lighter. Followed to each session&rsquo;s first compaction that is ' +
        tok(carry.carry_tokens) + ' tokens, about ' + usd(carry.carry_cost_usd) + '. It stays out of every total until the method is reviewed.</p>' : '') +
      '<p>Model routing and opportunity are never part of this total.</p>';
    out += '<div class="card st-total">' +
      '<h2 class="st-h">Saved in the last ' + days + ' days</h2>' +
      '<div class="st-total__row"><span class="st-total__usd">' + usd(totalUsd) + '</span>' +
        (totalTok > 0 ? '<span class="st-total__tok">' + tok(totalTok) + ' tokens kept out of context</span>' : '') + '</div>' +
      (totalUsd > 0 ? '<div class="st-bar" role="img" aria-label="Measured ' + usd(mUsd) + ', estimated ' + usd(eUsd) + '">' +
        '<span class="st-bar__m" style="width:' + mPct.toFixed(1) + '%"></span>' +
        '<span class="st-bar__e" style="width:' + (100 - mPct).toFixed(1) + '%"></span></div>' : '') +
      '<div class="st-bands">' +
        band('Measured', measured, 'Counted event by event.', ' st-m') +
        band('Estimated', estimated, 'Sized by a stated method.', ' st-e') +
      '</div>' +
      '<p class="plain-line">Valued at API rates. On a subscription this is room inside your limits; nothing is refunded.</p>' +
      foldHtml('How this total is built', method) +
      '</div>';
  }

  // ---- 3 + 4. Setup trims and compounding, side by side ----
  var pairA = '';
  if (trimLedger) {
    var trims = (sd.trims || []).filter(function(t) { return t && !t.closed_at && num(t.tokens_per_call) > 0; });
    var trimRows = trims.slice(0, 8).map(function(t) {
      var attested = t.source === 'attested' || t.tier === 'estimated';
      return '<li class="st-trim">' +
        '<div class="st-trim__what"><strong>' + esc(t.label || t.item || t.component || 'Setup trim') + '</strong>' +
          '<span class="st-rows__n">' + esc(day(t.ts) || 'undated') + ' &middot; ' + (attested ? 'attested' : 'recorded by the audit') +
          (t.scope && t.scope !== 'global' ? ' &middot; ' + esc(t.scope_label || 'one project') : '') + '</span></div>' +
        '<div class="st-trim__val st-v">' + usd(t.cost_saved_usd) + '<span class="st-rows__n">this period</span></div>' +
        '<p class="st-trim__math">' + count(t.tokens_per_call) + ' tokens per call &times; ' + count(t.requests) + ' calls since' +
          (num(t.projected_30d_usd) > 0 ? ' &middot; about ' + usd(t.projected_30d_usd) + ' per 30 days at current volume' : '') + '</p>' +
        '</li>';
    }).join('');
    var opts = sd.attested_options || [];
    var optLine = opts.length ? foldHtml('Attested size options', '<p>An attested trim is a size you entered from your own audit record. ' +
        'Each option, priced the same way:</p><ul>' + opts.map(function(o) {
          return '<li>' + count(o.tokens_per_call) + ' tokens per call: ' + tok(o.projected_30d_tokens) + ' tokens, about ' +
            usd(o.projected_30d_usd) + ' per 30 days' + (o.chosen ? ' (shown)' : '') + '</li>';
        }).join('') + '</ul>') : '';
    pairA += '<div class="card st-card">' +
      '<h2 class="st-h">Setup trims</h2>' +
      (trimRows
        ? '<p class="st-lead">A trim shrinks what loads on every call, so it pays again on each one until the item comes back.</p><ul class="st-trims">' + trimRows + '</ul>' + optLine
        : '<p class="st-lead">No setup trims recorded yet. When an audit archives or rewrites part of your setup, it appears here with its value per call.</p>') +
      '</div>';
  }
  var removedOnce = num(cp.removed_tokens), rereads = num(cp.reread_tokens);
  if (removedOnce > 0 && rereads > 0) {
    var carryN = rereads / removedOnce;
    var oncePct = Math.max(1.2, 100 / (1 + carryN));
    pairA += '<div class="card st-card">' +
      '<h2 class="st-h">Removed once, saved on every later turn</h2>' +
      '<div class="st-ratio" role="img" aria-label="' + tok(removedOnce) + ' removed once, ' + tok(rereads) + ' re-reads avoided">' +
        '<div class="st-ratio__row"><span class="st-ratio__k">Removed once</span><span class="st-ratio__track"><span class="st-ratio__once" style="width:' + oncePct.toFixed(2) + '%"></span></span><span class="st-v">' + tok(removedOnce) + '</span></div>' +
        '<div class="st-ratio__row"><span class="st-ratio__k">Re-reads avoided</span><span class="st-ratio__track"><span class="st-ratio__carry" style="width:100%"></span></span><span class="st-v">' + tok(rereads) + '</span></div>' +
      '</div>' +
      '<p class="st-lead">Removed context would have been read again on every later turn. On average each removed token was spared <strong>' +
        carryN.toFixed(0) + ' more times</strong> before the session compacted.</p>' +
      (num(cp.oneshot_usd) > 0 && num(cp.reread_usd) > 0
        ? '<ul class="st-windows"><li><span>First removal</span><span class="st-v">' + usd(cp.oneshot_usd) + '</span></li>' +
          '<li><span>Every turn after it</span><span class="st-v">' + usd(cp.reread_usd) + '</span></li></ul>' +
          '<p class="st-lead">The turns after a removal are worth ' + (num(cp.reread_usd) / num(cp.oneshot_usd)).toFixed(1) +
          '&times; the removal itself.</p>'
        : '') +
      '</div>';
  }
  if (pairA) out += '<div class="st-pair">' + pairA + '</div>';

  // ---- 5. Coverage line ----
  var classes = cov.classes || {};
  if (num(cov.sessions) > 0) {
    var missing = num(cov.ran_no_ledger_sessions);
    var warns = [];
    if (missing > 0) {
      warns.push(count(missing) + ' session' + (missing === 1 ? '' : 's') + ' ran Token Optimizer but ' + (missing === 1 ? 'is' : 'are') +
        ' missing from the ledger. Their savings are not counted yet, so the total understates.');
    }
    if (num(hs.overruns) > 0) {
      warns.push('Hooks ran out of time ' + count(hs.overruns) + ' times' + (hs.where ? ' ' + esc(hs.where) : '') +
        (num(hs.skipped_compressions) > 0 ? ' and skipped about ' + count(hs.skipped_compressions) + ' compressions (' +
          pct(hs.skip_rate) + ' of compressible output)' : '') + '.');
    }
    out += '<div class="st-cov">' +
      '<p class="st-cov__line">Token Optimizer ran in <strong>' + pct(cov.ran_traffic_share, 0) + '</strong> of your traffic (' +
        count(cov.ran_sessions) + ' of ' + count(cov.sessions) + ' sessions).' +
        (num(tb.sessions) > 0 ? ' ' + count(tb.sessions) + ' cloud sessions were rebuilt from transcripts.' : '') + '</p>' +
      warns.map(function(w) { return '<p class="st-warn">' + warnIcon + '<span>' + w + '</span></p>'; }).join('') +
      foldHtml('Where your traffic ran', '<ul>' + [
          ['ledger', 'Counted in the ledger'], ['logged_no_ledger', 'Logged, no repeat-read record'],
          ['ran_unlogged', 'Ran, missing from the ledger'], ['not_ran', 'Token Optimizer did not run'],
          ['unknown', 'Not scanned yet (transcript missing)']
        ].filter(function(c) { return classes[c[0]] && num(classes[c[0]].sessions) > 0; }).map(function(c) {
          var k = classes[c[0]];
          return '<li>' + c[1] + ': ' + count(k.sessions) + ' sessions, ' + pct(k.traffic_share) + ' of traffic</li>';
        }).join('') + '</ul><p>A session counts as covered only on evidence: a ledger row, a logged event, or Token Optimizer&rsquo;s own note in its transcript.</p>') +
      '</div>';
  }

  // ---- 6 + 7. Limit share and model routing ----
  var pairB = '';
  var actual = num(mr.actual_cost_usd);
  var limitShare = (actual > 0 && totalUsd > 0) ? totalUsd / (actual + totalUsd) : null;
  var ledgerShare = has(fleet.cost_share_net) ? num(fleet.cost_share_net) : (has(fleet.cost_share) ? num(fleet.cost_share) : null);
  if (limitShare != null || ledgerShare != null) {
    var shown = limitShare != null ? limitShare : ledgerShare;
    var windows = ((data.runway || {}).windows || []).filter(function(w) {
      return isFinite(Number(w.headroom_pct)) && isFinite(Number(w.without_headroom_pct));
    });
    var names = { five_hour: '5-hour limit', seven_day: 'Weekly limit' };
    pairB += '<div class="card st-card">' +
      '<h2 class="st-h">Your limits <span class="st-tag">Estimated</span></h2>' +
      '<p class="st-big"><span class="st-num">' + pct(shown) + '</span> of what this work would have cost never got spent.</p>' +
      '<p class="st-lead">Weighted by API price, the closest public stand-in for how plan limits count usage.' +
        (limitShare != null && ledgerShare != null ? ' Counted ledger alone, where Token Optimizer ran: ' + pct(ledgerShare) + '.' : '') + '</p>' +
      (windows.length ? '<ul class="st-windows">' + windows.map(function(w) {
          return '<li><span>' + esc(names[w.key] || ((w.label || 'Window') + ' limit')) + '</span><span class="st-v">' +
            num(w.headroom_pct) + '% left' + '<span class="st-rows__n">' + (w.would_be_capped ? 'cut off without it' : num(w.without_headroom_pct) + '% without') + '</span></span></li>';
        }).join('') + '</ul>' : '') +
      foldHtml('How this is estimated', '<p>Saved dollars divided by actual plus saved dollars, at API rates, over the same ' + days + ' days. ' +
        'Providers do not publish how they weight models and cache reads inside a limit, so no basis is better than an estimate. ' +
        'Cache reads are most of the tokens and about half the cost, which is why a token count overstates their weight.</p>') +
      '</div>';
  }
  var routingUsd = has(as.routing_usd) ? num(as.routing_usd) : num(mr.realized_cost_usd);
  if (has(mr.baseline_opus_share) && (routingUsd > 0 || has(mr.measured_baseline_opus_share))) {
    var base = pct(mr.baseline_opus_share, 0);
    var nowShare = has(mr.current_opus_share) ? pct(mr.current_opus_share, 0) : '';
    var handEntered = mr.baseline_source === 'pretool_baseline';
    pairB += '<div class="card st-card">' +
      '<h2 class="st-h">Model routing <span class="st-tag">Assumption</span></h2>' +
      '<p class="st-big"><span class="st-num">' + usd(routingUsd) + '</span>' +
        (handEntered ? ', assuming ' + base + ' of your work ran on Opus before Token Optimizer.' : ' against a measured ' + base + ' Opus baseline.') + '</p>' +
      '<p class="st-lead">' + (nowShare ? 'Now ' + nowShare + ' Opus. ' : '') +
        (handEntered ? 'The starting share was entered by hand, so this stays out of the total.' : 'Shown on its own line, outside the total.') + '</p>' +
      (has(mr.measured_baseline_opus_share)
        ? foldHtml('Measured alternative', '<p>Your earliest tracked month ran ' + pct(mr.measured_baseline_opus_share, 0) + ' Opus. Against that baseline, routing saved ' +
            usd(mr.measured_baseline_realized_cost_usd) + '.</p><p>Each point of assumed starting share moves this line; the measured month is the one figure on record.</p>')
        : '') +
      '</div>';
  }
  if (pairB) out += '<div class="st-pair">' + pairB + '</div>';

  return out ? '<section class="st" aria-label="What Token Optimizer saved">' + out + '</section>' : '';
}
