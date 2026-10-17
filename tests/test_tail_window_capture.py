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
tests pin the single spawn and the unchanged (branch, short_sha) result.
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
    expected_sha = real_run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, cwd=str(REPO),
    ).stdout.strip()
    assert branch == expected_branch
    assert sha == expected_sha


def test_capture_git_state_outside_repo(tmp_path):
    assert measure._capture_git_state(cwd=str(tmp_path)) == (None, None)
