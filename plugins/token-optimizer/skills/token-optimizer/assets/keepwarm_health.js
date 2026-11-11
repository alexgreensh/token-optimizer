// Keep-warm activity belongs in Health, separate from savings headline claims.
function keepwarmHealthHtml(data) {
  var kw = ((data || {}).cache_health || {}).keepwarm;
  if (!kw) return '';
  if (kw.state === 'platform-gap') return '<div class="card wide"><div class="card-header"><span>Keep-warm</span></div><p>Keep-warm is unavailable on ' + (kw.platform === 'Windows' ? 'Windows' : 'this platform') + '.</p></div>';
  if (kw.state === 'status-unavailable') return '<div class="card wide"><div class="card-header"><span>Keep-warm</span></div><p>Keep-warm status is unavailable.</p></div>';
  var q = kw.quota_value || {}, samples = q.measured || {};
  function meter(sample, label) {
    if (!sample || sample.median_pct == null) return '';
    return '<p>' + label + ': median ' + Number(sample.median_pct).toFixed(3) +
      ' percentage points per ping (' + Number(sample.n || 0) + ' samples).</p>';
  }
  var paused = {
    'negative-net': '30-day net value is negative.',
    'accounting-unavailable': 'cost accounting is unavailable.',
    'kill-switch': 'the environment switch is off.',
    'settings-disabled': 'keep-warm is off in settings.',
    'consent-declined': 'keep-warm is disabled in settings.',
    'tripwire-off': 'the safety tripwire stopped it.'
  };
  return '<div class="card wide"><div class="card-header"><span>Keep-warm</span></div>' +
    '<p>Keep-warm: paused while we prove it is safe and saves you money</p>' +
    (paused[kw.gate_reason] ? '<p>Also paused because ' + paused[kw.gate_reason] + '</p>' : '') +
    '<p>' + Number(kw.pings_7d || 0) + ' pings in the last 7 days.</p>' +
    '<p>' + Number(kw.pings_30d || 0) + ' pings in the last 30 days.</p>' +
    (Number(kw.manual_pings_30d || 0) ? '<p>' + Number(kw.manual_pings_30d) + ' manual warm-ups from the Claude Code mod.</p>' : '') +
    '<p>Estimated net value: $' + Number(kw.net_usd || 0).toFixed(2) + ' at API rates.</p>' +
    meter(samples.five_hour, '5-hour limit') + meter(samples.seven_day, 'Weekly limit') +
    ((!samples.five_hour || samples.five_hour.median_pct == null) &&
      (!samples.seven_day || samples.seven_day.median_pct == null) ?
      '<p>Limit impact has not been measured yet.</p>' : '') + '</div>';
}
