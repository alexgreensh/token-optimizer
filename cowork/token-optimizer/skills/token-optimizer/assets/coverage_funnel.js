// Command-output coverage card for the Health tab. Inlined into dashboard.html by
// dashboard_assets via its include marker. Reads data.coverage_funnel, built by
// scripts/_measure/coverage_report.py from the Bash hook's counter rows. Counters
// only: gate names, token counts and fixed-vocabulary reasons, never command text.
// Missing data renders a plain "could not read" note, never a row of zeros.
function coverageFunnelHtml(data) {
  var cf = data && data.coverage_funnel;
  if (!cf) return '';
  var e = (typeof esc === 'function') ? esc : function(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  };
  var head = '<div class="card wide cf-card"><div class="card-header"><span>Command output coverage</span></div>';
  if (!cf.available) {
    return head + '<p class="cf-lead">Could not read the command-output counters, so coverage is unknown (not zero).</p></div>';
  }
  if (cf.empty) {
    return head + '<p class="cf-lead">No command output has been counted yet. This fills in as you run commands.</p></div>';
  }
  var pct = function(x) { return (Number(x) * 100).toFixed(1) + '%'; };
  var tok = function(n) {
    n = Number(n) || 0;
    return n >= 1000000 ? (n / 1000000).toFixed(1) + 'M' : n.toLocaleString();
  };
  var lead = 'Token Optimizer saw ' + tok(cf.tokens) + ' tokens of command output in the last ' +
    (cf.window_days || 30) + ' days and acted on ' + pct(cf.acted_share) + '. Not eligible: ' +
    pct(cf.not_eligible_share) + '.';
  if (cf.top_reject) lead += ' Top reason: ' + cf.top_reject.reason + ' (' + pct(cf.top_reject.share) + ').';
  var html = head + '<p class="cf-lead">' + e(lead) + '</p>' +
    '<table class="session-table cf-table"><thead><tr><th>Where it ended</th><th>Results</th>' +
    '<th>Tokens</th><th>Share</th></tr></thead><tbody>';
  (cf.gates || []).forEach(function(g) {
    html += '<tr><td>' + e(g.label) + '</td><td>' + Number(g.results).toLocaleString() + '</td><td>' +
      tok(g.tokens) + '</td><td>' + pct(g.share) + '</td></tr>';
  });
  html += '</tbody></table><style>.cf-lead{margin:4px 0 10px}.cf-table{width:100%}</style></div>';
  return html;
}
