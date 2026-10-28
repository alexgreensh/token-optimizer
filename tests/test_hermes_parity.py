"""Hermes parity: every feature Claude Code and Codex get, through Hermes hooks.

Most tests here drive the real plugin (hermes/__init__.py) with fake Hermes hook
kwargs shaped like upstream's (hermes-agent hermes_cli/plugins.py and
website/docs/user-guide/features/hooks.md at commit 7ee6d99c), and let it run
the real engines in child processes against a throwaway home. A few use a
recording stand-in for the bridge where only the plugin's own decisions are
under test (which checkpoint, when).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HERMES_PAYLOAD = REPO / "hermes"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# hermes_cli/plugins.py VALID_HOOKS at upstream commit 7ee6d99c (parsed as text).
UPSTREAM_VALID_HOOKS = frozenset({
    "agent_loop_stopped", "api_request_error", "gateway_platform_event", "kanban_task_blocked",
    "kanban_task_claimed", "kanban_task_completed", "on_human_input_request",
    "on_human_input_resolved", "on_interim_message", "on_kanban_dispatch_tick",
    "on_kanban_task_updated", "on_kanban_worker_exited", "on_kanban_worker_spawned",
    "on_kanban_worker_stale_claim", "on_room_member_activity", "on_session_delete",
    "on_session_end", "on_session_finalize", "on_session_reset", "on_session_start",
    "on_skill_lifecycle", "on_stream_delta", "on_stream_end", "on_stream_start",
    "post_api_request", "post_approval_response", "post_auxiliary_call", "post_compaction",
    "post_gateway_admission", "post_llm_call", "post_tool_call", "pre_api_request",
    "pre_approval_request", "pre_auxiliary_call", "pre_command", "pre_gateway_dispatch",
    "pre_llm_call", "pre_tool_call", "pre_transcription", "pre_verify", "subagent_start",
    "subagent_stop", "transform_api_error_classification", "transform_llm_output",
    "transform_terminal_output", "transform_tool_result",
})

FIND_COMMAND = "find . -name '*.py'"
FIND_OUTPUT = "".join(f"./src/pkg{i % 7}/module_{i}.py\n" for i in range(600))


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Throwaway home with an empty Hermes home; no runtime overrides leak in."""
    h = tmp_path / "home"
    (h / ".hermes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for key in ("HERMES_HOME", "TOKEN_OPTIMIZER_RUNTIME", "TOKEN_OPTIMIZER_SNAPSHOT_DIR",
                "TOKEN_OPTIMIZER_PLUGIN_DATA", "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT",
                "CLAUDE_CONFIG_DIR", "CLAUDE_SESSION_ID", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    for key in [k for k in os.environ if k.startswith("TOKEN_OPTIMIZER_HERMES_")]:
        monkeypatch.delenv(key, raising=False)
    return h


def _load_plugin(path: Path = HERMES_PAYLOAD / "__init__.py", name: str = "to_hermes_parity_plugin"):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plugin(home):
    module = _load_plugin()
    # Deterministic host timeout: no Hermes config is importable here.
    module._hook_timeout_cache["value"] = 30.0
    yield module
    sys.modules.pop("to_hermes_parity_plugin", None)


class FakeCtx:
    """Stand-in for Hermes's PluginContext: records what register() does."""

    def __init__(self):
        self.hooks: dict[str, object] = {}
        self.commands: dict[str, object] = {}
        self.patterns: list[str] = []

    def register_hook(self, name, callback, **_kw):
        assert name in UPSTREAM_VALID_HOOKS, f"{name} is not a Hermes hook"
        assert callable(callback)
        self.hooks[name] = callback

    def register_command(self, name, handler, **_kw):
        self.commands[name] = handler

    def register_cli_command(self, name, **_kw):
        pass

    def register_redaction_patterns(self, patterns):
        self.patterns = list(patterns)
        return len(self.patterns)


class RecordingBridge:
    """Bridge stand-in that records spawns instead of starting processes."""

    def __init__(self):
        self.checkpoints: list[tuple[str, str, float | None]] = []
        self.rollups: list[str] = []
        self.tool_hook_calls: list[tuple[str, dict]] = []
        self.context_calls: list[list[str]] = []
        self.next_tool_result: dict | None = None

    def spawn_checkpoint(self, session_id, trigger, *, fill_pct=None, cwd=None):
        self.checkpoints.append((session_id, trigger, fill_pct))
        return True

    def run_rollup(self, session_id="", platform="hermes", reason="", context_tokens=None, skills=None):
        self.rollups.append(session_id)
        self.rollup_skills = skills

    def run_tool_hooks(self, event, payload, timeout=8.0):
        self.tool_hook_calls.append((event, payload))
        return self.next_tool_result

    def run_measure_context(self, args, payload, timeout=10.0):
        self.context_calls.append(list(args))
        return ""

    def run_turn_context(self, payload, timeout=10.0):
        self.context_calls.append(["hermes-turn-context", payload])
        return {"parts": ["LEAN NUDGE"], "verbosity": True} if "fill_pct" in payload else {"parts": []}

    def to_claude_payload(self, *a, **k):
        import hermes_hook_bridge

        return hermes_hook_bridge.to_claude_payload(*a, **k)

    def scripts_dir(self):
        return SCRIPTS


@pytest.fixture()
def recorder(plugin, monkeypatch):
    rec = RecordingBridge()
    monkeypatch.setattr(plugin, "_import_bridge", lambda: rec)
    return rec


def _wait_for(predicate, timeout: float = 30.0, interval: float = 0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _make_state_db(home: Path, session_id: str, *, cwd: str | None = None, with_messages: bool = True):
    """A Hermes state.db with one session and a short, realistic transcript."""
    db = home / ".hermes" / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, model TEXT, started_at REAL, ended_at REAL, end_reason TEXT,
            message_count INTEGER, tool_call_count INTEGER, api_call_count INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL,
            cost_status TEXT, title TEXT, cwd TEXT);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
            content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0);
        """
    )
    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, "claude-sonnet-4-5", now - 600, now, "user_exit", 6, 3, 4,
         42_000, 3_000, 30_000, 2_000, 0, 0.21, "estimated", "parser refactor", cwd),
    )
    if with_messages:
        def call(name, args, cid):
            return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

        rows = [
            ("user", "Refactor the parser module and fix the failing tokenizer test", None),
            ("assistant", "We decided to use a recursive descent parser instead of regex.",
             [call("read_file", {"path": "/proj/parser.py"}, "c1")]),
            ("tool", "line 1\nline 2", None),
            ("assistant", "Patching now.",
             [call("patch", {"path": "/proj/tokenizer.py", "old_string": "a", "new_string": "b"}, "c2"),
              call("todo_list", {"todos": [{"content": "fix tokenizer", "status": "in_progress"}]}, "c3")]),
            ("assistant", "Parser done. Next step: run the tokenizer tests.", None),
        ]
        for role, content, calls in rows:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
                (session_id, role, content, json.dumps(calls) if calls else None, now),
            )
    conn.commit()
    conn.close()


def _checkpoints(home: Path, session_id: str) -> list[Path]:
    return sorted(home.joinpath(".hermes").rglob(f"checkpoints/{session_id}-*.md"))


def _clean_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOKEN_OPTIMIZER_RUNTIME")}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHONUTF8": "1"})
    env.pop("HERMES_HOME", None)
    return env


# ---------------------------------------------------------------------------
# Registration, manifest, doctor
# ---------------------------------------------------------------------------

def _manifest_hooks() -> list[str]:
    import hermes_install

    return hermes_install._declared_hooks(HERMES_PAYLOAD / "plugin.yaml")


def test_plugin_registers_every_declared_hook(plugin):
    ctx = FakeCtx()
    plugin.register(ctx)
    import hermes_doctor

    declared = set(_manifest_hooks())
    assert set(ctx.hooks) == declared == hermes_doctor.EXPECTED_HOOKS == {n for n, _ in plugin.HOOKS}
    # The parity surface, not just the original four.
    for name in ("pre_tool_call", "transform_terminal_output", "transform_tool_result",
                 "post_compaction", "pre_auxiliary_call", "api_request_error"):
        assert name in ctx.hooks
    assert "token-optimizer" in ctx.commands


def test_manifest_version_bumped_for_new_features():
    text = (HERMES_PAYLOAD / "plugin.yaml").read_text(encoding="utf-8")
    assert re.search(r'^version:\s*"0\.2\.0"\s*$', text, re.MULTILINE)


def test_register_survives_a_host_that_rejects_one_hook(plugin):
    class PickyCtx(FakeCtx):
        def register_hook(self, name, callback, **kw):
            if name == "post_compaction":  # an older Hermes without this hook
                raise ValueError("unknown hook")
            super().register_hook(name, callback)

    ctx = PickyCtx()
    plugin.register(ctx)
    assert "post_compaction" not in ctx.hooks
    assert "transform_terminal_output" in ctx.hooks


def test_redaction_patterns_shared_with_hermes(plugin):
    ctx = FakeCtx()
    plugin.register(ctx)
    assert any(p.startswith("AKIA") for p in ctx.patterns)
    assert any(p.startswith("ghp_") for p in ctx.patterns)
    # Every pattern meets Hermes's acceptance rules, so none is refused noisily.
    assert all(plugin._literal_prefix_ok(p) for p in ctx.patterns)


@pytest.mark.parametrize("pattern, ok", [
    ("sk-[a-z]{20,}", True),
    ("abc(?:x|y)+", True),
    ("ab|.*", False),          # top-level alternation
    ("ab(a+)+", False),        # nested unbounded repeat
    ("a[0-9]+", False),        # one literal character
    ("[A-Z]{4}", False),       # no literal prefix
])
def test_redaction_filter_mirrors_hermes_rules(plugin, pattern, ok):
    assert plugin._literal_prefix_ok(pattern) is ok


def test_custom_patterns_from_the_hermes_home_reach_hermes(plugin, home, monkeypatch):
    custom = home / ".hermes" / "token-optimizer"
    custom.mkdir(parents=True, exist_ok=True)
    (custom / "redact-patterns.json").write_text(
        json.dumps({"patterns": [r"medx_[A-Za-z0-9]{32}"]}), encoding="utf-8")
    # A Claude Code custom file must not leak into Hermes.
    cc = home / ".claude" / "token-optimizer"
    cc.mkdir(parents=True)
    (cc / "redact-patterns.json").write_text(
        json.dumps({"patterns": [r"claudeonly_[a-z]{12}"]}), encoding="utf-8")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert r"medx_[A-Za-z0-9]{32}" in ctx.patterns
    assert not any(p.startswith("claudeonly_") for p in ctx.patterns)


def test_redaction_can_be_turned_off(plugin, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_HERMES_REDACTION", "0")
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.patterns == []


def _install(home: Path) -> Path:
    env = _clean_env(home)
    subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "hermes-install", "--enable"],
                   check=True, env=env, capture_output=True, text=True, timeout=120)
    return home / ".hermes" / "plugins" / "token-optimizer"


def _doctor(home: Path) -> dict:
    proc = subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "hermes-doctor", "--json"],
                          env=_clean_env(home), capture_output=True, text=True, timeout=180)
    return {c["name"]: c for c in json.loads(proc.stdout)["checks"]}


def test_doctor_reports_every_hook_registered(home):
    _install(home)
    checks = _doctor(home)
    reg = checks["Registered hooks"]
    assert reg["status"] == "OK", reg
    for name in _manifest_hooks():
        assert name in reg["detail"]
    assert checks["Engine scripts"]["status"] == "OK"
    assert checks["Credential redaction"]["status"] == "OK"
    assert checks["Installed copy up to date"]["status"] == "OK"


def test_doctor_names_a_hook_that_is_not_registered(home):
    plugin_dir = _install(home)
    init = plugin_dir / "__init__.py"
    text = init.read_text(encoding="utf-8")
    broken = text.replace('    ("post_compaction", on_post_compaction),\n', "", 1)
    assert broken != text
    init.write_text(broken, encoding="utf-8")
    checks = _doctor(home)
    assert checks["Registered hooks"]["status"] == "FAIL"
    assert "post_compaction" in checks["Registered hooks"]["detail"]
    assert checks["Installed copy up to date"]["status"] == "WARN"


# ---------------------------------------------------------------------------
# Terminal output: compression, archive + expand, savings, loop detection
# ---------------------------------------------------------------------------

def _terminal(plugin, session_id, command, output, returncode=0, task_id="task-1"):
    plugin.on_pre_tool_call(tool_name="terminal", args={"command": command}, task_id=task_id,
                            session_id=session_id, tool_call_id="call-term", turn_id="turn-1")
    return plugin.on_transform_terminal_output(command=command, output=output, returncode=returncode,
                                               task_id=task_id, env_type="local")


def test_terminal_output_is_compressed_archived_and_expandable(plugin, home):
    out = _terminal(plugin, "sess_term_01", FIND_COMMAND, FIND_OUTPUT)
    assert isinstance(out, str)
    assert len(out) < len(FIND_OUTPUT) // 3
    assert "600 paths total" in out
    m = re.search(r"^\s*(\S+ \S+ --runtime hermes expand [A-Za-z0-9_-]+)\s*$", out, re.MULTILINE)
    assert m, out[-400:]
    # The printed command works from a plain shell, with no runtime marker set.
    proc = subprocess.run(shlex.split(m.group(1)), env=_clean_env(home),
                          capture_output=True, text=True, timeout=120)
    assert proc.stdout.strip() == FIND_OUTPUT.strip()


def test_terminal_savings_are_recorded_for_the_session(plugin, home):
    _terminal(plugin, "sess_term_02", FIND_COMMAND, FIND_OUTPUT)
    dbs = list(home.joinpath(".hermes").rglob("trends.db"))
    assert dbs
    rows = []
    for db in dbs:
        conn = sqlite3.connect(db)
        rows += conn.execute(
            "SELECT feature, original_tokens, compressed_tokens FROM compression_events "
            "WHERE session_id = ?", ("sess_term_02",)).fetchall()
        conn.close()
    assert rows and rows[0][1] > rows[0][2]


def test_expand_output_is_returned_whole(plugin):
    """Running the printed expand command must give back the original, not a
    second compressed preview of it (the recovery exemption has to recognise
    the runtime-pinned form of the command)."""
    out = _terminal(plugin, "sess_term_05", FIND_COMMAND, FIND_OUTPUT)
    cmd = re.search(r"^\s*(\S+ \S+ --runtime hermes expand [A-Za-z0-9_-]+)\s*$", out, re.MULTILINE).group(1)
    again = _terminal(plugin, "sess_term_05", cmd, FIND_OUTPUT)
    assert again is None or FIND_OUTPUT in again
    import recovery_output

    assert recovery_output.is_expand_command(cmd)
    assert recovery_output.is_recovery_output("Bash", {"command": cmd})
    # Only a real runtime name is accepted in the pinned slot.
    assert not recovery_output.is_expand_command(cmd.replace("--runtime hermes", "--runtime ;rm"))


def test_failed_command_output_is_never_compressed(plugin):
    failing = "".join(f"src/m{i}.py:1: error: bad thing {i}\n" for i in range(400))
    out = _terminal(plugin, "sess_term_03", "make build", failing, returncode=2)
    assert out is None or failing in out


def test_loop_detection_nudges_on_identical_reruns(plugin):
    output = "E   AssertionError: boom\nFAILED tests/test_x.py::test_a\n1 failed in 0.1s\n"
    results = [_terminal(plugin, "sess_loop_01", "pytest tests/test_x.py", output, returncode=1)
               for _ in range(3)]
    assert results[0] is None and results[1] is None
    assert results[2] and "3 times this session" in results[2]
    assert results[2].startswith(output)


def test_bash_compress_switch_turns_compression_off_but_keeps_loop_detection(plugin, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_BASH_COMPRESS", "0")
    assert _terminal(plugin, "sess_term_06", FIND_COMMAND, FIND_OUTPUT) is None
    output = "E   AssertionError: boom\n1 failed in 0.1s\n"
    results = [_terminal(plugin, "sess_term_06", "pytest -x", output, returncode=1) for _ in range(3)]
    assert results[2] and "3 times this session" in results[2]


def test_terminal_session_falls_back_to_task_id(plugin):
    # No pre_tool_call seen for this task: attribute to the task id, never drop.
    out = plugin.on_transform_terminal_output(command=FIND_COMMAND, output=FIND_OUTPUT,
                                              returncode=0, task_id="orphan-task-77", env_type="")
    assert out and "600 paths total" in out


# ---------------------------------------------------------------------------
# Tool results: MCP archive + re-fetch guard; reads: dedup and delta
# ---------------------------------------------------------------------------

MCP_RESULT = json.dumps({"rows": [{"id": i, "name": f"row{i}", "v": "z" * 20} for i in range(300)]})


def test_large_mcp_result_is_trimmed_and_refetch_is_redirected(plugin):
    sid = "sess_mcp_01"
    args = {"query": "all rows"}
    first = plugin.on_pre_tool_call(tool_name="mcp__docs__fetch", args=args, task_id="t",
                                    session_id=sid, tool_call_id="call_mcp_1")
    assert first is None
    out = plugin.on_transform_tool_result(tool_name="mcp__docs__fetch", args=args, result=MCP_RESULT,
                                          task_id="t", session_id=sid, tool_call_id="call_mcp_1",
                                          status="ok")
    assert out and len(out) < len(MCP_RESULT) // 4
    assert "Full result archived" in out and "--runtime hermes expand" in out
    again = plugin.on_pre_tool_call(tool_name="mcp__docs__fetch", args=args, task_id="t",
                                    session_id=sid, tool_call_id="call_mcp_2")
    assert again and again["action"] == "block"
    assert "--runtime hermes expand" in again["message"]


def _read(plugin, sid, path, call_id):
    return plugin.on_pre_tool_call(tool_name="read_file", args={"path": str(path)}, task_id="t",
                                   session_id=sid, tool_call_id=call_id)


def test_unchanged_reread_is_answered_from_cache_and_edit_gives_a_diff(plugin, tmp_path):
    sid = "sess_read_01"
    src = tmp_path / "proj" / "module.py"
    src.parent.mkdir()
    src.write_text((SCRIPTS / "spawn_utils.py").read_text(encoding="utf-8"), encoding="utf-8")
    assert _read(plugin, sid, src, "r1") is None            # first read: full file
    second = _read(plugin, sid, src, "r2")
    assert second and second["action"] == "block"
    assert "unchanged" in second["message"]
    # The model edits it through Hermes's patch tool; the read cache refreshes.
    src.write_text(src.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    plugin.on_transform_tool_result(tool_name="patch", args={"path": str(src), "old_string": "x",
                                    "new_string": "y"}, result='{"success": true}', task_id="t",
                                    session_id=sid, tool_call_id="p1", status="ok")
    src.write_text(src.read_text(encoding="utf-8") + "extra = 1\n", encoding="utf-8")
    third = _read(plugin, sid, src, "r3")
    assert third and third["action"] == "block"
    assert "+extra = 1" in third["message"]


def test_partial_reads_are_not_treated_as_full_rereads(plugin, tmp_path):
    sid = "sess_read_02"
    src = tmp_path / "big.py"
    src.write_text((SCRIPTS / "spawn_utils.py").read_text(encoding="utf-8"), encoding="utf-8")
    assert _read(plugin, sid, src, "r1") is None
    ranged = plugin.on_pre_tool_call(tool_name="read_file", args={"path": str(src), "offset": 40, "limit": 20},
                                     task_id="t", session_id=sid, tool_call_id="r2")
    assert ranged is None


def test_relative_paths_never_use_the_read_cache(plugin, tmp_path, monkeypatch):
    """Hermes resolves relative paths against the session's directory; the
    plugin cannot see it, so it must not guess (it could map the wrong file)."""
    src = tmp_path / "rel.py"
    src.write_text((SCRIPTS / "spawn_utils.py").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for call in ("r1", "r2", "r3"):
        assert plugin.on_pre_tool_call(tool_name="read_file", args={"path": "rel.py"}, task_id="t",
                                       session_id="sess_read_04", tool_call_id=call) is None


def test_read_cache_can_be_turned_off(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_HERMES_READ_CACHE", "0")
    src = tmp_path / "m.py"
    src.write_text((SCRIPTS / "spawn_utils.py").read_text(encoding="utf-8"), encoding="utf-8")
    assert _read(plugin, "sess_read_03", src, "r1") is None
    assert _read(plugin, "sess_read_03", src, "r2") is None


# ---------------------------------------------------------------------------
# Checkpoints and continuity
# ---------------------------------------------------------------------------

def test_turn_end_writes_a_checkpoint_from_hermes_messages(plugin, home):
    sid = "sess_ckpt_01"
    _make_state_db(home, sid)
    plugin.on_session_end(session_id=sid, task_id="t", turn_id="turn-1", completed=True,
                          failed=False, interrupted=False, turn_exit_reason="done",
                          model="claude-sonnet-4-5", platform="cli")
    files = _wait_for(lambda: _checkpoints(home, sid))
    assert files, "no checkpoint written"
    text = files[0].read_text(encoding="utf-8")
    assert "Trigger: stop" in text
    assert "Refactor the parser module" in text
    assert "recursive descent" in text
    assert "fix tokenizer" in text
    # No cwd known: never stamp the git state of whatever directory TO runs in.
    assert "Git:" not in text
    # The translated transcript has no usage data: no made-up quality grade.
    assert "## Context Quality" not in text


def test_checkpoint_never_uses_a_claude_code_transcript(home):
    # A Claude Code transcript exists, the Hermes session has no messages.
    proj = home / ".claude" / "projects" / "-tmp-x"
    proj.mkdir(parents=True)
    (proj / "sess_ckpt_02.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "CLAUDE CODE TASK"}}) + "\n",
        encoding="utf-8")
    _make_state_db(home, "sess_ckpt_02", with_messages=False)
    subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "--runtime", "hermes",
                    "hermes-checkpoint", "--session", "sess_ckpt_02", "--trigger", "stop"],
                   env=_clean_env(home), capture_output=True, text=True, timeout=120)
    assert not _checkpoints(home, "sess_ckpt_02")
    assert not list(home.rglob("hermes-transcripts/*.jsonl"))


def test_checkpoint_scratch_transcript_is_removed(home):
    _make_state_db(home, "sess_ckpt_03")
    subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "--runtime", "hermes",
                    "hermes-checkpoint", "--session", "sess_ckpt_03", "--trigger", "stop"],
                   env=_clean_env(home), capture_output=True, text=True, timeout=120, check=True)
    assert _checkpoints(home, "sess_ckpt_03")
    assert not list(home.rglob("hermes-transcripts/*.jsonl"))


def test_compaction_clears_read_cache_and_restores_checkpoint(plugin, home, tmp_path):
    sid = "sess_compact_01"
    _make_state_db(home, sid)
    src = tmp_path / "keep.py"
    src.write_text((SCRIPTS / "spawn_utils.py").read_text(encoding="utf-8"), encoding="utf-8")
    assert _read(plugin, sid, src, "r1") is None
    assert _read(plugin, sid, src, "r2")["action"] == "block"
    # Hermes is about to compact: pre-compaction checkpoint.
    plugin.on_pre_auxiliary_call(aux_task="compression", session_id=sid, task_id="t",
                                 platform="cli", api_request_id="aux-1", retry_count=0)
    assert _wait_for(lambda: _checkpoints(home, sid))
    plugin.on_post_compaction(session_id=sid, old_session_id="", kind="summarize",
                              reason="pre_api", before_tokens=150_000, after_tokens=20_000,
                              messages_removed=40)
    # The file left the model's context: the next read must be served in full.
    assert _read(plugin, sid, src, "r3") is None
    # And the checkpoint is restored with the next turn.
    ctx = plugin.on_pre_llm_call(session_id=sid, user_message="", is_first_turn=False,
                                 conversation_history=[], model="claude-sonnet-4-5", platform="cli")
    assert ctx and "Post-compact recovery" in ctx["context"]
    assert "Refactor the parser module" in ctx["context"]


def test_rotated_session_restores_from_the_old_session(plugin, home):
    old, new = "sess_rot_old1", "sess_rot_new1"
    _make_state_db(home, old)
    subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "--runtime", "hermes",
                    "hermes-checkpoint", "--session", old, "--trigger", "auto"],
                   env=_clean_env(home), capture_output=True, text=True, timeout=120, check=True)
    plugin.on_post_compaction(session_id=new, old_session_id=old, kind="summarize", reason="overflow",
                              before_tokens=None, after_tokens=None, messages_removed=None)
    # Delivered with the next tool result when the turn is still running.
    out = plugin.on_transform_tool_result(tool_name="search_files", args={"pattern": "x"},
                                          result='{"matches": []}', task_id="t", session_id=new,
                                          tool_call_id="s1", status="ok")
    assert out
    assert "Refactor the parser module" in json.loads(out)["token_optimizer"]


def test_new_session_gets_a_prior_session_hint(plugin, home):
    _make_state_db(home, "sess_prior_01")
    subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), "--runtime", "hermes",
                    "hermes-checkpoint", "--session", "sess_prior_01", "--trigger", "end"],
                   env=_clean_env(home), capture_output=True, text=True, timeout=120, check=True)
    ctx = plugin.on_pre_llm_call(session_id="sess_next_02",
                                 user_message="continue the parser refactor from last session",
                                 is_first_turn=True, conversation_history=[],
                                 model="claude-sonnet-4-5", platform="cli")
    assert ctx and "RECOVERED DATA" in ctx["context"]
    assert "Refactor the parser module" in ctx["context"]


# ---------------------------------------------------------------------------
# Session lifecycle decisions (recording bridge)
# ---------------------------------------------------------------------------

def test_per_turn_session_end_checkpoints_without_ending_the_session(plugin, recorder):
    sid = "sess_life_01"
    plugin.on_post_api_request(session_id=sid, model="m",
                               usage={"input_tokens": 100, "output_tokens": 5, "prompt_tokens": 100})
    plugin._NUDGED.add(sid)
    plugin.on_session_end(session_id=sid, task_id="t", turn_id="turn-1", completed=True,
                          failed=False, interrupted=False, model="m", platform="cli")
    assert ("stop" in [t for _, t, _ in recorder.checkpoints])
    assert recorder.rollups == []
    assert plugin._TALLY[sid]["last_prompt"] == 100     # live reading kept for the next turn
    assert sid in plugin._NUDGED                         # once-per-session stays once per session


def test_finalize_rolls_up_once_and_writes_end_checkpoint(plugin, recorder):
    sid = "sess_life_02"
    plugin.on_session_finalize(session_id=sid, platform="cli", reason="exit")
    plugin.on_session_end(session_id=sid, completed=False, interrupted=True, platform="cli",
                          reason="keyboard_interrupt")   # CLI exit-path shape, no turn_id
    assert recorder.rollups == [sid]
    assert ("end" in [t for _, t, _ in recorder.checkpoints])


def test_exit_path_session_end_alone_still_rolls_up(plugin, recorder):
    plugin.on_session_end(session_id="sess_life_03", completed=False, interrupted=True,
                          platform="cli", reason="keyboard_interrupt")
    assert recorder.rollups == ["sess_life_03"]


def test_skill_loads_are_counted_and_sent_with_the_rollup(plugin, recorder):
    sid = "sess_skill_01"
    for _ in range(2):
        plugin.on_skill_lifecycle(action="loaded", skill_name="pdf-tools", provenance="installed",
                                  task_id="t", session_id=sid, use_count=3, reused=True)
    plugin.on_skill_lifecycle(action="patched", skill_name="pdf-tools", session_id=sid)
    plugin.on_skill_lifecycle(action="loaded", skill_name="git-helper", session_id=sid)
    plugin.on_session_finalize(session_id=sid, platform="cli")
    assert recorder.rollups == [sid]
    assert recorder.rollup_skills == {"pdf-tools": 2, "git-helper": 1}


def test_rollup_skill_argument_is_validated():
    src = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    start = src.index("_HERMES_SKILLS_MAX = 200")
    end = src.index("def _parse_hermes_rollup_context(args):")
    ns: dict = {"json": json}
    exec(src[start:end], ns)  # noqa: S102 - the parser under test, isolated from measure's import
    parse = ns["_parse_hermes_rollup_skills"]
    assert parse(["--session", "s1", "--skills-json", '{"pdf": 2, "x": 0, "y": "3", "": 1}']) == {"s1": {"pdf": 2}}
    assert parse(["--session", "s1", "--skills-json", "not json"]) == {}
    assert parse(["--skills-json", '{"pdf": 2}']) == {}


def test_progressive_checkpoints_follow_live_fill_bands(plugin, recorder, monkeypatch):
    monkeypatch.setattr(plugin, "_context_window", lambda model: 100_000)
    sid = "sess_band_01"

    def api(prompt):
        plugin.on_post_api_request(session_id=sid, model="m",
                                   usage={"input_tokens": prompt, "output_tokens": 1, "prompt_tokens": prompt})

    api(22_000)
    api(25_000)
    api(52_000)
    api(53_000)
    triggers = [t for _, t, _ in recorder.checkpoints]
    assert triggers == ["progressive-20", "progressive-50"]
    assert recorder.checkpoints[0][2] == pytest.approx(22.0)


def test_edit_batch_prefanout_and_stop_failure_checkpoints(plugin, recorder):
    sid = "sess_mile_01"
    for i in range(4):
        plugin.on_post_tool_call(tool_name="write_file", args={"path": f"/p/f{i % 2}.py", "content": "x"},
                                 result="{}", session_id=sid, task_id="t", status="ok")
    plugin.on_pre_tool_call(tool_name="delegate_task", args={"tasks": [{"goal": "a"}]},
                            session_id=sid, task_id="t", tool_call_id="d1")
    plugin.on_pre_tool_call(tool_name="delegate_task", args={"tasks": [{"goal": "b"}]},
                            session_id=sid, task_id="t", tool_call_id="d2")
    plugin.on_api_request_error(session_id=sid, retryable=True, reason="rate_limit")
    plugin.on_api_request_error(session_id=sid, retryable=False, reason="context_length")
    triggers = [t for _, t, _ in recorder.checkpoints]
    assert triggers.count("milestone-edit-batch") == 1
    assert triggers.count("milestone-pre-fanout") == 1
    assert triggers.count("stop-failure") == 1


def test_failed_writes_do_not_count_toward_an_edit_batch(plugin, recorder):
    for _ in range(6):
        plugin.on_post_tool_call(tool_name="patch", args={"path": "/p/a.py"}, result="{}",
                                 session_id="sess_mile_02", task_id="t", status="error")
    assert recorder.checkpoints == []


def test_precompact_checkpoint_once_per_compaction(plugin, recorder):
    for attempt in range(3):   # retries fire pre_auxiliary_call once per attempt
        plugin.on_pre_auxiliary_call(aux_task="compression", session_id="sess_aux_01",
                                     retry_count=attempt)
    plugin.on_pre_auxiliary_call(aux_task="title_generation", session_id="sess_aux_01")
    assert [t for _, t, _ in recorder.checkpoints] == ["auto"]


def test_subagent_sprawl_note_reaches_the_model(plugin, recorder):
    parent = "sess_sub_01"
    for i in range(8):
        plugin.on_subagent_start(parent_session_id=parent, child_session_id=f"child{i}",
                                 child_role="worker", child_goal="g")
    out = plugin.on_transform_tool_result(tool_name="todo_list", args={}, result='{"todos": []}',
                                          session_id=parent, task_id="t", tool_call_id="x", status="ok")
    assert out and "8 subagents are running" in json.loads(out)["token_optimizer"]
    plugin.on_subagent_stop(parent_session_id=parent, child_session_id="child0")
    assert plugin._SUBAGENTS[parent] == 7


def test_session_delete_and_reset_drop_memory(plugin, recorder):
    sid = "sess_mem_01"
    plugin.on_post_api_request(session_id=sid, model="m", usage={"input_tokens": 5, "prompt_tokens": 5})
    plugin._add_pending(sid, "note")
    plugin.on_session_delete(session_id=sid, reason="delete", profile="default")
    assert sid not in plugin._TALLY and sid not in plugin._PENDING
    plugin.on_post_api_request(session_id="sess_mem_02", model="m", usage={"input_tokens": 5, "prompt_tokens": 5})
    plugin.on_session_reset(session_id="sess_mem_02", platform="cli", reason="new")
    assert "sess_mem_02" not in plugin._TALLY


def test_lean_output_nudge_reaches_the_model_at_high_fill(plugin, home, monkeypatch):
    monkeypatch.setattr(plugin, "_context_window", lambda model: 100_000)
    sid = "sess_lean_01"
    plugin.on_post_api_request(session_id=sid, model="m",
                               usage={"input_tokens": 80_000, "output_tokens": 10, "prompt_tokens": 80_000})
    plugin._maybe_checkpoint = lambda *a, **k: False
    ctx = plugin.on_pre_llm_call(session_id=sid, user_message="", is_first_turn=False,
                                 conversation_history=[{"role": "user", "content": "x"}] * 4,
                                 model="m", platform="cli")
    assert ctx and "focused and spartan" in ctx["context"]
    assert "80% (100k window)" in ctx["context"]


def test_lean_output_nudge_is_paced_like_claude_code(plugin, recorder, monkeypatch):
    monkeypatch.setattr(plugin, "_context_window", lambda model: 100_000)
    sid = "sess_lean_02"
    plugin.on_post_api_request(session_id=sid, model="m",
                               usage={"input_tokens": 80_000, "prompt_tokens": 80_000})

    def turn():
        plugin.on_pre_llm_call(session_id=sid, user_message="", conversation_history=[], model="m")
        return sum(1 for c in recorder.context_calls if c[0] == "hermes-turn-context" and "fill_pct" in c[1])

    assert turn() == 1
    assert turn() == 1                         # inside the 5 minute cooldown
    for _ in range(3):
        plugin._VERBOSITY[sid]["last"] = 0.0   # cooldown elapsed
        turn()
    assert plugin._VERBOSITY[sid]["count"] == 3  # never more than 3 per session


def test_lean_output_nudge_off_switch(plugin, recorder, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_HERMES_VERBOSITY", "0")
    monkeypatch.setattr(plugin, "_context_window", lambda model: 100_000)
    plugin.on_post_api_request(session_id="sess_lean_03", model="m",
                               usage={"input_tokens": 80_000, "prompt_tokens": 80_000})
    plugin.on_pre_llm_call(session_id="sess_lean_03", user_message="", conversation_history=[], model="m")
    assert not any("fill_pct" in c[1] for c in recorder.context_calls if c[0] == "hermes-turn-context")


def test_pre_tool_budget_stays_inside_the_host_timeout(plugin):
    plugin._hook_timeout_cache["value"] = 2.0
    assert plugin._pre_tool_timeout() <= 0.8
    plugin._hook_timeout_cache["value"] = 0.0     # 0 = unbounded in Hermes
    assert plugin._pre_tool_timeout() == plugin._PRE_TOOL_TIMEOUT_S


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------

class ExplodingBridge:
    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError("engine exploded")
        return boom


HOOK_CALLS = [
    ("on_pre_llm_call", dict(session_id="s1", user_message="hi", is_first_turn=True,
                             conversation_history=[], model="m", platform="cli")),
    ("on_post_api_request", dict(session_id="s1", model="m", usage={"input_tokens": 99_000_000})),
    ("on_pre_tool_call", dict(tool_name="read_file", args={"path": "/x"}, session_id="s1",
                              task_id="t", tool_call_id="c")),
    ("on_pre_tool_call", dict(tool_name="mcp__a__b", args={}, session_id="s1", task_id="t")),
    ("on_pre_tool_call", dict(tool_name="delegate_task", args={}, session_id="s1", task_id="t")),
    ("on_post_tool_call", dict(tool_name="patch", args={"path": "/x"}, session_id="s1")),
    ("on_transform_terminal_output", dict(command="ls", output="x" * 5000, returncode=0, task_id="t")),
    ("on_transform_tool_result", dict(tool_name="mcp__a__b", args={}, result="y" * 9000,
                                      session_id="s1", tool_call_id="c")),
    ("on_pre_auxiliary_call", dict(aux_task="compression", session_id="s1")),
    ("on_post_compaction", dict(session_id="s1", old_session_id="s0")),
    ("on_api_request_error", dict(session_id="s1", retryable=False)),
    ("on_subagent_start", dict(parent_session_id="s1")),
    ("on_subagent_stop", dict(parent_session_id="s1")),
    ("on_skill_lifecycle", dict(action="loaded", skill_name="pdf", session_id="s1")),
    ("on_session_start", dict(session_id="s1", model="m", platform="cli")),
    ("on_session_end", dict(session_id="s1", turn_id="t1")),
    ("on_session_finalize", dict(session_id="s1")),
    ("on_session_reset", dict(session_id="s1")),
    ("on_session_delete", dict(session_id="s1", deleted_ids=["s1", "s2"])),
]


@pytest.mark.parametrize("name, kwargs", HOOK_CALLS)
def test_every_hook_fails_open_when_the_engine_breaks(plugin, monkeypatch, name, kwargs):
    monkeypatch.setattr(plugin, "_import_bridge", lambda: ExplodingBridge())
    result = getattr(plugin, name)(**kwargs)
    assert result is None or isinstance(result, (str, dict))
    if name == "on_pre_tool_call":
        assert result is None      # never block a tool because of our own failure


@pytest.mark.parametrize("name", sorted({n for n, _ in HOOK_CALLS}))
def test_every_hook_tolerates_garbage_kwargs(plugin, monkeypatch, name):
    monkeypatch.setattr(plugin, "_import_bridge", lambda: None)
    garbage = dict(session_id=12, tool_name=None, args="nope", output=b"bytes", result=[1],
                   command=None, usage="x", parent_session_id=object(), deleted_ids=5)
    getattr(plugin, name)(**garbage)
    getattr(plugin, name)()


def test_missing_engine_scripts_keep_original_output(plugin, monkeypatch):
    import hermes_hook_bridge

    monkeypatch.setattr(hermes_hook_bridge, "scripts_dir", lambda: Path("/nonexistent-dir"))
    assert _terminal(plugin, "sess_fail_01", FIND_COMMAND, FIND_OUTPUT) is None


def test_hung_engine_is_cut_off_at_the_budget(plugin, monkeypatch, tmp_path):
    import hermes_hook_bridge

    slow = tmp_path / "hermes_tool_hooks.py"
    slow.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(hermes_hook_bridge, "scripts_dir", lambda: tmp_path)
    monkeypatch.setattr(plugin, "_pre_tool_timeout", lambda: 0.5)
    start = time.time()
    assert plugin.on_pre_tool_call(tool_name="read_file", args={"path": "/x.py"}, session_id="s",
                                   task_id="t", tool_call_id="c") is None
    assert time.time() - start < 10


# ---------------------------------------------------------------------------
# Slash command
# ---------------------------------------------------------------------------

def test_slash_command_expand_and_help(plugin, home):
    out = _terminal(plugin, "sess_cmd_01", FIND_COMMAND, FIND_OUTPUT)
    key = re.search(r"--runtime hermes expand ([A-Za-z0-9_-]+)", out).group(1)
    text = plugin._handle_command(f"expand {key}")
    assert "./src/pkg0/module_0.py" in text
    assert "checkpoint" in plugin._handle_command("help")
    assert "Usage" in plugin._handle_command("expand ../../etc/passwd")


# ---------------------------------------------------------------------------
# Translation and pinned constants
# ---------------------------------------------------------------------------

def test_hermes_messages_translate_to_checkpoint_records():
    import hermes_session

    def call(name, args):
        return {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": json.dumps([{"type": "text", "text": "do it"}])},
        {"role": "assistant", "content": "ok", "tool_calls": json.dumps([
            call("read_file", {"path": "/a.py", "offset": 1, "limit": 2000}),
            call("read_file", {"path": "/b.py", "offset": 10, "limit": 5}),
            call("patch", {"mode": "patch", "patch": "*** Update File: /c.py\n*** Add File: /d.py\n"}),
            call("delegate_task", {"tasks": [{"goal": "g1"}, {"goal": "g2"}]}),
            call("todo_list", {"todos": [{"content": "t", "status": "pending"}]}),
            call("terminal", {"command": "ls"}),
        ])},
        {"role": "tool", "content": "TOOL OUTPUT"},
    ]
    recs = hermes_session.messages_to_transcript_records(msgs)
    assert [r["type"] for r in recs] == ["user", "assistant"]
    assert recs[0]["message"]["content"] == "do it"
    uses = [(b["name"], b["input"]) for b in recs[1]["message"]["content"] if b["type"] == "tool_use"]
    assert uses[0] == ("Read", {"file_path": "/a.py"})
    assert uses[1] == ("Read", {"file_path": "/b.py", "offset": 10, "limit": 5})
    assert ("Edit", {"file_path": "/c.py"}) in uses and ("Edit", {"file_path": "/d.py"}) in uses
    assert [u for u in uses if u[0] == "Agent"][1][1]["description"] == "g2"
    assert ("Bash", {"command": "ls"}) in uses
    assert any(u[0] == "TodoWrite" for u in uses)


def _measure_constant(name: str):
    text = (SCRIPTS / "measure.py").read_text(encoding="utf-8")
    m = re.search(rf"^{name} = (.+)$", text, re.MULTILINE)
    assert m, name
    raw = m.group(1)
    env_default = re.search(r"_int_env\([^,]+,\s*(\d+)\)", raw)
    return int(env_default.group(1)) if env_default else eval(raw)  # noqa: S307 - literal list


def test_mirrored_constants_match_their_sources(plugin):
    import hermes_session

    assert list(hermes_session.PROGRESSIVE_BANDS) == _measure_constant("_PROGRESSIVE_BANDS")
    assert hermes_session.EDIT_BATCH_WRITE_THRESHOLD == _measure_constant("_EDIT_BATCH_WRITE_THRESHOLD")
    assert hermes_session.EDIT_BATCH_FILE_THRESHOLD == _measure_constant("_EDIT_BATCH_FILE_THRESHOLD")
    assert plugin._CHECKPOINT_COOLDOWN_S == _measure_constant("_CHECKPOINT_COOLDOWN_SECONDS")
    assert plugin._VERBOSITY_MAX_NUDGES == _measure_constant("_VERBOSITY_MAX_NUDGES")
    assert plugin._VERBOSITY_COOLDOWN_S == _measure_constant("_VERBOSITY_COOLDOWN_SEC")
    codex = (SCRIPTS / "codex_hook_bridge.py").read_text(encoding="utf-8")
    assert f"_SUBAGENT_SPRAWL_THRESHOLD = {plugin._SUBAGENT_SPRAWL_THRESHOLD}" in codex


# ---------------------------------------------------------------------------
# End to end: installed plugin, realistic session
# ---------------------------------------------------------------------------

def test_end_to_end_session_on_the_installed_plugin(home, tmp_path):
    plugin_dir = _install(home)
    module = _load_plugin(plugin_dir / "__init__.py", name="to_hermes_parity_installed")
    try:
        module._hook_timeout_cache["value"] = 30.0
        ctx = FakeCtx()
        module.register(ctx)
        hooks = ctx.hooks
        sid = "sess_e2e_0001"
        _make_state_db(home, sid, cwd=str(tmp_path))
        model = "claude-sonnet-4-5"

        hooks["on_session_start"](session_id=sid, model=model, platform="cli")
        hooks["pre_llm_call"](session_id=sid, user_message="Refactor the parser", is_first_turn=True,
                              conversation_history=[], model=model, platform="cli", task_id="t", turn_id="u1")
        hooks["post_api_request"](session_id=sid, model=model, task_id="t", turn_id="u1",
                                  usage={"input_tokens": 9_000, "cache_read_tokens": 40_000,
                                         "cache_write_tokens": 1_000, "output_tokens": 800,
                                         "reasoning_tokens": 0, "prompt_tokens": 50_000})
        assert hooks["pre_tool_call"](tool_name="terminal", args={"command": FIND_COMMAND}, task_id="t",
                                      session_id=sid, tool_call_id="c1", turn_id="u1") is None
        compressed = hooks["transform_terminal_output"](command=FIND_COMMAND, output=FIND_OUTPUT,
                                                        returncode=0, task_id="t", env_type="local")
        hooks["post_tool_call"](tool_name="terminal", args={"command": FIND_COMMAND}, result="{}",
                                session_id=sid, task_id="t", tool_call_id="c1", status="ok")
        assert hooks["transform_tool_result"](tool_name="terminal", args={}, result="{}", session_id=sid,
                                              task_id="t", tool_call_id="c1", status="ok") is None
        for _ in range(2):
            hooks["on_skill_lifecycle"](action="loaded", skill_name="pdf-tools", provenance="installed",
                                        task_id="t", session_id=sid, use_count=1, reused=False)
        hooks["on_session_end"](session_id=sid, task_id="t", turn_id="u1", completed=True, failed=False,
                                interrupted=False, turn_exit_reason="done", model=model, platform="cli")
        hooks["on_session_finalize"](session_id=sid, platform="cli", reason="exit")

        # Compression happened.
        assert compressed and len(compressed) < len(FIND_OUTPUT) // 3
        # Checkpoints were written (fill band, turn end, session end).
        triggers = _wait_for(lambda: {p.name.split(sid + "-", 1)[1] for p in _checkpoints(home, sid)}
                             if len(_checkpoints(home, sid)) >= 3 else None, timeout=60)
        assert triggers, [p.name for p in _checkpoints(home, sid)]
        joined = " ".join(triggers)
        assert "progressive-20" in joined and "stop" in joined and "end" in joined
        # Savings and the session rollup landed in the dashboard database.
        def rollup_row():
            for db in home.joinpath(".hermes").rglob("trends.db"):
                conn = sqlite3.connect(db)
                try:
                    saved = conn.execute("SELECT COUNT(*) FROM compression_events WHERE session_id = ?",
                                         (sid,)).fetchone()[0]
                    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
                    logged = conn.execute("SELECT skills_json FROM session_log WHERE jsonl_path LIKE ?",
                                          ("hermes:%",)).fetchall() if "session_log" in tables else []
                finally:
                    conn.close()
                if saved and logged:
                    return saved, logged
            return None
        landed = _wait_for(rollup_row, timeout=90)
        assert landed
        # Skill usage from on_skill_lifecycle reached the dashboard database.
        assert json.loads(landed[1][0][0]) == {"pdf-tools": 2}
    finally:
        sys.modules.pop("to_hermes_parity_installed", None)
