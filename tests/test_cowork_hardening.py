#!/usr/bin/env python3
"""Cowork hardening fixes (branch cowork-hardening).

Covers the six doc-grounded Cowork fixes. Each fix is gated on ``is_cowork()``
and must never regress native desktop Claude Code, so every test asserts the
desktop path is unchanged alongside the Cowork behavior.

  injection shape: UserPromptSubmit-path context wrapped in the documented
         hookSpecificOutput.additionalContext envelope in Cowork.
  unified per-session state dir: quality-cache + checkpoints + run-once
         markers resolve under the SAME base as SNAPSHOT_DIR in Cowork.
  is_cowork() keys on the Cowork-only markers (CLAUDE_CODE_CONTAINER_ID, a
         /plugins/synced/ plugin path). CLAUDE_CODE_REMOTE alone is the Claude
         Code cloud signal (is_claude_code_cloud), not Cowork.
  measure.py self-location resolver recognizes the /plugins/synced/ tree.
  report prints an HONEST platform-overhead caveat in Cowork (no fabricated
         calibration number).

Run: python3 -m pytest tests/test_cowork_hardening.py -q
"""

from __future__ import annotations
from helpers.measure_split import measure_source as _split_measure_source

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402
import runtime_env  # noqa: E402


def _cache_clear(fn):
    clear = getattr(fn, "cache_clear", None)
    if callable(clear):
        clear()


_COWORK_ENV = (
    "CLAUDE_CODE_REMOTE",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
    "TOKEN_OPTIMIZER_REMOTE_HOST",
    "CLAUDE_CODE_CONTAINER_ID",
    "CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE",
    "TOKEN_OPTIMIZER_CLOUD_KIT_VERSION",
    "AI_AGENT",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
)


def _reset_cowork_env(monkeypatch):
    for var in _COWORK_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TOKEN_OPTIMIZER_NO_PROC_SCAN", "1")
    _cache_clear(runtime_env.is_cowork)
    _cache_clear(runtime_env.is_claude_code_cloud)
    _cache_clear(runtime_env.remote_host)
    _cache_clear(runtime_env.detect_runtime)


# --------------------------------------------------------------------------- #
# CLAUDE_CODE_REMOTE alone means Claude Code cloud, not Cowork
# --------------------------------------------------------------------------- #

def test_remote_env_alone_is_neither_cowork_nor_cloud(monkeypatch):
    """The documented CLAUDE_CODE_REMOTE=true says "a hosted VM", not which one.
    Read as a Cowork marker it labelled every cloud session "Claude Code
    (Cowork)" (2026-10-19); read as cloud it switched a self-hosted runner or a
    desktop with the variable exported to the lean profile (review #4). Alone
    it now resolves to "remote": no Cowork caveats, no cloud lean profile."""
    _reset_cowork_env(monkeypatch)
    assert runtime_env.is_cowork() is False  # nothing set -> not cowork
    assert runtime_env.is_claude_code_cloud() is False

    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert runtime_env.is_cowork() is False
    assert runtime_env.is_claude_code_cloud() is False
    assert runtime_env.remote_host() == "remote"
    assert runtime_env.is_remote_vm() is True
    # Still the claude runtime, never a new runtime name.
    monkeypatch.setenv("CLAUDECODE", "1")
    _cache_clear(runtime_env.detect_runtime)
    assert runtime_env.detect_runtime() == "claude"


@pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on"])
def test_remote_truthy_values_mean_remote(monkeypatch, val):
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", val)
    assert runtime_env.remote_host() == "remote"
    assert runtime_env.is_claude_code_cloud() is False
    assert runtime_env.is_cowork() is False


@pytest.mark.parametrize("val", ["false", "0", "", "no", "off"])
def test_remote_falsy_values_leave_both_off(monkeypatch, val):
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", val)
    assert runtime_env.is_cowork() is False
    assert runtime_env.is_claude_code_cloud() is False


