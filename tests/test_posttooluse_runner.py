#!/usr/bin/env python3
"""Regression tests for the consolidated PostToolUse dispatcher.

The SIX former PostToolUse hooks.json entries (80s of combined declared budget,
six process spawns on the hottest path in the product -- PostToolUse fires on
EVERY tool call) are collapsed into ONE that runs
``hooks/posttooluse_runner.py``:

  1. bash_compress_hook.py --quiet          [Bash]                              15s sync
  2. archive_result.py --quiet              [mcp__.*]                           15s async
  3. archive_result.py --quiet              [Bash|Read|Glob|Grep|Agent]         15s async
  4. context_intel.py --quiet               [Bash|Read|Grep|Glob|mcp__.*]       15s async
  5. read_cache.py --invalidate --quiet     [Edit|Write|MultiEdit|NotebookEdit] 10s sync
  6. measure.py quality-cache --quiet --throttle-only  [the union matcher]      10s sync

These tests pin the six deliverables of that consolidation:
  (a) ONE hooks.json entry replaces six, under the union matcher, synchronous.
  (b) matcher gating is preserved PER TOOL: every subcommand fires on exactly
      the tools its own entry fired on, and on no others.
  (c) failure isolation: one subcommand raising -- or calling sys.exit() --
      never aborts the others, and the hook always exits 0.
  (d) the ONE shared deadline bounds total wall time (a hung subcommand cannot
      run to the host's ceiling).
  (e) stdout order and shape are preserved: at most one JSON document, emitted
      byte-identically in the common single-producer case.
  (f) the throttle-only invariant is intact: a cache MISS never parses a
      transcript, and a not-due tick does no filesystem work at all.

Modelled on tests/test_userpromptsubmit_runner_139.py.

Run: python3 -m pytest tests/test_posttooluse_runner.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS_JSON = HOOKS / "hooks.json"
RUNNER = HOOKS / "posttooluse_runner.py"

UNION_MATCHER = "Bash|Read|Glob|Grep|Agent|Edit|Write|MultiEdit|NotebookEdit|mcp__.*"

# The six commands the consolidation replaces. None of them may survive as a
# separate PostToolUse entry.
FORMER_COMMANDS = (
    "bash_compress_hook.py",
    "archive_result.py",
    "context_intel.py",
    "read_cache.py --invalidate",
    "quality-cache --quiet --throttle-only",
)


def _posttooluse_entries():
    cfg = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    return cfg["hooks"]["PostToolUse"]


def _load_runner(monkeypatch, tmp_path):
    """Import hooks/posttooluse_runner.py fresh, pointed at the repo's scripts."""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    # Keep any lazy measure import isolated from the host's real ~/.claude state.
    # claude_home() honors CLAUDE_CONFIG_DIR only when the directory exists;
    # a missing dir is rejected and falls back to the host's real ~/.claude.
    (tmp_path / "claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    spec = importlib.util.spec_from_file_location("ptu_runner_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _record_subcommands(monkeypatch, runner):
    """Replace every subcommand with a recorder. Returns the ordered call log."""
    calls: list[str] = []
    for attr, label in (
        ("_sub_bash_compress", "bash_compress"),
        ("_sub_archive_result", "archive_result"),
        ("_sub_context_intel", "context_intel"),
        ("_sub_read_cache_invalidate", "read_cache_invalidate"),
        ("_sub_quality_cache", "quality_cache"),
    ):
        monkeypatch.setattr(
            runner, attr, (lambda lbl: lambda payload: calls.append(lbl))(label)
        )
    return calls


def _no_real_deadline(monkeypatch, runner):
    """Don't arm a real watchdog thread inside the test process (it os._exit()s)."""
    monkeypatch.setattr(runner, "_install_runner_deadline", lambda total_seconds=None: None)
    monkeypatch.setattr(runner, "_clear_runner_deadline", lambda: None)


# --------------------------------------------------------------------------- #
# (a) ONE entry replaces six
# --------------------------------------------------------------------------- #


def test_one_posttooluse_entry_replaces_six():
    entries = _posttooluse_entries()
    assert len(entries) == 1, (
        f"expected exactly ONE consolidated PostToolUse entry, found {len(entries)}. "
        "Six separate entries meant six process spawns on every tool call."
    )
    (entry,) = entries
    hooks = entry["hooks"]
    assert len(hooks) == 1
    command = hooks[0]["command"]
    assert "hooks/posttooluse_runner.py" in command, (
        "the single entry must dispatch the consolidated runner"
    )


def test_consolidated_entry_uses_the_union_matcher():
    """The union IS the old entry-6 matcher: entry 6 already matched every tool
    any of the other five did, so the union changes the host-level gate for no
    tool at all. Dropping the matcher entirely would fire on tools that
    previously got nothing."""
    (entry,) = _posttooluse_entries()
    assert entry.get("matcher") == UNION_MATCHER


def test_consolidated_entry_is_synchronous_and_declares_one_timeout():
    """A hook group cannot be half-async, and three of the six subcommands
    cannot be async: bash_compress returns updatedToolOutput, read_cache
    --invalidate races the sync PreToolUse reader, and quality-cache has an
    ungated systemMessage print path. So the group is sync."""
    (entry,) = _posttooluse_entries()
    hook = entry["hooks"][0]
    assert hook.get("async", False) is False, (
        "the consolidated PostToolUse entry must be synchronous"
    )
    assert hook["timeout"] == 10, (
        "expected a single 10s declared timeout replacing 15+15+15+15+10+10=80s"
    )


def test_none_of_the_six_former_commands_survive_as_separate_entries():
    blob = json.dumps(_posttooluse_entries())
    for fragment in FORMER_COMMANDS:
        assert fragment not in blob, (
            f"{fragment!r} is still registered as its own PostToolUse entry; "
            "the runner is supposed to reproduce it in-process"
        )


def test_total_declared_posttooluse_budget_collapsed():
    total = sum(
        h["timeout"] for e in _posttooluse_entries() for h in e["hooks"]
    )
    assert total == 10, f"combined declared PostToolUse budget is {total}s, expected 10s"


# --------------------------------------------------------------------------- #
# (b) matcher gating preserved per tool
# --------------------------------------------------------------------------- #

# Derived directly from the six hooks.json matchers. quality_cache is on every
# row because its matcher IS the union the host already matched.
EXPECTED_BY_TOOL = {
    "Bash": ["bash_compress", "archive_result", "context_intel", "quality_cache"],
    "Read": ["archive_result", "context_intel", "quality_cache"],
    "Glob": ["archive_result", "context_intel", "quality_cache"],
    "Grep": ["archive_result", "context_intel", "quality_cache"],
    "Agent": ["archive_result", "quality_cache"],
    "Edit": ["read_cache_invalidate", "quality_cache"],
    "Write": ["read_cache_invalidate", "quality_cache"],
    "MultiEdit": ["read_cache_invalidate", "quality_cache"],
    "NotebookEdit": ["read_cache_invalidate", "quality_cache"],
    "mcp__server__thing": ["archive_result", "context_intel", "quality_cache"],
}


@pytest.mark.parametrize("tool_name,expected", sorted(EXPECTED_BY_TOOL.items()))
def test_matcher_gating_is_preserved_per_tool(monkeypatch, tmp_path, tool_name, expected):
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    calls = _record_subcommands(monkeypatch, runner)
    monkeypatch.setattr(
        runner, "_read_hook_input", lambda: {"tool_name": tool_name, "session_id": "s1"}
    )

    assert runner.main() == 0
    assert calls == expected, (
        f"tool {tool_name!r}: ran {calls}, expected {expected}. A hook firing on "
        "a tool its matcher excluded is exactly what consolidation must not do."
    )


def test_agent_does_not_get_context_intel(monkeypatch, tmp_path):
    """Explicit: entry 3 (archive_result) included Agent, entry 4
    (context_intel) did NOT. The union matcher includes Agent, so only the
    in-process gate keeps context_intel off it."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    calls = _record_subcommands(monkeypatch, runner)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Agent"})
    runner.main()
    assert "context_intel" not in calls
    assert "archive_result" in calls


def test_edit_does_not_get_bash_compress_or_archive(monkeypatch, tmp_path):
    """An Edit fired only entries 5 and 6 before. It must not gain the Bash
    compressor or the archiver."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    calls = _record_subcommands(monkeypatch, runner)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Edit"})
    runner.main()
    assert "bash_compress" not in calls and "archive_result" not in calls


