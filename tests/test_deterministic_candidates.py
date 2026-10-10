"""Deterministic candidates: which parts of a workflow could be plain code.

Synthetic transcripts only. Every fixture is written inside the worktree
(.pytest_cache/detcand/<uuid>), never /tmp and never a real ~/.claude.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import deterministic_candidates as dc  # noqa: E402

SANDBOX_ROOT = REPO / ".pytest_cache" / "detcand"


@pytest.fixture
def sandbox():
    root = SANDBOX_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


def price(model, fresh, out, cr, cc, c1h, c5m):
    """Flat fake rate card: dollars = tokens at their own weights."""
    return (fresh * 3.0 + out * 15.0 + cr * 0.3 + cc * 3.75) / 1e6


# ---------------------------------------------------------------------------
# Transcript builders
# ---------------------------------------------------------------------------

USAGE = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 100}


def _bash(cmd):
    return {"tool": "Bash", "input": {"command": cmd}}


def _edit(path="src/app.py"):
    return {"tool": "Edit", "input": {"file_path": path, "old_string": "a", "new_string": "b"}}


def write_claude(path: Path, turns, prefix, model="claude-sonnet-4-5", usage=None, reply="ok"):
    """turns: list of lists of steps. A step is {"tool","input","ok"(default True),"usage"}."""
    usage = usage or USAGE
    lines = []
    n = 0
    for t, steps in enumerate(turns):
        lines.append({"type": "user", "message": {"role": "user", "content": f"prompt {t} {prefix}"}})
        for step in steps:
            n += 1
            tid = f"toolu-{prefix}-{n}"
            u = dict(step.get("usage") or usage)
            lines.append({
                "type": "assistant", "requestId": f"req-{prefix}-{n}",
                "message": {"id": f"msg-{prefix}-{n}", "model": model, "usage": u,
                            "content": [{"type": "tool_use", "id": tid, "name": step["tool"], "input": step["input"]}]},
            })
            lines.append({
                "type": "user",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": tid, "content": "output",
                    "is_error": not step.get("ok", True)}]},
            })
        n += 1
        lines.append({
            "type": "assistant", "requestId": f"req-{prefix}-{n}",
            "message": {"id": f"msg-{prefix}-{n}", "model": model, "usage": dict(usage),
                        "content": [{"type": "text", "text": reply}]},
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def write_codex(path: Path, turns, prefix, model="gpt-5.4", reply="ok"):
    """turns: list of lists of steps; a step is {"cmd"|"patch"|"agent"|"stdin", "exit"}."""
    recs = [
        {"type": "session_meta", "payload": {"id": f"codex-{prefix}", "cli_version": "0.1"}},
        {"type": "turn_context", "payload": {"model": model}},
    ]
    total_in = total_cached = total_out = 0
    n = 0

    def token_count():
        nonlocal total_in, total_cached, total_out
        total_in += 1100
        total_cached += 1000
        total_out += 20
        recs.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total_in, "cached_input_tokens": total_cached,
                                  "output_tokens": total_out},
            "last_token_usage": {"input_tokens": 1100, "cached_input_tokens": 1000, "output_tokens": 20}}}})

    for t, steps in enumerate(turns):
        recs.append({"type": "event_msg", "payload": {"type": "user_message", "message": f"do thing {t} {prefix}"}})
        for step in steps:
            n += 1
            cid = f"call-{prefix}-{n}"
            if "cmd" in step:
                recs.append({"type": "response_item", "payload": {
                    "type": "function_call", "name": "exec_command", "call_id": cid,
                    "arguments": json.dumps({"cmd": step["cmd"], "workdir": "/work/proj"})}})
                code = step.get("exit", 0)
                recs.append({"type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": cid,
                    "output": f"Chunk ID: 1\nWall time: 1.2 seconds\nProcess exited with code {code}\nOutput:\nx"}})
            elif "stdin" in step:
                recs.append({"type": "response_item", "payload": {
                    "type": "function_call", "name": "write_stdin", "call_id": cid,
                    "arguments": json.dumps({"session_id": 1, "chars": step["stdin"]})}})
            elif "patch" in step:
                recs.append({"type": "response_item", "payload": {
                    "type": "custom_tool_call", "name": "apply_patch", "call_id": cid,
                    "input": f"*** Begin Patch\n*** Update File: {step['patch']}\n@@\n-a\n+b\n*** End Patch"}})
            elif "agent" in step:
                recs.append({"type": "response_item", "payload": {
                    "type": "function_call", "name": "spawn_agent", "call_id": cid,
                    "arguments": json.dumps({"agent_type": "worker", "message": step["agent"]})}})
            token_count()
        recs.append({"type": "event_msg", "payload": {"type": "agent_message", "message": reply}})
        token_count()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in recs) + "\n", encoding="utf-8")
    return path


def run_dc(runtime, paths, **kw):
    files = [(p, p.stat().st_mtime) for p in paths]
    kw.setdefault("use_cache", False)
    kw.setdefault("budget_s", 30.0)
    return dc.run(runtime, files, price, **kw)


def kinds(result):
    return [c["kind"] for c in result["candidates"]]


# ---------------------------------------------------------------------------
# Normalisation and classification
# ---------------------------------------------------------------------------

def test_normalise_collapses_numbers_hashes_dates_quotes_and_paths():
    a = dc.normalise_command('cd /Users/alice/proj && git log --since 2026-10-10 -n 5 abc1234def "fix login 42"')
    b = dc.normalise_command('cd /home/bob/other && git log --since 2025-01-02 -n 20 9f8e7d6c5b "other message"')
    assert a == b
    assert "<date>" in a and "<n>" in a and "<hash>" in a and "<str>" in a


def test_normalise_absolute_path_prefixes_posix_and_windows():
    posix = dc.normalise_command("python /Users/alice/proj/scripts/run_tests.py --seed 42")
    win = dc.normalise_command(r"python C:\Users\bob\proj\scripts\run_tests.py --seed 7")
    win2 = dc.normalise_command(r"python D:\work\x\run_tests.py --seed 9")
    assert posix == win == win2 == "python <dir>/run_tests.py --seed <n>"
    unc = dc.normalise_command(r"type \\server\share\dir\notes.txt")
    assert "server" not in unc and unc.endswith("<dir>/notes.txt")
    assert "alice" not in posix and "bob" not in win


def test_normalise_keeps_relative_paths_urls_and_dev_null():
    out = dc.normalise_command("curl -s https://example.com/a/b > /dev/null; ls ./scripts/x.sh")
    assert "https://example.com/a/b" in out and "/dev/null" in out and "./scripts/x.sh" in out


def test_normalise_blanks_secret_env_and_long_tokens():
    out = dc.normalise_command("API_KEY=abc123 TOKEN='x y' run --id a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5")
    assert "abc123" not in out and "x y" not in out and "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5" not in out


@pytest.mark.parametrize("cmd", [
    "pytest", "python3 -m pytest -q tests/test_x.py", "cd /a/b && pytest -x 2>&1 | tail -20",
    "npm test", "npm run build", "cargo test --all", "go test ./...", "npx tsc --noEmit",
    "npx eslint src", "ruff check .", "make test", "make", "make lint build",
    "black --check .", "uv run pytest", "bash scripts/check-mirror-sync.sh",
    "pytest && echo done",
])
def test_check_commands_recognised(cmd):
    assert dc.is_check_command(cmd)


@pytest.mark.parametrize("cmd", [
    "git status", "ls -la", "make deploy", "make install", "black .", "ruff check --fix .",
    "npm install", "echo hi", "pytest && git push", "rm -rf build", "",
])
def test_non_check_commands_rejected(cmd):
    assert not dc.is_check_command(cmd)


# ---------------------------------------------------------------------------
# repeated_sequence
# ---------------------------------------------------------------------------

SEQ = lambda n: [  # noqa: E731
    _bash(f"git status --short {n}"),
    _bash(f"git diff --stat HEAD~{n}"),
    {"tool": "Grep", "input": {"pattern": f"TODO {n}", "glob": "*.py"}},
]


def test_repeated_sequence_across_three_sessions(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(3)]
    res = run_dc("claude", paths)
    seq = [c for c in res["candidates"] if c["kind"] == "repeated_sequence"]
    assert len(seq) == 1
    c = seq[0]
    assert c["sessions_seen"] == 3 and c["times_seen"] == 3 and c["sequence_length"] == 3
    assert c["tokens"]["input_tokens"] == 3 * 3 * 1110 and c["tokens"]["cache_read_tokens"] == 3 * 3 * 1000
    assert c["tokens"]["output_tokens"] == 3 * 3 * 20
    assert c["tokens"]["total_tokens"] == c["tokens"]["input_tokens"] + c["tokens"]["output_tokens"]
    assert c["cost_usd"] > 0 and c["basis"] == "measured on your transcripts"
    assert "git status" in c["example"] and len(c["example"]) <= 200
    assert c["suggestion"]
    assert res["basis"] == "measured on your transcripts"


def test_repeated_sequence_needs_three_sessions(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(2)]
    res = run_dc("claude", paths)
    assert "repeated_sequence" not in kinds(res)


def test_repeated_sequence_needs_three_calls(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)[:2]], f"s{i}") for i in range(4)]
    assert "repeated_sequence" not in kinds(run_dc("claude", paths))


def test_repeated_sequence_prefers_the_longest_run(sandbox):
    long_seq = lambda n: SEQ(n) + [_bash(f"python3 report.py --day {n}")]  # noqa: E731
    paths = [write_claude(sandbox / f"s{i}.jsonl", [long_seq(i)], f"s{i}") for i in range(3)]
    seq = [c for c in run_dc("claude", paths)["candidates"] if c["kind"] == "repeated_sequence"]
    assert [c["sequence_length"] for c in seq] == [4]


def test_read_only_runs_are_not_sequences(sandbox):
    reads = [{"tool": "Read", "input": {"file_path": f"/a/b/f{i}.py"}} for i in range(4)]
    paths = [write_claude(sandbox / f"s{i}.jsonl", [reads], f"s{i}") for i in range(4)]
    assert "repeated_sequence" not in kinds(run_dc("claude", paths))


def test_resumed_session_history_is_counted_once(sandbox):
    one = write_claude(sandbox / "a.jsonl", [SEQ(1)], "same")
    for name in ("b", "c"):
        (sandbox / f"{name}.jsonl").write_bytes(one.read_bytes())
        os.utime(sandbox / f"{name}.jsonl", (time.time() + 5, time.time() + 5))
    paths = [sandbox / "a.jsonl", sandbox / "b.jsonl", sandbox / "c.jsonl"]
    assert "repeated_sequence" not in kinds(run_dc("claude", paths))


def test_sidechain_records_are_not_the_sessions_own_calls(sandbox):
    paths = []
    for i in range(3):
        p = write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}")
        recs = [json.loads(ln) for ln in p.read_text().splitlines()]
        for r in recs:
            r["isSidechain"] = True
        p.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        paths.append(p)
    assert run_dc("claude", paths)["candidates"] == []


# ---------------------------------------------------------------------------
# templated_subagent
# ---------------------------------------------------------------------------

def _agent(i, head="Summarise the failing test output and list the three most likely causes of file", tail=""):
    return {"tool": "Agent", "input": {"subagent_type": "general-purpose",
                                       "prompt": f"{head} number {i} in build {1000 + i}. " + "x" * 400 + tail}}


def test_templated_subagent_five_launches(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [[_agent(i), _agent(i + 10)]], f"s{i}") for i in range(3)]
    res = run_dc("claude", paths)
    sub = [c for c in res["candidates"] if c["kind"] == "templated_subagent"]
    assert len(sub) == 1 and sub[0]["times_seen"] == 6 and sub[0]["sessions_seen"] == 3
    assert "launching turns only" in sub[0]["tokens_scope"]
    assert sub[0]["example"].startswith("Summarise the failing test output")


def test_templated_subagent_four_launches_is_not_enough(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [[_agent(i), _agent(i + 10)]], f"s{i}") for i in range(2)]
    assert "templated_subagent" not in kinds(run_dc("claude", paths))


def test_subagent_prompts_that_differ_in_first_300_chars_do_not_group(sandbox):
    steps = [{"tool": "Agent", "input": {"prompt": f"Task {chr(97 + i) * 40} do thing " + "y" * 400}} for i in range(6)]
    assert "templated_subagent" not in kinds(run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")]))


# ---------------------------------------------------------------------------
# polling_loop
# ---------------------------------------------------------------------------

def test_polling_loop_four_runs_without_edit(sandbox):
    steps = [_bash(f"sleep 30 && gh run view {1000 + i} --json status") for i in range(4)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    poll = [c for c in res["candidates"] if c["kind"] == "polling_loop"]
    assert len(poll) == 1 and poll[0]["times_seen"] == 4 and poll[0]["sessions_seen"] == 1
    assert "gh run view <n>" in poll[0]["example"]


def test_polling_loop_three_runs_is_not_enough(sandbox):
    steps = [_bash(f"gh run view {i}") for i in range(3)]
    assert "polling_loop" not in kinds(run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")]))


def test_edit_between_runs_resets_the_poll(sandbox):
    steps = [_bash("npm run lint"), _bash("npm run lint"), _edit(), _bash("npm run lint"), _bash("npm run lint")]
    assert "polling_loop" not in kinds(run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")]))


def test_poll_runs_on_the_same_side_of_an_edit_still_count(sandbox):
    steps = [_bash("kubectl get pods")] * 4 + [_edit()] + [_bash("kubectl get pods")]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    poll = [c for c in res["candidates"] if c["kind"] == "polling_loop"]
    assert len(poll) == 1 and poll[0]["times_seen"] == 4


def test_parameter_sweep_is_not_labeled_polling(sandbox):
    """run-0..run-5 collapse to one normalised shape, but each
    literal command ran ONCE. That is a sweep, not a re-check loop: wrong
    label and wrong remedy text ("script that waits") otherwise."""
    steps = [_bash(f"run-{i}") for i in range(6)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    assert "polling_loop" not in kinds(res)
    sweep = [c for c in res["candidates"] if c["kind"] == "parameter_sweep"]
    assert len(sweep) == 1 and sweep[0]["times_seen"] == 6
    assert sweep[0]["suggestion"] != ""


def test_identical_literal_command_is_still_polling(sandbox):
    steps = [_bash("make check")] * 5
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    poll = [c for c in res["candidates"] if c["kind"] == "polling_loop"]
    assert len(poll) == 1 and poll[0]["times_seen"] == 5
    assert "parameter_sweep" not in kinds(res)


def test_dominant_literal_counts_as_polling_strays_do_not(sandbox):
    """4x one literal + 1x a different literal under the same shape: the
    loop is real for the repeated literal; the stray is not loop evidence."""
    steps = [_bash("curl -s api/health-7")] * 4 + [_bash("curl -s api/health-9")]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    poll = [c for c in res["candidates"] if c["kind"] == "polling_loop"]
    assert len(poll) == 1 and poll[0]["times_seen"] == 4
    assert "parameter_sweep" not in kinds(res)


def test_sweep_below_min_runs_reports_nothing(sandbox):
    steps = [_bash(f"run-{i}") for i in range(3)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    assert "parameter_sweep" not in kinds(res)
    assert "polling_loop" not in kinds(res)


# ---------------------------------------------------------------------------
# check_only_turn
# ---------------------------------------------------------------------------

def test_check_only_turns_detected(sandbox):
    turns = [[_bash("python3 -m pytest -q tests/test_a.py")] for _ in range(3)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", turns, "s", reply="All green.")])
    chk = [c for c in res["candidates"] if c["kind"] == "check_only_turn"]
    assert len(chk) == 1 and chk[0]["times_seen"] == 3
    assert "pytest" in chk[0]["example"]
    # both API calls of every user turn are measured (the call and the short reply)
    assert chk[0]["tokens"]["output_tokens"] == 3 * 2 * 20


def test_failing_check_is_not_a_candidate(sandbox):
    turns = [[{**_bash("pytest -q"), "ok": False}] for _ in range(3)]
    assert "check_only_turn" not in kinds(run_dc("claude", [write_claude(sandbox / "s.jsonl", turns, "s")]))


def test_long_reply_is_not_a_check_only_turn(sandbox):
    turns = [[_bash("pytest -q")] for _ in range(3)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", turns, "s", reply="Here is a long analysis. " * 20)])
    assert "check_only_turn" not in kinds(res)


def test_turn_with_other_tool_calls_is_not_check_only(sandbox):
    turns = [[_bash("pytest -q"), _bash("git status")] for _ in range(3)]
    assert "check_only_turn" not in kinds(run_dc("claude", [write_claude(sandbox / "s.jsonl", turns, "s")]))


def test_unknown_exit_status_is_not_success(sandbox):
    path = write_claude(sandbox / "s.jsonl", [[_bash("pytest -q")] for _ in range(3)], "s")
    kept = [ln for ln in path.read_text().splitlines() if '"tool_result"' not in ln]
    path.write_text("\n".join(kept) + "\n")
    assert "check_only_turn" not in kinds(run_dc("claude", [path]))


# ---------------------------------------------------------------------------
# Redaction and privacy
# ---------------------------------------------------------------------------

AWS = "AKIAABCDEFGHIJKLMNOP"
BEARER = "Bearer abcdef0123456789ZYXWVUTSRQ"


def test_secret_inside_command_never_reaches_output(sandbox):
    def seq(n):
        return [
            _bash(f"curl -H 'Authorization: {BEARER}' https://api.example.com/v1/items?n={n}"),
            _bash(f"AWS_ACCESS_KEY_ID={AWS} aws s3 ls s3://bucket-{n}"),
            _bash(f"export {AWS} ; echo {AWS} {n}"),
        ]
    paths = [write_claude(sandbox / f"s{i}.jsonl", [seq(i)], f"s{i}") for i in range(3)]
    cache_dir = sandbox / "cache"
    res = run_dc("claude", paths, use_cache=True, cache_dir=cache_dir)
    blob = json.dumps(res) + (cache_dir / dc.CACHE_NAME).read_text()
    assert res["candidates"], "the sequence should still be found"
    for secret in (AWS, "abcdef0123456789ZYXWVUTSRQ", BEARER):
        assert secret not in blob


def test_redaction_failure_withholds_examples(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(3)]
    res = run_dc("claude", paths, redact=dc.Redactor(None))
    assert res["candidates"] and all(c["example"] == dc.WITHHELD for c in res["candidates"])

    def boom(_text):
        raise RuntimeError("pattern file broken")
    res = run_dc("claude", paths, redact=dc.Redactor(boom))
    assert all(c["example"] == dc.WITHHELD for c in res["candidates"])


def test_example_is_capped_at_200_chars(sandbox):
    long_steps = lambda n: [  # noqa: E731
        _bash("echo " + " ".join(f"word{k}x" for k in range(60)) + f" {n}"),
        _bash("ls " + " ".join(f"dir{k}y" for k in range(60))),
        _bash("cat " + " ".join(f"file{k}z" for k in range(60))),
    ]
    paths = [write_claude(sandbox / f"s{i}.jsonl", [long_steps(i)], f"s{i}") for i in range(3)]
    seq = [c for c in run_dc("claude", paths)["candidates"] if c["kind"] == "repeated_sequence"]
    assert seq and len(seq[0]["example"]) <= 200


def test_no_file_contents_in_output(sandbox):
    marker = "SUPER-SECRET-FILE-BODY"
    steps = lambda n: [  # noqa: E731
        {"tool": "Read", "input": {"file_path": f"/Users/alice/proj/a{n}.py"}},
        _bash(f"git diff {n}"),
        {"tool": "Write", "input": {"file_path": "/Users/alice/proj/out.py", "content": marker}},
        _bash(f"pytest -q {n}"),
    ]
    paths = [write_claude(sandbox / f"s{i}.jsonl", [steps(i)], f"s{i}") for i in range(3)]
    blob = json.dumps(run_dc("claude", paths))
    assert marker not in blob and "alice" not in blob


# ---------------------------------------------------------------------------
# Windows paths
# ---------------------------------------------------------------------------

def test_windows_paths_group_across_sessions_and_hide_user_names(sandbox):
    roots = [r"C:\Users\alice\proj", r"D:\work\bob\proj", r"C:\Users\carol\dev\proj"]

    def seq(root, n):
        return [
            _bash(rf"python {root}\scripts\collect.py --day {n}"),
            _bash(rf"type {root}\out\report_{n}.txt"),
            _bash(rf"cd {root} && pytest -q tests\test_{n}.py"),
        ]
    paths = [write_claude(sandbox / f"s{i}.jsonl", [seq(r, i)], f"s{i}") for i, r in enumerate(roots)]
    res = run_dc("claude", paths)
    seq_c = [c for c in res["candidates"] if c["kind"] == "repeated_sequence"]
    assert len(seq_c) == 1 and seq_c[0]["sessions_seen"] == 3
    blob = json.dumps(res)
    for name in ("alice", "bob", "carol", "Users"):
        assert name not in blob


# ---------------------------------------------------------------------------
# Codex-shaped transcripts
# ---------------------------------------------------------------------------

def test_codex_repeated_sequence(sandbox):
    def seq(n):
        return [{"cmd": f"git status --short {n}"}, {"cmd": f"git diff --stat HEAD~{n}"}, {"cmd": f"rg -n TODO-{n} src"}]
    paths = [write_codex(sandbox / f"c{i}.jsonl", [seq(i)], f"c{i}") for i in range(3)]
    res = run_dc("codex", paths)
    c = [x for x in res["candidates"] if x["kind"] == "repeated_sequence"]
    assert len(c) == 1 and c[0]["sessions_seen"] == 3
    assert c[0]["tokens"]["cache_read_tokens"] > 0 and c[0]["tokens"]["output_tokens"] > 0
    assert c[0]["cost_usd"] > 0


def test_codex_check_only_turn_uses_exit_code_in_output(sandbox):
    turns = [[{"cmd": "cargo test --all"}] for _ in range(3)]
    res = run_dc("codex", [write_codex(sandbox / "c.jsonl", turns, "c", reply="Passed.")])
    assert "check_only_turn" in kinds(res)
    failing = [[{"cmd": "cargo test --all", "exit": 101}] for _ in range(3)]
    res = run_dc("codex", [write_codex(sandbox / "d.jsonl", failing, "d", reply="Passed.")])
    assert "check_only_turn" not in kinds(res)


def test_codex_polling_via_write_stdin_and_exec(sandbox):
    steps = [{"stdin": ""} for _ in range(4)]
    res = run_dc("codex", [write_codex(sandbox / "c.jsonl", [steps], "c")])
    assert "polling_loop" in kinds(res)
    steps = [{"cmd": "gh run watch"}, {"cmd": "gh run watch"}, {"patch": "src/a.py"}, {"cmd": "gh run watch"}, {"cmd": "gh run watch"}]
    res = run_dc("codex", [write_codex(sandbox / "d.jsonl", [steps], "d")])
    assert "polling_loop" not in kinds(res)


def test_codex_templated_subagent(sandbox):
    steps = [{"agent": f"Review module number {i} for dead code and report " + "z" * 400} for i in range(5)]
    res = run_dc("codex", [write_codex(sandbox / "c.jsonl", [steps], "c")])
    assert "templated_subagent" in kinds(res)


def test_codex_usage_is_deltas_not_cumulative_totals(sandbox):
    turns = [[{"cmd": "make test"}] for _ in range(3)]
    path = write_codex(sandbox / "c.jsonl", turns, "c", reply="ok")
    trace = dc.extract_codex(path, dc._KeyMaker(dc.default_redactor()), set())
    assert sum(t.fresh + t.cr for t in trace.turns) == 6 * 1100
    assert sum(t.out for t in trace.turns) == 6 * 20


# ---------------------------------------------------------------------------
# Other runtimes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", ["cursor", "copilot", "hermes", "antigravity", "grok", "opencode", "pi"])
def test_unmeasurable_runtimes_get_an_explicit_entry(runtime):
    res = dc.run(runtime, [], price)
    assert res["status"] == "not_measurable" and res["candidates"] == []
    assert f"not measurable on {runtime}" in res["reason"]
    assert res["not_measurable"][0]["runtime"] == runtime
    assert "not measurable" in res["summary"]


def test_runtime_support_table_names_every_runtime():
    table = {e["runtime"]: e for e in dc.runtime_support_table()}
    assert table["claude"]["measurable"] and table["codex"]["measurable"]
    for name in ("opencode", "copilot", "hermes", "cursor", "antigravity", "grok", "pi"):
        assert not table[name]["measurable"] and f"not measurable on {name}" in table[name]["reason"]


# ---------------------------------------------------------------------------
# Ranking, cap, budget, cache, never-fail
# ---------------------------------------------------------------------------

def test_cap_ten_sorted_by_tokens(sandbox):
    paths = []
    for i in range(12):
        usage = dict(USAGE, output_tokens=20 + i * 500)
        steps = [{**_bash(f"poll-tool-{chr(97 + i)} --watch"), "usage": usage} for _ in range(4)]
        paths.append(write_claude(sandbox / f"s{i}.jsonl", [steps], f"s{i}", usage=usage))
    res = run_dc("claude", paths)
    cands = res["candidates"]
    assert len(cands) == 10
    totals = [c["tokens"]["total_tokens"] for c in cands]
    assert totals == sorted(totals, reverse=True)
    assert res["totals"]["total_tokens"] > 0


def test_budget_exhaustion_returns_partial_results(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(4)]
    res = run_dc("claude", paths, budget_s=0.0)
    assert res["partial"] is True and res["status"] in ("ok", "no_data")
    assert res["sessions_scanned"] < len(paths) and res["sessions_found"] == len(paths)
    assert "partial" in res["summary"]


def test_budget_hit_mid_file_is_partial_not_an_error(sandbox):
    path = write_claude(sandbox / "s.jsonl", [SEQ(1)], "s")
    with pytest.raises(dc.BudgetExceeded):
        dc.extract_claude(path, dc._KeyMaker(dc.default_redactor()), set(), deadline=time.monotonic() - 1)


def test_sessions_cap_is_newest_first(sandbox):
    paths = []
    for i in range(6):
        p = write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}")
        os.utime(p, (1_000_000 + i * 100, 1_000_000 + i * 100))
        paths.append(p)
    res = run_dc("claude", paths, max_sessions=3)
    seq = [c for c in res["candidates"] if c["kind"] == "repeated_sequence"]
    assert res["sessions_scanned"] == 3 and res["sessions_not_scanned_cap"] == 3
    assert seq and seq[0]["sessions_seen"] == 3


def test_internal_failure_is_reported_not_raised(sandbox, monkeypatch):
    paths = [write_claude(sandbox / "s.jsonl", [SEQ(1)], "s")]

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(dc.EXTRACTORS, "claude", boom)
    res = run_dc("claude", paths)
    assert res["status"] == "error" and "kaboom" in res["error"] and res["candidates"] == []
    assert "coach output unaffected" in res["summary"]


def test_corrupt_and_hostile_lines_are_skipped(sandbox):
    path = write_claude(sandbox / "s.jsonl", [SEQ(1)], "s")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not json\n[1,2,3]\n42\n" + json.dumps({"type": "assistant", "message": {"usage": {"output_tokens": float("inf")}, "content": 5}}) + "\n")
    res = run_dc("claude", [path])
    assert res["status"] in ("ok", "no_data")


def test_cache_hit_skips_reading_and_new_transcript_invalidates(sandbox, monkeypatch):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(3)]
    cache_dir = sandbox / "cache"
    first = run_dc("claude", paths, use_cache=True, cache_dir=cache_dir)
    assert first["cached"] is False and first["candidates"]

    def boom(*a, **k):
        raise AssertionError("cache hit must not read transcripts")
    monkeypatch.setitem(dc.EXTRACTORS, "claude", boom)
    second = run_dc("claude", paths, use_cache=True, cache_dir=cache_dir)
    assert second["cached"] is True and second["candidates"] == first["candidates"]

    newer = write_claude(sandbox / "s9.jsonl", [SEQ(9)], "s9")
    os.utime(newer, (time.time() + 100, time.time() + 100))
    third = run_dc("claude", paths + [newer], use_cache=True, cache_dir=cache_dir)
    assert third["status"] == "error"  # key changed -> it tried to read -> the patched extractor raised


def test_partial_results_are_cached_only_briefly(sandbox):
    key = "k"
    dc.cache_store(sandbox, key, {"partial": True, "candidates": []}, now=1000.0)
    assert dc.cache_load(sandbox, key, now=1000.0 + 10) is not None
    assert dc.cache_load(sandbox, key, now=1000.0 + dc.PARTIAL_CACHE_TTL_S + 1) is None
    dc.cache_store(sandbox, "full", {"partial": False, "candidates": []}, now=1000.0)
    assert dc.cache_load(sandbox, "full", now=1000.0 + 10 ** 6) is not None


def test_unwritable_cache_dir_does_not_fail(sandbox):
    blocker = sandbox / "file"
    blocker.write_text("x")
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(3)]
    res = run_dc("claude", paths, use_cache=True, cache_dir=blocker / "sub")
    assert res["status"] == "ok"


# ---------------------------------------------------------------------------
# measure.py integration (subprocess, sandbox HOME inside the worktree)
# ---------------------------------------------------------------------------

def _cli(sandbox, args, runtime=None, extra_env=None):
    env = dict(os.environ)
    home = sandbox / "home"
    (home / ".claude" / "projects" / "proj").mkdir(parents=True, exist_ok=True)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CODEX_HOME", "HERMES_HOME",
                "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_RUNTIME"):
        env.pop(key, None)
    env.update({"HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(sandbox / "snap"), "PYTHONIOENCODING": "utf-8"})
    if runtime:
        env["TOKEN_OPTIMIZER_RUNTIME"] = runtime
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), *args], capture_output=True, text=True,
                          env=env, cwd=str(sandbox), timeout=240)


def _json_out(stdout):
    """coach prints a human [Warning] line before the JSON when cwd matches no project dir."""
    start = 0 if stdout.lstrip().startswith("{") else stdout.index("\n{") + 1
    return json.loads(stdout[start:])


def _seed_projects(sandbox, n=3):
    for i in range(n):
        write_claude(sandbox / "home" / ".claude" / "projects" / "proj" / f"sess{i}.jsonl", [SEQ(i)], f"cli{i}")


def test_cli_standalone_json(sandbox):
    _seed_projects(sandbox)
    proc = _cli(sandbox, ["deterministic-candidates", "--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["status"] == "ok" and data["runtime"] == "claude" and data["days"] == 30
    seq = [c for c in data["candidates"] if c["kind"] == "repeated_sequence"]
    assert seq and seq[0]["sessions_seen"] == 3 and seq[0]["cost_usd"] > 0
    assert (sandbox / "snap" / dc.CACHE_NAME).exists()
    again = json.loads(_cli(sandbox, ["deterministic-candidates", "--json"]).stdout)
    assert again["cached"] is True


def test_cli_standalone_human_output_and_days(sandbox):
    _seed_projects(sandbox)
    proc = _cli(sandbox, ["deterministic-candidates", "--days", "7", "--no-cache"])
    assert proc.returncode == 0, proc.stderr
    assert "measured on your transcripts" in proc.stdout and "repeated_sequence" in proc.stdout
    assert proc.stderr == ""  # no TTY, no progress line


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_cli_progress_line_goes_to_a_tty_stderr_once(sandbox):
    _seed_projects(sandbox)
    master, slave = os.openpty()
    try:
        env = dict(os.environ)
        home = sandbox / "home"
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CODEX_HOME", "HERMES_HOME",
                    "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_RUNTIME"):
            env.pop(key, None)
        env.update({"HOME": str(home), "USERPROFILE": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                    "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(sandbox / "snap"), "PYTHONIOENCODING": "utf-8"})
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / "measure.py"), "deterministic-candidates", "--no-cache"],
                                stdout=subprocess.PIPE, stderr=slave, text=True, env=env, cwd=str(sandbox))
        os.close(slave)
        slave = None
        chunks = []
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:        # EIO once the child closed its end
                break
            if not data:
                break
            chunks.append(data)
        out, _ = proc.communicate(timeout=240)
        err = b"".join(chunks).decode("utf-8", "replace")
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
    assert proc.returncode == 0
    assert err.count("reading session") == 1 and "reading session 1/3" in err   # throttled: one line for 3 files
    assert "reading session" not in out


def test_cli_foreign_runtime_is_explicit(sandbox):
    proc = _cli(sandbox, ["deterministic-candidates", "--json"], runtime="cursor")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["status"] == "not_measurable" and "not measurable on cursor" in data["reason"]


def test_coach_json_carries_the_block_and_survives_breakage(sandbox):
    _seed_projects(sandbox)
    proc = _cli(sandbox, ["coach", "--json"])
    assert proc.returncode == 0, proc.stderr
    data = _json_out(proc.stdout)
    det = data["deterministic_candidates"]
    assert det["basis"] == "measured on your transcripts" and det["status"] == "ok"
    assert any(c["kind"] == "repeated_sequence" for c in det["candidates"])
    # a corrupt cache file and an unreadable transcript must not break coach
    (sandbox / "snap" / dc.CACHE_NAME).write_text("{not json")
    (sandbox / "home" / ".claude" / "projects" / "proj" / "broken.jsonl").write_bytes(b"\xff\xfe\x00garbage\n" * 50)
    proc = _cli(sandbox, ["coach", "--json"])
    assert proc.returncode == 0, proc.stderr
    assert "deterministic_candidates" in _json_out(proc.stdout)


def test_coach_human_output_has_one_summary_line(sandbox):
    _seed_projects(sandbox)
    proc = _cli(sandbox, ["coach"])
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("Deterministic candidates:")]
    assert len(lines) == 1 and "measured on your transcripts" in lines[0]


def test_measure_wrapper_never_raises(monkeypatch):
    import measure
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda **kw: (_ for _ in ()).throw(OSError("disk gone")))
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    res = measure._deterministic_candidates_data(use_cache=False)
    assert res["status"] == "error" and res["candidates"] == []


# ---------------------------------------------------------------------------
# Inline scripts: the launcher alone is not the command
# ---------------------------------------------------------------------------

def _heredoc(body, launcher="python3 -", tag="EOF"):
    return f"{launcher} <<'{tag}'\n{body}\n{tag}"


INLINE_FORMS = {
    "heredoc": lambda body: _heredoc(body),
    "heredoc_cd": lambda body: "cd /Users/alice/proj && " + _heredoc(body),
    "heredoc_unquoted_tag": lambda body: _heredoc(body, launcher="python3", tag="PY").replace("'PY'", "PY"),
    "python_c": lambda body: f'python3 -c "{body}"',
    "bash_c": lambda body: f"bash -c '{body}'",
    "bash_lc": lambda body: f'bash -lc "{body}"',
    "sh_c": lambda body: f'sh -c "{body}"',
    "node_e": lambda body: f'node -e "{body}"',
    "ruby_e": lambda body: f"ruby -e '{body}'",
    "perl_e": lambda body: f"perl -e '{body}'",
    "here_string": lambda body: f'python3 - <<< "{body}"',
}


def _distinct_body(form, i):
    # structurally different scripts: different identifiers, so literal blanking cannot merge them
    return f"import json, sys; value_{'x' * i}_{i} = json.load(open(sys.argv[1])); print(value_{'x' * i}_{i}.keys())"


@pytest.mark.parametrize("form", sorted(INLINE_FORMS))
def test_ten_different_inline_scripts_are_not_a_polling_loop(sandbox, form):
    steps = [_bash(INLINE_FORMS[form](_distinct_body(form, i))) for i in range(10)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    assert "polling_loop" not in kinds(res), res["candidates"]


@pytest.mark.parametrize("form", sorted(INLINE_FORMS))
def test_the_same_inline_script_five_times_is_one_polling_loop(sandbox, form):
    body = "import json, sys; print(json.load(open(sys.argv[1])).keys())"
    steps = [_bash(INLINE_FORMS[form](body)) for _ in range(5)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    poll = [c for c in res["candidates"] if c["kind"] == "polling_loop"]
    assert len(poll) == 1 and poll[0]["times_seen"] == 5
    assert "import json, sys" in poll[0]["example"]


def test_inline_script_example_is_launcher_plus_first_60_chars_of_body(sandbox):
    body = "import json, sys\n" + "total = sum(range(10))  # padding padding padding padding padding\n" * 4
    steps = [_bash(_heredoc(body)) for _ in range(4)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    ex = [c for c in res["candidates"] if c["kind"] == "polling_loop"][0]["example"]
    assert ex.startswith("Bash: python3 -")
    assert "import json, sys total = sum(range(10))" in ex
    assert "padding padding padding padding padding total = sum" not in ex   # cut near 60 chars
    assert len(ex) < 130 and "\n" not in ex


def test_inline_scripts_that_differ_only_in_literals_still_match(sandbox):
    # Still grouped under one normalised key; the label is parameter_sweep
    # because each literal body ran once.
    steps = [_bash(_heredoc(f"print('row {i}', {i * 7})")) for i in range(4)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    assert kinds(res).count("parameter_sweep") == 1


def test_a_shift_operator_in_an_inline_script_does_not_truncate_the_body(sandbox):
    steps = [_bash('python3 -c "value = 1 << 3; ' + ("q" * i) + f'_name_{i} = value"') for i in range(6)]
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")])
    assert "polling_loop" not in kinds(res)


def test_non_launcher_heredocs_keep_the_old_behaviour(sandbox):
    # `cat <<EOF > file` is not a script launcher; it is not what this fix is about
    a = dc.normalise_command("cat <<'EOF' > notes.txt\nhello\nEOF")
    assert a == "cat"


def test_repeated_sequence_does_not_match_unrelated_inline_scripts(sandbox):
    def seq(i):
        return [_bash("git status --short"), _bash(_heredoc(_distinct_body("heredoc", i))), _bash("git diff --stat")]
    paths = [write_claude(sandbox / f"s{i}.jsonl", [seq(i)], f"s{i}") for i in range(3)]
    assert "repeated_sequence" not in kinds(run_dc("claude", paths))


def test_repeated_sequence_matches_the_same_inline_script(sandbox):
    def seq(i):
        return [_bash("git status --short"), _bash(_heredoc("import os; print(os.getcwd())")), _bash("git diff --stat")]
    paths = [write_claude(sandbox / f"s{i}.jsonl", [seq(i)], f"s{i}") for i in range(3)]
    res = run_dc("claude", paths)
    seq = [c for c in res["candidates"] if c["kind"] == "repeated_sequence"]
    assert len(seq) == 1 and "import os; print(os.getcwd())" in seq[0]["example"]


def test_check_only_turn_key_carries_the_script_body():
    km = dc._KeyMaker(dc.default_redactor())
    one = km.build("Bash", {"command": "pytest -q && " + _heredoc("print('a')\nimport alpha")})
    two = km.build("Bash", {"command": "pytest -q && " + _heredoc("print('a')\nimport beta")})
    assert one[0] != two[0]
    same = km.build("Bash", {"command": "pytest -q && " + _heredoc("print('a')\nimport alpha")})
    assert one[0] == same[0]


def test_inline_script_body_secrets_never_reach_output_or_cache(sandbox):
    body = f"import boto3; key = '{AWS}'; auth = '{BEARER}'; print(key)"
    steps = [_bash(_heredoc(body)) for _ in range(4)]
    cache_dir = sandbox / "cache"
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")], use_cache=True, cache_dir=cache_dir)
    assert "polling_loop" in kinds(res)
    blob = json.dumps(res) + (cache_dir / dc.CACHE_NAME).read_text()
    for secret in (AWS, "abcdef0123456789ZYXWVUTSRQ", BEARER):
        assert secret not in blob


def test_unterminated_and_empty_inline_scripts_do_not_crash():
    for cmd in ("python3 - <<'EOF'", "python3 - <<'EOF'\nprint(1)", "python3 -c", "bash -c ''", "python3 - <<<", "node -e"):
        key, shape, _chk, _lit = dc._KeyMaker(dc.default_redactor()).command(cmd)
        assert key.startswith("B:") and shape.startswith("Bash:")


# ---------------------------------------------------------------------------
# Wording: the tokens are what the turns used, not what a script would save
# ---------------------------------------------------------------------------

def _seq_result(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(3)]
    return run_dc("claude", paths)


def test_summary_line_says_what_the_turns_used(sandbox):
    res = _seq_result(sandbox)
    line = res["summary"]
    assert "the turns that ran them used" in line and "API-equivalent" in line
    assert not re.search(r"could be plain code,? [\d,]+ tokens", line)
    assert "measured on your transcripts" in line


def test_no_user_facing_string_claims_the_tokens_are_savings():
    src = (SCRIPTS / "deterministic_candidates.py").read_text(encoding="utf-8")
    assert "workflow pattern(s) could be plain code" not in src
    assert "tokens saved" not in src.lower() and "tokens it saves" not in src.lower()


def test_json_field_docs_say_used_not_saved():
    src = (SCRIPTS / "deterministic_candidates.py").read_text(encoding="utf-8")
    assert "the turns that ran them used" in src
    assert dc.USED_NOT_SAVED_NOTE == (
        "Moving a step to code removes those turns; the saving is at most this much.")


def test_result_carries_the_used_not_saved_note(sandbox):
    res = _seq_result(sandbox)
    assert res["tokens_note"] == dc.USED_NOT_SAVED_NOTE
    assert res["basis"] == "measured on your transcripts"
    assert all(c["basis"] == "measured on your transcripts" for c in res["candidates"])


# ---------------------------------------------------------------------------
# Progress line: throttled, stderr only, silent without a TTY
# ---------------------------------------------------------------------------

def test_progress_is_throttled_to_one_line_per_two_seconds(sandbox):
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(30)]
    seen = []
    run_dc("claude", paths, progress=seen.append)
    assert len(seen) == 1, seen               # a 30-file run takes well under 2 s: only the first line


def test_throttle_prints_first_then_waits_two_seconds():
    clock = {"t": 100.0}
    seen = []
    th = dc.Throttle(seen.append, interval_s=2.0, clock=lambda: clock["t"])
    th("a")
    clock["t"] += 1.9
    th("b")
    clock["t"] += 0.2
    th("c")
    clock["t"] += 5
    th("d")
    assert seen == ["a", "c", "d"]


def test_throttle_without_a_callback_is_a_noop():
    assert dc.Throttle(None)("x") is None


# ---------------------------------------------------------------------------
# Secrets that are not env assignments or known token shapes (review F4)
# ---------------------------------------------------------------------------

def _j(*parts):
    """Join at runtime so no literal here matches a real-secret shape (push protection)."""
    return "".join(parts)


GLPAT = _j("glpat-", "AbCdEfGhIjKlMnOpQrSt")
# (command, fragments that must NOT survive). Every command also gets two innocuous
# follow-ups so it becomes a repeated sequence the detector reports.
LEAKY_COMMANDS = [
    ("curl -u admin:Tr0ub4dor3xyz https://svc.local/api", ["Tr0ub4dor3xyz", "admin:"]),
    ("curl --user admin:Tr0ub4dor3xyz https://svc.local/api", ["Tr0ub4dor3xyz", "admin:"]),
    ("curl --user=admin:Tr0ub4dor3xyz https://svc.local/api", ["Tr0ub4dor3xyz", "admin:"]),
    ("curl -uadmin:Tr0ub4dor3xyz https://svc.local/api", ["Tr0ub4dor3xyz", "admin:"]),
    ("curl --proxy-user pxy:Tr0ub4dor3xyz https://svc.local/api", ["Tr0ub4dor3xyz"]),
    ("curl -u 'admin:Tr0ub4dor3 xyz' https://svc.local/api", ["Tr0ub4dor3"]),
    (f"echo {GLPAT} | docker login -u me --password-stdin registry.example.com", [GLPAT, "AbCdEfGhIjKlMnOpQrSt"]),
    ("echo hunter2hunter2 | docker login -u me --password-stdin registry.example.com", ["hunter2hunter2"]),
    ("echo -n hunter2hunter2 | base64 | docker login --password-stdin r.io", ["hunter2hunter2"]),
    ("printf '%s' Zebra-Quartz-77 | podman login --password-stdin r.io", ["Zebra-Quartz"]),
    ("net use Z: \\\\srv\\share /user:corp\\bob P@ssw0rdWin!", ["P@ssw0rdWin!"]),
    ("net use \\\\srv\\share P@ssw0rdWin! /user:corp\\bob", ["P@ssw0rdWin!"]),
    ("cmdkey /add:srv /user:bob /pass:P@ssw0rdWin!", ["P@ssw0rdWin!"]),
    ("git clone https://bob:Sup3rS3cretPW@git.example.com/x.git", ["Sup3rS3cretPW"]),
    ("lftp ftp://bob:Sup3rS3cretPW@files.example.com/in", ["Sup3rS3cretPW"]),
    ("rsync -a bob:Sup3rS3cretPW@files.example.com:/in ./out", ["Sup3rS3cretPW"]),
    ("deploy --password hunter2hunter2 --env prod", ["hunter2hunter2"]),
    ("deploy --token=Zebra-Quartz-77 --env prod", ["Zebra-Quartz"]),
    ("git config user.email jane.doe@private-company.com && git log --author=jane.doe@private-company.com",
     ["jane.doe@private-company.com", "private-company.com"]),
    ("mail -s hi jane.doe@private-company.com < msg.txt", ["jane.doe@private-company.com"]),
]


@pytest.mark.parametrize("cmd,needles", LEAKY_COMMANDS)
def test_normalise_drops_credentials_and_emails_in_command_shapes(cmd, needles):
    out = dc.normalise_command(cmd)
    for needle in needles:
        assert needle not in out, (needle, out)


@pytest.mark.parametrize("cmd,needles", LEAKY_COMMANDS)
def test_such_commands_never_reach_the_result_or_the_cache(sandbox, cmd, needles):
    def steps():
        return [_bash(cmd), _bash("ls -la"), _bash("git status")]
    paths = [write_claude(sandbox / f"s{i}.jsonl", [steps()], f"leak{i}") for i in range(4)]
    cache_dir = sandbox / "cache"
    res = run_dc("claude", paths, use_cache=True, cache_dir=cache_dir)
    assert res["candidates"], "the sequence should still be found"
    blob = json.dumps(res) + (cache_dir / dc.CACHE_NAME).read_text()
    for needle in needles:
        assert needle not in blob, (needle, [c["example"] for c in res["candidates"]])


@pytest.mark.parametrize("cmd,kept", [
    ("git push -u origin main", ["-u origin", "main"]),
    ("docker run -u 1000:1000 alpine id", ["-u", "alpine"]),
    ("git clone git@github.com:org/repo.git", ["git@github.com"]),
    ("curl -s -o out.json https://api.example.com/v1/items", ["curl", "-o", "https://api.example.com/v1/items"]),
    ("echo done | tee build.log", ["tee build.log"]),
    ("psql -U app -h db -c 'select 1'", ["-U app"]),
    ("ssh deploy@build-box.internal uptime", ["uptime"]),
])
def test_normalise_keeps_the_shape_of_ordinary_commands(cmd, kept):
    out = dc.normalise_command(cmd)
    for fragment in kept:
        assert fragment in out, (fragment, out)


def test_glpat_in_a_snippet_of_an_inline_script_is_redacted(sandbox):
    body = f"h={{'PRIVATE-TOKEN': '{GLPAT}'}}; import requests"
    steps = [_bash(_heredoc(body)) for _ in range(4)]
    cache_dir = sandbox / "cache"
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")], use_cache=True, cache_dir=cache_dir)
    blob = json.dumps(res) + (cache_dir / dc.CACHE_NAME).read_text()
    assert "AbCdEfGhIjKlMnOpQrSt" not in blob


def test_inline_script_snippet_drops_basic_auth_and_emails(sandbox):
    body = "os.system('curl -u admin:Tr0ub4dor3xyz x'); m='jane.doe@private-company.com'"
    steps = [_bash(_heredoc(body)) for _ in range(4)]
    cache_dir = sandbox / "cache"
    res = run_dc("claude", [write_claude(sandbox / "s.jsonl", [steps], "s")], use_cache=True, cache_dir=cache_dir)
    blob = json.dumps(res) + (cache_dir / dc.CACHE_NAME).read_text()
    assert "Tr0ub4dor3xyz" not in blob and "jane.doe@private-company.com" not in blob


def test_secret_env_value_that_is_already_a_redaction_marker_leaves_no_debris():
    out = dc.normalise_command("export GITHUB_TOKEN=[CREDENTIAL REDACTED: GitHub PAT classic] && gh api user")
    assert out == "export GITHUB_TOKEN=<secret> && gh api user"


# The example is the text a human and the model read. It is redacted a second time
# when the candidate is finished; this pins that layer on its own, with key-making
# redaction switched off (a mutant removing the second pass survived the suite).
STRIPE = _j("sk_live_", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def test_example_layer_redacts_even_when_key_making_did_not(sandbox):
    steps = lambda: [_bash(f"stripe-cli {STRIPE}"), _bash("ls -la"), _bash("git status")]  # noqa: E731
    paths = [write_claude(sandbox / f"s{i}.jsonl", [steps()], f"ex{i}") for i in range(4)]
    passthrough = dc.Redactor(lambda text: text)
    keys = dc._KeyMaker(passthrough)
    traces = [dc.extract_claude(p, keys, set(), None) for p in paths]
    traces = [t for t in traces if t is not None]
    assert any(STRIPE in c.example for c in dc.detect_sequences(traces, None)[0]), \
        "fixture must put the secret into the pre-redaction example"
    cands, _totals, _partial = dc.analyse(traces, price, dc.default_redactor())
    assert cands
    assert all(STRIPE not in c["example"] and "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in c["example"] for c in cands)
    assert any("[CREDENTIAL REDACTED" in c["example"] for c in cands)


def test_example_layer_withholds_when_the_redactor_fails_only_there(sandbox):
    steps = lambda: [_bash(f"stripe-cli {STRIPE}"), _bash("ls -la"), _bash("git status")]  # noqa: E731
    paths = [write_claude(sandbox / f"s{i}.jsonl", [steps()], f"ex{i}") for i in range(4)]
    keys = dc._KeyMaker(dc.Redactor(lambda text: text))
    traces = [t for t in (dc.extract_claude(p, keys, set(), None) for p in paths) if t is not None]
    cands, _t, _p = dc.analyse(traces, price, dc.Redactor(None))
    assert cands and all(c["example"] == dc.WITHHELD for c in cands)


def test_stale_cache_entries_from_before_the_scrubbers_are_not_served(sandbox, monkeypatch):
    """A cache written by an older algorithm may hold an unscrubbed example."""
    paths = [write_claude(sandbox / f"s{i}.jsonl", [SEQ(i)], f"s{i}") for i in range(3)]
    files = [(p, p.stat().st_mtime) for p in paths]
    monkeypatch.setattr(dc, "ALGO_VERSION", 2)  # last version that stored unscrubbed examples
    old_key = dc.cache_key("claude", 30, 60, "", files)
    monkeypatch.undo()
    cache_dir = sandbox / "cache"
    dc.cache_store(cache_dir, old_key, {"status": "ok", "candidates": [{"example": "curl -u admin:Tr0ub4dor3xyz x"}]})
    # same inputs, current algorithm: the stored entry must not be a hit
    res = dc.run("claude", files, price, cache_dir=cache_dir, max_sessions=60, use_cache=True, budget_s=30.0)
    assert "Tr0ub4dor3xyz" not in json.dumps(res)
