// Deterministic statusline dependencies, never a live process or network call.
const fs = require('node:fs');
const path = require('node:path');
const cp = require('node:child_process');
const RealDate = Date;
global.Date = class extends RealDate {
  constructor(...args) { super(...(args.length ? args : [1793870400000])); }
  static now() { return 1793870400000; }
};
Object.defineProperty(process, 'pid', {value: 4242});
const effects = [];
const effectPath = process.env.TO_SPLIT_NODE_EFFECTS;
const work = path.resolve(process.env.TO_SPLIT_NODE_WORK);
const originalWrite = fs.writeFileSync.bind(fs);
cp.execFileSync = (command, args, options = {}) => {
  effects.push(['process', command, args, options]);
  return options.encoding ? '' : Buffer.alloc(0);
};
for (const name of ['execSync', 'spawnSync', 'exec', 'execFile', 'spawn']) {
  cp[name] = () => { throw new Error('unmanifested statusline process boundary'); };
}
for (const name of ['writeFileSync', 'appendFileSync', 'mkdirSync', 'unlinkSync', 'renameSync']) {
  const original = fs[name].bind(fs);
  fs[name] = (...args) => {
    const targets = name === 'renameSync' ? args.slice(0, 2) : args.slice(0, 1);
    for (const target of targets) {
      if (typeof target === 'number') continue;
      const resolved = path.resolve(String(target));
      if (resolved !== work && !resolved.startsWith(work + path.sep)) {
        throw new Error('statusline write outside fixture');
      }
    }
    return original(...args);
  };
}
process.on('exit', () => originalWrite(effectPath, JSON.stringify(effects) + '\n'));
