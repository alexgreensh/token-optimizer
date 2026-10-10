#!/usr/bin/env python3
"""Regression tests for v5.11.19 fixes:

1. Under a non-English locale (e.g. he_IL.UTF-8) `ps` emits
   localized lstart dates, breaking measure.py's positional parser so every
   session is dropped. The `ps` subprocesses must force LC_ALL=C / LC_TIME=C.
2. Codex async-hook bug — `measure.py setup-hook` writes a Claude Code hook
   with {"async": true} into settings.json. Codex skips async hooks, so
   setup-hook must no-op under any non-Claude runtime.
3. Lean-output nudge: the gentle verbosity-steer tier must use
   SessionEfficiency for its degradation gate and displayed metric.

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


# ---------- 1. Locale-proof ps ----------

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
def _probe(fill, score=66, session_efficiency=60, **kw):
    measure._read_quality_cache = lambda cp: {
        'fill_pct': fill,
        'score': score,
        'session_efficiency': session_efficiency,
        'model_context_window': 1_000_000,
        'nudge_count': 0,
        'last_nudge_time': 0,
    }
    return measure.run_verbosity_steer(quiet=True, **kw)
# A known transcript, as every real caller supplies. Without one the session
# identity guard refuses to speak (see NOIDENT below), so the tier boundaries
# have to be probed on the trusted path.
_TP = measure.__file__
print('AT30:' + ('1' if _probe(30, transcript_path=_TP) else '0'))
print('AT25:' + ('1' if _probe(25, transcript_path=_TP) else '0'))
print('AT24:' + ('1' if _probe(24, transcript_path=_TP) else '0'))
print('AT19:' + ('1' if _probe(19, transcript_path=_TP) else '0'))
# Reporter-validated scenarios. ResourceHealth 66 at 63% fill does
# not mean a flawless session is behaviorally degraded, so the gentle tier must
# stay silent when SessionEfficiency is 100.
flawless = _probe(63, score=66, session_efficiency=100, transcript_path=_TP)
print('FLAWLESS63:' + ('1' if flawless else '0'))
# A behaviorally degraded session at the same fill must still receive the
# gentle nudge, and the message must name the metric it actually displays.
degraded = _probe(63, score=66, session_efficiency=60, transcript_path=_TP)
print('DEGRADED63:' + ('1' if degraded else '0'))
print('DEGRADED63_PAYLOAD:' + (degraded or ''))
# The degradation gate is strict: exactly 75 is not below the threshold.
efficiency_boundary = _probe(63, score=66, session_efficiency=75, transcript_path=_TP)
print('EFFICIENCY75:' + ('1' if efficiency_boundary else '0'))
# The strong tier remains fill-driven at 75%, even with perfect behavior, but
# its displayed ResourceHealth score must no longer be called quality.
strong = _probe(75, score=50, session_efficiency=100, transcript_path=_TP)
print('STRONG75:' + ('1' if strong else '0'))
print('STRONG75_PAYLOAD:' + (strong or ''))
# Guard: no transcript_path and no session_id means the transcript was inferred
# and cannot be verified. A brand-new session must not inherit these numbers.
print('NOIDENT:' + ('1' if _probe(30) else '0'))
"""


def test_lean_nudge_boundary_is_25pct():
    """Pin the gentle floor, SessionEfficiency gate, and honest tier labels."""
    r = _run(_LEAN_PROBE)
    assert r.returncode == 0, f"lean probe crashed: {r.stderr}"
    out = r.stdout
    assert "AT30:1" in out, f"gentle nudge should fire at 30% fill: {out!r}"
    assert "AT25:1" in out, f"gentle nudge should fire at exactly 25% fill: {out!r}"
    assert "AT24:0" in out, f"gentle nudge should NOT fire at 24% fill: {out!r}"
    assert "AT19:0" in out, f"gentle nudge should NOT fire at 19% fill: {out!r}"
    assert "FLAWLESS63:0" in out, (
        f"flawless 63%-fill session must stay quiet despite ResourceHealth 66: {out!r}"
    )
    assert "DEGRADED63:1" in out, f"SessionEfficiency 60 must still nudge: {out!r}"
    assert "session efficiency 60/100" in out, (
        f"gentle nudge must report SessionEfficiency: {out!r}"
    )
    assert "EFFICIENCY75:0" in out, (
        f"SessionEfficiency exactly 75 must not cross the degradation gate: {out!r}"
    )
    assert "STRONG75:1" in out, f"strong tier must still fire at 75% fill: {out!r}"
    assert "resource health 50/100" in out, (
        f"strong nudge must label ResourceHealth honestly: {out!r}"
    )
    assert "quality 66/100" not in out and "quality 50/100" not in out, (
        f"verbosity-steer payloads must not call ResourceHealth quality: {out!r}"
    )
    # Session identity guard: an inferred transcript with no session_id to verify
    # it against must stay silent, even at a fill that would otherwise nudge.
    # This is the observed bug -- a nudge fired on the first prompt of an empty
    # session quoting another session's numbers.
    assert "NOIDENT:0" in out, f"unverifiable session must not nudge: {out!r}"