def test_container_id_is_cowork_only_without_cloud_evidence(monkeypatch):
    """Review 2 MED-3: CLAUDE_CODE_CONTAINER_ID is in every Cowork VM, so with
    no cloud evidence it still means Cowork (state base, Cowork envelope,
    once-per-session guard). The Anthropic-hosted cloud VM sets it too, but
    always with the session id and the cloud environment type (live probe
    cse_01Au8fmq3iUyLMULzXTMuy5o), and those win."""
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_CONTAINER_ID", "cowork-abc")
    assert runtime_env.is_cowork() is True
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert runtime_env.is_cowork() is True
    assert runtime_env.is_claude_code_cloud() is False
    for key, value in (("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_01X"),
                       ("CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE", "cloud_default"),
                       ("TOKEN_OPTIMIZER_CLOUD_KIT_VERSION", "5.13.50")):
        monkeypatch.setenv(key, value)
        assert runtime_env.is_claude_code_cloud() is True, key
        assert runtime_env.is_cowork() is False, key
        monkeypatch.delenv(key)


def test_synced_root_is_cowork_only_without_cloud_evidence(monkeypatch):
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/root/.claude/plugins/synced/acct/token-optimizer")
    assert runtime_env.is_cowork() is True
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_01X")
    assert runtime_env.is_cowork() is False
    assert runtime_env.is_claude_code_cloud() is True


def test_is_cowork_synced_plugin_root_still_triggers(monkeypatch):
    # Genuine Cowork signal: org-console account-synced plugins land under
    # /plugins/synced/. This path is Cowork-specific and must still trigger.
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv(
        "CLAUDE_PLUGIN_ROOT",
        "/root/.claude/plugins/synced/acct/token-optimizer",
    )
    _cache_clear(runtime_env.is_cowork)
    assert runtime_env.is_cowork() is True


def test_is_cowork_synced_plugin_data_still_triggers(monkeypatch):
    # CLAUDE_PLUGIN_DATA under /plugins/synced/ is the same genuine signal.
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv(
        "CLAUDE_PLUGIN_DATA",
        "/root/.claude/plugins/synced/acct/token-optimizer/data",
    )
    _cache_clear(runtime_env.is_cowork)
    assert runtime_env.is_cowork() is True


@pytest.mark.parametrize(
    "ai_agent",
    [
        # Native Claude Code CLI exports this exact shape into every hook
        # subprocess (observed on 2.1.258). The "_harness" suffix is NOT a
        # Cowork-only marker.
        "claude-code_2.1.258_harness",
        # Older observed Cowork value that the local CLI now also emits.
        "claude-code_2-1-231_harness",
        # Bare harness without the claude-code prefix must not match either.
        "harness",
        # The local CLI's "_agent" variant was never a match; keep it false.
        "claude-code_2-1-229_agent",
    ],
)
def test_is_cowork_native_ai_agent_harness_is_not_cowork(monkeypatch, ai_agent):
    """Regression: native Claude Code sets AI_AGENT=claude-code_<ver>_harness.

    Before the fix, is_cowork() matched ``claude-code`` + ``harness`` substrings
    in AI_AGENT and returned True on every native hook fire. Native Claude Code
    (local CLI) exports the SAME ``_harness`` suffix as Cowork, so AI_AGENT is
    ambiguous and must NOT contribute a Cowork signal. This test pins the fix:
    AI_AGENT alone never triggers is_cowork(), regardless of its value.
    """
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("AI_AGENT", ai_agent)
    _cache_clear(runtime_env.is_cowork)
    assert runtime_env.is_cowork() is False, (
        f"AI_AGENT={ai_agent!r} must not trigger is_cowork() (native CLI also sets it)"
    )


def test_is_cowork_native_ai_agent_does_not_shadow_real_marker(monkeypatch):
    """AI_AGENT being set must not prevent a genuine Cowork signal.

    The removal of the AI_AGENT check must not break the positive path: when a
    real Cowork marker (a synced plugin root) is present alongside the native
    AI_AGENT harness value, is_cowork() still returns True; with the cloud
    session id it is cloud.
    """
    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("AI_AGENT", "claude-code_2.1.258_harness")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/root/.claude/plugins/synced/acct/token-optimizer")
    assert runtime_env.is_cowork() is True

    _reset_cowork_env(monkeypatch)
    monkeypatch.setenv("AI_AGENT", "claude-code_2.1.258_harness")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_CODE_REMOTE_SESSION_ID", "cse_01X")
    assert runtime_env.is_cowork() is False
    assert runtime_env.is_claude_code_cloud() is True