@pytest.mark.parametrize("tool_name", ["Bash", "mcp__server__thing", "Read", "Agent"])
def test_archive_result_runs_exactly_once_for_both_registrations(
    monkeypatch, tmp_path, tool_name
):
    """archive_result was registered TWICE, under two DISJOINT matchers
    ('mcp__.*' and 'Bash|Read|Glob|Grep|Agent') with a byte-identical command,
    so it ran at most once per tool call. The consolidated alternation must not
    turn that into two runs."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    calls = _record_subcommands(monkeypatch, runner)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": tool_name})
    runner.main()
    assert calls.count("archive_result") == 1


def test_dispatch_order_follows_hooks_json_entry_order(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    calls = _record_subcommands(monkeypatch, runner)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})
    runner.main()
    assert calls == ["bash_compress", "archive_result", "context_intel", "quality_cache"]


# --------------------------------------------------------------------------- #
# (c) failure isolation: Exception AND SystemExit
# --------------------------------------------------------------------------- #


def test_one_subcommand_raising_never_aborts_the_others(monkeypatch, tmp_path, capsys):
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    calls = _record_subcommands(monkeypatch, runner)

    def _boom(_payload):
        raise RuntimeError("simulated archive_result failure")

    monkeypatch.setattr(runner, "_sub_archive_result", _boom)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})

    assert runner.main() == 0, "a subcommand failure must never abort the hook"
    assert calls == ["bash_compress", "context_intel", "quality_cache"]
    err = capsys.readouterr().err
    assert "archive_result failed, continuing" in err, (
        "the failure must be logged to stderr, not swallowed silently"
    )


def test_a_subcommand_calling_sys_exit_never_aborts_the_others(
    monkeypatch, tmp_path, capsys
):
    """measure.py's own dispatch blocks call sys.exit() freely. In six separate
    processes that ended one process; in one shared process it would end all
    five subcommands unless SystemExit is caught alongside Exception."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    calls = _record_subcommands(monkeypatch, runner)

    def _exit(_payload):
        sys.exit(0)

    monkeypatch.setattr(runner, "_sub_bash_compress", _exit)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})

    assert runner.main() == 0
    assert calls == ["archive_result", "context_intel", "quality_cache"], (
        "a subcommand's sys.exit() must not take the rest of the group with it"
    )
    assert "bash_compress_hook failed, continuing" in capsys.readouterr().err


