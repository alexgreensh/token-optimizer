"""Exercise recovery rendering without installing UI dependencies."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _node(script, data):
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    proc = subprocess.run(["node", "-e", script], input=json.dumps(data), text=True,
                          cwd=ROOT, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    if isinstance(result, dict) and result.get("skip"):
        pytest.skip(result["skip"])
    return result


@pytest.mark.parametrize("status", ["needs_attention", "no_concerning_signal_observed", "unmeasurable", None])
def test_vscode_tooltip_and_webview_keep_recovery_neutral(status):
    result = _node(r"""
const fs = require('node:fs'), vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
if (!stripTypeScriptTypes) {
  process.stdout.write(JSON.stringify({skip:'Node TypeScript stripping unavailable'}));
  process.exit(0);
}
const status = JSON.parse(fs.readFileSync(0, 'utf8'));
function load(file, expression) {
  const src = fs.readFileSync(file, 'utf8').replace(/^import .*$/gm, '').replace(/^export /gm, '');
  return vm.runInNewContext(stripTypeScriptTypes(src, {mode: 'transform'}) + '\n' + expression,
    { crypto: require('node:crypto'), status });
}
const tooltip = load('vscode-extension/src/format.ts',
  'buildTooltip({hasData:true, scoped:true, agents:[], compactions:3, compactionRecovery:status}, {nowMs:0})');
const html = load('vscode-extension/src/statusPanel.ts', 'new StatusPanel(() => {}).html()');
const app = {innerHTML:'', querySelectorAll: () => []};
const context = {document:{getElementById:() => app}, window:{addEventListener:()=>{}},
  acquireVsCodeApi: () => ({postMessage:()=>{}})};
vm.createContext(context);
vm.runInContext(html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1], context);
context.model = {hasData:true, scoped:true, compactions:{count:3, recovery:status}};
vm.runInContext('render(model)', context);
process.stdout.write(JSON.stringify({tooltip, panel:app.innerHTML}));
""", status)
    assert "3 (observing)" in result["tooltip"]
    assert "3 (observing)" in result["panel"]
    assert "needs attention" not in result["tooltip"] + result["panel"]
    assert "recovery ok" not in result["tooltip"] + result["panel"]


@pytest.mark.parametrize("legacy", [False, True])
def test_dashboard_displays_compactions_and_recovery_without_loss_or_score_bar(legacy):
    bd = ({"compaction_depth": {"compactions": 2, "score": 10, "detail": "~88% lost"}}
          if legacy else {"compactions": {"count": 2, "weighted": False},
                          "compaction_recovery": {"status": "needs_attention", "score": 10}})
    html = _node(r"""
const fs = require('node:fs'), vm = require('node:vm');
const q = {breakdown: JSON.parse(fs.readFileSync(0, 'utf8'))};
const source = fs.readFileSync('skills/token-optimizer/assets/dashboard.html', 'utf8');
const block = source.split('// Breakdown bars')[1].split('// Waste estimate')[0];
const html = vm.runInNewContext('var html="";\n' + block + '\nhtml', {q, esc:String});
process.stdout.write(JSON.stringify(html));
""", bd)
    assert "Compactions" in html and "2 compactions" in html
    assert "Recovery" in html and "observing" in html
    assert "lost" not in html and "60-70" not in html and "needs_attention" not in html
    assert "transition:width" not in html


def test_published_compaction_examples_do_not_claim_fixed_loss():
    for base in [ROOT / "skills/token-optimizer/assets", ROOT / "docs-site/public/images"]:
        for name in ["quality-example.svg", "status-bar.svg", "hero-terminal.svg", "user-profiles.svg"]:
            text = (base / name).read_text()
            assert not any(term in text for term in ["~65%", "88%", "60-70%", "30% remains", "context: lost"]), name
