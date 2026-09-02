#!/usr/bin/env python3
"""SessionStart stdout must satisfy BOTH hosts' output contracts.

THE BUG (observed live, Codex 0.150.0-alpha.12.2, 2026-08-29)::

    SessionStart hook (failed)
      error: hook returned invalid session start JSON output

Codex loads Token Optimizer's Claude-flavoured ``hooks/hooks.json`` DIRECTLY --
``~/.codex/config.toml`` carries
``[hooks.state."token-optimizer@alexgreensh-token-optimizer:hooks/hooks.json:session_start:0:0"]``
through ``:3:0`` -- so ``codex_hook_bridge.py`` (which does wrap its output) is
NOT in this path at all.

CODEX'S CONTRACT, verified two ways
-----------------------------------
1. Its own embedded ``session-start.command.output`` JSON schema (extracted from
   the shipped binary): ``additionalProperties: false`` at BOTH levels; allowed
   top-level keys are ``continue``, ``hookSpecificOutput``, ``stopReason``,
   ``suppressOutput``, ``systemMessage``; ``hookSpecificOutput`` allows only
   ``additionalContext`` + ``hookEventName`` and REQUIRES ``hookEventName``.

2. Live probes -- a hook emitting a fixed payload, run under
   ``codex exec --dangerously-bypass-hook-trust`` against an unreachable model
   provider so the session-start hooks fire and nothing is billed. Verdicts::

     stdout                                              Codex verdict
     --------------------------------------------------  -------------
     ""                          (empty)                 Completed
     "   \n\n"                   (whitespace)            Completed
     "hello world"               (plain, no bracket)     Completed
     "line one\nline two"        (plain, multi-line)     Completed
     "hi [Token Optimizer] x"    (bracket NOT first)     Completed
     '{"systemMessage":"a"}'     (one object)            Completed
     '{"continue":true,"hookSpecificOutput":{...}}'      Completed
     '[Token Optimizer] hi'      (leading bracket)       FAILED
     '\n[Token Optimizer] hi'    (leading bracket)       FAILED
     '[1,2,3]'                   (JSON array)            FAILED
     '{"sysMsg":"a"}\n{"sysMsg":"b"}'  (two objects)     FAILED
     '{"systemMessage":"a"}\nplain'    (trailing text)   FAILED
     '{"hookSpecificOutput":{"additionalContext":"x"}}'  FAILED (no hookEventName)
     '{"decision":"block","reason":"x"}'                 FAILED (unknown keys)

   i.e. Codex TRIMS stdout; empty -> no-op; if the first non-space character is
   ``{`` or ``[`` the WHOLE buffer must be one schema-valid JSON document;
   anything else is ignored as plain text.

WHY TOKEN OPTIMIZER TRIPPED IT
------------------------------
Every human-readable Token Optimizer hook line starts with ``[Token Optimizer]``
-- a ``[``, which Codex reads as the start of a JSON array. Three of the five
SessionStart subcommands emit such a line, captured from the real launcher chain
(``hooks/python-launcher.sh -> hooks/run.py``) with ``CLAUDE_PLUGIN_ROOT`` set
and ``CODEX_HOME``/``TOKEN_OPTIMIZER_RUNTIME`` unset, which is exactly the
environment Codex gives a plugin hook:

  * ``measure.py ensure-health --once-mark``                    -> raw text
  * ``measure.py compact-restore --compact``                    -> raw text
  * ``measure.py compact-restore --new-session-only --once-mark`` -> raw text
  * ``measure.py quality-cache --force --quiet --once-mark``    -> 0..N
    ``{"systemMessage": ...}`` objects; two or more is NDJSON, also rejected
  * ``read_cache.py --clear-compacted --quiet``                 -> always silent
    (every line, success and failure, goes to stderr)

``measure.py`` already had a Codex guard on ``compact-restore``, gated on
``detect_runtime() == "codex"``. It never fired: Codex sets NEITHER ``CODEX_HOME``
NOR ``TOKEN_OPTIMIZER_RUNTIME`` in a hook subprocess (verified with an
env-dumping hook under ``env -i``), only ``CLAUDE_PLUGIN_ROOT``/
``CLAUDE_PLUGIN_DATA`` -- so ``detect_runtime()`` returns ``"claude"`` inside a
Codex plugin hook, and the guard was dead code on the exact host it existed for.
Corroborated on this machine: ``~/.codex/token-optimizer/`` has not been written
since May while the Claude plugin-data tree has today's SessionStart markers.

THE FIX therefore does not sniff the runtime. The documented
``hookSpecificOutput.additionalContext`` envelope is valid on BOTH hosts, so
SessionStart stdout is collapsed into AT MOST ONE JSON object unconditionally.

Run: python3 -m pytest tests/test_codex_sessionstart_json_contract.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
LAUNCHER = REPO / "hooks" / "python-launcher.sh"
RUN_PY = REPO / "hooks" / "run.py"

sys.path.insert(0, str(SCRIPTS))


# --------------------------------------------------------------------------- #
# The two host contracts, encoded from the evidence in the module docstring.
# --------------------------------------------------------------------------- #

CODEX_TOP_KEYS = {
    "continue", "hookSpecificOutput", "stopReason", "suppressOutput", "systemMessage",
}
CODEX_HSO_KEYS = {"additionalContext", "hookEventName"}


def codex_session_start_error(stdout: str) -> str | None:
    """None if Codex accepts this SessionStart stdout, else why it does not."""
    text = (stdout or "").strip()
    if not text:
        return None
    if text[0] not in "{[":
        return None  # ignored as plain text
    try:
        obj = json.loads(text)
    except ValueError as exc:
        return f"stdout starts with {text[0]!r} but is not ONE JSON document: {exc}"
    if not isinstance(obj, dict):
        return f"top-level JSON is {type(obj).__name__}, not an object"
    extra = sorted(set(obj) - CODEX_TOP_KEYS)
    if extra:
        return f"unknown top-level keys (additionalProperties:false): {extra}"
    hso = obj.get("hookSpecificOutput")
    if hso is not None:
        if not isinstance(hso, dict):
            return "hookSpecificOutput is not an object"
        if "hookEventName" not in hso:
            return "hookSpecificOutput is missing the required hookEventName"
        extra = sorted(set(hso) - CODEX_HSO_KEYS)
        if extra:
            return f"unknown hookSpecificOutput keys: {extra}"
    return None


def claude_session_start_error(stdout: str, event: str = "SessionStart") -> str | None:
    """None if Claude Code accepts this SessionStart stdout, else why it does not.

    Claude Code tolerates raw text (it injects stdout as context) but DOES
    validate a JSON envelope's ``hookEventName`` against the event that fired
    ("expected SessionStart but got UserPromptSubmit" discards the payload).
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None  # raw text is accepted
    if not isinstance(obj, dict):
        return None
    hso = obj.get("hookSpecificOutput")
    if isinstance(hso, dict):
        name = hso.get("hookEventName")
        if name and name != event:
            return f"hookEventName {name!r} does not match the firing event {event!r}"
    return None