def test_a_raising_subcommand_does_not_suppress_a_later_subcommands_stdout(
    monkeypatch, tmp_path, capsys
):
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    _record_subcommands(monkeypatch, runner)

    def _boom(_payload):
        print('{"hookSpecificOutput": {"partial": true}}', end="")
        raise RuntimeError("dies after writing")

    def _later(_payload):
        print('{"hookSpecificOutput": {"hookEventName": "PostToolUse"}}', end="")

    monkeypatch.setattr(runner, "_sub_bash_compress", _boom)
    monkeypatch.setattr(runner, "_sub_archive_result", _later)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})

    assert runner.main() == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert parsed["hookSpecificOutput"]["partial"] is True


# --------------------------------------------------------------------------- #
# (d) the shared deadline bounds total wall time
# --------------------------------------------------------------------------- #


def test_the_shared_deadline_is_the_only_kill_switch_and_has_margin(
    monkeypatch, tmp_path
):
    runner = _load_runner(monkeypatch, tmp_path)
    # 2s of margin under hook_runtime's silent BUDGET_POSTTOOL_RUNNER backstop,
    # so the runner always self-exits 0 with its buffered stdout emitted rather
    # than being hard-killed mid-write.
    sys.path.insert(0, str(SCRIPTS))
    from hook_runtime import BUDGET_POSTTOOL_RUNNER, resolve_entry_budget

    assert runner._RUNNER_TOTAL_BUDGET <= BUDGET_POSTTOOL_RUNNER - 2.0
    secs, label = resolve_entry_budget("posttooluse_runner", [])
    assert secs == BUDGET_POSTTOOL_RUNNER and label, (
        "module_runner must still arm a silent per-entry backstop for the runner"
    )
    (entry,) = _posttooluse_entries()
    assert BUDGET_POSTTOOL_RUNNER <= entry["hooks"][0]["timeout"] / 2.0


def test_shared_deadline_bounds_total_wall_time_for_a_hung_subcommand(tmp_path):
    """A hung subcommand must not run to the host ceiling. This exercises the
    REAL HookDeadline (its os._exit(0) is uncatchable), in a subprocess."""
    code = f"""
import importlib.util, sys, time
sys.argv = ["posttooluse_runner"]
spec = importlib.util.spec_from_file_location("ptu", {str(RUNNER)!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m._RUNNER_TOTAL_BUDGET = 1.0
m._read_hook_input = lambda: {{"tool_name": "Bash"}}
m._sub_bash_compress = lambda payload: time.sleep(60)
m._sub_archive_result = lambda payload: None
m._sub_context_intel = lambda payload: None
m._sub_quality_cache = lambda payload: None
sys.exit(m.main())
"""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, f"hung hook must still exit 0: {proc.stderr[-2000:]}"
    assert elapsed < 8.0, (
        f"a hung subcommand ran {elapsed:.1f}s against a 1.0s shared deadline; "
        "the six-entry wiring let it run to the host's 15s ceiling"
    )


