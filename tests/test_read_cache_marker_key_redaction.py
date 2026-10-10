"""F7: the read-cache first-read ledger markers key on the REDACTED path.

``shadow_fr:<path>`` / ``active_fr:<path>`` were stored as sqlite meta keys with
the raw path, while the same function already redacted the path for its
``tool_outputs`` row: a path segment shaped like a credential (a URL-ish
checkout dir, a token in a cache folder name) landed on disk through the key.
The key is now built from the redacted path on write AND on lookup, so the
edit-after-read follow-up still resolves.

Secrets are assembled at runtime (push protection scans test files).
Run: python3 -m pytest tests/test_read_cache_marker_key_redaction.py -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_read_cache_first_read_retarget import (  # noqa: E402
    SESSION,
    _compression_events,
    _run_read_cache,
)

SECRET_DIR = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
FRAG = "Z9y8X7w6V5u4"


def _json_file(path: Path) -> Path:
    """json is structure-supported but not an active cohort: the shadow path."""
    obj = {"items": [{"id": i, "name": f"item_{i}", "payload": "x" * 200} for i in range(200)]}
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return path


def _meta_keys(root: Path) -> list[str]:
    keys: list[str] = []
    for db in root.rglob("*.db"):
        try:
            conn = sqlite3.connect(str(db))
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for t in tables:
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                    if "key" in cols and "value" in cols:
                        keys += [str(r[0]) for r in conn.execute(f"SELECT key FROM {t}")]
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return keys


def _read(tmp_path: Path, f: Path):
    return _run_read_cache(
        tmp_path,
        {"tool_name": "Read", "tool_input": {"file_path": str(f), "offset": 0, "limit": 0},
         "session_id": SESSION, "agent_id": SESSION},
        extra_env={"HOME": str(tmp_path / "home")},
    )


def _edit(tmp_path: Path, f: Path):
    """The PostToolUse invalidate hook (`read_cache.py --invalidate`)."""
    import os
    import subprocess
    env = dict(os.environ)
    env.update({"TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path), "TOKEN_OPTIMIZER_READ_CACHE": "1",
                "HOME": str(tmp_path / "home")})
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "read_cache.py"), "--invalidate"],
        input=json.dumps({"tool_name": "Edit",
                          "tool_input": {"file_path": str(f), "old_string": "x", "new_string": "y"},
                          "session_id": SESSION, "agent_id": SESSION}),
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_shadow_marker_key_holds_no_raw_path_secret(tmp_path):
    d = tmp_path / SECRET_DIR
    d.mkdir()
    f = _json_file(d / "data.json")
    out = _read(tmp_path, f)
    assert out.returncode == 0, out.stderr
    keys = _meta_keys(tmp_path)
    shadow = [k for k in keys if k.startswith("shadow_fr:")]
    assert shadow, f"shadow marker was not armed (keys: {keys})"
    assert not any(FRAG in k for k in keys), [k for k in keys if FRAG in k]


def test_shadow_followup_still_resolves_through_the_redacted_key(tmp_path):
    d = tmp_path / SECRET_DIR
    d.mkdir()
    f = _json_file(d / "data.json")
    assert _read(tmp_path, f).returncode == 0
    out = _edit(tmp_path, f)
    assert out.returncode == 0, out.stderr
    followups = [e for e in _compression_events(tmp_path, SESSION)
                 if e["detail"] == "shadow first-read followed by edit"]
    assert len(followups) == 1, _compression_events(tmp_path, SESSION)


def test_ordinary_path_marker_key_is_unchanged(tmp_path):
    (tmp_path / "plain").mkdir()
    f = _json_file(tmp_path / "plain" / "data.json")
    assert _read(tmp_path, f).returncode == 0
    keys = [k for k in _meta_keys(tmp_path) if k.startswith("shadow_fr:")]
    assert keys == [f"shadow_fr:{f.resolve()}"], keys
