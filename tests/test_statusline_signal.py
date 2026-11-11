"""U3 — near-zero-cost statusline existence signal for a resumable checkpoint.

Never silently miss a resume (R2): when a relevance-cleared checkpoint exists
for the current session, the statusline shows a compact ``⤸ resumable`` token.
The signal is terminal UI ONLY -- it must never appear in any hook
``additionalContext`` payload (zero billed tokens). The SessionStart hook
(compact_restore) writes a per-session flag file when its pointer fires; the
statusline reads that flag and renders the token. A stale flag (>30 min) is
ignored so the signal does not outlive the resumable window.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
STATUSLINE = SCRIPTS / "statusline.js"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_SIGNAL = "⤸ resumable"


def _node_available() -> bool:
    return shutil.which("node") is not None


def _run_statusline(script_path: Path, payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = dict(subprocess.os.environ)
    env["HOME"] = str(home)
    # A real CLAUDE_CONFIG_DIR would outrank the pinned HOME (#198).
    env.pop("CLAUDE_CONFIG_DIR", None)
    # Windows resolves the home dir via USERPROFILE (and HOMEDRIVE+HOMEPATH),
    # not HOME, so point those at the tmp home too or node reads the real
    # profile and never finds the test flag file.
    env["USERPROFILE"] = str(home)
    env["HOMEDRIVE"] = home.drive or env.get("HOMEDRIVE", "")
    env["HOMEPATH"] = str(home)[len(home.drive):] if home.drive else str(home)
    return subprocess.run(
        ["node", str(script_path)],
        input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", timeout=60, env=env,
    )


def _payload(sid: str, cwd: str) -> dict:
    return {
        "model": {"display_name": "Test"},
        "workspace": {"current_dir": cwd},
        "session_id": sid,
    }


def _flag_dir(home: Path) -> Path:
    return home / ".claude" / "token-optimizer"


def _write_flag(home: Path, sid: str, cp_path: str = "/tmp/cp.md", ts_ms=None):
    d = _flag_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", sid or "")
    (d / f"resumable-{safe}.json").write_text(
        json.dumps({"checkpoint": cp_path, "ts": ts_ms if ts_ms is not None else int(time.time() * 1000)}),
        encoding="utf-8")


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


# --- T1: relevant checkpoint flag present -> signal in statusline ---

def test_signal_present_when_flag_present(tmp_path):
    if not _node_available():
        pytest.skip("node not available")
    install = tmp_path / "scripts"
    install.mkdir()
    shutil.copy2(STATUSLINE, install / "statusline.js")
    shutil.copy2(SCRIPTS / "measure.py", install / "measure.py")
    home = tmp_path / "home"
    sid = "aaaa1111-2222-3333-8444-aaaaaaaaaaaa"
    _write_flag(home, sid)
    res = _run_statusline(install / "statusline.js", _payload(sid, str(tmp_path)), home)
    assert res.returncode == 0, f"stderr={res.stderr!r}"
    assert _SIGNAL in res.stdout, (
        f"statusline must show the resumable signal when a flag is present; got: {res.stdout!r}")


# --- T2: no flag -> signal absent ---

def test_signal_absent_when_no_flag(tmp_path):
    if not _node_available():
        pytest.skip("node not available")
    install = tmp_path / "scripts"
    install.mkdir()
    shutil.copy2(STATUSLINE, install / "statusline.js")
    shutil.copy2(SCRIPTS / "measure.py", install / "measure.py")
    home = tmp_path / "home"
    sid = "aaaa1111-2222-3333-8444-aaaaaaaaaaaa"
    res = _run_statusline(install / "statusline.js", _payload(sid, str(tmp_path)), home)
    assert res.returncode == 0, f"stderr={res.stderr!r}"
    assert _SIGNAL not in res.stdout, (
        f"statusline must NOT show the signal when no flag is present; got: {res.stdout!r}")


# --- T3: stale flag (>30 min) -> signal absent ---

def test_signal_absent_when_flag_stale(tmp_path):
    if not _node_available():
        pytest.skip("node not available")
    install = tmp_path / "scripts"
    install.mkdir()
    shutil.copy2(STATUSLINE, install / "statusline.js")
    shutil.copy2(SCRIPTS / "measure.py", install / "measure.py")
    home = tmp_path / "home"
    sid = "aaaa1111-2222-3333-8444-aaaaaaaaaaaa"
    stale_ms = int(time.time() * 1000) - (31 * 60 * 1000)
    _write_flag(home, sid, ts_ms=stale_ms)
    res = _run_statusline(install / "statusline.js", _payload(sid, str(tmp_path)), home)
    assert res.returncode == 0, f"stderr={res.stderr!r}"
    assert _SIGNAL not in res.stdout, (
        f"stale flag must not show the signal; got: {res.stdout!r}")


# --- T4: the signal is UI-only -- never in compact_restore additionalContext ---

def _cp(tmp_path, filename, active_task, work_paths, age_seconds=60):
    cp = tmp_path / filename
    cp.write_text("# Session State Checkpoint\n# Generated: test\nbody\n", encoding="utf-8")
    sidecar = {"version": 1, "active_task": active_task, "decisions": [],
               "modified_files": [{"path": p, "action": "edit", "range": None} for p in work_paths],
               "recent_reads": []}
    (tmp_path / cp.name.replace(".md", ".json")).write_text(json.dumps(sidecar), encoding="utf-8")
    return {"filename": filename, "path": str(cp),
            "created": datetime.now() - timedelta(seconds=age_seconds), "trigger": "stop"}


def test_signal_never_in_hook_additional_context(m, tmp_path, monkeypatch, capsys):
    """The resumable signal is statusline UI only. The SessionStart pointer
    (additionalContext) must never carry it -- zero billed tokens."""
    proj = tmp_path / "token-optimizer"
    proj.mkdir()
    cp = _cp(tmp_path, "a1b2c3d4-20260811-120000-checkpoint.md",
             "fix checkpoint injection in token optimizer",
             ["/Users/alex/projects/other/token-optimizer/measure.py"])
    monkeypatch.setattr(m, "CHECKPOINT_DIR", tmp_path)
    monkeypatch.setattr(m, "list_checkpoints", lambda: [cp])
    # Isolate the resumable-flag write to tmp so the test never touches the
    # real ~/.claude/token-optimizer.
    flag_dir = tmp_path / "cache"
    flag_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", flag_dir, raising=True)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.compact_restore(session_id="live-session-id", cwd=str(proj),
                          new_session_only=True)
    out = buf.getvalue()
    # The pointer fires (relevant) but must NOT carry the UI-only signal.
    assert "Cross-session checkpoint" in out, "fixture sanity: pointer should fire"
    assert _SIGNAL not in out, (
        f"the resumable signal must never appear in additionalContext (billed); got: {out!r}")


# --- T4b: eligible candidate below relevance threshold -> flag STILL written (D4) ---

def test_flag_written_for_eligible_candidate_even_below_threshold(m, tmp_path, monkeypatch):
    """D4: the resumable flag must fire whenever an ELIGIBLE (age + own-session
    filtered) candidate exists -- not only when the billed pointer clears the
    relevance bar. This is the missed-genuine-resume case the statusline was
    built for (R2): the billed pointer stays silent (no tokens), but the flag is
    still written so the near-zero-cost statusline can signal it."""
    # An unrelated checkpoint whose work lives OUTSIDE the cwd -> no cwd match and
    # low relevance, so the billed pointer must stay silent.
    cp = _cp(tmp_path, "a1b2c3d4-20260811-120000-checkpoint.md",
             "marketing audit content strategy for Q2",
             ["/Users/alex/projects/marketing-audit/report.md"])
    proj = tmp_path / "unrelated_cwd"
    proj.mkdir()
    monkeypatch.setattr(m, "CHECKPOINT_DIR", tmp_path)
    monkeypatch.setattr(m, "list_checkpoints", lambda: [cp])
    flag_dir = tmp_path / "cache"
    flag_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "QUALITY_CACHE_DIR", flag_dir, raising=True)

    sid = "live-session-id"
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.compact_restore(session_id=sid, cwd=str(proj), new_session_only=True)
    out = buf.getvalue()

    # Billed pointer stays silent (nothing relevant enough).
    assert "checkpoint" not in out.lower() or "available" not in out.lower(), (
        f"billed pointer must stay silent below threshold; got: {out!r}")
    # But the flag IS written for the eligible candidate (this is the D4 fix;
    # the old code cleared the flag and returned here).
    flag_path = m._resumable_flag_path(sid)
    assert flag_path is not None and flag_path.exists(), (
        "the resumable flag must be written for an eligible candidate even when "
        "the billed pointer does not fire (D4)")
    flag = json.loads(flag_path.read_text(encoding="utf-8"))
    assert flag.get("checkpoint") == str(cp["path"]), (
        f"the flag must point at the eligible candidate; got {flag.get('checkpoint')!r}")


# --- T5: Codex carries NO resumable signal (D4: the dead accessor was removed) ---

def test_codex_has_no_resumable_signal_accessor():
    """D4: ``codex_statusline.resumable_signal`` was dead code -- Codex's native
    status_line is a static item list that cannot invoke Python per render, so
    nothing could ever call it. It has been removed rather than left as a false
    promise that Codex surfaces the signal."""
    import codex_statusline
    importlib.reload(codex_statusline)
    assert not hasattr(codex_statusline, "resumable_signal"), (
        "the dead resumable_signal accessor must be removed (or genuinely wired "
        "into a Codex render surface, which the native status_line cannot provide)")
