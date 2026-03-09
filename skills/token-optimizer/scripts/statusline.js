#!/usr/bin/env node
// Token Optimizer - Claude Code Status Line
// Shows: model | project | context bar used% | Q:score
//
// Install: python3 measure.py setup-quality-bar
// The quality score is updated by a UserPromptSubmit hook every ~2 minutes.

const fs = require('fs');
const path = require('path');
const os = require('os');

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  try {
    const data = JSON.parse(input);
    const model = data.model?.display_name || 'Claude';
    const dir = data.workspace?.current_dir || process.cwd();
    const remaining = data.context_window?.remaining_percentage;

    // Context window bar (scaled to 80% real limit)
    let ctx = '';
    if (remaining != null) {
      const rem = Math.round(remaining);
      const rawUsed = Math.max(0, Math.min(100, 100 - rem));
      const used = Math.min(100, Math.round((rawUsed / 80) * 100));

      const filled = Math.floor(used / 10);
      const bar = '\u2588'.repeat(filled) + '\u2591'.repeat(10 - filled);

      if (used < 63) {
        ctx = ` \x1b[32m${bar} ${used}%\x1b[0m`;
      } else if (used < 81) {
        ctx = ` \x1b[33m${bar} ${used}%\x1b[0m`;
      } else if (used < 95) {
        ctx = ` \x1b[38;5;208m${bar} ${used}%\x1b[0m`;
      } else {
        ctx = ` \x1b[5;31m${bar} ${used}%\x1b[0m`;
      }
    }

    // Context quality score from cache file
    let qScore = '';
    const qFile = path.join(os.homedir(), '.claude', 'token-optimizer', 'quality-cache.json');
    if (fs.existsSync(qFile)) {
      try {
        const q = JSON.parse(fs.readFileSync(qFile, 'utf8'));
        const s = q.score;
        if (s != null) {
          if (s >= 85) {
            qScore = ` \x1b[2m|\x1b[0m \x1b[32mContext Quality ${s}%\x1b[0m`;
          } else if (s >= 70) {
            qScore = ` \x1b[2m|\x1b[0m \x1b[2mContext Quality ${s}%\x1b[0m`;
          } else if (s >= 50) {
            qScore = ` \x1b[2m|\x1b[0m \x1b[33mContext Quality ${s}%\x1b[0m`;
          } else {
            qScore = ` \x1b[2m|\x1b[0m \x1b[31mContext Quality ${s}%\x1b[0m`;
          }
        }
      } catch (e) {}
    }

    const dirname = path.basename(dir);
    process.stdout.write(`\x1b[2m${model}\x1b[0m \x1b[2m|\x1b[0m \x1b[2m${dirname}\x1b[0m${ctx}${qScore}`);
  } catch (e) {
    // Silent fail - never break the status line
  }
});
