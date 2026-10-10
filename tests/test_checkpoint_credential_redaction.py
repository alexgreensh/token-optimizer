"""SEC: every string in checkpoint state must pass the credential redactor.

Regression for the class fix: compact_capture used to redact only
``current_step``, ``decisions`` and ``error_context`` field-by-field.
``open_questions``, ``todos``, ``agent_state`` descriptions, ``active_plan``,
paths and every FUTURE string field were persisted verbatim to both the
checkpoint .md and the .json sidecar, and resume_lean/compact_restore read
them back. One recursive pass now covers the entire extracted state (and the
quality summary the sidecar embeds), so no field can bypass it.

The fixture plants a distinct secret shape in EVERY state field, runs the
real compact_capture end-to-end, and asserts no secret survives in either
artifact. A final test injects a brand-new state field via monkeypatch —
that is the "new string field must not bypass the pass" guard: it fails
the day a per-field allowlist replaces the deep pass.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _j(*parts: str) -> str:
    """Join fixture fragments at runtime so no literal in this file matches a
    real-secret shape (GitHub push protection scans test files too)."""
    return "".join(parts)


# One secret shape per field so a partial redactor is caught by name.
SEC_QUESTION = _j("sk-", "A" * 24)                       # OpenAI/Anthropic key
SEC_DECISION = _j("ghp_", "C" * 36)                      # GitHub PAT classic
SEC_AGENT_DESC = _j("xoxb-", "123456789012-", "D" * 20)  # Slack bot token
SEC_AGENT_TYPE = _j("hf_", "E" * 34)                     # HuggingFace token
SEC_TODO = _j("npm_", "F" * 36)                          # npm token
SEC_FILE = _j("gho_", "G" * 36)                          # GitHub OAuth token
SEC_READ = _j("sk-", "H" * 24)                           # in a Read path
SEC_PLAN = _j("sk-ant-", "I" * 24)                       # in a plan path
SEC_ERROR = _j("AKIA", "IOSFODNN7EXAMPLE")               # AWS access key
SEC_FIX = _j("postgres://", "svc:s3cr3t@db.internal:5432/app")  # DB URI
SEC_USER = _j("sk_live_", "J" * 26)                      # Stripe live key
SEC_ASSISTANT = _j("AIza", "SyA-", "K" * 32)             # Google API key
SEC_NESTED = _j("rk_live_", "L" * 26)                    # Stripe restricted
SEC_NEW_FIELD = _j("sk-", "M" * 24)                      # for the future-field guard

ALL_SECRETS = [
    SEC_QUESTION, SEC_DECISION, SEC_AGENT_DESC, SEC_AGENT_TYPE, SEC_TODO,
    SEC_FILE, SEC_READ, SEC_PLAN, SEC_ERROR, SEC_FIX, SEC_USER,
    SEC_ASSISTANT, SEC_NESTED, SEC_NEW_FIELD,
]


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cp_dir, raising=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path, raising=True)
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", tmp_path / "qc", raising=True)
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path / "snap", raising=True)
    monkeypatch.setattr(mod, "TRENDS_DB", tmp_path / "snap" / "trends.db", raising=True)
    # Git probing is irrelevant to the secret claim; keep it deterministic.
    monkeypatch.setattr(mod, "_capture_git_state", lambda cwd=None: (None, None), raising=True)
    yield mod, cp_dir, tmp_path
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _transcript(tmp_path: Path) -> Path:
    """A transcript that plants one secret in every extracted state field."""
    projects = tmp_path / "projects" / "-work-proj"
    projects.mkdir(parents=True, exist_ok=True)
    plan_path = f"/work/docs/plans/auth-{SEC_PLAN}.md"
    records = [
        # last_user + open_questions (has "?")
        {"type": "user", "message": {"content": [
            {"type": "text", "text": f"does {SEC_QUESTION} work as the key?"}]}},
        # decisions + last_assistant (a sentence with a decision verb)
        {"type": "assistant", "message": {"content": [
            {"type": "text",
             "text": f"Because the deploy broke I decided to use {SEC_DECISION} "
                     f"for auth. Also check {SEC_ASSISTANT}."}]}},
        # error_context pair: error text then a "fixed" follow-up
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"error: auth failed for {SEC_ERROR}"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"fixed by moving to {SEC_FIX}"}]}},
        # agent_state (subagent_type + description both carry secrets)
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Task",
             "input": {"subagent_type": f"agent-{SEC_AGENT_TYPE}",
                       "description": f"deploy using {SEC_AGENT_DESC}"}}]}},
        # todos
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "TodoWrite",
             "input": {"todos": [
                 {"content": f"rotate {SEC_TODO} before release",
                  "status": "in_progress"}]}}]}},
        # active_files (Edit) + recent_reads + active_plan (Read)
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": f"/work/{SEC_FILE}/app.py"}},
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": f"/work/{SEC_READ}/readme.md"}},
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": plan_path}}]}},
        # final user msg -> last_user (no "?" so it stays out of
        # open_questions, keeping the two fields independently observable)
        {"type": "user", "message": {"content": [
            {"type": "text", "text": f"ship it with {SEC_USER}"}]}},
        # final assistant text -> last_assistant (Continuation section)
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "on it now"}]}},
    ]
    p = projects / "sess-redact.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def _capture(m, cp_dir, tmp_path, monkeypatch, state_override=None):
    transcript = _transcript(tmp_path)
    if state_override is not None:
        orig = m._extract_session_state
        monkeypatch.setattr(
            m, "_extract_session_state",
            lambda *a, **k: state_override(orig(*a, **k)))
    out = m.compact_capture(transcript_path=str(transcript),
                            session_id="sess-redact-test", trigger="stop")
    assert out, "compact_capture returned no checkpoint path"
    md = Path(out).read_text(encoding="utf-8")
    sidecar_path = Path(out).with_suffix(".json")
    assert sidecar_path.exists(), "JSON sidecar missing"
    sidecar_text = sidecar_path.read_text(encoding="utf-8")
    return md, sidecar_text, json.loads(sidecar_text)


def test_all_checkpoint_fields_are_redacted_in_md_and_sidecar(m, monkeypatch):
    mod, cp_dir, tmp_path = m
    md, sidecar_text, _sidecar = _capture(mod, cp_dir, tmp_path, monkeypatch)
    for secret in ALL_SECRETS:
        if secret in (SEC_NESTED, SEC_NEW_FIELD):
            continue  # only planted by the nested/future-field tests
        assert secret not in md, f"{secret[:14]}… survived in checkpoint .md"
        assert secret not in sidecar_text, (
            f"{secret[:14]}… survived in JSON sidecar")
    assert "[CREDENTIAL REDACTED:" in md, "expected redaction placeholders in .md"
    assert "[CREDENTIAL REDACTED:" in sidecar_text, (
        "expected redaction placeholders in sidecar")


def test_open_questions_todos_agents_named_fields_redacted(m, monkeypatch):
    """The named gaps: open_questions, todos, agent descriptions."""
    mod, cp_dir, tmp_path = m
    _, _, sidecar = _capture(mod, cp_dir, tmp_path, monkeypatch)
    oq = json.dumps(sidecar.get("open_questions", []))
    assert SEC_QUESTION not in oq and "CREDENTIAL REDACTED" in oq
    todos = json.dumps(sidecar.get("todos", []))
    assert SEC_TODO not in todos and "CREDENTIAL REDACTED" in todos
    agents = json.dumps(sidecar.get("agent_state", []))
    assert SEC_AGENT_DESC not in agents and SEC_AGENT_TYPE not in agents
    assert "CREDENTIAL REDACTED" in agents


def test_nested_values_inside_known_fields_are_redacted(m, monkeypatch):
    """A per-field redactor that only handles top-level strings leaks any
    nested container a future extractor adds inside a field it DOES list
    (the sidecar serializes decisions wholesale)."""
    mod, cp_dir, tmp_path = m

    def add_nested(state):
        state["decisions"].append({"detail": [f"token {SEC_NESTED}"]})
        return state

    md, sidecar_text, _ = _capture(
        mod, cp_dir, tmp_path, monkeypatch, state_override=add_nested)
    assert SEC_NESTED not in md, "nested decision leaked into .md"
    assert SEC_NESTED not in sidecar_text, "nested decision leaked into sidecar"


def test_new_state_field_cannot_bypass_the_pass(m, monkeypatch):
    """Guard: a field added to _extract_session_state tomorrow must be
    covered by the write path without anyone remembering to list it.

    Spies on the deep redactor: the WHOLE state dict (including the
    injected field) must be what goes through it. An implementation that
    falls back to naming fields individually either never calls the deep
    pass (spy sees nothing) or calls it on per-field fragments that do not
    contain the new field -- both fail here.
    """
    mod, cp_dir, tmp_path = m
    import credential_patterns as cp

    calls = []
    real_deep = cp.redact_credentials_deep
    monkeypatch.setattr(
        cp, "redact_credentials_deep",
        lambda v: calls.append(v) or real_deep(v))

    def add_field(state):
        state["future_nested_field"] = {
            "deep": [{"k": [f"value {SEC_NEW_FIELD} tail"]}],
            "count": 3,
        }
        return state

    md, sidecar_text, _ = _capture(
        mod, cp_dir, tmp_path, monkeypatch, state_override=add_field)
    assert calls, "redact_credentials_deep was never invoked"
    assert any(
        isinstance(arg, dict) and "future_nested_field" in arg
        for arg in calls
    ), "the full state (incl. the new field) did not go through the deep pass"
    assert SEC_NEW_FIELD not in md and SEC_NEW_FIELD not in sidecar_text


def test_quality_summary_topic_is_redacted(m, monkeypatch):
    """The sidecar embeds the whole quality summary; its `topic` is derived
    from user text and was persisted raw."""
    mod, cp_dir, tmp_path = m
    _, sidecar_text, sidecar = _capture(mod, cp_dir, tmp_path, monkeypatch)
    quality = sidecar.get("quality")
    if quality is not None:
        blob = json.dumps(quality)
        for secret in ALL_SECRETS:
            assert secret not in blob, f"{secret[:14]}… inside sidecar quality"
    assert SEC_QUESTION not in sidecar_text


# --- companion writers: same persistence contract, different sinks ----------

def test_extract_topic_never_returns_a_secret(m):
    """topic lands in session_log + quality cache + checkpoints; the first
    user message is where it comes from, so the extractor itself redacts."""
    mod, _cp, _t = m
    topic = mod._extract_topic(f"please wire up {SEC_USER} for me")
    assert topic is not None
    assert SEC_USER not in topic
    assert "CREDENTIAL REDACTED" in topic


def test_context_intel_decisions_are_redacted(tmp_path, monkeypatch):
    """session_decisions meta persisted raw tool-output sentences before."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import context_intel
    from session_store import SessionStore

    store = SessionStore("redact-intel", snapshot_dir=tmp_path)
    try:
        text = (
            f"We decided to rotate {SEC_DECISION} because it leaked into the "
            "log output yesterday during the deploy run. And we chose vault."
        )
        context_intel._extract_decisions(text, store)
        raw = store.get_meta("session_decisions")
        assert raw, "no decisions were stored"
        stored = json.loads(raw)
        blob = json.dumps(stored)
        assert SEC_DECISION not in blob
        assert "CREDENTIAL REDACTED" in blob
    finally:
        store.close()