# --------------------------------------------------------------------------- #
# documented additionalContext envelope emitter
# --------------------------------------------------------------------------- #

def test_emit_additional_context_wraps_raw_text_userpromptsubmit(capsys):
    measure._emit_additional_context("continue where we left off", event="UserPromptSubmit")
    out = capsys.readouterr().out.strip()
    obj = json.loads(out)  # must be exactly one valid JSON object
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert obj["hookSpecificOutput"]["additionalContext"] == "continue where we left off"
    assert obj["continue"] is True


def test_emit_additional_context_default_event_is_sessionstart(capsys):
    measure._emit_additional_context("hello")
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_emit_additional_context_empty_emits_nothing(capsys):
    measure._emit_additional_context("")
    measure._emit_additional_context("   ")
    measure._emit_additional_context(None)
    assert capsys.readouterr().out == ""


def test_emit_additional_context_passthrough_when_already_enveloped(capsys):
    pre = json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": "x"}})
    measure._emit_additional_context(pre, event="SessionStart")
    out = capsys.readouterr().out.strip()
    # Passthrough: the pre-enveloped payload is emitted unchanged (event NOT
    # rewritten to SessionStart), never double-wrapped.
    assert json.loads(out) == json.loads(pre)


def test_codex_wrapper_is_alias_of_shared_emitter(capsys):
    measure._emit_codex_session_start("codex text")
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert obj["hookSpecificOutput"]["additionalContext"] == "codex text"


# --------------------------------------------------------------------------- #
# single per-session state dir in Cowork
# --------------------------------------------------------------------------- #

def test_state_dirs_share_one_base_structurally():
    # In-process invariant (holds in any runtime): quality-cache, checkpoints, and
    # the once-marker all derive from ONE base, so there is never a dual location.
    assert measure.QUALITY_CACHE_DIR == measure._STATE_BASE / "token-optimizer"
    assert measure.CHECKPOINT_DIR == measure._STATE_BASE / "token-optimizer" / "checkpoints"
    mk = measure._once_per_session_marker("ensure-health", "sess-123")
    assert mk is not None
    assert mk.parent == measure.QUALITY_CACHE_DIR


def _run_state_probe(tmp_path, *, cowork):
    """Import measure in a fresh subprocess under a fake Cowork home and return
    the resolved state dirs, so module-level path resolution is exercised for
    real (constants are import-time)."""
    home = tmp_path / "home"
    pdata = home / ".claude" / "plugins" / "data" / "token-optimizer-mkt"
    pdata.mkdir(parents=True)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@mkt": {"version": "5.0.0"}}}),
        encoding="utf-8",
    )
    probe = (
        "import json, measure, runtime_env\n"
        "print(json.dumps({\n"
        "  'cowork': runtime_env.is_cowork(),\n"
        "  'state_base': str(measure._STATE_BASE),\n"
        "  'plugin_data': str(measure._RESOLVED_PLUGIN_DATA),\n"
        "  'snapshot': str(measure.SNAPSHOT_DIR),\n"
        "  'quality': str(measure.QUALITY_CACHE_DIR),\n"
        "  'checkpoint': str(measure.CHECKPOINT_DIR),\n"
        "}))\n"
    )
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(SCRIPTS),
        "CLAUDE_PLUGIN_DATA": str(pdata),
        "CLAUDECODE": "1",
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
    }
    if cowork:
        env["CLAUDE_PLUGIN_ROOT"] = str(home / ".claude" / "plugins" / "synced" / "acct" / "token-optimizer")
    res = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1]), pdata


def test_cowork_unifies_state_under_plugin_data_base(tmp_path):
    data, pdata = _run_state_probe(tmp_path, cowork=True)
    assert data["cowork"] is True
    # All per-session state resolves under the SAME plugin-data base as snapshots
    # (no dual write to ~/.claude/token-optimizer).
    assert data["state_base"] == str(pdata)
    for key in ("snapshot", "quality", "checkpoint"):
        assert data[key].startswith(str(pdata)), (key, data[key])


