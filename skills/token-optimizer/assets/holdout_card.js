// Control-group card for the Health tab. Inlined into dashboard.html by dashboard_assets.
// Reads data.holdout, built by scripts/_measure/holdout_report.py. It renders ONLY when the
// holdout is on and both arms have enough sessions; otherwise it returns '' so users who
// never enabled the control group see nothing at all.
function holdoutHtml(data) {
  var h = data && data.holdout;
  if (!h || !h.ready || !h.metrics) return '';
  var e = (typeof esc === 'function') ? esc : function(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  };
  var num = function(x, d) { return Number(x).toLocaleString(undefined, {maximumFractionDigits: d}); };
  var pct = function(x) { return (Number(x) * 100).toFixed(1) + '%'; };
  var rows = [
    ['API calls per session', 'calls_per_session', function(x) { return num(x, 1); }],
    ['Tokens per call (input + output)', 'tokens_per_call', function(x) { return num(x, 0); }],
    ['Sessions with a re-expand', 'reexpand_rate', pct]
  ];
  var cell = function(m, f) { return f(m.mean) + ' <span class="ho-ci">(' + f(m.low) + ' to ' + f(m.high) + ')</span>'; };
  var html = '<div class="card wide ho-card"><div class="card-header"><span>With and without Token Optimizer</span></div>' +
    '<p class="ho-lead">' + e('Sessions where Token Optimizer ran in shadow (' + h.arms.control + ') compared with sessions where it acted (' +
    h.arms.treatment + '). ' + Math.round((h.interval || 0.95) * 100) + '% intervals; sessions under ' + h.min_calls + ' calls are left out.') + '</p>' +
    '<table class="session-table ho-table"><thead><tr><th>Measure</th><th>Without</th><th>With</th><th>Difference</th></tr></thead><tbody>';
  rows.forEach(function(r) {
    var m = h.metrics[r[1]];
    if (!m) return;
    html += '<tr><td>' + e(r[0]) + '</td><td>' + cell(m.control, r[2]) + '</td><td>' + cell(m.treatment, r[2]) +
      '</td><td>' + cell(m.difference, r[2]) + '</td></tr>';
  });
  html += '</tbody></table><style>.ho-ci{opacity:.7;font-size:12px}.ho-lead{margin:4px 0 10px}.ho-table{width:100%}</style></div>';
  return html;
}