def test_slow_handlers_cannot_starve_later_quality_cache(tmp_path):
    """Four 0.8s handlers must all start, with over-budget work reported."""
    marker = tmp_path / "handler-starts.log"
    code = f"""
import importlib.util, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location("posttooluse_runner", {str(RUNNER)!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m._RUNNER_TOTAL_BUDGET = 2.0
m._read_hook_input = lambda: {{"tool_name": "Bash"}}

def slow(name):
    def handler(_payload):
        with Path({str(marker)!r}).open("a", encoding="utf-8") as f:
            f.write(name + "\\n")
            f.flush()
        time.sleep(0.8)
    return handler

m._sub_bash_compress = slow("bash_compress")
m._sub_archive_result = slow("archive_result")
m._sub_context_intel = slow("context_intel")
m._sub_quality_cache = slow("quality_cache")
sys.exit(m.main())
"""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    starts = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
    assert starts == [
        "bash_compress",
        "archive_result",
        "context_intel",
        "quality_cache",
    ], f"later handlers were starved: {starts!r}; stderr={proc.stderr!r}"
    assert "budget" in proc.stderr.lower(), (
        "a handler that exhausts its fair-share budget must be visible on stderr"
    )


def test_budget_seed_is_not_consumed_before_first_handler(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)

    class _FixedDeadline:
        def remaining(self):
            return 2.0

    monkeypatch.setattr(runner, "_RUNNER_DEADLINE", _FixedDeadline())
    monkeypatch.setattr(runner, "_SUBCOMMANDS_PENDING", 0)
    assert runner._runner_budget(10.0, subcommand_count_hint=4) == 0.0
    assert runner._runner_budget(10.0) == 0.5, (
        "the first handler must receive 1/4 of the remaining time; seeding "
        "must not consume a handler slot"
    )


def test_budget_exhaustion_skip_is_visible(monkeypatch, tmp_path, capsys):
    runner = _load_runner(monkeypatch, tmp_path)

    class _ExhaustedDeadline:
        def remaining(self):
            return 0.01

        def cancel(self):
            pass

    monkeypatch.setattr(runner, "_RUNNER_DEADLINE", _ExhaustedDeadline())
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})
    monkeypatch.setattr(runner, "_sub_bash_compress", lambda _p: pytest.fail("ran"))
    assert runner.main() == 0
    assert "budget was exhausted before dispatch" in capsys.readouterr().err


def test_deadline_budget_is_shared_not_per_subcommand(monkeypatch, tmp_path):
    """One deadline for the whole runner, seeded with the number of subcommands
    that will ACTUALLY run for this tool -- an Edit runs two, not five, and must
    get half the remaining budget each rather than a fifth."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    _record_subcommands(monkeypatch, runner)
    seeds = []
    real = runner._runner_budget

    def _spy(default_seconds, subcommand_count_hint=None):
        if subcommand_count_hint is not None:
            seeds.append(subcommand_count_hint)
        return real(default_seconds, subcommand_count_hint)

    monkeypatch.setattr(runner, "_runner_budget", _spy)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Edit"})
    runner.main()
    assert seeds == [2], f"budget seeded with {seeds}, expected the 2 matching subcommands"


def test_runner_consumes_hook_runtime_per_entry_budgets(monkeypatch, tmp_path):
    """After consolidation module_runner no longer matches the five original
    PostToolUse rules (it sees module 'posttooluse_runner'), so the runner must
    consume them itself -- otherwise the evidence-based per-entry table is
    orphaned by the very change that was supposed to fix the latency."""
    runner = _load_runner(monkeypatch, tmp_path)
    sys.path.insert(0, str(SCRIPTS))
    from hook_runtime import BUDGET_POSTTOOL, BUDGET_POSTTOOL_MEASURE

    sentinel = 99.0
    assert runner._entry_budget("bash_compress_hook", ["--quiet"], sentinel) == BUDGET_POSTTOOL
    assert runner._entry_budget("archive_result", ["--quiet"], sentinel) == BUDGET_POSTTOOL
    assert runner._entry_budget("context_intel", ["--quiet"], sentinel) == BUDGET_POSTTOOL
    assert (
        runner._entry_budget("read_cache", ["--invalidate", "--quiet"], sentinel)
        == BUDGET_POSTTOOL
    )
    assert (
        runner._entry_budget(
            "measure", ["quality-cache", "--quiet", "--throttle-only"], sentinel
        )
        == BUDGET_POSTTOOL_MEASURE
    )
    # An unbudgeted entry keeps the caller's fallback rather than silently 0.
    assert runner._entry_budget("nope", [], sentinel) == sentinel


# --------------------------------------------------------------------------- #
# (e) stdout order and shape
# --------------------------------------------------------------------------- #


def test_single_json_producer_is_emitted_byte_identically(monkeypatch, tmp_path, capsys):
    """The common case: exactly one subcommand writes JSON, and the host must
    see precisely the bytes the standalone hook produced."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    _record_subcommands(monkeypatch, runner)
    payload = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": {
                    "stdout": "compressed",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                },
            }
        }
    )
    monkeypatch.setattr(
        runner, "_sub_bash_compress", lambda _p: print(payload)
    )
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})
    runner.main()
    assert capsys.readouterr().out == payload + "\n"


