const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const vm = require('node:vm');
const ts = require(process.env.TEST_TYPESCRIPT_PATH || 'typescript');

// Bun's native .ts loader need not use Node's require.extensions / Module._load.
// Evaluate transpiled CommonJS with an explicit require and a private module cache.
function loadSource(entry, stub = () => undefined) {
  const cache = new Map();
  function load(filename) {
    if (cache.has(filename)) return cache.get(filename).exports;
    const module = { exports: {} };
    cache.set(filename, module);
    const nativeRequire = Module.createRequire(filename);
    const sourceRequire = request => {
      const replacement = stub(request, filename);
      if (replacement !== undefined) return replacement;
      if (/^\.{1,2}\//.test(request)) {
        const source = path.resolve(path.dirname(filename), request.replace(/\.(?:js|ts)$/, '')) + '.ts';
        if (fs.existsSync(source)) return load(source);
      }
      return nativeRequire(request);
    };
    const source = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
      fileName: filename,
    }).outputText;
    const evaluate = vm.runInThisContext(Module.wrap(source), { filename });
    evaluate.call(module.exports, module.exports, sourceRequire, module, filename, path.dirname(filename));
    return module.exports;
  }
  return load(path.resolve(__dirname, entry));
}

module.exports = { loadSource };
