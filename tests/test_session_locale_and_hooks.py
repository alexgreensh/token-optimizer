#!/usr/bin/env python3
"""Regression tests for v5.11.19 fixes:

1. GitHub #73 — under a non-English locale (e.g. he_IL.UTF-8) `ps` emits
   localized lstart dates, breaking measure.py's positional parser so every
   session is dropped. The `ps` subprocesses must force LC_ALL=C / LC_TIME=C.
2. Codex async-hook bug — `measure.py setup-hook` writes a Claude Code hook
   with {"async": true} into settings.json. Codex skips async hooks, so
   setup-hook must no-op under any non-Claude runtime.
3. Lean-output nudge — the gentle verbosity-steer tier must start at 25% fill
   (was 55%).

Run directly:  python3 tests/test_session_locale_and_hooks.py
Exits non-zero on first failure.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "plugins" / "token-optimizer" / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"


def _run(code, env=None):
    full_env = {**os.environ, "PYTHONUTF8": "1"}
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(SCRIPTS), env=full_env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


# ---------- 1. Locale-proof ps (GitHub #73) ----------

_CAPTURE_PS_ENV = """
import sys; sys.path.insert(0, '.')
import subprocess as sp
captured = []
class _R:
    returncode = 1
    stdout = ''
    stderr = ''
def _rec(*a, **k):
    captured.append(k.get('env'))
    return _R()
sp.run = _rec
import measure
measure._collect_posix_claude_sessions('claude')
measure._find_session_version_for_pid(999999)
envs = [e for e in captured if e]
import json
print(json.dumps([{'LC_ALL': e.get('LC_ALL'), 'LC_TIME': e.get('LC_TIME')} for e in envs]))
"""


def test_ps_calls_force_c_locale():
    r = _run(_CAPTURE_PS_ENV)
    assert r.returncode == 0, f"capture script crashed: {r.stderr}"
    envs = json.loads(r.stdout.strip().splitlines()[-1])
    # _collect_posix_claude_sessions always fires its ps call; the per-pid
    # version probe is skipped when ~/.claude/projects is absent (clean CI box),
    # so assert >= 1 here and rely on the source-count test below to prove BOTH
    # call sites carry the locale env.
    assert len(envs) >= 1, f"expected at least one ps call to run, got {envs}"
    for e in envs:
        assert e["LC_ALL"] == "C", f"ps call missing LC_ALL=C: {e}"
        assert e["LC_TIME"] == "C", f"ps call missing LC_TIME=C: {e}"


def test_ps_source_documents_issue_73():
    src = MEASURE.read_text(encoding="utf-8")
    # Three ps calls parse/inspect lstart: the two production collectors plus
    # the codex_doctor diagnostic probe. All must force C locale.
    assert src.count('"LC_ALL": "C", "LC_TIME": "C"') >= 3, \
        "all ps subprocess calls that touch lstart must force C locale"


# ---------- 2. Codex async-hook guard ----------

def test_setup_hook_noop_under_codex():
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "claude"
        cfg.mkdir()
        env = {
            "TOKEN_OPTIMIZER_RUNTIME": "codex",
            "CLAUDE_CONFIG_DIR": str(cfg),
            "HOME": td,
        }
        r = subprocess.run(
            [sys.executable, str(MEASURE), "setup-hook"],
            cwd=str(SCRIPTS), env={**os.environ, **env, "PYTHONUTF8": "1"},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        assert r.returncode == 0, f"setup-hook errored under codex: {r.stderr}"
        assert "targets Claude Code" in r.stdout, f"missing guard message: {r.stdout!r}"
        settings = cfg / "settings.json"
        if settings.exists():
            body = settings.read_text(encoding="utf-8")
            assert '"async"' not in body, "setup-hook wrote an async hook under Codex"


def test_setup_hook_writes_async_for_claude():
    """Positive control: under Claude Code the hook IS async (correct there)."""
    src = MEASURE.read_text(encoding="utf-8")
    assert '{"type": "command", "command": HOOK_COMMAND, "async": True}' in src, \
        "Claude Code path must still install an async SessionEnd hook"


# ---------- 3. Lean-output gentle tier starts at 25% ----------

_LEAN_PROBE = """
import sys; sys.path.insert(0, '.')
import pathlib
import measure
class _P:
    def exists(self):
        return True
measure._find_current_session_jsonl = lambda: pathlib.Path(measure.__file__)
measure._quality_cache_path_for = lambda fp=None: _P()
def _probe(fill, **kw):
    measure._read_quality_cache = lambda cp: {
        'fill_pct': fill, 'score': 50, 'nudge_count': 0, 'last_nudge_time': 0,
    }
    return measure.run_verbosity_steer(quiet=True, **kw)
# A known transcript, as every real caller supplies. Without one the session
# identity guard refuses to speak (see NOIDENT below), so the tier boundaries
# have to be probed on the trusted path.
_TP = measure.__file__
print('AT30:' + ('1' if _probe(30, transcript_path=_TP) else '0'))
print('AT25:' + ('1' if _probe(25, transcript_path=_TP) else '0'))
print('AT24:' + ('1' if _probe(24, transcript_path=_TP) else '0'))
print('AT20:' + ('1' if _probe(20, transcript_path=_TP) else '0'))
# Guard: no transcript_path and no session_id means the transcript was inferred
# and cannot be verified. A brand-new session must not inherit these numbers.
print('NOIDENT:' + ('1' if _probe(30) else '0'))
"""


def test_lean_nudge_boundary_is_25pct():
    r = _run(_LEAN_PROBE)
    assert r.returncode == 0, f"lean probe crashed: {r.stderr}"
    out = r.stdout
    assert "AT30:1" in out, f"gentle nudge should fire at 30% fill: {out!r}"
    assert "AT25:1" in out, f"gentle nudge should fire at exactly 25% fill: {out!r}"
    assert "AT24:0" in out, f"gentle nudge should NOT fire at 24% fill: {out!r}"
    assert "AT20:0" in out, f"gentle nudge should NOT fire at 20% fill: {out!r}"
    # Session identity guard: an inferred transcript with no session_id to verify
    # it against must stay silent, even at a fill that would otherwise nudge.
    # This is the observed bug -- a nudge fired on the first prompt of an empty
    # session quoting another session's numbers.
    assert "NOIDENT:0" in out, f"unverifiable session must not nudge: {out!r}"


# ---------- 4. Dual-tree parity ----------

def test_measure_py_dual_tree_parity():
    """The canonical skills/ measure.py and the generated plugins/ mirror must
    be byte-identical, or these tests (which read the plugins/ copy) could pass
    while the shipped/canonical copy drifts."""
    canonical = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"
    mirror = MEASURE  # plugins/.../measure.py
    assert canonical.exists(), f"canonical measure.py missing: {canonical}"
    assert mirror.exists(), f"plugins mirror measure.py missing: {mirror}"
    assert canonical.read_bytes() == mirror.read_bytes(), \
        "measure.py drift between skills/ and plugins/ — run scripts/sync-codex-marketplace-plugin.sh"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
