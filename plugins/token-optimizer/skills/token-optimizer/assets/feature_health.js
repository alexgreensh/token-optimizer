// Feature health card for the Health tab. Inlined into dashboard.html by
// dashboard_assets via its include marker. Reads data.feature_health, built by
// scripts/_measure/feature_health.py. Missing or unavailable data renders a
// plain "could not read" note, never a row of zeros.
function featureHealthHtml(data) {
  var fh = data && data.feature_health;
  if (!fh) return '';
  var e = (typeof esc === 'function') ? esc : function(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  };
  var head = '<div class="card wide fh-card"><div class="card-header"><span>Feature health</span></div>';
  if (!fh.available) {
    return head + '<p class="fh-lead">Could not read the savings ledger, so feature activity is unknown (not zero).</p></div>';
  }
  var labels = {
    ok: 'Working', quiet: 'Went quiet', fading: 'Fading', no_effect: 'No effect',
    rare: 'Rarely used', 'new': 'New', idle: 'Not used lately', never: 'Not used yet', unknown: 'Unknown'
  };
  var colors = {
    ok: 'var(--c-green, #22c55e)', quiet: 'var(--c-red, #ef4444)', no_effect: 'var(--c-red, #ef4444)',
    fading: 'var(--c-warning-2, #fbbf24)'
  };
  var warn = Number(fh.warnings) || 0;
  var lead = warn > 0
    ? warn + (warn === 1 ? ' savings feature needs' : ' savings features need') + ' a look. Compared with your own earlier rate, scaled to how much you used it in the last ' + (fh.window_days || 30) + ' days.'
    : 'Every savings feature is firing at about your usual rate for the last ' + (fh.window_days || 30) + ' days.';
  var html = head + '<p class="fh-lead">' + e(lead) + '</p>' +
    '<table class="session-table fh-table"><thead><tr><th>Feature</th><th>Status</th>' +
    '<th>Last ' + (fh.window_days || 30) + ' days</th><th>Expected</th><th>Last fired</th></tr></thead><tbody>';
  var order = {quiet: 0, no_effect: 1, fading: 2};
  var rows = (fh.features || []).slice().sort(function(a, b) {
    var x = order.hasOwnProperty(a.status) ? order[a.status] : 9;
    var y = order.hasOwnProperty(b.status) ? order[b.status] : 9;
    return x - y;
  });
  rows.forEach(function(r) {
    var dot = colors[r.status] ? '<span class="fh-dot" style="background:' + colors[r.status] + '"></span>' : '';
    var status = dot + e(labels[r.status] || r.status);
    if (r.message) status += '<div class="fh-msg">' + e(r.message) + '</div>';
    html += '<tr><td>' + e(r.label || r.feature) + '</td><td>' + status + '</td>' +
      '<td>' + (r.events == null ? 'unknown' : Number(r.events).toLocaleString()) + '</td>' +
      '<td>' + (r.expected == null ? 'n/a' : Math.round(Number(r.expected)).toLocaleString()) + '</td>' +
      '<td>' + e(r.last_event || 'never') + '</td></tr>';
  });
  html += '</tbody></table>' +
    '<style>.fh-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}' +
    '.fh-msg{font-size:12px;margin-top:2px}.fh-lead{margin:4px 0 10px}.fh-table{width:100%}</style></div>';
  return html;
}
