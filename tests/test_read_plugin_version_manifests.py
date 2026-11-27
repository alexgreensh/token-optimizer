"""`_read_plugin_version` must find the manifest the active runtime ships (#357).

The Codex marketplace artifact is staged by scripts/sync-codex-marketplace-plugin.sh
from only skills/, hooks/ and .codex-plugin/plugin.json - no .claude-plugin
directory. The reader only probed .claude-plugin/plugin.json, so a Codex install
resolved TOKEN_OPTIMIZER_VERSION to the "0.0.0" default, pilot-report rendered
"0.0.0", and the dashboard downgrade guard (5.13.x on disk > 0.0.0) blocked every
dashboard refresh as a false downgrade.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from helpers.measure_split import exec_source_module as _split_exec_module  # noqa: E402
from _measure import daemon  # noqa: E402


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "data"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


def _fake_layout(tmp_path, *, claude=None, codex=None):
    """A fake plugin root with the given manifests; returns the facade __file__."""
    root = tmp_path / "plugin"
    if claude is not None:
        d = root / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({"version": claude}), encoding="utf-8")
    if codex is not None:
        d = root / ".codex-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({"version": codex}), encoding="utf-8")
    fake = root / "skills" / "token-optimizer" / "scripts" / "measure.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    return fake


def test_codex_only_manifest_is_found(measure, monkeypatch, tmp_path):
    fake = _fake_layout(tmp_path, codex="5.13.81")
    monkeypatch.setattr(measure, "__file__", str(fake))
    assert daemon._read_plugin_version() == "5.13.81"


def test_codex_only_manifest_without_runtime_env(measure, monkeypatch, tmp_path):
    monkeypatch.delenv("TOKEN_OPTIMIZER_RUNTIME", raising=False)
    fake = _fake_layout(tmp_path, codex="5.13.81")
    monkeypatch.setattr(measure, "__file__", str(fake))
    assert daemon._read_plugin_version() == "5.13.81"


def test_both_manifests_prefers_active_runtime(measure, monkeypatch, tmp_path):
    fake = _fake_layout(tmp_path, claude="1.0.0", codex="2.0.0")
    monkeypatch.setattr(measure, "__file__", str(fake))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "codex")
    assert daemon._read_plugin_version() == "2.0.0"
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    assert daemon._read_plugin_version() == "1.0.0"


def test_no_manifest_falls_back_to_default(measure, monkeypatch, tmp_path):
    fake = _fake_layout(tmp_path)
    monkeypatch.setattr(measure, "__file__", str(fake))
    assert daemon._read_plugin_version(default="0.0.0") == "0.0.0"


def test_parent_project_manifest_is_not_picked_up(measure, monkeypatch, tmp_path):
    # A manifest above the plugin root - a parent project, or ~/.codex-plugin
    # when pytest tmp lives under the user profile - must never be selected.
    # Only the plugin root of the shipped layout counts.
    outer = tmp_path / ".codex-plugin"
    outer.mkdir()
    (outer / "plugin.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    fake = _fake_layout(tmp_path / "nested" / "deeper")
    monkeypatch.setattr(measure, "__file__", str(fake))
    assert daemon._read_plugin_version(default="0.0.0") == "0.0.0"


def test_non_shipped_layout_falls_back_without_probing(measure, monkeypatch, tmp_path):
    # A measure.py outside <root>/skills/token-optimizer/scripts/ is a
    # skill-only install: fall back without probing its directories at all.
    d = tmp_path / ".codex-plugin"
    d.mkdir()
    (d / "plugin.json").write_text(json.dumps({"version": "9.9.9"}), encoding="utf-8")
    fake = tmp_path / "random" / "measure.py"
    fake.parent.mkdir()
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(measure, "__file__", str(fake))
    assert daemon._read_plugin_version(default="0.0.0") == "0.0.0"


def test_unparseable_manifest_falls_back(measure, monkeypatch, tmp_path):
    root = tmp_path / "plugin"
    d = root / ".codex-plugin"
    d.mkdir(parents=True)
    (d / "plugin.json").write_text("{not json", encoding="utf-8")
    fake = root / "skills" / "token-optimizer" / "scripts" / "measure.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    monkeypatch.setattr(measure, "__file__", str(fake))
    assert daemon._read_plugin_version(default="0.0.0") == "0.0.0"
