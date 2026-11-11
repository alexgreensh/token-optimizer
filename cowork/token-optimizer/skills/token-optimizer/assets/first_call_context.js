// "Context before your first message": one big number (latest monthly median of
// the first API call), the change since the first month, the monthly dot chart,
// and a fold with the components, recorded trims and measured steps.
// A measured step is priced on main-thread calls since the trim, the same count
// the Setup cleanup card uses for that trim.
function firstCallContextHtml(result) {
  var months = result && Array.isArray(result.months) ? result.months.filter(function(m) {
    return /^\d{4}-\d{2}$/.test(m.month) && Number(m.median) > 0;
  }) : [];
  if (!months.length) return '';
  var esc = function(v) { return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;'); };
  var n = function(x) { return Number(x).toLocaleString('en-US'); };
  var k = function(x) { x = Number(x) || 0; return Math.abs(x) >= 1000 ? Math.round(x / 1000) + 'K' : String(Math.round(x)); };
  var monthName = function(m) { return new Date(Date.UTC(+m.slice(0, 4), +m.slice(5, 7) - 1, 15)).toLocaleString('en-US', {month: 'long', timeZone: 'UTC'}); };
  var max = Math.max.apply(null, months.map(function(m) { return Number(m.p75) || 0; })) || 1;
  var dates = (result.trim_dates || []).filter(function(d) { return /^\d{4}-\d{2}-\d{2}$/.test(d); });
  var left = 44, width = Math.max(400, left + months.length * 70), height = 170, step = (width - left) / months.length;
  var y = function(v) { return 125 - 100 * (Number(v) || 0) / max; };
  var svg = '<svg viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="Monthly first call context, median and interquartile range" style="width:100%;min-width:400px;">';
  [0, max / 2, max].forEach(function(t) {
    svg += '<text x="' + (left - 8) + '" y="' + (y(t) + 3) + '" text-anchor="end" fill="currentColor" font-size="10" opacity=".7">' + k(t) + '</text>' +
      '<line x1="' + left + '" x2="' + width + '" y1="' + y(t) + '" y2="' + y(t) + '" stroke="currentColor" opacity=".12"/>';
  });
  months.forEach(function(m, i) {
    var x = Math.round(left + (i + .5) * step);
    svg += '<line x1="' + x + '" x2="' + x + '" y1="' + y(m.p25) + '" y2="' + y(m.p75) + '" stroke="currentColor" stroke-width="5" opacity=".5"/>';
    svg += '<circle cx="' + x + '" cy="' + y(m.median) + '" r="4" fill="currentColor"><title>' + m.month + ': median ' + n(m.median) + ', p25 ' + n(m.p25) + ', p75 ' + n(m.p75) + ', n=' + Number(m.n) + '</title></circle>';
    var marks = dates.filter(function(d) { return d.slice(0, 7) === m.month; });
    if (marks.length) svg += '<path d="M' + (x - 4) + ' 7 l8 0 -4 7 z" fill="currentColor"><title>Recorded trim: ' + marks.join(', ') + '</title></path>';
    svg += '<text x="' + x + '" y="148" text-anchor="middle" fill="currentColor" font-size="10">' + m.month.slice(2) + '</text>';
  });
  svg += '</svg>';
  var first = months[0], latest = months[months.length - 1];
  var delta = Number(latest.median) - Number(first.median);
  var change = months.length > 1 ? (delta >= 0 ? '+' : '-') + k(Math.abs(delta)) + ' since ' + monthName(first.month) : '';
  var names = {skills:'Skills listing',mcp_tools:'MCP tools',mcp_instructions:'MCP instructions',agents:'Agents',claude_md:'CLAUDE.md',memory_files:'Memory files',other_instructions:'Other instruction files',first_user:'First user message',SessionStart_token_optimizer:'SessionStart, Token Optimizer',SessionStart_other_or_unknown:'SessionStart, other or unknown',UserPromptSubmit_token_optimizer:'UserPromptSubmit, Token Optimizer',UserPromptSubmit_other_or_unknown:'UserPromptSubmit, other or unknown'};
  var comps = Object.keys(names).filter(function(c) { return latest.components && latest.components[c]; })
    .map(function(c) { return {name: names[c], c: latest.components[c]}; })
    .sort(function(a, b) { return Number(b.c.median) - Number(a.c.median); });
  var split = comps.length ? '<ul class="st-rows st-cols">' + comps.map(function(r) {
    return '<li><span>' + r.name + '<span class="st-sub">seen in ' + Number(r.c.observed) + ' of ' + Number(latest.n) + '</span></span><span class="st-v">' + n(r.c.median) + '</span></li>';
  }).join('') + '</ul>' : '';
  var steps = '';
  var ss = result.setup_steps;
  if (ss && ss.available && Array.isArray(ss.steps) && ss.steps.length) {
    steps = ss.steps.map(function(s) {
      var day = new Date(Number(s.trim_epoch) * 1000).toISOString().slice(0, 10);
      var line;
      if (s.status === 'measured') {
        var calls = n(s.calls_since) + ' main-thread calls since the trim', usd = Number(s.usd);
        line = 'first call context fell from ' + n(s.before_median) + ' to ' + n(s.after_median) + ' tokens' + (s.observed_drop_tokens > s.drop_tokens ? '; credited ' + n(s.drop_tokens) + ' per call, the trim\'s own size,' : ', a step of ' + n(s.drop_tokens)) +
          ' carried by ' + calls + (usd > 0 ? ', about $' + usd.toFixed(2) + ' at each call\'s cache-read rate (measured)' : '') + '.';
      } else if (s.status === 'no_step') {
        line = 'no step down is visible in the first call context, so nothing is credited here and any earlier estimate stands.';
      } else if (s.status === 'pending') {
        line = 'waiting for ' + n(Math.max(0, s.window - s.n_after)) + ' more sessions before the before and after can be compared.';
      } else if (s.status === 'overlap') {
        line = 'another trim falls too close to tell them apart, so nothing is credited here.';
      } else {
        line = 'cannot be measured from the first call context yet (' + String(s.reason || 'not enough sessions') + ').';
      }
      return '<li>Trim on ' + day + ': ' + esc(line) + '</li>';
    }).join('');
    steps = '<p>Measured against the first call context (your first message excluded, 20 sessions before against 20 after):</p><ul>' + steps + '</ul>';
  }
  var fold = '<details class="fold"><summary>What\'s in it</summary><div class="fold__body">' +
    '<p>' + monthName(latest.month) + ', ' + n(latest.n) + ' sessions, median tokens per component. Components overlap and do not add up to the total.</p>' + split +
    '<p>Recorded trim dates: ' + (dates.length ? dates.join(', ') : 'none observed') + '. A date alone does not prove a step down.</p>' + steps +
    '</div></details>';
  return '<div class="card wide st-card"><h2 class="st-h">Context before your first message</h2>' +
    '<p class="st-mega__item st-mega__item--card"><span class="st-mega st-mega--tok st-mega--card">' + k(latest.median) + '</span>' +
    '<span class="st-mega__unit">tokens before Claude answers your first message, per session' + (change ? '</span><span class="st-mega__note">' + change : '') + '</span></p>' +
    '<p class="st-lead">Median of the first API call each month, your first message and hooks included. Dots show the median; bars show p25 to p75; triangles mark recorded trims.</p>' +
    '<div style="overflow-x:auto">' + svg + '</div>' + fold + '</div>';
}