def test_two_json_producers_collapse_into_one_document(monkeypatch, tmp_path, capsys):
    """bash_compress (updatedToolOutput, Bash only) and archive_result
    (updatedMCPToolOutput, mcp only) are mutually exclusive today. If that ever
    changes, two JSON documents on one stream is unparseable -- they must merge
    into one object, never concatenate."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    _record_subcommands(monkeypatch, runner)
    monkeypatch.setattr(
        runner,
        "_sub_bash_compress",
        lambda _p: print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedToolOutput": {"stdout": "a"},
                    }
                }
            )
        ),
    )
    monkeypatch.setattr(
        runner,
        "_sub_archive_result",
        lambda _p: print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedMCPToolOutput": "pointer",
                    }
                }
            )
        ),
    )
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})
    runner.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)  # ONE document, or this raises
    hso = parsed["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert hso["updatedToolOutput"] == {"stdout": "a"}
    assert hso["updatedMCPToolOutput"] == "pointer"


def test_plain_text_alone_is_passed_through_unchanged(monkeypatch, tmp_path, capsys):
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    _record_subcommands(monkeypatch, runner)
    monkeypatch.setattr(
        runner, "_sub_bash_compress", lambda _p: print("[Token Optimizer] a notice")
    )
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})
    runner.main()
    assert capsys.readouterr().out == "[Token Optimizer] a notice\n"


def test_plain_text_beside_json_never_corrupts_the_document(
    monkeypatch, tmp_path, capsys
):
    """A '[Token Optimizer] ...' line starts with '[' and reads as the start of
    a JSON array. Concatenated with a real JSON object on one shared stream it
    breaks the parse for BOTH."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    _record_subcommands(monkeypatch, runner)
    monkeypatch.setattr(
        runner, "_sub_bash_compress", lambda _p: print("[Token Optimizer] advisory")
    )
    monkeypatch.setattr(
        runner,
        "_sub_archive_result",
        lambda _p: print(json.dumps({"hookSpecificOutput": {"updatedMCPToolOutput": "p"}})),
    )
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Bash"})
    runner.main()
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["hookSpecificOutput"]["updatedMCPToolOutput"] == "p"
    assert "advisory" in captured.err, (
        "diverted plain text must stay visible on stderr, not vanish"
    )