def test_verbosity_min_fill_is_clamped():
    """A misconfigured floor must not turn the nudge into background noise or
    silently disable it. 0/negative would fire on the first prompt of an empty
    session (the bug the identity guard exists to stop); >= 75 would hand the
    range to the strong tier and make the gentle tier unreachable."""
    probe = (
        "import sys; sys.path.insert(0, '.')\n"
        "import measure\n"
        "print('V:' + str(measure._VERBOSITY_NUDGE_MIN_FILL))\n"
    )
    for raw, want in (("-5", 1), ("0", 1), ("20", 20), ("74", 74), ("999", 74), ("abc", 25)):
        env = dict(os.environ, TOKEN_OPTIMIZER_VERBOSITY_MIN_FILL=raw)
        r = subprocess.run(
            [sys.executable, "-c", probe], cwd=str(MEASURE.parent),
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, f"probe crashed for {raw!r}: {r.stderr}"
        assert f"V:{want}" in r.stdout, (
            f"TOKEN_OPTIMIZER_VERBOSITY_MIN_FILL={raw!r} should resolve to "
            f"{want}, got: {r.stdout!r}"
        )


# ---------- 4. Dual-tree parity ----------

# ---------- 3. Claude Desktop / WSL 2 ccd-cli launcher (issue #192) ----------

_MATCHER_PROBE = """
import sys; sys.path.insert(0, '.')
import json
import measure
m = measure._command_matches_process
ccd = "/home/user/.claude/remote/ccd-cli/2.1.271 --output-format stream-json --verbose"
result = {
    # POSITIVE: the versioned WSL 2 desktop launcher must be detected as a
    # `claude` session even though argv[0]'s basename is the version string.
    "ccd_matches_claude": m(ccd, "claude"),
    # POSITIVE: a non-standard-HOME absolute path (e.g. WSL 2 /mnt/c) still
    # matches. `ps`/`/proc/<pid>/cmdline` never emit a literal `~`; they give an
    # absolute or relative real path, so an abbreviated-`~` case would be fake.
    "ccd_home_relative": m("/mnt/c/Users/alex/.claude/remote/ccd-cli/2.1.271 --model x", "claude"),
    # NEGATIVE: must not satisfy a codex probe (no false cross-runtime match).
    "ccd_not_codex": m("/home/user/.claude/remote/ccd-cli/2.1.271", "codex"),
    # NEGATIVE: the launcher path appearing only as an ARGUMENT must not match.
    "path_as_arg": m("/usr/bin/vim /home/user/.claude/remote/ccd-cli/notes.txt", "claude"),
    # NEGATIVE: a bundled/spawned binary UNDER the version dir (e.g. ripgrep)
    # must not match -- the version-shaped basename is anchored to the end.
    "ccd_bundled_binary": m("/home/u/.claude/remote/ccd-cli/2.1.271/rg", "claude"),
    # NEGATIVE: a non-version helper file under the ccd-cli dir must not match.
    "ccd_helper_script": m("/home/u/.claude/remote/ccd-cli/helper.sh", "claude"),
    # NEGATIVE: an in-progress `.download` partial must not match.
    "ccd_download_partial": m("/home/u/.claude/remote/ccd-cli/2.1.271.download", "claude"),
    # NEGATIVE: the bare ccd-cli directory itself must not match.
    "ccd_bare_dir": m("/home/u/.claude/remote/ccd-cli/", "claude"),
    # NEGATIVE: an unrelated remote-relay server process must not be swept in.
    "relay_server": m("/home/user/.claude/remote/srv/abc123/server --serve --socket /x", "claude"),
    # REGRESSION: the pre-existing bare/basename matches still work.
    "bare_claude": m("claude --resume abc", "claude"),
    "basename_claude": m("/usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude", "claude"),
}
print(json.dumps(result))
"""


def test_ccd_cli_launcher_detected_issue_192():
    r = _run(_MATCHER_PROBE)
    assert r.returncode == 0, f"probe crashed: {r.stderr}"
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["ccd_matches_claude"], "WSL 2 ccd-cli launcher not detected as a claude session"
    assert res["ccd_home_relative"], "non-standard-HOME absolute ccd-cli launcher not detected"
    assert not res["ccd_not_codex"], "ccd-cli launcher wrongly matched a codex probe"
    assert not res["path_as_arg"], "ccd-cli path matched when it was only an argument"
    assert not res["ccd_bundled_binary"], "bundled binary under the ccd-cli version dir wrongly matched"
    assert not res["ccd_helper_script"], "non-version ccd-cli helper file wrongly matched"
    assert not res["ccd_download_partial"], "ccd-cli .download partial wrongly matched"
    assert not res["ccd_bare_dir"], "bare ccd-cli directory wrongly matched"
    assert not res["relay_server"], "remote relay server process wrongly matched as a claude session"
    assert res["bare_claude"], "regression: bare `claude` no longer matches"
    assert res["basename_claude"], "regression: absolute-path claude basename no longer matches"


_CCD_COLLECT_PROBE = """
import sys; sys.path.insert(0, '.')
import json
import subprocess as sp
import measure

# A synthetic `ps` table: header + the two Claude Desktop WSL 2 processes from
# issue #192 (relay `server` and the versioned `ccd-cli` Code session) plus an
# unrelated process. Only the ccd-cli line is a real Claude Code CLI session.
_PS = (
    "  PID  PPID TTY      STARTED                        ELAPSED COMMAND\\n"
    " 4242  4100 ??       Mon Sep 15 09:00:00 2026         01:00 "
    "/home/user/.claude/remote/ccd-cli/2.1.271 --output-format stream-json --verbose --model sonnet\\n"
    " 4200  4100 ??       Mon Sep 15 08:59:00 2026         02:00 "
    "/home/user/.claude/remote/srv/abc123/server --serve --socket /home/user/.claude/remote/run/x/s\\n"
    " 9999  4100 pts/0    Mon Sep 15 09:30:00 2026         00:30 node /path/to/app.js\\n"
)

class _R:
    returncode = 0
    stdout = _PS
    stderr = ""

sp.run = lambda *a, **k: _R()
sessions = measure._collect_posix_claude_sessions("claude")
pids = sorted(s["pid"] for s in sessions)
print(json.dumps({"pids": pids}))
"""


def test_ccd_cli_session_collected_end_to_end_issue_192():
    r = _run(_CCD_COLLECT_PROBE)
    assert r.returncode == 0, f"probe crashed: {r.stderr}"
    res = json.loads(r.stdout.strip().splitlines()[-1])
    # Exactly the ccd-cli Code session (pid 4242): the relay `server` and the
    # unrelated `node` process must be excluded.
    assert res["pids"] == [4242], f"expected only the ccd-cli session, got {res['pids']}"


def test_ccd_cli_source_documents_issue_192():
    src = MEASURE.read_text(encoding="utf-8")
    assert "/.claude/remote/ccd-cli/" in src, \
        "ccd-cli launcher segment missing from the process matcher"
    assert "issue #192" in src, "issue #192 reference missing from measure.py"


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


def test_hooks_dual_tree_parity():
    """Every file in hooks/ must be byte-identical between the canonical tree
    and the plugins/ mirror, except hooks.json which is intentionally async-
    stripped in the mirror (see test_mirror_has_async_stripped_but_is_otherwise_identical).
    Catches drift in run.py, module_runner.py, python-launcher.sh, etc."""
    canonical_hooks = REPO / "hooks"
    mirror_hooks = REPO / "plugins" / "token-optimizer" / "hooks"
    assert canonical_hooks.is_dir(), f"canonical hooks/ missing: {canonical_hooks}"
    assert mirror_hooks.is_dir(), f"plugins mirror hooks/ missing: {mirror_hooks}"
    # hooks.json is the declared async exception; everything else must match.
    # Skip __pycache__ (bytecode cache, not source).
    for cf in canonical_hooks.iterdir():
        if cf.name == "hooks.json" or cf.name == "__pycache__":
            continue
        mf = mirror_hooks / cf.name
        assert mf.exists(), f"mirror hooks/{cf.name} missing"
        assert cf.read_bytes() == mf.read_bytes(), (
            f"hooks/{cf.name} drift between canonical and plugins/ — "
            f"run scripts/sync-codex-marketplace-plugin.sh"
        )


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
