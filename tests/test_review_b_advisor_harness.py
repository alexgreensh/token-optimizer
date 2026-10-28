"""Review B follow-ups for #287 (advisor usage) and #288 (harness text).

Advisor turns: billing prices each advisor iteration at the advisor's own
model and never drops tokens; keep-warm replay and the cache-rewrite detector
size the prefix from the executor, like the live path. Harness text: more
harness shapes (interrupts, cut-off notices, bare local commands, leading
system reminders, Codex injected context) never become the Active Task, the
topic or a "most expensive prompt".
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

EXECUTOR = "claude-sonnet-5-5"
ADVISOR = "claude-opus-5-5"


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(mod, "_load_pricing_tier", lambda: "anthropic")
    yield mod, tmp_path
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _assistant(usage, ts="2026-10-27T10:00:00Z", req="r1", model=EXECUTOR):
    return {"type": "assistant", "timestamp": ts, "requestId": req,
            "message": {"model": model, "usage": usage,
                        "content": [{"type": "text", "text": "ok"}]}}


def _exec(inp=100, cr=0, cc=0, out=10):
    return {"type": "message", "input_tokens": inp, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc, "output_tokens": out}


def _advisor_usage(advisor_model=ADVISOR):
    """Documented shape: top-level is the executor sum; advisor rides in iterations."""
    advisor = {"type": "advisor_message", "input_tokens": 300_000,
               "output_tokens": 2_000, "cache_read_input_tokens": 50_000}
    if advisor_model is not None:
        advisor["model"] = advisor_model
    return {"input_tokens": 200, "output_tokens": 20,
            "cache_read_input_tokens": 800_000, "cache_creation_input_tokens": 1_000,
            "iterations": [_exec(cr=400_000, cc=500), advisor, _exec(cr=400_000, cc=500)]}


# --------------------------------------------------------------- finding 4

def test_advisor_iteration_is_priced_at_the_advisor_model(m):
    mod, root = m
    path = _write(root / "projects" / "-p" / "s.jsonl", [_assistant(_advisor_usage())])
    turn = mod.parse_session_turns(str(path))[0]
    executor_cost = mod._get_model_cost(EXECUTOR, 200, 20, 800_000, 1_000, per_request=True)
    advisor_cost = mod._get_model_cost(ADVISOR, 300_000, 2_000, 50_000, 0, per_request=True)
    assert advisor_cost != mod._get_model_cost(EXECUTOR, 300_000, 2_000, 50_000, 0, per_request=True)
    assert turn["cost_usd"] == pytest.approx(executor_cost + advisor_cost, abs=1e-6)
    # Executor token fields stay the billed aggregate.
    assert (turn["input_tokens"], turn["cache_read"], turn["cache_creation"]) == (200, 800_000, 1_000)

    session = mod._parse_session_jsonl(str(path))
    breakdown = session["model_usage_breakdown"]
    assert breakdown[ADVISOR]["fresh_input"] == 300_000
    assert breakdown[ADVISOR]["cache_read"] == 50_000
    assert breakdown[ADVISOR]["output"] == 2_000
    assert breakdown[EXECUTOR]["fresh_input"] == 200
    assert session["total_output_tokens"] == 2_020
    assert session["api_calls"] == 1  # the advisor part is not another call


def test_unknown_advisor_model_falls_back_to_executor_price(m):
    mod, root = m
    path = _write(root / "projects" / "-p" / "s.jsonl",
                  [_assistant(_advisor_usage("claude-made-up-9"))])
    turn = mod.parse_session_turns(str(path))[0]
    expected = (mod._get_model_cost(EXECUTOR, 200, 20, 800_000, 1_000, per_request=True)
                + mod._get_model_cost(EXECUTOR, 300_000, 2_000, 50_000, 0, per_request=True))
    assert turn["cost_usd"] == pytest.approx(expected, abs=1e-6)
    breakdown = mod._parse_session_jsonl(str(path))["model_usage_breakdown"]
    assert "claude-made-up-9" not in breakdown
    assert breakdown[EXECUTOR]["fresh_input"] == 300_200


def test_aggregate_that_already_holds_advisor_tokens_is_split_not_doubled(m):
    mod, _ = m
    usage = _advisor_usage()
    usage["input_tokens"] += 300_000
    usage["output_tokens"] += 2_000
    usage["cache_read_input_tokens"] += 50_000
    parts = mod._billed_usage_parts(usage, EXECUTOR)
    assert [p[0] for p in parts] == [EXECUTOR, ADVISOR]
    assert parts[0][1]["inp"] == 200 and parts[1][1]["inp"] == 300_000
    total = {k: sum(p[1][k] for p in parts) for k in mod._USAGE_BUCKET_KEYS}
    assert total["inp"] == usage["input_tokens"]  # nothing dropped, nothing doubled
    assert total["cr"] == usage["cache_read_input_tokens"]


def test_streamed_advisor_records_count_once(m):
    mod, root = m
    usage = _advisor_usage()
    path = _write(root / "projects" / "-p" / "s.jsonl",
                  [_assistant(usage), _assistant(usage)])
    session = mod._parse_session_jsonl(str(path))
    assert session["model_usage_breakdown"][ADVISOR]["fresh_input"] == 300_000
    assert session["api_calls"] == 1


# ------------------------------------------------------------ findings 2, 3

def _rewrite_after_pause(mod, root):
    """Prev turn: advisor aggregate ~2x executor. Resume after 2h re-writes the
    executor's whole prefix in its first iteration (a real paid rewrite)."""
    prev = {"input_tokens": 200, "cache_read_input_tokens": 980_900,
            "cache_creation_input_tokens": 24,
            "iterations": [_exec(cr=490_000), {"type": "advisor_message", "input_tokens": 9},
                           _exec(cr=490_900, cc=24)]}
    resume = {"input_tokens": 200, "cache_read_input_tokens": 489_000,
              "cache_creation_input_tokens": 489_100,
              "iterations": [_exec(cc=489_000), _exec(cr=489_000, cc=100)]}
    path = _write(root / "projects" / "-p" / "s.jsonl", [
        _assistant(prev, ts="2026-10-27T08:00:00Z", req="a"),
        _assistant(resume, ts="2026-10-27T10:00:00Z", req="b")])
    return mod.parse_session_turns(str(path))


