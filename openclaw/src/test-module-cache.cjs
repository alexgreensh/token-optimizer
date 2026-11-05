const path = require('node:path');

// HOME is captured by several entry-point dependencies, not only index.ts.
function clearSourceModules() {
  for (const filename of Object.keys(require.cache)) {
    if (filename.startsWith(__dirname + path.sep) && filename.endsWith('.ts') && !filename.endsWith('.test.ts')) {
      delete require.cache[filename];
    }
  }
}

function withFreshSourceModules(load) {
  clearSourceModules();
  try {
    return load();
  } finally {
    clearSourceModules();
  }
}

module.exports = { withFreshSourceModules };