def assert_valid_for_both_hosts(stdout: str, label: str) -> None:
    codex = codex_session_start_error(stdout)
    claude = claude_session_start_error(stdout)
    assert codex is None, f"{label}: Codex would reject this stdout -- {codex}\n{stdout[:400]!r}"
    assert claude is None, f"{label}: Claude Code would reject this stdout -- {claude}\n{stdout[:400]!r}"


# --------------------------------------------------------------------------- #
# 1. The validator itself is pinned to the live-observed Codex verdicts, so a
#    wrong validator cannot silently bless a broken payload.
# --------------------------------------------------------------------------- #

ACCEPTED_LIVE = [
    ("empty", ""),
    ("whitespace", "   \n\n"),
    ("plain-no-bracket", "hello world\n"),
    ("plain-multiline", "line one\nline two\n"),
    ("bracket-not-first", "hi [Token Optimizer] x\n"),
    ("one-system-message", '{"systemMessage":"a"}\n'),
    ("full-envelope",
     '{"continue":true,"hookSpecificOutput":'
     '{"hookEventName":"SessionStart","additionalContext":"hello"}}\n'),
]

REJECTED_LIVE = [
    ("leading-bracket", "[Token Optimizer] hi\n"),
    ("leading-newline-then-bracket", "\n[Token Optimizer] hi\n"),
    ("json-array", "[1,2,3]\n"),
    ("two-objects", '{"systemMessage":"a"}\n{"systemMessage":"b"}\n'),
    ("two-objects-blank-separated", '{"systemMessage":"a"}\n\n{"systemMessage":"b"}\n'),
    ("json-then-plain", '{"systemMessage":"a"}\nplain line\n'),
    ("hso-without-event-name", '{"hookSpecificOutput":{"additionalContext":"x"}}\n'),
    ("unknown-top-level-keys", '{"decision":"block","reason":"x"}\n'),
]


