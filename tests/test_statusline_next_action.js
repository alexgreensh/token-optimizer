const assert = require('assert');
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const base = fs.mkdtempSync(path.resolve('out/.tmp/status-next-'));
const current = path.join(base, 'statusline-current.js');
fs.copyFileSync('skills/token-optimizer/scripts/statusline.js', current);
fs.copyFileSync('skills/token-optimizer/scripts/measure.py', path.join(base, 'measure.py'));
const snap = path.join(base, 'snap');
fs.mkdirSync(path.join(snap, 'status-bar'), {recursive: true});
const home = path.join(base, 'home');
fs.mkdirSync(home);
const env = {...process.env, HOME: home, TOKEN_OPTIMIZER_SNAPSHOT_DIR: snap};
env.GIT_CEILING_DIRECTORIES = path.resolve('out');
delete env.COLUMNS;
delete env.CLAUDE_CONFIG_DIR;
delete env.CLAUDE_PLUGIN_DATA;
delete env.TOKEN_OPTIMIZER_PLUGIN_DATA;
const input = JSON.stringify({session_id: 'abcdef123', model: {display_name:'Test'}, workspace:{current_dir:home}});
function run(file) {
  const result = cp.spawnSync('node', [file], {input, encoding:'utf8', env, cwd: home});
  assert.strictEqual(result.status, 0, result.stderr);
  return result.stdout;
}
function cache(data) { fs.writeFileSync(path.join(snap, 'status-bar/global.json'), JSON.stringify({computed_at:Date.now()/1000, ...data})); }
try {
  cache({savings:{savings_headline:{display:{short:'legacy'}}}});
  const expected = '\x1b[38;5;245mTest\x1b[0m \x1b[38;5;245m|\x1b[0m \x1b[38;5;245mhome\x1b[0m \x1b[38;5;245m|\x1b[0m \x1b[38;5;245mQuality --\x1b[0m\n\x1b[32mlegacy ↗\x1b[0m';
  assert.strictEqual(run(current), expected, 'legacy cache output changed');
  cache({counted_headline:{display:{short:'counted'}}, savings:{savings_headline:{display:{short:'legacy'}}}, next_action:{short:'\u001b[31m'+'A'.repeat(50)+'\u0007'}});
  let out = run(current);
  assert(out.includes('counted') && !out.includes('legacy'));
  assert(out.includes('A'.repeat(40)) && !out.includes('A'.repeat(41)));
  assert(!out.includes('\u0007') && !out.includes('\u001b[31m'+'A'));
  cache({counted_headline:null, savings:{savings_headline:{display:{short:'legacy'}}}});
  out = run(current);
  assert(out.includes('legacy') && !out.includes('A'.repeat(40)));
  cache({counted_headline:{display:null}, savings:{savings_headline:{display:{short:'legacy'}}}});
  assert(run(current).includes('legacy'));
  fs.writeFileSync(path.join(snap, 'status-bar/global-pending-new.json'), JSON.stringify({computed_at:Date.now()/1000+1, next_action:{short:'newest'}}));
  out = run(current);
  assert(out.includes('newest') && !out.includes('legacy'));
  console.log('6 statusline scenarios passed');
} finally { fs.rmSync(base, {recursive:true, force:true}); }