def test_desktop_state_stays_on_runtime_home(tmp_path):
    # Same plugin-data dir resolvable, but NOT cowork -> state stays under
    # ~/.claude (RUNTIME_DIR), byte-identical to pre-fix behavior.
    data, pdata = _run_state_probe(tmp_path, cowork=False)
    assert data["cowork"] is False
    claude = str(Path(pdata).parents[2])  # <home>/.claude
    assert data["quality"] == str(Path(claude) / "token-optimizer")
    assert data["checkpoint"] == str(Path(claude) / "token-optimizer" / "checkpoints")
    # Snapshots still resolve to the plugin-data dir (unchanged).
    assert data["snapshot"].startswith(str(pdata))


# --------------------------------------------------------------------------- #
# synced-plugin self-location resolver
# --------------------------------------------------------------------------- #

def test_synced_plugin_detected_and_uses_plugin_root_path(monkeypatch):
    synced = "/root/.claude/plugins/synced/acct/token-optimizer/skills/token-optimizer/scripts/measure.py"
    monkeypatch.setattr(measure, "__file__", synced)
    assert measure._is_running_from_synced_plugin() is True
    # Update-safe: the resolver emits the ${CLAUDE_PLUGIN_ROOT} form, not the
    # version/sync-pinned absolute path.
    assert measure._get_measure_py_path() == (
        "${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py"
    )


def test_desktop_path_not_flagged_as_synced():
    # The real repo path has no /plugins/synced/ segment.
    assert measure._is_running_from_synced_plugin() is False


# --------------------------------------------------------------------------- #
# honest platform-overhead caveat in Cowork (no fabricated number)
# --------------------------------------------------------------------------- #

def _minimal_snapshot():
    return {
        "label": "test",
        "timestamp": "2026-08-14T00:00:00",
        "components": {
            "core_system": {"tokens": 12900, "note": "desktop"},
        },
        "totals": {
            "controllable_tokens": 55400,
            "fixed_tokens": 12900,
            "estimated_total": 68300,
        },
    }


def test_report_prints_honest_caveat_in_cowork(monkeypatch, capsys):
    monkeypatch.setattr(measure, "is_cowork", lambda: True)
    measure.print_snapshot_summary(_minimal_snapshot())
    out = capsys.readouterr().out
    assert "NOTE (Cowork)" in out
    assert "not measurable from inside" in out.lower() or "NOT" in out
    assert "controllable slice only" in out
    assert "Core system (desktop est.)" in out
    # It must NOT quietly present the plain full-picture line.
    assert "Context used before typing" not in out
    # And it must NOT fabricate a Cowork-specific overhead number: the only token
    # figures printed are the real measured ones (12,900 / 68,300).
    assert "68,300" in out
    assert "12,900" in out


def test_report_desktop_path_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(measure, "is_cowork", lambda: False)
    measure.print_snapshot_summary(_minimal_snapshot())
    out = capsys.readouterr().out
    assert "Context used before typing" in out
    assert "NOTE (Cowork)" not in out
    assert "Core system (fixed)" in out


def test_compact_restore_envelope_event_derives_from_hook_event_name():
    """PR #142 (@danikdanik): the compact-restore dispatch must choose the emitted
    envelope's hookEventName from the firing hook's stdin `hook_event_name`, NOT
    from is_cowork(). is_cowork() false-positives on the local CLI (hook
    subprocesses inherit the harness AI_AGENT marker), so the old static
    `event="UserPromptSubmit" if _cw else "SessionStart"` made a SessionStart:compact
    hook emit a UserPromptSubmit envelope that Claude Code rejects, discarding the
    post-compaction recovery context. Source-pin the fix so it cannot silently
    revert to the runtime-detection choice.
    """
    src = _split_measure_source(SCRIPTS / "measure.py")
    # The compact-restore dispatch must read the event from stdin.
    assert 'event=str(hook_input.get("hook_event_name") or "SessionStart")' in src, (
        "compact-restore must derive the envelope event from stdin hook_event_name"
    )
    # And must NOT reinstate the is_cowork-based static choice at the emit site.
    assert '_buf.getvalue(), event="UserPromptSubmit" if _cw else "SessionStart"' not in src, (
        "compact-restore reverted to the is_cowork static event choice (PR #142 regression)"
    )