def test_stdin_is_read_once_with_the_largest_of_the_six_caps():
    """Six processes each got their own stdin from the host; one process reads
    once. The cap must be the LARGEST of the six, or bash_compress and
    archive_result start seeing truncated payloads."""
    sys.path.insert(0, str(SCRIPTS))
    import archive_result

    spec = importlib.util.spec_from_file_location("ptu_cap_check", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod._STDIN_MAX_BYTES >= archive_result._STDIN_MAX_BYTES
    # bash_compress_hook.main() reads 5 MB.
    assert mod._STDIN_MAX_BYTES >= 5_242_880


def test_every_subcommand_sees_the_same_payload(monkeypatch, tmp_path):
    """One stdin, one payload, handed to all of them -- and a subcommand
    mutating its copy must not corrupt the next one's."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    seen = []
    for attr in (
        "_sub_bash_compress",
        "_sub_archive_result",
        "_sub_context_intel",
        "_sub_quality_cache",
    ):
        monkeypatch.setattr(runner, attr, lambda p: seen.append(dict(p)))
    payload = {"tool_name": "Bash", "session_id": "s9", "tool_response": "x" * 100}
    monkeypatch.setattr(runner, "_read_hook_input", lambda: payload)
    runner.main()
    assert len(seen) == 4
    assert all(s == payload for s in seen)


def test_shared_stdin_patch_reaches_the_real_hook_modules(monkeypatch, tmp_path):
    """bash_compress_hook imports read_stdin_hook_input INSIDE main(); the
    others bind it at module import. Patching hook_io before either happens is
    what makes one stdin serve all of them."""
    runner = _load_runner(monkeypatch, tmp_path)
    sys.path.insert(0, str(SCRIPTS))
    import hook_io

    original = hook_io.read_stdin_hook_input
    try:
        runner._install_shared_stdin({"tool_name": "Bash", "session_id": "s"})
        from hook_io import read_stdin_hook_input as patched

        assert patched(5_242_880) == {"tool_name": "Bash", "session_id": "s"}
        assert patched(max_bytes=1_000_000)["tool_name"] == "Bash"
    finally:
        hook_io.read_stdin_hook_input = original


# --------------------------------------------------------------------------- #
# (f) the throttle-only invariant
# --------------------------------------------------------------------------- #


def test_quality_cache_is_called_with_the_invariant_kwargs(monkeypatch, tmp_path):
    """tests/test_hook_runtime_parity.py::
    test_throttle_only_cache_miss_never_parses_transcript enforces the invariant
    inside quality_cache(), armed by pure_time_throttle=True AND force=False.
    The runner must pass exactly those, or the invariant silently stops
    applying to the only path that fires on every tool call."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    monkeypatch.setattr(runner, "_delegate_to_quality_cache_gate", lambda hi: False)

    seen = {}

    class _FakeMeasure:
        def _daemon_midsession_pulse(self):
            pass

        def quality_cache(self, **kw):
            seen.update(kw)

        def evaluate_cohort_tripwire(self):
            pass

    fake = _FakeMeasure()
    monkeypatch.setattr(runner, "_measure", lambda: fake)
    monkeypatch.setattr(runner, "_quality_cache_self_heal", lambda: None)
    monkeypatch.setattr(runner, "_throttle_tick_due", lambda payload, throttle=120: True)
    monkeypatch.setattr(
        runner,
        "_read_hook_input",
        lambda: {
            "tool_name": "Edit",
            "session_id": "sess-ptu",
            "transcript_path": "/tmp/t.jsonl",
        },
    )
    monkeypatch.setattr(runner, "_sub_read_cache_invalidate", lambda _p: None)

    assert runner.main() == 0
    assert seen == {
        "throttle_seconds": 120,
        "warn_threshold": 70,
        "quiet": True,
        "session_jsonl": "/tmp/t.jsonl",
        "force": False,
        "pure_time_throttle": True,
        "session_id": "sess-ptu",
        "warn": False,
    }


def test_throttle_only_cache_miss_never_parses_a_transcript_through_the_runner(
    monkeypatch, tmp_path
):
    """The invariant, end to end through the runner against the REAL
    measure.quality_cache: no cache file -> no transcript parse, no cache
    written. PostToolUse deliberately never bootstraps the cache."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    monkeypatch.setattr(runner, "_delegate_to_quality_cache_gate", lambda hi: False)
    measure = runner._measure()

    transcript = tmp_path / "session-ptu.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    cache_dir = tmp_path / "qcache"
    monkeypatch.setattr(measure, "QUALITY_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        measure,
        "_parse_jsonl_for_quality",
        lambda _p: pytest.fail("cache miss fell through to transcript parsing"),
    )
    monkeypatch.setattr(measure, "_daemon_midsession_pulse", lambda: None)
    monkeypatch.setattr(runner, "_quality_cache_self_heal", lambda: None)
    monkeypatch.setattr(measure, "evaluate_cohort_tripwire", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_throttle_tick_due", lambda payload, throttle=120: True)
    monkeypatch.setattr(
        runner,
        "_read_hook_input",
        lambda: {"tool_name": "Read", "transcript_path": str(transcript)},
    )
    monkeypatch.setattr(runner, "_sub_archive_result", lambda _p: None)
    monkeypatch.setattr(runner, "_sub_context_intel", lambda _p: None)

    assert runner.main() == 0
    assert not cache_dir.exists(), "PostToolUse must never bootstrap the quality cache"


def test_a_not_due_tick_touches_nothing(monkeypatch, tmp_path):
    """measure.py's own dispatch answers the throttle question BEFORE the
    daemon pulse, the settings.json self-heal and the config.json read
    (`if not due: sys.exit(0)` comes first). Two file reads on every tool call
    is exactly the cost that ordering exists to avoid, so the runner keeps it."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    monkeypatch.setattr(runner, "_delegate_to_quality_cache_gate", lambda hi: False)
    touched = []
    monkeypatch.setattr(
        runner, "_measure", lambda: pytest.fail("not-due tick imported measure work")
    )
    monkeypatch.setattr(
        runner, "_quality_cache_self_heal", lambda: touched.append("self_heal")
    )
    monkeypatch.setattr(runner, "_throttle_tick_due", lambda payload, throttle=120: False)
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Grep"})
    monkeypatch.setattr(runner, "_sub_archive_result", lambda _p: None)
    monkeypatch.setattr(runner, "_sub_context_intel", lambda _p: None)

    assert runner.main() == 0
    assert touched == [], "a not-due tick must not read settings.json or config.json"


def test_throttle_gate_prefers_a_measure_free_implementation(monkeypatch, tmp_path):
    """The import-diet seam. When hook_runtime grows a measure-free
    quality_cache_tick_due, the runner must use it and never import measure on
    a not-due tick -- that is the whole 682ms-cold win. Until then the fallback
    to measure._quality_cache_tick_due is authoritative."""
    runner = _load_runner(monkeypatch, tmp_path)
    sys.path.insert(0, str(SCRIPTS))
    import hook_runtime

    seen = {}

    def _gate(throttle_seconds, filepath, session_id):
        seen["args"] = (throttle_seconds, filepath, session_id)
        return False

    monkeypatch.setattr(hook_runtime, "quality_cache_tick_due", _gate, raising=False)
    monkeypatch.setattr(
        runner, "_measure", lambda: pytest.fail("the seam did not avoid the measure import")
    )

    due = runner._throttle_tick_due(
        {"transcript_path": "/tmp/x.jsonl", "session_id": "s"}, 120
    )
    assert due is False
    assert seen["args"] == (120, "/tmp/x.jsonl", "s")


def test_throttle_gate_falls_back_to_measure_when_no_seam_exists(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    sys.path.insert(0, str(SCRIPTS))
    import hook_runtime

    monkeypatch.delattr(hook_runtime, "quality_cache_tick_due", raising=False)

    calls = []

    class _FakeMeasure:
        def _quality_cache_tick_due(self, throttle_seconds, filepath=None, session_id=None):
            calls.append((throttle_seconds, filepath, session_id))
            return True

    monkeypatch.setattr(runner, "_measure", lambda: _FakeMeasure())
    assert runner._throttle_tick_due({"transcript_path": "/t.jsonl"}, 120) is True
    assert calls == [(120, "/t.jsonl", None)]


def test_throttle_gate_fails_open(monkeypatch, tmp_path):
    """A broken gate must degrade to 'ask the real quality_cache()', which
    enforces the throttle itself -- never to a permanently frozen statusline."""
    runner = _load_runner(monkeypatch, tmp_path)
    sys.path.insert(0, str(SCRIPTS))
    import hook_runtime

    def _broken(*_a, **_k):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(hook_runtime, "quality_cache_tick_due", _broken, raising=False)
    assert runner._throttle_tick_due({}, 120) is True


# --------------------------------------------------------------------------- #
# (g) composing with the import diet (quality_cache_gate.py)
# --------------------------------------------------------------------------- #


def test_quality_cache_delegates_to_the_gate_module_when_present(monkeypatch, tmp_path):
    """The import diet landed quality_cache_gate.py as a standalone hook script
    built to REPLACE this event's `measure.py quality-cache --throttle-only`
    entry -- the same entry this consolidation folds in. They must compose: the
    runner delegates to the gate wholesale, so the gate stays the single source
    of truth for the throttle decision and the runner inherits its cold-import
    win instead of racing it."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)

    called = []
    gate = type(sys)("quality_cache_gate")
    gate.main = lambda: called.append("gate")
    monkeypatch.setitem(sys.modules, "quality_cache_gate", gate)
    # If delegation works, NOTHING below the gate may run.
    monkeypatch.setattr(
        runner, "_measure", lambda: pytest.fail("delegation still imported measure")
    )
    monkeypatch.setattr(
        runner,
        "_throttle_tick_due",
        lambda *a, **k: pytest.fail("the runner re-asked a question the gate owns"),
    )
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Grep"})
    monkeypatch.setattr(runner, "_sub_archive_result", lambda _p: None)
    monkeypatch.setattr(runner, "_sub_context_intel", lambda _p: None)

    assert runner.main() == 0
    assert called == ["gate"], "the gate module must own the quality-cache subcommand"


def test_quality_cache_falls_back_to_the_inline_path_without_the_gate_module(
    monkeypatch, tmp_path
):
    """A tree where the gate module has not landed must keep working on the
    tested inline path -- the failure mode is 'no speedup', never 'no tick'."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    monkeypatch.setitem(sys.modules, "quality_cache_gate", None)  # forces ImportError

    ran = []
    monkeypatch.setattr(runner, "_throttle_tick_due", lambda *a, **k: ran.append("gate") or True)
    monkeypatch.setattr(runner, "_quality_cache_self_heal", lambda: None)

    class _FakeMeasure:
        def _daemon_midsession_pulse(self):
            pass

        def quality_cache(self, **kw):
            ran.append("quality_cache")

        def evaluate_cohort_tripwire(self):
            pass

    monkeypatch.setattr(runner, "_measure", lambda: _FakeMeasure())
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {"tool_name": "Grep"})
    monkeypatch.setattr(runner, "_sub_archive_result", lambda _p: None)
    monkeypatch.setattr(runner, "_sub_context_intel", lambda _p: None)

    assert runner.main() == 0
    assert ran == ["gate", "quality_cache"]


def test_a_gate_module_without_main_does_not_swallow_the_tick(monkeypatch, tmp_path):
    """Defensive: if the gate lands with a shape this runner does not recognise,
    delegation must decline and the inline path must still run the tick. A
    silently skipped tick freezes the statusline for the whole session."""
    runner = _load_runner(monkeypatch, tmp_path)
    gate = type(sys)("quality_cache_gate")  # no main()
    monkeypatch.setitem(sys.modules, "quality_cache_gate", gate)
    assert runner._delegate_to_quality_cache_gate({}) is False


def test_invariant_holds_through_the_real_gate_module(monkeypatch, tmp_path):
    """The production path now delegates to the REAL quality_cache_gate.py, so
    the throttle-only invariant has to hold through it, not just through the
    inline fallback: no throttle marker (cache miss) must mean no transcript
    parse and no cache bootstrap on the per-tool-call path."""
    runner = _load_runner(monkeypatch, tmp_path)
    _no_real_deadline(monkeypatch, runner)
    sys.path.insert(0, str(SCRIPTS))
    import quality_cache_gate

    cache_dir = tmp_path / "token-optimizer"
    cache_dir.mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "session-real-gate.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    monkeypatch.setattr(
        quality_cache_gate, "_resolve_quality_cache_dir", lambda: cache_dir
    )
    monkeypatch.setattr(
        runner, "_measure", lambda: pytest.fail("a cache miss imported measure.py")
    )
    monkeypatch.setattr(runner, "_read_hook_input", lambda: {
        "tool_name": "Grep", "transcript_path": str(transcript), "session_id": "s-real"
    })
    monkeypatch.setattr(runner, "_sub_archive_result", lambda _p: None)
    monkeypatch.setattr(runner, "_sub_context_intel", lambda _p: None)

    assert runner.main() == 0
    assert not list(cache_dir.glob("quality-cache-*.json")), (
        "PostToolUse must never bootstrap the quality cache, gate or no gate"
    )


