"""TOKEN_OPTIMIZER_VERSION must equal the shipped manifest version.

Reported externally (PR #96, danikdanik): the constant was hand-maintained with
a "keep in sync with plugin.json + marketplace.json" comment and had drifted
four releases behind, so a fresh install rendered a stale version in the
dashboard header. The reporter noted it had also drifted across at least two
earlier releases, making it a release-process gap rather than a one-off.

The repo's own version audit did not catch it: it scans manifests and does not
reach this constant. This test is the gate that was missing.
"""

import importlib
import json
import re
import subprocess
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "token-optimizer" / "scripts"


def _manifest_version():
    return json.loads(
        (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )["version"]


def _load():
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    return importlib.import_module("measure")


def test_version_constant_matches_plugin_manifest():
    assert _load().TOKEN_OPTIMIZER_VERSION == _manifest_version()


def test_version_constant_matches_marketplace_manifest():
    market = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    found = json.dumps(market)
    assert _manifest_version() in found, (
        "marketplace.json no longer carries the plugin version"
    )


def test_version_is_derived_not_hardcoded():
    """A literal here is the defect itself: it will drift again."""
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    line = next(
        l for l in src.splitlines()
        if l.startswith("TOKEN_OPTIMIZER_VERSION =")
    )
    assert '"' not in line.split("=", 1)[1], (
        "TOKEN_OPTIMIZER_VERSION is a hardcoded literal again; derive it from "
        "the manifest so a release cannot leave it behind"
    )


def test_version_read_falls_back_without_manifest(tmp_path):
    """Skill-only installs have no plugin directory and must still run."""
    assert _load()._read_plugin_version.__doc__
    mod = _load()
    assert mod._read_plugin_version(default="0.0.0") == _manifest_version()


def test_bundled_dashboard_core_versions_match_plugin_manifest():
    """Bundled labels need synchronized fallbacks when the manifest is absent."""
    for source, name in (
        (ROOT / "opencode" / "src" / "dashboard" / "generator.ts", "CORE_VERSION"),
        (ROOT / "openclaw" / "src" / "dashboard.ts", "CORE_VERSION_FALLBACK"),
    ):
        text = source.read_text(encoding="utf-8")
        versions = re.findall(rf'^const {name} = "([^"]+)";', text, re.MULTILINE)
        assert versions == [_manifest_version()], f"{source}: {name} must match shipped manifest"
    compiled = (ROOT / "openclaw" / "dist" / "dashboard.js").read_text(encoding="utf-8")
    assert f'const CORE_VERSION_FALLBACK = "{_manifest_version()}";' in compiled


def test_patch_bump_updates_dashboard_core_labels(tmp_path):
    """A release bump cannot leave installed dashboard labels one version behind."""
    for relative in (
        ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
        ".codex-plugin/plugin.json", "opencode/src/dashboard/generator.ts",
        "openclaw/src/dashboard.ts", "scripts/bump_patch_version.py",
    ):
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, dest)
    old = _manifest_version()
    bumped = subprocess.run(
        [sys.executable, str(tmp_path / "scripts" / "bump_patch_version.py")],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert bumped != old
    assert json.loads((tmp_path / ".claude-plugin/plugin.json").read_text())["version"] == bumped
    for relative, name in (
        ("opencode/src/dashboard/generator.ts", "CORE_VERSION"),
        ("openclaw/src/dashboard.ts", "CORE_VERSION_FALLBACK"),
    ):
        text = (tmp_path / relative).read_text()
        assert f'const {name} = "{bumped}";' in text
        assert f'const {name} = "{old}";' not in text


def test_auto_release_rebuilds_and_guards_after_version_bump():
    """The release job must verify its final built version before pushing a tag."""
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(encoding="utf-8")
    release_step = workflow.split("      - name: Release the new prices", 1)[1].split("      - name: Open a review PR instead", 1)[0]
    bump = release_step.index("VERSION=$(python3 scripts/bump_patch_version.py)")
    build = release_step.index("(cd openclaw && npm run build)", bump)
    guard = release_step.index("python -m pytest tests/test_version_constant_tracks_manifest.py -q", build)
    publish = release_step.index("git push --atomic", guard)
    assert bump < build < guard < publish
