const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');
const Module = require('node:module');
const { withFreshSourceModules } = require('./test-module-cache.cjs');
const originalEnv = Object.fromEntries(['HOME', 'USERPROFILE', 'TOKEN_OPTIMIZER_CHECKPOINT_ROOT'].map(key => [key, process.env[key]]));
// Every store this plugin writes lives under HOME, so point it at a temp folder before loading anything.
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'restore-credit-home-'));
process.env.HOME = home;
process.env.USERPROFILE = home;
process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT = path.join(home, 'checkpoints');
after(() => {
  for (const [key, value] of Object.entries(originalEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
});
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
const originalTsLoader = require.extensions['.ts'];
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, ...rest) {
  if (/^\.{1,2}\//.test(request) && parent?.filename.endsWith('.ts')) {
    const candidate = path.resolve(path.dirname(parent.filename), request) + '.ts';
    if (fs.existsSync(candidate)) return candidate;
  }
  return resolve.call(this, request, parent, ...rest);
};
let plugin;
try {
  plugin = withFreshSourceModules(() => require('./index.ts').default);
} finally {
  Module._resolveFilename = resolve;
  if (originalTsLoader === undefined) delete require.extensions['.ts'];
  else require.extensions['.ts'] = originalTsLoader;
}

const cacheDir = path.join(home, '.openclaw', 'token-optimizer', 'read-cache');
function readCacheFile(agent, session, tokens) {
  fs.mkdirSync(cacheDir, { recursive: true });
  const entry = { mtime: 1, offset: 0, limit: 0, tokensEst: tokens, readCount: 3, lastAccess: Date.now(), digest: 'd' };
  fs.writeFileSync(path.join(cacheDir, `${agent}-${session}.json`), JSON.stringify({ files: { '/work/a.ts': entry } }));
}
function checkpoint(session) {
  const dir = path.join(process.env.TOKEN_OPTIMIZER_CHECKPOINT_ROOT, session);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `progressive-50-${Date.now()}.md`);
  fs.writeFileSync(file, '## Checkpoint\n- Task: fix the bug\n');
  fs.writeFileSync(path.join(dir, 'manifest.jsonl'), JSON.stringify({ file, trigger: 'progressive-50', createdAt: new Date().toISOString() }) + '\n');
}
function mockApi() {
  const handlers = new Map();
  const api = {
    handlers,
    logger: { info() {}, warn() {}, error() {}, debug() {} },
    registerService() {},
    on: (event, handler) => handlers.set(event, handler),
    session: { workflow: { enqueueNextTurnInjection: async () => ({ enqueued: true }) } },
  };
  try { plugin.register(api); } catch { /* handlers are registered before any directory lookup */ }
  return api;
}
const restoreCredits = () => {
  const file = path.join(home, '.openclaw', 'token-optimizer', 'savings-events.jsonl');
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map(JSON.parse).filter(r => r.event_type === 'checkpoint_restore');
};

test('a restore credits the working set the compaction cleared, under the same agent key as the read cache', async () => {
  const api = mockApi();
  const session = 'restore-credit-a';
  checkpoint(session);
  readCacheFile('unknown', session, 120000);
  const ctx = { sessionId: session, sessionKey: session };
  await api.handlers.get('before_compaction')({ messages: [] }, ctx);
  // The compaction removed this agent's read cache.
  assert.equal(fs.existsSync(path.join(cacheDir, `unknown-${session}.json`)), false);
  await api.handlers.get('after_compaction')({}, ctx);
  const credits = restoreCredits().filter(r => r.session_id === session);
  assert.equal(credits.length, 1);
  assert.equal(credits[0].tokens_saved, 120000);
});

test('a sibling agent\'s read cache that survives the compaction is not credited', async () => {
  const api = mockApi();
  const session = 'restore-credit-b';
  checkpoint(session);
  readCacheFile('unknown', session, 120000);
  readCacheFile('other', session, 30000);
  const ctx = { sessionId: session, sessionKey: session };
  await api.handlers.get('before_compaction')({ messages: [] }, ctx);
  await api.handlers.get('after_compaction')({}, ctx);
  const credits = restoreCredits().filter(r => r.session_id === session);
  assert.equal(credits.length, 1);
  assert.equal(credits[0].tokens_saved, 120000);
});

test('a restore without a prior capture credits what is still in the read cache', async () => {
  const api = mockApi();
  const session = 'restore-credit-c';
  checkpoint(session);
  readCacheFile('unknown', session, 50000);
  await api.handlers.get('after_compaction')({}, { sessionId: session, sessionKey: session });
  const credits = restoreCredits().filter(r => r.session_id === session);
  assert.equal(credits.length, 1);
  assert.equal(credits[0].tokens_saved, 50000);
});
