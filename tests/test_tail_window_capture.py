"""Pin the tail-window read to the whole-file read it replaced.

``_iter_tail_lines`` exists so ``_extract_session_state`` stops reading and
json-parsing every byte of a transcript to keep its last 500 records (the
issue #253 profiling follow-up). Its contract is exact equivalence: whatever
the old whole-file pass collected, the windowed pass must collect. These
tests drive both paths over hostile shapes - invalid lines, oversized lines,
CRLF and CR endings, missing trailing newline, multi-byte UTF-8 straddling
window cuts - with a chunk size small enough to force window growth, and
assert identical records.

``_capture_git_state`` went from two ``git rev-parse`` spawns to one; the
tests pin the single spawn, the unchanged branch, and a 7-char sha label
(git's own --short auto-lengthens past 7 in large repos, so the label is the
first 7 chars of HEAD, not whatever --short would print).
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections import deque
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

measure = importlib.import_module("measure")
MAX_LINE = measure.codex_session.MAX_JSONL_LINE_CHARS


def full_read_records(path, tail_lines):
    """The pre-change algorithm: stream the whole file, keep the tail."""
    records = deque(maxlen=tail_lines)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if len(line) > MAX_LINE:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return list(records)


def window_records(path, tail_lines, chunk_size):
    records = deque(maxlen=tail_lines)
    for line in measure._iter_tail_lines(path, tail_lines, chunk_size=chunk_size):
        if len(line) > MAX_LINE:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(records)


def write(path, text):
    path.write_bytes(text.encode("utf-8"))


def line(i, pad=0):
    return json.dumps({"type": "user", "i": i, "pad": "y" * pad})


CASES = {
    "mixed-validity": "".join(
        (line(i) if i % 3 else "not json {{{") + "\n" for i in range(300)
    ),
    "blank-lines": "\n\n" + line(1) + "\n\n\n" + line(2) + "\n\n",
    "crlf": (line(1) + "\r\n" + line(2) + "\r\n" + "broken\r\n" + line(3) + "\r\n"),
    "cr-only": line(1) + "\r" + line(2) + "\r" + line(3),
    "no-trailing-newline": line(1) + "\n" + line(2),
    "multibyte": "".join(
        json.dumps({"i": i, "emoji": "\U0001f389" * 40}) + "\n" for i in range(60)
    ),
    "empty": "",
    "one-line": line(1) + "\n",
}

CHUNKS = [17, 64, 1 << 20]
TAILS = [1, 5, 500]


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("chunk", CHUNKS)
@pytest.mark.parametrize("tail", TAILS)
def test_window_matches_full_read(tmp_path, case, chunk, tail):
    p = tmp_path / "t.jsonl"
    write(p, CASES[case])
    assert window_records(p, tail, chunk) == full_read_records(p, tail)


def test_oversized_lines_force_window_growth(tmp_path):
    """Oversized lines are skipped; the window must reach past them."""
    p = tmp_path / "big.jsonl"
    oversized = json.dumps({"big": "z" * (MAX_LINE + 10)})
    body = line(0) + "\n" + oversized + "\n" + "".join(
        line(i) + "\n" for i in range(1, 8)
    )
    write(p, body)
    assert window_records(p, 500, 64) == full_read_records(p, 500)
    assert window_records(p, 500, 64)[0]["i"] == 0


def test_window_extends_until_enough_valid_records(tmp_path):
    """300 valid + 400 invalid lines: tail=250 must skip the invalid tail
    block and reach back into the valid run, exactly like the full read."""
    p = tmp_path / "mixed.jsonl"
    body = "".join(line(i) + "\n" for i in range(300))
    body += "".join(f"junk{i}\n" for i in range(400))
    write(p, body)
    got = window_records(p, 250, 128)
    assert got == full_read_records(p, 250)
    assert got[0]["i"] == 50 and len(got) == 250


def test_extract_session_state_end_to_end(tmp_path, monkeypatch):
    """The public function returns identical state via the windowed read."""
    p = tmp_path / "s.jsonl"
    body = "".join(
        json.dumps({"type": "user", "message": {"content": f"step {i}?"},
                    "i": i}) + "\n"
        for i in range(900)
    )
    write(p, body)
    orig = measure._iter_tail_lines
    # Force tiny windows so the public function crosses many window cuts.
    monkeypatch.setattr(measure, "_iter_tail_lines",
                        lambda fp, tl: orig(fp, tl, chunk_size=64))
    state = measure._extract_session_state(str(p))
    assert state is not None
    assert state["current_step"]["last_user"].startswith("step 899")


def test_capture_git_state_single_spawn(monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(*a, **kw):
        calls.append(a[0])
        return real_run(*a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    branch, sha = measure._capture_git_state(cwd=str(REPO))
    rev_parse_calls = [c for c in calls if "rev-parse" in c]
    assert len(rev_parse_calls) == 1

    expected_branch = real_run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, cwd=str(REPO),
    ).stdout.strip()
    full_sha = real_run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, cwd=str(REPO),
    ).stdout.strip()
    assert branch == expected_branch
    assert sha == full_sha[:7]


def test_capture_git_state_outside_repo(tmp_path):
    assert measure._capture_git_state(cwd=str(tmp_path)) == (None, None)


@pytest.mark.parametrize("synthetic", [
    {"isMeta": True, "message": {"content": "Your response was cut off. Resume?"}},
    {"isCompactSummary": True, "message": {"content": "Summary TODO"}},
    {"message": {"content": "<command-name>/compact</command-name>\n<command-message>compact</command-message>\n<command-args></command-args>"}},
    {"message": {"content": "<local-command-caveat>Ignore?"}},
    {"message": {"content": "<local-command-stdout>Output?"}},
    {"message": {"content": "<local-command-stderr>Error?"}},
    {"message": {"content": "<task-notification>Finished?"}},
    {"message": {"content": "[SYSTEM NOTIFICATION] TODO"}},
])
def test_checkpoint_ignores_harness_task_and_questions(tmp_path, synthetic):
    p = tmp_path / "checkpoint.jsonl"
    records = [{"type": "user", "message": {"content": "fix the login bug"}},
               {"type": "user", **synthetic}]
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    state = measure._extract_session_state(str(p))
    assert state["current_step"]["last_user"] == "fix the login bug"
    assert state["open_questions"] == []


@pytest.mark.parametrize("content, expected", [
    ("<command-name>/ce:debug</command-name>\n<command-message>ce:debug</command-message>\n<command-args>fix login and keep tests?</command-args>",
     "/ce:debug fix login and keep tests?"),
    ("<command-name>briefing</command-name><command-args></command-args>", "/briefing"),
    ("<command-name>/compact</command-name><command-args>keep login details</command-args>", ""),
    ("<command-name>/clear</command-name><command-args></command-args>", ""),
    ("<command-name>/broken", ""),
    ("<command-message>compact</command-message>", ""),
    ("I saw <command-name>/compact</command-name> in the logs?", "I saw <command-name>/compact</command-name> in the logs?"),
    ("Your response above was cut off. Please explain why?", "Your response above was cut off. Please explain why?"),
    ("/debug fix login?", "/debug fix login?"),
    ("תקן התחברות", "תקן התחברות"),
    ([{"type": "tool_result", "text": "not human?", "content": "output?"}], ""),
    ([{"type": "tool_result", "content": "output?"}, {"type": "text", "text": "real followup?"}], "real followup?"),
    ([{"type": "text", "text": "<task-notification>done?"}, {"type": "text", "text": "real followup?"}], "real followup?"),
    (None, ""),
    (42, ""),
])
def test_task_text_preserves_genuine_commands_and_ignores_transport(tmp_path, content, expected):
    record = {"type": "user", "message": {"content": content}}
    p = tmp_path / "task.jsonl"
    p.write_text(json.dumps(record), encoding="utf-8")
    assert measure._task_user_text(record) == expected
    assert measure._extract_session_state(str(p))["current_step"]["last_user"] == expected
    assert measure._parse_session_jsonl(str(p))["topic"] == measure._extract_topic(expected)


def test_raw_skill_and_scoring_text_unchanged():
    content = "<command-name>/briefing</command-name><command-args>evening</command-args>"
    record = {"type": "user", "message": {"content": content}, "isMeta": True}
    assert measure._task_user_text(record) == ""
    assert measure._extract_user_text(record) == content
    assert measure._leading_user_text(record["message"]) == content


def test_task_text_skips_malformed_text_values():
    record = {"message": {"content": [
        {"type": "text", "text": None}, {"type": "text", "text": 5}, "real task"]}}
    assert measure._task_user_text(record) == "real task"


def test_task_command_rejects_repeated_tags_without_backtracking():
    # A wildcard nested in a repeated-tag regex takes exponential time here.
    import time
    text = "<command-args>x</command-args>" * 1000 + "!"
    start = time.monotonic()
    assert measure._task_user_text({"message": {"content": text}}) == ""
    assert time.monotonic() - start < 1.0


@pytest.mark.parametrize("duplicate_field", ["name", "message", "args"])
@pytest.mark.parametrize("consumer", ["helper", "checkpoint", "topic"])
def test_complete_duplicate_command_fields_cannot_become_task(tmp_path, duplicate_field, consumer):
    duplicate_value = "/ce:review" if duplicate_field == "name" else "harness TODO?"
    wrapper = (
        "<command-name>/ce:debug</command-name>"
        "<command-message>ce:debug</command-message>"
        "<command-args>fix login</command-args>"
        f"<command-{duplicate_field}>{duplicate_value}</command-{duplicate_field}>"
    )
    record = {"type": "user", "message": {"content": wrapper}}
    if consumer == "helper":
        assert measure._task_user_text(record) == ""
    else:
        p = tmp_path / "duplicate.jsonl"
        real = {"type": "user", "message": {"content": "fix the login bug"}}
        # Checkpoint keeps the latest human task; topic finds the first one.
        records = [real, record] if consumer == "checkpoint" else [record, real]
        p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        if consumer == "checkpoint":
            state = measure._extract_session_state(str(p))
            assert state["current_step"]["last_user"] == "fix the login bug"
            assert state["open_questions"] == []
        else:
            assert measure._parse_session_jsonl(str(p))["topic"] == "fix the login bug"


def test_command_and_followup_blocks_keep_order_and_separator(tmp_path):
    record = {"type": "user", "message": {"content": [
        {"type": "text", "text": "<command-name>/ce:debug</command-name><command-args>fix login</command-args>"},
        {"type": "text", "text": "Keep the logout flow unchanged?"},
    ]}}
    expected = "/ce:debug fix login\nKeep the logout flow unchanged?"
    p = tmp_path / "followup.jsonl"
    p.write_text(json.dumps(record), encoding="utf-8")
    assert measure._task_user_text(record) == expected
    state = measure._extract_session_state(str(p))
    assert state["current_step"]["last_user"] == expected
    assert state["open_questions"] == [expected]
    assert measure._parse_session_jsonl(str(p))["topic"] == "/ce:debug fix login"