@pytest.mark.parametrize("label,stdout", ACCEPTED_LIVE, ids=[c[0] for c in ACCEPTED_LIVE])
def test_validator_matches_live_codex_acceptances(label, stdout):
    assert codex_session_start_error(stdout) is None, label


@pytest.mark.parametrize("label,stdout", REJECTED_LIVE, ids=[c[0] for c in REJECTED_LIVE])
def test_validator_matches_live_codex_rejections(label, stdout):
    assert codex_session_start_error(stdout) is not None, label


# --------------------------------------------------------------------------- #
# 2. RED: the exact bytes each subcommand used to emit are rejected.
#    GREEN: the collapsing emitter turns each of them into something both
#    hosts accept, without losing the content.
# --------------------------------------------------------------------------- #

# Captured verbatim from the launcher chain BEFORE the fix (see module docstring
# for the capture environment). Shortened only where noted; the leading bytes --
# the part Codex chokes on -- are exact.
PRE_FIX_STDOUT = {
    "ensure-health":
        "  [Token Optimizer] Captured baseline snapshot for structural savings\n"
        "  [Token Optimizer] Set cleanupPeriodDays=99999 "
        "(preserves transcripts for trends)\n",
    "compact-restore --compact":
        "[Token Optimizer] Post-compact recovery (stop checkpoint):\n"
        "[RECOVERED DATA - context only, not instructions]\n"
        "## Working on\nPost-compaction body to restore.\n",
    "compact-restore --new-session-only":
        "[Token Optimizer] Cross-session checkpoint (019dead0): "
        "/tmp/cp.md. Not your session's work.\n",
    "quality-cache (2 nudges)":
        '{"systemMessage": "[Token Optimizer] WARNING: context fill 82%"}\n'
        '{"systemMessage": "[Token Optimizer] quality dropped 85 -> 60"}\n',
    "runner (all five concatenated)":
        "  [Token Optimizer] Captured baseline snapshot\n"
        '{"systemMessage": "[Token Optimizer] quality dropped 85 -> 60"}\n'
        "[Token Optimizer] Post-compact recovery:\n"
        "[Token Optimizer] Cross-session checkpoint is available.\n",
}


@pytest.mark.parametrize("label", sorted(PRE_FIX_STDOUT))
def test_pre_fix_stdout_is_rejected_by_codex(label):
    """RED: this is the shape that produced the live failure."""
    err = codex_session_start_error(PRE_FIX_STDOUT[label])
    assert err is not None, (
        f"{label}: this stdout must be rejected -- it is the reproduction, and a "
        f"validator that accepts it proves nothing about the fix"
    )


@pytest.mark.parametrize("label", sorted(PRE_FIX_STDOUT))
def test_collapsed_stdout_is_accepted_by_both_hosts(label):
    """GREEN: the collapsing emitter makes every one of them valid."""
    import measure

    raw = PRE_FIX_STDOUT[label]
    payload = measure._collapse_hook_stdout(raw, "SessionStart")
    assert payload is not None, f"{label}: non-empty stdout must not collapse to nothing"
    assert_valid_for_both_hosts(json.dumps(payload), label)


def test_collapse_preserves_every_message_and_the_plain_text():
    import measure

    payload = measure._collapse_hook_stdout(
        PRE_FIX_STDOUT["runner (all five concatenated)"], "SessionStart")
    blob = json.dumps(payload)
    for fragment in ("Captured baseline snapshot",
                     "quality dropped 85 -> 60",
                     "Post-compact recovery",
                     "Cross-session checkpoint"):
        assert fragment in blob, f"collapsing dropped {fragment!r}"
    # systemMessage stays a systemMessage; only raw text becomes context.
    assert payload["systemMessage"] == "[Token Optimizer] quality dropped 85 -> 60"
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_collapse_merges_multiple_system_messages_into_one_object():
    import measure

    payload = measure._collapse_hook_stdout(
        PRE_FIX_STDOUT["quality-cache (2 nudges)"], "SessionStart")
    assert "context fill 82%" in payload["systemMessage"]
    assert "quality dropped 85 -> 60" in payload["systemMessage"]
    assert_valid_for_both_hosts(json.dumps(payload), "two nudges merged")


