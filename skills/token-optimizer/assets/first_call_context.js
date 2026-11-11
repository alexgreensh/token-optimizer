function firstCallContextHtml(result) {
  var months = result && Array.isArray(result.months) ? result.months.filter(function(m) {
    return /^\d{4}-\d{2}$/.test(m.month) && Number(m.median) > 0;
  }) : [];
  if (!months.length) return '';
  var max = Math.max.apply(null, months.map(function(m) { return Number(m.p75) || 0; })) || 1;
  var dates = (result.trim_dates || []).filter(function(d) { return /^\d{4}-\d{2}-\d{2}$/.test(d); });
  var width = Math.max(400, months.length * 70), height = 170, step = width / months.length;
  var svg = '<svg viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="Monthly first call context, median and interquartile range" style="width:100%;min-width:400px;">';
  months.forEach(function(m, i) {
    var x = Math.round((i + .5) * step), y = function(n) { return 125 - 100 * (Number(n) || 0) / max; };
    svg += '<line x1="' + x + '" x2="' + x + '" y1="' + y(m.p25) + '" y2="' + y(m.p75) + '" stroke="currentColor" stroke-width="5" opacity=".5"/>';
    svg += '<circle cx="' + x + '" cy="' + y(m.median) + '" r="4" fill="currentColor"><title>' + m.month + ': median ' + Number(m.median).toLocaleString() + ', p25 ' + Number(m.p25).toLocaleString() + ', p75 ' + Number(m.p75).toLocaleString() + ', n=' + Number(m.n) + '</title></circle>';
    var marks = dates.filter(function(d) { return d.slice(0, 7) === m.month; });
    if (marks.length) svg += '<path d="M' + (x - 4) + ' 7 l8 0 -4 7 z" fill="currentColor"><title>Recorded trim: ' + marks.join(', ') + '</title></path>';
    svg += '<text x="' + x + '" y="148" text-anchor="middle" fill="currentColor" font-size="10">' + m.month.slice(2) + '</text>';
  });
  svg += '</svg>';
  var latest = months[months.length - 1], names = {skills:'Skills listing',mcp_tools:'MCP tools',mcp_instructions:'MCP instructions',agents:'Agents',claude_md:'CLAUDE.md',memory_files:'Memory files',other_instructions:'Other instruction files',first_user:'First user message',SessionStart_token_optimizer:'SessionStart, Token Optimizer',SessionStart_other_or_unknown:'SessionStart, other or unknown',UserPromptSubmit_token_optimizer:'UserPromptSubmit, Token Optimizer',UserPromptSubmit_other_or_unknown:'UserPromptSubmit, other or unknown'};
  var split = Object.keys(names).filter(function(k) { return latest.components && latest.components[k]; }).map(function(k) {
    var c = latest.components[k];
    return '<div>' + names[k] + ': ' + Number(c.median).toLocaleString() + ' estimated tokens (' + Number(c.observed) + '/' + Number(latest.n) + ' observed)</div>';
  }).join('');
  var steps = '';
  var ss = result.setup_steps;
  if (ss && ss.available && Array.isArray(ss.steps) && ss.steps.length) {
    var n = function(x) { return Number(x).toLocaleString(); };
    steps = ss.steps.map(function(s) {
      var day = new Date(Number(s.trim_epoch) * 1000).toISOString().slice(0, 10);
      var line;
      if (s.status === 'measured') {
        line = 'first call context fell from ' + n(s.before_median) + ' to ' + n(s.after_median) + ' tokens' + (s.observed_drop_tokens > s.drop_tokens ? '; credited ' + n(s.drop_tokens) + ' per call, the trim\'s own size,' : ', a step of ' + n(s.drop_tokens)) +
          ' carried by ' + n(s.calls_since) + ' calls since' + (s.usd > 0 ? ', about $' + Number(s.usd).toFixed(2) + ' at cache-read prices (measured)' : '') + '.';
      } else if (s.status === 'no_step') {
        line = 'no step down is visible in the first call context, so nothing is credited here and any earlier estimate stands.';
      } else if (s.status === 'pending') {
        line = 'waiting for ' + n(Math.max(0, s.window - s.n_after)) + ' more sessions before the before and after can be compared.';
      } else if (s.status === 'overlap') {
        line = 'another trim falls too close to tell them apart, so nothing is credited here.';
      } else {
        line = 'cannot be measured from the first call context yet (' + String(s.reason || 'not enough sessions') + ').';
      }
      return '<div>Trim on ' + day + ': ' + line.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</div>';
    }).join('');
    steps = '<p>Measured against the first call context (your first message excluded, 20 sessions before against 20 after):</p>' + steps;
  }
  return '<div class="card wide"><div class="card-header"><span>Context before your first message</span></div>' +
    '<p>First API call, including your first message and hooks. Dots show the median; bars show p25 to p75; triangles mark recorded trims.</p><div style="overflow-x:auto">' + svg + '</div>' +
    '<p>Latest month: ' + latest.month + ', ' + Number(latest.n) + ' sessions. Components are estimated separately where observed; they may overlap and do not add to the API total.</p>' + split +
    '<p>Recorded trim dates: ' + (dates.length ? dates.join(', ') : 'none observed') + '. A date alone does not prove a step down.</p>' + steps + '</div>';
}
