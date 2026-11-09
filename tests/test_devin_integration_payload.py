"""Integration of the independently extended Devin payloads."""
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Devin adapter requires POSIX")
SCRIPTS = Path(__file__).resolve().parents[1] / "skills/token-optimizer/scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.mark.parametrize("missing", [
    ("bash_search_results.py",),
    ("credential_url_patterns.py",),
    ("bash_search_results.py", "credential_url_patterns.py"),
])
def test_refresh_accepts_both_branch_payload_histories(tmp_path, monkeypatch, missing):
    import devin_install as installer
    from devin_verify import verify_install

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_DEVIN_HOME", raising=False)
    installer.install()
    plugin = installer.state.data_dir() / "plugin"
    manifest = installer.manifest_path()
    record = json.loads(manifest.read_text())
    for name in missing:
        path = plugin / name
        path.unlink()
        del record["files"][str(path)]
    manifest.write_text(json.dumps(record))
    installer.install()
    assert all((plugin / name).is_file() for name in missing)
    assert verify_install()


def test_payload_shapes_reject_unrelated_missing_module(tmp_path):
    from devin_source import PAYLOAD
    from devin_verify import payload_key_shapes

    plugin = tmp_path / "plugin"
    expected, shapes = payload_key_shapes(plugin, PAYLOAD, tmp_path / "SKILL.md")
    assert sorted(expected - {str(plugin / "bash_compress.py")}) not in shapes
    assert sorted(expected - {str(plugin / "_tomli.py")}) not in shapes
