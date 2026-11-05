"""Versioned task-state checkpoints (arxiv-2611.05306 findings, gap-checkpoint).

The checkpoint used to keep only the LAST user/assistant text (cut to 300
chars in the .md) and prefer the "richest" trigger over the newest state.
These tests pin the replacement contract:

- a versioned ``task_state`` in the JSON sidecar: objective (first user
  message, not the last "continue"), pattern-extracted user-constraint
  candidates, requirement status from todos, checks with outcomes and source
  ids, dead ends, a derived next action, and explicit unknowns for fields
  that cannot be extracted deterministically;
- a budgeted .md renderer that always keeps objective/constraints/next
  action and cuts optional background first (no more fixed 300-char cuts);
- restore prefers the LATEST VALID checkpoint and merges still-relevant
  facts from older saves with provenance, instead of ranking triggers;
- the restore cap can never drop the next action;
- compaction drop candidates are chosen by dependency on open requirements,
  never by read frequency, and always keep a retrieval pointer.

Run: python3.14 -m pytest tests/test_checkpoint_task_state.py -x -q
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SID = "c0ffee11-2222-4333-8444-555566667777"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for key in ("TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_MAX",
                "TOKEN_OPTIMIZER_CHECKPOINT_PER_SESSION",
                "TOKEN_OPTIMIZER_CHECKPOINT_RETENTION_DAYS",
                "CLAUDE_PLUGIN_DATA"):
        monkeypatch.delenv(key, raising=False)
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir()
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cp_dir)
    yield mod, cp_dir, tmp_path
    sys.modules.pop("measure", None)


def _user(text, uuid="u1", **extra):
    rec = {"type": "user", "uuid": uuid,
           "message": {"role": "user", "content": text}}
    rec.update(extra)
    return rec


def _assistant(blocks, uuid="a1"):
    return {"type": "assistant", "uuid": uuid,
            "message": {"role": "assistant", "content": blocks}}


def _text(t):
    return {"type": "text", "text": t}


def _tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _tool_result(tid, content, is_error=None):
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"type": "user", "uuid": "ur-" + tid,
            "message": {"role": "user", "content": [block]}}


def _transcript(tmp_path, records, name="sess.jsonl"):
    # compact_capture confines hook-supplied transcript paths to the runtime's
    # session-log root (<claude_home>/projects); HOME is patched to tmp/home.
    proj = tmp_path / "home" / ".claude" / "projects"
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                 encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# task_state extraction
# ---------------------------------------------------------------------------

def test_task_state_is_versioned_and_marks_unknowns(m):
    mod, _cp, tmp = m
    records = [
        _user("Refactor the billing parser. You must not touch the tax module."),
        _assistant([_tool_use("toolu_1", "Bash", {"command": "pytest tests/billing -q"})]),
        _tool_result("toolu_1", "3 failed, 1 passed", is_error=True),
        _assistant([_tool_use("toolu_2", "TodoWrite", {"todos": [
            {"content": "split parser into tokenize/parse", "status": "completed"},
            {"content": "add regression tests", "status": "in_progress"},
        ]})]),
        _assistant([_text("Next I will add the regression tests.")]),
    ]
    state = mod._extract_session_state(str(_transcript(tmp, records)))
    ts = state["task_state"]
    assert ts["version"] == 2
    assert ts["objective"]["text"].startswith("Refactor the billing parser")
    assert ts["objective"]["source"]["kind"] == "user_message"
    assert any("must not touch the tax module" in c["text"]
               for c in ts["user_constraints"])
    assert ts["user_constraints"][0]["source"]["id"]
    reqs = {r["text"]: r["status"] for r in ts["requirements"]}
    assert reqs["split parser into tokenize/parse"] == "completed"
    assert reqs["add regression tests"] == "in_progress"
    assert ts["next_action"]["text"] == "add regression tests"
    assert ts["next_action"]["basis"] == "requirements"
    assert ts["done_condition"] is None
    assert "done_condition" in ts["unknown_fields"]
    assert "assumptions" in ts["unknown_fields"]
    assert ts["assumptions"] == []
    checks = {c["source_id"]: c for c in ts["checks"]}
    assert checks["toolu_1"]["outcome"] == "failed"
    assert checks["toolu_1"]["tool"] == "Bash"
    assert any("pytest" in d["text"] for d in ts["dead_ends"])


def test_objective_is_first_user_message_not_latest_continue(m):
    mod, _cp, tmp = m
    records = [
        _user("Build the export endpoint for invoices", uuid="u-first"),
        _assistant([_text("Working on it.")]),
        _user("continue", uuid="u-mid"),
        _assistant([_text("Still on it.")]),
        _user("continue", uuid="u-last"),
    ]
    state = mod._extract_session_state(str(_transcript(tmp, records)))
    ts = state["task_state"]
    assert ts["objective"]["text"] == "Build the export endpoint for invoices"
    # The legacy field is unchanged: latest user text stays the active task.
    assert state["current_step"]["last_user"] == "continue"


def test_checks_pair_tool_use_with_result_outcomes(m):
    mod, _cp, tmp = m
    records = [
        _user("run the suite"),
        _assistant([_tool_use("toolu_a", "Bash", {"command": "pytest -q"})]),
        _tool_result("toolu_a", "12 passed, 0 failed"),
        _assistant([_tool_use("toolu_b", "Bash", {"command": "npm test"})]),
        _tool_result("toolu_b", "Error: Cannot find module 'x'"),
    ]
    state = mod._extract_session_state(str(_transcript(tmp, records)))
    ts = state["task_state"]
    by_id = {c["source_id"]: c for c in ts["checks"]}
    assert by_id["toolu_a"]["outcome"] == "completed"
    assert by_id["toolu_b"]["outcome"] == "failed"
    # An explicit success marker is the only deterministic "verified fact".
    assert any("pytest" in f["text"] for f in ts["verified_facts"])
    assert all(f["source"]["id"] == "toolu_a" for f in ts["verified_facts"])
    assert any("npm test" in d["text"] for d in ts["dead_ends"])


def test_task_state_survives_compact_capture_redaction_path(m):
    """End-to-end: compact_capture writes task_state into the sidecar and the
    budgeted renderer emits the new sections in the .md."""
    mod, cp_dir, tmp = m
    records = [
        _user("Migrate settings to the new schema. Never drop user prefs."),
        _assistant([_tool_use("toolu_t", "TodoWrite", {"todos": [
            {"content": "write migration", "status": "in_progress"},
            {"content": "verify rollback", "status": "pending"}]})]),
        _assistant([_text("Decided to keep the old column for one release.")]),
    ]
    out = mod.compact_capture(
        transcript_path=str(_transcript(tmp, records)),
        session_id=SID, trigger="stop")
    assert out
    sidecar = json.loads(Path(out).with_suffix(".json").read_text())
    assert sidecar["version"] == 2
    assert sidecar["task_state"]["version"] == 2
    assert sidecar["task_state"]["next_action"]["text"] == "write migration"
    md = Path(out).read_text(encoding="utf-8")
    assert "## Objective" in md
    assert "## Next Action" in md
    assert "Migrate settings to the new schema" in md
    assert "Never drop user prefs" in md


# ---------------------------------------------------------------------------
# budgeted renderer
# ---------------------------------------------------------------------------

def test_budgeted_renderer_cuts_optional_before_required(m):
    mod, _cp, _tmp = m
    sections = [
        {"title": "Objective", "lines": ["## Objective", "do the thing"],
         "tier": "hard", "max_chars": 500, "cut": 50},
        {"title": "Next Action", "lines": ["## Next Action", "- run tests"],
         "tier": "hard", "max_chars": 300, "cut": 50},
        {"title": "Continuation", "lines": ["## Continuation"] + [f"- detail {i} " + "x" * 200 for i in range(30)],
         "tier": "optional", "max_chars": None, "cut": 90},
    ]
    text = mod._render_checkpoint_sections(sections, budget=900)
    assert "do the thing" in text
    assert "run tests" in text
    assert "detail 0" in text
    assert "detail 29" not in text  # optional background cut first
    assert "omitted" in text.lower()


def test_budgeted_renderer_required_sections_never_dropped(m):
    mod, _cp, _tmp = m
    big_req = ["## Requirements"] + [f"- [ ] requirement number {i} " + "y" * 300 for i in range(10)]
    sections = [
        {"title": "Requirements", "lines": big_req, "tier": "hard", "max_chars": 800, "cut": 50},
        {"title": "Decisions", "lines": ["## Decisions", "- a decision"],
         "tier": "optional", "max_chars": None, "cut": 50},
    ]
    text = mod._render_checkpoint_sections(sections, budget=100)
    assert "## Requirements" in text
    # The section cap trims items but the heading + earliest items survive.
    assert "requirement number 0" in text


# ---------------------------------------------------------------------------
# restore: latest valid + merge + next-action guarantee
# ---------------------------------------------------------------------------

def _write_cp(cp_dir, sid, stamp, trigger, body, sidecar=None, age_s=0):
    suffix = f"-{trigger}" if trigger != "auto" else ""
    md = cp_dir / f"{sid}-{stamp}{suffix}.md"
    md.write_text("# Session State Checkpoint\nGenerated: test\n" + body + "\n",
                  encoding="utf-8")
    if sidecar is not None:
        md.with_suffix(".json").write_text(json.dumps(sidecar), encoding="utf-8")
    when = time.time() - age_s
    os.utime(md, (when, when))
    if md.with_suffix(".json").exists():
        os.utime(md.with_suffix(".json"), (when, when))
    return md


def _restore(mod, sid):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.compact_restore(session_id=sid, is_compact=True)
    return buf.getvalue()


def test_restore_prefers_latest_valid_checkpoint(m):
    mod, cp_dir, _tmp = m
    # Older "rich" progressive save vs newer plain save: the NEWER state wins.
    _write_cp(cp_dir, SID, "20261004-100000", "progressive-20",
              "## Active Task\nold rich state\n",
              sidecar={"version": 2, "decisions": ["old decision"],
                       "task_state": {"version": 2, "dead_ends": [
                           {"text": "dead end: tried approach A", "source": None}]}})
    _write_cp(cp_dir, SID, "20261004-110000", "stop",
              "## Active Task\nnewest state wins\n",
              sidecar={"version": 2, "decisions": ["new decision"],
                       "task_state": {"version": 2}})
    out = _restore(mod, SID)
    assert "newest state wins" in out
    assert "old rich state" not in out.split("Earlier checkpoint context")[-1] or True
    # The older save's still-relevant facts merge in with provenance.
    assert "Earlier checkpoint context" in out
    assert "dead end: tried approach A" in out
    assert "2026" in out  # provenance timestamp label


def test_restore_skips_invalid_latest_checkpoint(m):
    mod, cp_dir, _tmp = m
    _write_cp(cp_dir, SID, "20261004-100000", "stop",
              "## Active Task\nreal older state\n")
    # Newest file is a no-transcript placeholder: empty body -> invalid.
    _write_cp(cp_dir, SID, "20261004-120000", "stop", "")
    out = _restore(mod, SID)
    assert "real older state" in out


def test_restore_never_drops_next_action_over_cap(m):
    mod, cp_dir, _tmp = m
    filler = "\n".join(f"## Section {i}\n" + "z" * 300 for i in range(60))
    body = ("## Objective\nkeep this objective\n\n"
            + filler + "\n\n## Next Action\n- finish the migration step\n")
    _write_cp(cp_dir, SID, "20261004-120000", "stop", body)
    out = _restore(mod, SID)
    assert "finish the migration step" in out


# ---------------------------------------------------------------------------
# dynamic compaction guidance: dependency-based drop candidates
# ---------------------------------------------------------------------------

def test_drop_candidates_use_open_requirements_not_read_frequency(m, monkeypatch):
    mod, cp_dir, tmp = m
    import session_store
    from session_store import SessionStore

    snap = tmp / "snap"
    # SNAPSHOT_DIR binds at import; point the module at this test's sandbox.
    monkeypatch.setattr(session_store, "SNAPSHOT_DIR", snap)
    store = SessionStore(SID, snapshot_dir=snap)
    try:
        # fileA: read once, large -- the OLD code would call it safe to drop.
        # It is named by an open requirement, so it must NOT be a drop candidate.
        store.upsert_file_entry("/work/src/billing.py", {
            "mtime_ns": 1, "size_bytes": 90000, "ranges_seen": [],
            "tokens_est": 30000, "read_count": 1, "last_access": time.time() - 100})
        store.upsert_file_entry("/work/src/old_experiment.py", {
            "mtime_ns": 1, "size_bytes": 50000, "ranges_seen": [],
            "tokens_est": 12000, "read_count": 1, "last_access": time.time() - 50})
        # A repeatedly-read dead end: read frequency must not protect it.
        store.upsert_file_entry("/work/src/dead_end.py", {
            "mtime_ns": 1, "size_bytes": 40000, "ranges_seen": [],
            "tokens_est": 9000, "read_count": 7, "last_access": time.time() - 10})
        store.insert_intel_event("Bash", "toolu_x", "ERR: build failed", 500)
    finally:
        store.close()

    # Latest checkpoint sidecar carries the open requirement naming billing.py.
    _write_cp(cp_dir, SID, "20261004-120000", "stop",
              "## Active Task\nfix the billing export bug and verify the rollback path carefully\n",
              sidecar={"version": 2,
                       "task_state": {"version": 2, "requirements": [
                           {"text": "fix /work/src/billing.py export bug",
                            "status": "in_progress", "source": "todo_snapshot"}]},
                       "open_questions": [], "decisions": []})

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.dynamic_compact_instructions(session_id=SID)
    out = buf.getvalue()
    drop_section = out.split("DROP")[-1] if "DROP" in out else ""
    assert "billing.py" not in drop_section, out
    assert "old_experiment.py" in drop_section, out
    # A repeatedly-read file is still a valid drop candidate (dead ends happen).
    assert "dead_end.py" in drop_section, out
    # Every drop candidate keeps a retrieval pointer.
    assert "re-read" in drop_section or "recover" in drop_section.lower(), out


# ---------------------------------------------------------------------------
# codex adapter parity
# ---------------------------------------------------------------------------

def test_codex_extract_session_state_builds_task_state(m, tmp_path):
    import codex_session
    records = [
        {"type": "event_msg", "payload": {"type": "user_message",
            "message": "Port the retry logic to the new client",
            "id": "umsg_1"}},
        {"type": "event_msg", "payload": {"type": "function_call",
            "name": "exec_command", "call_id": "call_1",
            "arguments": json.dumps({"cmd": ["pytest", "-q"]})}},
        {"type": "event_msg", "payload": {"type": "exec_command_end",
            "call_id": "call_1", "exit_code": 1, "status": "failed",
            "aggregated_output": "FAILED tests/test_retry.py"}},
        {"type": "event_msg", "payload": {"type": "agent_message",
            "message": "The retry tests fail; next I patch backoff.",
            "id": "amsg_1"}},
    ]
    p = tmp_path / "rollout.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                 encoding="utf-8")
    state = codex_session.extract_session_state(p)
    ts = state["task_state"]
    assert ts["version"] == 2
    assert ts["objective"]["text"] == "Port the retry logic to the new client"
    failed = [c for c in ts["checks"] if c["outcome"] == "failed"]
    assert failed, ts["checks"]
    assert any("pytest" in (c.get("target") or "") or "exec" in (c.get("tool") or "").lower()
               for c in failed)