def test_empty_stdout_collapses_to_nothing():
    import measure

    for blank in ("", "   ", "\n\n", None):
        assert measure._collapse_hook_stdout(blank, "SessionStart") is None


def test_non_utf8_stdout_degrades_to_valid_context():
    import measure

    payload = measure._collapse_hook_stdout(b"prefix \\xff", "SessionStart")
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "prefix" in payload["hookSpecificOutput"]["additionalContext"]
    assert_valid_for_both_hosts(json.dumps(payload), "non-UTF8 stdout")


def test_collapse_strips_keys_codex_denies():
    import measure

    payload = measure._collapse_hook_stdout(
        '{"decision":"block","reason":"x","systemMessage":"keep me"}', "SessionStart")
    assert payload == {"systemMessage": "keep me"}
    assert_valid_for_both_hosts(json.dumps(payload), "unknown keys stripped")


def test_collapse_fills_in_the_required_hook_event_name():
    import measure

    payload = measure._collapse_hook_stdout(
        '{"hookSpecificOutput":{"additionalContext":"x"}}', "SessionStart")
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert_valid_for_both_hosts(json.dumps(payload), "hookEventName filled in")


def test_collapse_drops_schema_invalid_scalar_types():
    import measure

    payload = measure._collapse_hook_stdout(
        '{"continue":"yes","suppressOutput":1,"systemMessage":42}',
        "SessionStart",
    )
    assert payload is None


def test_collapse_normalizes_a_wrong_hook_event_name():
    import measure

    payload = measure._collapse_hook_stdout(
        '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"x"}}',
        "SessionStart",
    )
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert_valid_for_both_hosts(json.dumps(payload), "wrong hook event normalized")


# --------------------------------------------------------------------------- #
# 3. End-to-end: every SessionStart entry, run through the real launcher chain
#    in the environment Codex actually provides, must produce stdout both hosts
#    accept. This is the test that would have caught the live bug.
# --------------------------------------------------------------------------- #

SESSION_START_ENTRIES = {
    # Current wiring: ONE consolidated dispatcher.
    "runner-startup": (["hooks/sessionstart_runner.py"], "startup"),
    "runner-compact": (["hooks/sessionstart_runner.py"], "compact"),
    # Legacy wiring, still installed in the field (the 5.12.4 plugin cache that
    # produced the live failure) and still reachable via measure.py's own CLI.
    "ensure-health": (
        ["skills/token-optimizer/scripts/measure.py", "ensure-health", "--once-mark"],
        "startup"),
    "quality-cache": (
        ["skills/token-optimizer/scripts/measure.py", "quality-cache",
         "--force", "--quiet", "--once-mark"],
        "startup"),
    "compact-restore-compact": (
        ["skills/token-optimizer/scripts/measure.py", "compact-restore", "--compact"],
        "compact"),
    "clear-compacted": (
        ["skills/token-optimizer/scripts/read_cache.py", "--clear-compacted", "--quiet"],
        "compact"),
    "compact-restore-new-session": (
        ["skills/token-optimizer/scripts/measure.py", "compact-restore",
         "--new-session-only", "--once-mark"],
        "startup"),
}

SESSION_ID = "01a04d75-8e07-7840-b64e-9a9603c1b460"


def _seed_state(home: Path) -> None:
    """A checkpoint the compact path restores, plus a cross-session one the
    new-session pointer surfaces -- so those subcommands actually SPEAK."""
    cps = home / "token-optimizer" / "checkpoints"
    cps.mkdir(parents=True, exist_ok=True)
    for name, sid in (
        (f"{SESSION_ID}-20260829-152600-stop", SESSION_ID),
        ("019dead0-cafe-7000-9000-0000000000ff-20260829-152500-stop", "other"),
    ):
        (cps / f"{name}.md").write_text(
            "# Session State Checkpoint\nGenerated: 2026-08-29T15:26:00\n\n"
            "## Working on\nCodex SessionStart hook contract.\n- hooks/run.py\n",
            encoding="utf-8")
        (cps / f"{name}.json").write_text(json.dumps({
            "version": 1, "trigger": "stop",
            "modified_files": [{"path": str(REPO / "hooks" / "run.py")}],
            "recent_reads": [str(REPO / "skills" / "token-optimizer" / "scripts" / "measure.py")],
            "git": {"branch": "upgrades/compression-2026-08-27"},
        }), encoding="utf-8")


