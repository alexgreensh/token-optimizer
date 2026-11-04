#!/usr/bin/env python3
"""Issue #303: prompt-continuity re-injected the same Cold-resume lean block on
every subagent <task-notification>.

Two guards, both inside ``_continuity_prompt_hint`` so the hook entry, the
consolidated UserPromptSubmit runner and the Hermes bridge all get them:
  1. harness-generated prompts (``_HARNESS_TASK_PREFIXES``) never resume anything
  2. an identical resume block is injected once per session; compaction / clear
     re-arms it; a different block still injects; no session id fails open.

The hook tests run the real ``measure.py prompt-continuity`` subprocess against
an isolated state dir with an on-disk checkpoint.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"

PRIOR_SID = "11111111-2222-3333-4444-555555555555"
CUR_SID = "99999999-8888-7777-6666-555555555555"
CUR_SID_2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _env(tmp):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("TOKEN_OPTIMIZER_") and k not in (
            "TOKEN_OPTIMIZER_TREE_GUARD",
        ):
            del env[k]
    env.update({
        "HOME": str(tmp),
        "USERPROFILE": str(tmp),
        "CLAUDE_CONFIG_DIR": str(tmp / ".claude"),
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp / "snap"),
        "XDG_DATA_HOME": str(tmp / "xdg"),
        "TOKEN_OPTIMIZER_RUNTIME": "claude",
    })
    return env


def _checkpoint_dir(env):
    code = (
        "import sys; sys.path.insert(0, %r); import measure; "
        "print(measure.CHECKPOINT_DIR)" % str(SCRIPTS)
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                         text=True, timeout=60, check=True)
    return Path(out.stdout.strip().splitlines()[-1])


def _write_checkpoint(cp_dir, sid, cwd, task="refactor the billing module"):
    cp_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{sid}-{ts}-auto"
    sidecar = {
        "session_id": sid,
        "active_task": task,
        "decisions": ["Keep the billing retry loop idempotent"],
        "modified_files": [{"path": f"{cwd}/src/billing.py"}],
        "recent_reads": [f"{cwd}/README.md"],
        "git": {"branch": "main", "sha": "abc123"},
    }
    (cp_dir / f"{base}.md").write_text("# checkpoint\n", encoding="utf-8")
    (cp_dir / f"{base}.json").write_text(json.dumps(sidecar), encoding="utf-8")


def _hook(env, prompt, sid, cwd):
    payload = json.dumps({"prompt": prompt, "session_id": sid, "cwd": str(cwd)})
    r = subprocess.run([sys.executable, str(MEASURE), "prompt-continuity"], env=env,
                       input=payload, capture_output=True, text=True, timeout=60)
    out = r.stdout.strip()
    if not out:
        return ""
    last = out.splitlines()[-1]
    data = json.loads(last)
    return (data.get("hookSpecificOutput") or {}).get("additionalContext", "")


@pytest.fixture
def flow(tmp_path):
    env = _env(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    cp_dir = _checkpoint_dir(env)
    _write_checkpoint(cp_dir, PRIOR_SID, proj)
    return env, proj, cp_dir


NOTIFICATION = (
    "<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>\n"
    "<summary>Agent finished. Please continue last session %s</summary>\n"
    "</task-notification>" % PRIOR_SID
)


def test_plain_resume_injects_lean_block(flow):
    env, proj, _ = flow
    out = _hook(env, "continue last session", CUR_SID, proj)
    assert "Cold-resume lean" in out


def test_task_notification_never_injects(flow):
    env, proj, _ = flow
    for _ in range(3):
        assert _hook(env, NOTIFICATION, CUR_SID, proj) == ""


def test_other_harness_prefixes_never_inject(flow):
    env, proj, _ = flow
    for prefix in ("<local-command-caveat>", "[SYSTEM NOTIFICATION",
                   "[Request interrupted by user", "<local-command-stdout>"):
        assert _hook(env, prefix + " continue last session", CUR_SID, proj) == ""


def test_notification_does_not_consume_dedup_for_real_resume(flow):
    env, proj, _ = flow
    assert _hook(env, NOTIFICATION, CUR_SID, proj) == ""
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)


def test_identical_block_injected_once_per_session(flow):
    env, proj, _ = flow
    first = _hook(env, "continue last session", CUR_SID, proj)
    assert "Cold-resume lean" in first
    assert _hook(env, "continue last session", CUR_SID, proj) == ""
    assert _hook(env, "where we left off", CUR_SID, proj) == ""


def test_other_session_still_injects(flow):
    env, proj, _ = flow
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID_2, proj)


def test_different_block_still_injects(flow):
    env, proj, cp_dir = flow
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)
    other = "77777777-6666-5555-4444-333333333333"
    _write_checkpoint(cp_dir, other, proj, task="migrate the auth tables")
    out = _hook(env, "resume session %s" % other, CUR_SID, proj)
    assert "Cold-resume lean" in out
    assert "auth tables" in out


def test_no_session_id_fails_open(flow):
    env, proj, _ = flow
    a = _hook(env, "continue last session", "", proj)
    b = _hook(env, "continue last session", "", proj)
    assert "Cold-resume lean" in a and "Cold-resume lean" in b


def test_compaction_rearms_resume_block(flow):
    env, proj, _ = flow
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)
    assert _hook(env, "continue last session", CUR_SID, proj) == ""
    payload = json.dumps({"session_id": CUR_SID, "source": "compact", "cwd": str(proj)})
    subprocess.run([sys.executable, str(MEASURE), "compact-restore", "--compact"], env=env,
                   input=payload, capture_output=True, text=True, timeout=60)
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)


RUNNER = REPO / "hooks" / "sessionstart_runner.py"


@pytest.mark.parametrize("source", ["compact", "clear"])
def test_session_start_runner_rearms_on_context_reset(flow, source):
    """The consolidated SessionStart runner is what Claude Code really fires."""
    env, proj, _ = flow
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)
    assert _hook(env, "continue last session", CUR_SID, proj) == ""
    payload = json.dumps({"session_id": CUR_SID, "source": source, "cwd": str(proj),
                          "hook_event_name": "SessionStart"})
    subprocess.run([sys.executable, str(RUNNER)], env=env, input=payload,
                   capture_output=True, text=True, timeout=90)
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)


@pytest.mark.parametrize("source", ["startup", "resume"])
def test_session_start_runner_keeps_dedup_on_other_sources(flow, source):
    env, proj, _ = flow
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)
    payload = json.dumps({"session_id": CUR_SID, "source": source, "cwd": str(proj),
                          "hook_event_name": "SessionStart"})
    subprocess.run([sys.executable, str(RUNNER)], env=env, input=payload,
                   capture_output=True, text=True, timeout=90)
    assert _hook(env, "continue last session", CUR_SID, proj) == ""


def test_non_compact_session_start_keeps_dedup(flow):
    env, proj, _ = flow
    assert "Cold-resume lean" in _hook(env, "continue last session", CUR_SID, proj)
    payload = json.dumps({"session_id": CUR_SID, "source": "startup", "cwd": str(proj)})
    subprocess.run([sys.executable, str(MEASURE), "compact-restore", "--new-session-only",
                    "--once-mark"], env=env, input=payload, capture_output=True,
                   text=True, timeout=60)
    assert _hook(env, "continue last session", CUR_SID, proj) == ""


def test_clear_markers_only_touches_own_session(flow):
    env, proj, _ = flow
    code = (
        "import sys; sys.path.insert(0, %r); import measure as m;"
        "m._ran_once_this_session('resume-0123456789abcdef', 'zz-abcdef-1');"
        "m._ran_once_this_session('resume-0123456789abcdef', 'abcdef-1');"
        "m._ran_once_this_session('ensure-health', 'abcdef-1');"
        "m._clear_resume_dedup_markers('abcdef-1');"
        "print(m._ran_once_this_session('resume-0123456789abcdef', 'zz-abcdef-1'),"
        " m._ran_once_this_session('resume-0123456789abcdef', 'abcdef-1'),"
        " m._ran_once_this_session('ensure-health', 'abcdef-1'))" % str(SCRIPTS)
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.stdout.strip().splitlines()[-1] == "True False True", r.stderr


def test_runner_path_dedups(flow, monkeypatch):
    """The consolidated UserPromptSubmit runner calls the same core function."""
    env, proj, _ = flow
    code = (
        "import sys, json; sys.path.insert(0, %r); sys.path.insert(0, %r);"
        "import measure;"
        "a = measure._continuity_prompt_hint(prompt_text='continue last session', session_id=%r, cwd=%r);"
        "b = measure._continuity_prompt_hint(prompt_text='continue last session', session_id=%r, cwd=%r);"
        "c = measure._continuity_prompt_hint(prompt_text=%r, session_id=%r, cwd=%r);"
        "print(json.dumps([bool(a), bool(b), bool(c)]))"
        % (str(SCRIPTS), str(REPO / "hooks"), CUR_SID, str(proj), CUR_SID, str(proj),
           NOTIFICATION, CUR_SID_2, str(proj))
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                       text=True, timeout=60)
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [True, False, False], r.stderr


UPS_RUNNER = REPO / "hooks" / "userpromptsubmit_runner.py"


def _runner(env, prompt, sid, cwd):
    payload = json.dumps({"prompt": prompt, "session_id": sid, "cwd": str(cwd),
                          "hook_event_name": "UserPromptSubmit"})
    r = subprocess.run([sys.executable, str(UPS_RUNNER)], env=env, input=payload,
                       capture_output=True, text=True, timeout=90)
    return r.stdout


def test_userpromptsubmit_runner_real_flow(flow):
    """The consolidated runner Claude Code actually fires: notification never
    injects; a real resume injects once; the repeat is silent."""
    env, proj, _ = flow
    for _ in range(2):
        assert "Cold-resume lean" not in _runner(env, NOTIFICATION, CUR_SID, proj)
    assert "Cold-resume lean" in _runner(env, "continue last session", CUR_SID, proj)
    assert "Cold-resume lean" not in _runner(env, "continue last session", CUR_SID, proj)


def test_concurrent_identical_prompts_inject_exactly_once(flow):
    """Several subagents finishing at once: O_EXCL claim means one winner."""
    env, proj, _ = flow
    payload = json.dumps({"prompt": "continue last session", "session_id": CUR_SID,
                          "cwd": str(proj)})
    procs = [subprocess.Popen([sys.executable, str(MEASURE), "prompt-continuity"], env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for _ in range(6)]
    outs = [p.communicate(payload, timeout=90)[0] for p in procs]
    assert sum("Cold-resume lean" in o for o in outs) == 1


def test_unwritable_marker_dir_fails_open(flow):
    """If the marker cannot be written the block is injected as before."""
    env, proj, cp_dir = flow
    code = (
        "import sys; sys.path.insert(0, %r); import measure as m;"
        "from pathlib import Path;"
        "m.QUALITY_CACHE_DIR = Path('/proc/nonexistent/x');"
        "a = m._continuity_prompt_hint(prompt_text='continue last session', session_id=%r, cwd=%r);"
        "b = m._continuity_prompt_hint(prompt_text='continue last session', session_id=%r, cwd=%r);"
        "print(bool(a), bool(b))" % (str(SCRIPTS), CUR_SID, str(proj), CUR_SID, str(proj))
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.stdout.strip().splitlines()[-1] == "True True", r.stderr


@pytest.mark.parametrize("sid", ["", "abc", None, 12345])
def test_unusable_session_ids_fail_open(flow, sid):
    env, proj, _ = flow
    code = (
        "import sys; sys.path.insert(0, %r); import measure as m;"
        "a = m._continuity_prompt_hint(prompt_text='continue last session', session_id=%r, cwd=%r);"
        "b = m._continuity_prompt_hint(prompt_text='continue last session', session_id=%r, cwd=%r);"
        "print(bool(a), bool(b))" % (str(SCRIPTS), sid, str(proj), sid, str(proj))
    )
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                       text=True, timeout=60)
    last = r.stdout.strip().splitlines()[-1]
    # 12345 sanitizes to the unusable sentinel ("unknown"), so it fails open too.
    assert last == "True True", (last, r.stderr)


def test_prefix_gate_is_start_anchored_and_whitespace_tolerant(flow):
    env, proj, _ = flow
    assert _hook(env, "\n  " + NOTIFICATION, CUR_SID, proj) == ""
    # A user prompt that merely mentions the tag mid-text is still the user's.
    assert "Cold-resume lean" in _hook(
        env, "continue last session (ignore the <task-notification> noise)", CUR_SID, proj)