def test_delegation_gives_the_gate_the_payload_and_the_quiet_flag(monkeypatch, tmp_path):
    """The gate reads its own stdin and parses its own sys.argv. This runner has
    already drained stdin, and module_runner leaves argv as
    ['posttooluse_runner'] -- so without repair the gate would see an empty
    payload (falling back to the GLOBAL throttle marker, one window shared
    across every concurrent session) and quiet=False."""
    runner = _load_runner(monkeypatch, tmp_path)
    sys.path.insert(0, str(SCRIPTS))
    import quality_cache_gate

    seen = {}

    def _fake_main():
        seen["argv"] = list(sys.argv)
        seen["payload"] = quality_cache_gate._read_stdin_payload()
        return 0

    monkeypatch.setattr(quality_cache_gate, "main", _fake_main)
    payload = {"tool_name": "Grep", "transcript_path": "/t.jsonl", "session_id": "sid-9"}
    argv_before = list(sys.argv)

    assert runner._delegate_to_quality_cache_gate(payload) is True
    assert seen["payload"] == payload, "the gate must receive the already-read payload"
    assert "--quiet" in seen["argv"], "the standalone entry passed --quiet; keep it"
    assert seen["argv"][0] == "quality_cache_gate"
    # And nothing leaks out of the call.
    assert sys.argv == argv_before
    assert quality_cache_gate._read_stdin_payload.__name__ != "<lambda>"


def test_delegation_restores_argv_even_when_the_gate_raises(monkeypatch, tmp_path):
    runner = _load_runner(monkeypatch, tmp_path)
    sys.path.insert(0, str(SCRIPTS))
    import quality_cache_gate

    def _boom():
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(quality_cache_gate, "main", _boom)
    argv_before = list(sys.argv)
    reader_before = quality_cache_gate._read_stdin_payload
    with pytest.raises(RuntimeError):
        runner._delegate_to_quality_cache_gate({"session_id": "s"})
    assert sys.argv == argv_before
    assert quality_cache_gate._read_stdin_payload is reader_before