def test_codex_backfill_archive_is_redacted(m, monkeypatch, tmp_path):
    """_codex_backfill_tool_archive wrote raw tool output to tool-archive
    JSON files — a token in command output landed on disk verbatim."""
    mod, _cp, _t = m
    archive_dir = tmp_path / "tool-archive" / "sess-x"
    monkeypatch.setattr(mod, "_use_codex_session_adapter", lambda *a: True)
    monkeypatch.setattr(mod, "_archive_dir_for_session", lambda sid: archive_dir)
    secret = _j("sk-", "N" * 24)
    monkeypatch.setattr(
        mod.codex_session, "iter_tool_outputs",
        lambda *a, **k: iter([{
            "tool_use_id": "call_redact_1",
            "tool_name": "shell",
            "tool_type": "codex",
            "command_or_path": f"curl -H 'Bearer {secret}' https://api.x",
            "output": f"ok, token {secret} accepted; " + "pad " * 200,
            "timestamp": "2026-10-10T00:00:00Z",
        }]))
    fake = tmp_path / "fake.jsonl"
    fake.write_text("{}\n", encoding="utf-8")
    n = mod._codex_backfill_tool_archive(
        filepath=str(fake), session_id="sess-x")
    assert n == 1
    for f in archive_dir.iterdir():
        text = f.read_text(encoding="utf-8")
        assert secret not in text, f"{f.name} persisted the secret"
    entry = json.loads(
        (archive_dir / "call_redact_1.json").read_text(encoding="utf-8"))
    assert "CREDENTIAL REDACTED" in entry["response"]
    assert "CREDENTIAL REDACTED" in entry["command_or_path"]


def test_redact_credentials_deep_covers_containers(m):
    """The deep pass itself: strings in dict keys, tuples, sets, and nested
    lists are all redacted; non-strings pass through untouched."""
    import credential_patterns as cp
    secret = _j("sk-", "P" * 24)
    src = {
        f"key-{secret}": "v",
        "list": [f"a {secret}", 7, None],
        "tuple": (f"b {secret}", 3.5),
        "set": {f"c {secret}"},
        "nested": {"deep": [{"x": f"d {secret}"}]},
        "num": 42,
        "none": None,
    }
    out = cp.redact_credentials_deep(src)
    blob = json.dumps(out, default=list)
    assert secret not in blob
    assert out["num"] == 42 and out["none"] is None
    assert out["list"][1] == 7
    assert "CREDENTIAL REDACTED" in blob
    # input not mutated
    assert secret in json.dumps(src, default=list)