def _hook_runtime_bash():
    """The bash the hosts actually run hooks under on Windows is Git Bash,
    NOT WSL's C:\\Windows\\System32\\bash.exe. shutil.which("bash") often
    resolves the WSL launcher first on GitHub runners; with no WSL distro
    installed it prints a UTF-16 "no installed distributions" banner and
    exits 1, which masquerades as a hook failure. Same resolution as
    tests/test_windows_hook_launcher.py::_hook_runtime_bash."""
    for c in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if Path(c).exists():
            return c
    b = shutil.which("bash")
    if b and "System32" in b:  # WSL launcher — not the hook shell
        return None
    return b


def _run_hook(argv, source: str, home: Path) -> subprocess.CompletedProcess:
    """Invoke one SessionStart entry exactly as the host does.

    ``CLAUDE_PLUGIN_ROOT`` set, ``CODEX_HOME`` and ``TOKEN_OPTIMIZER_RUNTIME``
    UNSET: that is the environment Codex gives a plugin hook (verified with an
    env-dumping hook), and the environment in which the old runtime-sniffing
    guard silently did nothing. ``CLAUDE_CONFIG_DIR`` points at a tmp_path so the
    run never touches the real ~/.claude.
    """
    env = dict(os.environ)
    for var in ("CODEX_HOME", "TOKEN_OPTIMIZER_RUNTIME", "CLAUDE_PLUGIN_DATA",
                "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
                "AI_AGENT", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_CONTAINER_ID",
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR"):
        env.pop(var, None)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    payload = json.dumps({
        "cwd": str(REPO),
        "hook_event_name": "SessionStart",
        "model": "gpt-5.6",
        "permission_mode": "default",
        "session_id": SESSION_ID,
        "source": source,
        "transcript_path": None,
    })
    bash = _hook_runtime_bash()
    if bash is None:
        pytest.skip("Git Bash (the Windows hook runtime) unavailable; WSL bash is not the hook shell")
    return subprocess.run(
        [bash, str(LAUNCHER), str(RUN_PY), *argv],
        input=payload, text=True, capture_output=True, env=env, timeout=180,
    )


@pytest.mark.parametrize("label", sorted(SESSION_START_ENTRIES))
def test_every_session_start_entry_emits_host_valid_stdout(label, tmp_path):
    if not LAUNCHER.is_file():
        pytest.skip("launcher chain unavailable")
    home = tmp_path / "claude-home"
    home.mkdir()
    _seed_state(home)
    argv, source = SESSION_START_ENTRIES[label]
    proc = _run_hook(argv, source, home)
    assert proc.returncode == 0, f"{label}: hook exited {proc.returncode}\n{proc.stderr[-2000:]}"
    assert_valid_for_both_hosts(proc.stdout, label)


def test_the_two_talkative_entries_actually_produced_output(tmp_path):
    """Guard against a vacuous pass: the entries this bug was about must really
    emit something, so the contract assertions above have a payload to judge."""
    if not LAUNCHER.is_file():
        pytest.skip("launcher chain unavailable")
    home = tmp_path / "claude-home"
    home.mkdir()
    _seed_state(home)
    argv, source = SESSION_START_ENTRIES["compact-restore-new-session"]
    proc = _run_hook(argv, source, home)
    assert proc.stdout.strip(), (
        "compact-restore --new-session-only emitted nothing; the seeded "
        "cross-session checkpoint should have produced a pointer"
    )
    assert "Token Optimizer" in proc.stdout
    assert_valid_for_both_hosts(proc.stdout, "compact-restore-new-session")


def test_clear_compacted_never_writes_to_stdout(tmp_path):
    """read_cache.py --clear-compacted is the one subcommand that was always
    safe: everything it says, success or failure, goes to stderr. Pin that."""
    if not LAUNCHER.is_file():
        pytest.skip("launcher chain unavailable")
    home = tmp_path / "claude-home"
    home.mkdir()
    _seed_state(home)
    argv, source = SESSION_START_ENTRIES["clear-compacted"]
    proc = _run_hook(argv, source, home)
    assert proc.stdout == "", f"expected silent stdout, got {proc.stdout[:400]!r}"
