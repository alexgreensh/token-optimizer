const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const Module = require('node:module');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');
const resolve = Module._resolveFilename;
Module._resolveFilename = function(request, parent, ...rest) {
  if (request.startsWith('./') && request.endsWith('.js') && parent?.filename.endsWith('.ts')) {
    const candidate = require('node:path').resolve(require('node:path').dirname(parent.filename), request.slice(0,-3)+'.ts');
    if (fs.existsSync(candidate)) return candidate;
  }
  return resolve.call(this, request, parent, ...rest);
};
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText, filename);
const {knownModelRate} = require('./pricing.ts');
test('savings cards reject unsupported foreign variants and retain known zero and dated cards', () => {
  for (const id of ['gpt-5.4-unlisted-variant','gpt-5.6-unlisted-variant','gemini-2.5-pro-unlisted-variant','gpt-5.99','deepseek-v99']) assert.equal(knownModelRate(id,'input'),null,id);
  assert.ok(knownModelRate('openai/gpt-5.4-2026-03-05','input') > 0);
  assert.ok(knownModelRate('google/gemini-2.5-pro','input') > 0);
  assert.equal(knownModelRate('local','input'),0);
  assert.equal(knownModelRate('gpt-5.4','cacheWrite'),0);
});