def test_keepwarm_replay_uses_executor_prefix_and_sees_the_rewrite(m):
    mod, root = m
    turns = _rewrite_after_pause(mod, root)
    assert turns[0]["ctx_cache"] == 490_924
    now = mod._keepwarm_iso_to_epoch("2026-10-27T13:00:00Z")
    out = mod._keepwarm_reconstruct_session_events(turns, now)
    (event,) = out["events"]
    assert event["prefix_proxy"] == 490_924
    assert event["rewrote"] is True
    # Tail uses the final executor too, matching the live arm proxy.
    assert out["tail"]["prefix_proxy"] == 489_100


def test_rewrite_detector_threshold_uses_executor_prefix(m):
    mod, root = m
    turns = _rewrite_after_pause(mod, root)
    profile = {"effective_ttl_seconds": 3600, "recoverable_kind": "avoidable_behavioral"}
    tokens, cost, _, had = mod._detect_explicit_ttl(
        turns, mod.PRICING_TIERS["anthropic"], profile, dominant_model=EXECUTOR)
    assert had is True
    assert tokens == 489_100  # dollars stay on the billed aggregate
    assert cost > 0


def test_plain_turns_keep_aggregate_prefix(m):
    mod, root = m
    path = _write(root / "projects" / "-p" / "s.jsonl", [
        _assistant({"input_tokens": 1, "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20})])
    turn = mod.parse_session_turns(str(path))[0]
    assert "ctx_cache" not in turn
    assert mod._turn_prefix_cached(turn) == 120
    assert mod._turn_resume_write(turn) == 20


# --------------------------------------------------------------- finding 5

def test_session_baseline_is_context_size_not_advisor_sum(m, monkeypatch):
    mod, root = m
    proj = root / "projects" / "-p"
    usage = {"input_tokens": 200, "cache_read_input_tokens": 800_000,
             "cache_creation_input_tokens": None,
             "iterations": [_exec(cr=400_000), _exec(cr=400_000, cc=91_051)]}
    _write(proj / "s.jsonl", [_assistant(usage)])
    monkeypatch.setattr(mod, "find_projects_dir", lambda: proj)
    assert mod.get_session_baselines()[0]["baseline_tokens"] == 491_151


# ------------------------------------------------- findings 9, 10, 11, 14, 15

@pytest.mark.parametrize("content", [
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
    "Your response above was cut off mid-stream. Please continue from where you stopped.",
    "<command-name>/model</command-name><command-message>model</command-message><command-args></command-args>",
    "<command-name>/context</command-name><command-args></command-args>",
    "<command-name>/exit</command-name><command-args>  </command-args>",
    "<command-name>/init</command-name><command-args></command-args>",
    "<system-reminder>Plan mode is on.</system-reminder>",
    "<system-reminder>unclosed reminder",
])
def test_harness_text_never_becomes_active_task(m, content):
    mod, root = m
    path = _write(root / "s.jsonl", [
        {"type": "user", "message": {"content": "implement caching in api"}},
        {"type": "user", "message": {"content": content}}])
    assert mod._task_user_text({"message": {"content": content}}) == ""
    assert mod._extract_session_state(str(path))["current_step"]["last_user"] == "implement caching in api"


@pytest.mark.parametrize("content, expected", [
    ("<command-name>/model</command-name><command-args>opus</command-args>", "/model opus"),
    ("<command-message>review</command-message><command-name>/review</command-name>", "/review"),
    ("<command-name>/ce:debug</command-name><command-message>ce:debug</command-message>", "/ce:debug"),
    ("<system-reminder>be brief</system-reminder>\n\nfix the login bug", "fix the login bug"),
    ("Your response above was cut off. Please explain why?", "Your response above was cut off. Please explain why?"),
])
def test_real_tasks_are_kept(m, content, expected):
    mod, _ = m
    assert mod._task_user_text({"message": {"content": content}}) == expected


def test_installed_skill_named_like_a_local_command_is_kept(m, monkeypatch):
    mod, _ = m
    monkeypatch.setattr(mod, "_resolve_installed_skill", lambda name: name if name == "context" else None)
    text = "<command-name>/context</command-name><command-args></command-args>"
    assert mod._task_user_text({"message": {"content": text}}) == "/context"


# -------------------------------------------------------------- finding 13

def test_costly_prompts_skip_harness_text(m):
    mod, root = m
    big = {"input_tokens": 50_000, "output_tokens": 5_000}
    small = {"input_tokens": 10, "output_tokens": 1}
    path = _write(root / "s.jsonl", [
        {"type": "user", "message": {"content": "write the parser please"}},
        _assistant(small, req="a"),
        {"type": "user", "isCompactSummary": True,
         "message": {"content": "This session is being continued from a previous one."}},
        _assistant(big, req="b"),
        {"type": "user", "message": {"content": "<local-command-caveat>Caveat: ignore</local-command-caveat>"}},
        _assistant(big, req="c")])
    texts = [p["text"] for p in mod._extract_costly_prompts(str(path), tier="anthropic")]
    assert texts == ["write the parser please"]


# -------------------------------------------------------------- finding 16

def test_meta_records_are_not_counted_as_human_messages(m):
    mod, root = m
    path = _write(root / "projects" / "-p" / "s.jsonl", [
        {"type": "user", "message": {"content": "real prompt"}},
        {"type": "user", "isMeta": True, "message": {"content": "caveat"}},
        {"type": "user", "isCompactSummary": True, "message": {"content": "summary"}},
        _assistant({"input_tokens": 5, "output_tokens": 1})])
    assert mod._parse_session_jsonl(str(path))["message_count"] == 2


# -------------------------------------------------------------- finding 12

def _codex_user(text):
    return {"type": "response_item", "timestamp": "2026-10-27T10:00:00Z",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


def _codex_event(text):
    return {"type": "event_msg", "timestamp": "2026-10-27T10:00:01Z",
            "payload": {"type": "user_message", "message": text}}


INJECTED = [
    "# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>be nice</INSTRUCTIONS>",
    "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>",
    "<user_instructions>always test?</user_instructions>",
]


def test_codex_injected_context_never_becomes_topic_or_task(m, tmp_path):
    mod, _ = m
    codex = mod.codex_session
    records = ([_codex_user(t) for t in INJECTED]
               + [_codex_user("Fix the flaky upload test"), _codex_event("Fix the flaky upload test"),
                  {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": "Looking."}]}},
                  _codex_user("<turn_aborted>\nThe user interrupted the previous turn on purpose.\n</turn_aborted>"),
                  _codex_user("<user_action>stopped</user_action>")])
    path = _write(tmp_path / "rollout.jsonl", records)
    state = codex.extract_session_state(str(path))
    assert state["current_step"]["last_user"] == "Fix the flaky upload test"
    assert state["open_questions"] == []
    assert codex._parse_session_records(iter(records))["topic"] == "Fix the flaky upload test"
    quality = codex.parse_jsonl_for_quality(str(path))
    assert quality is None or quality["topic"] == "Fix the flaky upload test"


def test_codex_topic_prefers_user_message_event(m):
    mod, _ = m
    codex = mod.codex_session
    records = [_codex_user("response item wording"), _codex_event("event wording")]
    assert codex._parse_session_records(iter(records))["topic"] == "event wording"


# --------------------------------------------------------------- finding 1

_TS_RUNNER = r"""
const fs = require('fs'), path = require('path'), Module = require('module');
const ts = require(path.join(process.argv[1], 'node_modules', 'typescript'));
const src = fs.readFileSync(path.join(process.argv[1], 'src', 'jsonlTail.ts'), 'utf8');
const js = ts.transpileModule(src, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020}}).outputText;
const mod = new Module('jsonlTail'); mod.paths = module.paths; mod._compile(js, 'jsonlTail.js');
process.stdout.write(JSON.stringify(mod.exports.parseLatestUsage(fs.readFileSync(0, 'utf8'))));
"""


@pytest.mark.skipif(shutil.which("node") is None
                    or not (REPO / "vscode-extension" / "node_modules" / "typescript").is_dir(),
                    reason="needs node and vscode-extension dev dependencies")
def test_vscode_tail_reads_executor_context_on_advisor_turns():
    usage = {"input_tokens": 200, "cache_creation_input_tokens": 180_827,
             "cache_read_input_tokens": 800_000,
             "iterations": [_exec(cr=400_000, cc=89_776),
                            {"type": "advisor_message", "input_tokens": 494_410},
                            _exec(cr=400_000, cc=91_051)]}
    line = json.dumps({"type": "assistant", "message": {"model": EXECUTOR, "usage": usage}})
    out = subprocess.run(["node", "-e", _TS_RUNNER, str(REPO / "vscode-extension")],
                         input=line + "\n", capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["tokens"] == 491_151
