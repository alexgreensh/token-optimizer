"""Redact BEFORE truncating: a secret that straddles a length cut must not survive.

Every checkpoint/topic/decision field is cut to a fixed width (200, 500, 150,
117 chars...). When the cut ran first, the redactor saw only a prefix of the
secret: ``ghp_`` + 18 of 36 chars is no longer a GitHub token shape, a database
URI cut before its ``@`` no longer matches, and a PEM block is always cut before
its END line. The fragment was then persisted verbatim.

Each case plants the secret so the cut lands inside it, runs the real writer,
and asserts no recognisable fragment reaches disk. Secrets are assembled at
runtime (push protection scans test files).
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

TOKEN = "ghp_" + "Q7r8T9u0V1w2X3y4Z5a6B7c8D9e0F1g2H3i4"  # 40 chars
TOKEN_FRAG = "Q7r8T9u0"
DB_PW = "Sup3rS3cretPw"
DB_URI = "postgres://svc:" + DB_PW + "!@db.internal/app"
PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" + "BKcwggSjAgEAAoIBAQC7Zk3Lm9Xq"
PEM = "-----BEGIN PRIVATE KEY-----\n" + PEM_BODY + "\n-----END PRIVATE KEY-----"
PEM_FRAG = "MIIEvQIBADANBg"

# (id, secret text, fragments that must never be persisted, offset into the
# secret where the cut lands)
STRADDLES = [
    ("token", TOKEN, [TOKEN_FRAG, "ghp_"], 18),
    ("db-uri-cut-before-at", DB_URI, [DB_PW], DB_URI.index("@")),
    ("pem-cut-inside-block", PEM, [PEM_FRAG, "BKcwggSjAgEA"], 50),
]


def _place(cut: int, secret: str, offset: int, lead: str = "", tail: str = " tail") -> str:
    """Text whose character at index ``cut`` falls ``offset`` chars into ``secret``."""
    start = cut - offset
    assert start >= len(lead), "cut too close to the start for this lead-in"
    return lead + "a" * (start - len(lead)) + secret + tail


def _clean(text: str) -> str:
    return text.replace("\n", " ")


@pytest.fixture
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOKEN_OPTIMIZER_REDACT_PATTERNS_FILE", raising=False)
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir()
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cp_dir)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", tmp_path / "qc")
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(mod, "TRENDS_DB", tmp_path / "snap" / "trends.db")
    monkeypatch.setattr(mod, "_capture_git_state", lambda cwd=None: (None, None))
    yield mod, tmp_path
    sys.modules.pop("measure", None)


def _transcript(tmp_path: Path, records: list[dict]) -> Path:
    d = tmp_path / "projects" / "-w"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def _user(text: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def _assistant(text: str = "ok", tool_use: dict | None = None) -> dict:
    blocks = [{"type": "text", "text": text}]
    if tool_use:
        blocks.append(tool_use)
    return {"type": "assistant", "message": {"content": blocks}}


def _persisted(mod, tmp_path, records, sid) -> str:
    out = mod.compact_capture(transcript_path=str(_transcript(tmp_path, records)),
                              session_id=sid, trigger="stop")
    assert out, "checkpoint was not written"
    p = Path(out)
    return p.read_text(encoding="utf-8") + p.with_suffix(".json").read_text(encoding="utf-8")


def _assert_clean(blob: str, frags: list[str]) -> None:
    for frag in frags:
        assert frag not in blob, f"{frag!r} persisted"


# Each field: (id, cut width, record builder taking the straddling text).
CLAUDE_FIELDS = [
    ("last_user", 500, lambda t: [_user(t), _assistant("ok")]),
    ("last_assistant", 500, lambda t: [_user("hello"), _assistant(t)]),
    ("user_open_question", 200, lambda t: [_user(t + " ok?"), _assistant("ok"), _user("go")]),
    ("assistant_open_question", 200, lambda t: [_user("hi"), _assistant(t + " TODO"), _user("go")]),
    ("assistant_decision", 200, lambda t: [_user("hi"), _assistant("we decided to use " + t), _user("go")]),
    ("agent_description", 100, lambda t: [_user("hi"), _assistant("go", {"type": "tool_use", "name": "Task", "input": {"description": t, "subagent_type": "x"}})]),
    ("todo_content", 120, lambda t: [_user("hi"), _assistant("go", {"type": "tool_use", "name": "TodoWrite", "input": {"todos": [{"content": t, "status": "pending"}]}})]),
    ("error_context", 200, lambda t: [_user("hi"), _assistant("error: " + t), _assistant("fixed it, switched approach")]),
    ("error_fix", 200, lambda t: [_user("hi"), _assistant("error: boom"), _assistant("fix " + t)]),
]


@pytest.mark.parametrize("sid,secret,frags,offset", STRADDLES, ids=[s[0] for s in STRADDLES])
@pytest.mark.parametrize("field,cut,build", CLAUDE_FIELDS, ids=[f[0] for f in CLAUDE_FIELDS])
def test_checkpoint_field_redacts_before_cut(m, field, cut, build, sid, secret, frags, offset):
    mod, tmp = m
    # The cut counts from the start of the persisted snippet, so a field that
    # prefixes the planted text (a decision sentence, an error line) shifts it.
    lead = {"assistant_decision": "we decided to use ", "error_context": "error: ", "error_fix": "fix "}.get(field, "")
    text = _place(cut - len(lead), secret, offset)
    if sid != "pem-cut-inside-block":
        text = _clean(text)
    blob = _persisted(mod, tmp, build(text), f"{field}-{sid}")
    _assert_clean(blob, frags)


# --- topics -------------------------------------------------------------------

@pytest.mark.parametrize("pad", range(90, 120, 3))
def test_measure_topic_redacts_before_cut(m, pad):
    mod, _ = m
    topic = mod._extract_topic("x" * pad + " " + TOKEN)
    assert topic is not None
    assert "ghp_" not in topic and TOKEN_FRAG not in topic


@pytest.mark.parametrize("pad", range(90, 120, 3))
def test_codex_topic_redacts_before_cut(m, pad):
    import codex_session
    topic = codex_session._extract_topic("x" * pad + " " + TOKEN)
    assert topic is not None
    assert "ghp_" not in topic and TOKEN_FRAG not in topic


def _codex_file(tmp_path: Path, text: str, role_type: str = "user_message") -> Path:
    recs = [
        {"type": "event_msg", "payload": {"type": role_type, "message": text}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": text}},
    ]
    p = tmp_path / "rollout.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    return p


@pytest.mark.parametrize("sid,secret,frags,offset", STRADDLES, ids=[s[0] for s in STRADDLES])
def test_codex_session_state_redacts_before_cut(m, tmp_path, sid, secret, frags, offset):
    import codex_session
    text = _place(500, secret, offset)
    state = codex_session.extract_session_state(_codex_file(tmp_path, text))
    _assert_clean(json.dumps(state), frags)
    q = _place(200, secret, offset, tail=" ok?")
    state = codex_session.extract_session_state(_codex_file(tmp_path, q))
    _assert_clean(json.dumps(state), frags)


# --- context_intel decisions ----------------------------------------------------

class _Store:
    def __init__(self):
        self.meta = {}

    def get_meta(self, k):
        return self.meta.get(k)

    def set_meta(self, k, v):
        self.meta[k] = v


@pytest.mark.parametrize("sid,secret,frags,offset", STRADDLES[:2], ids=[s[0] for s in STRADDLES[:2]])
def test_extracted_decision_redacts_before_cut(sid, secret, frags, offset):
    import context_intel
    lead = "we decided to use "
    sentence = _place(150 - len(lead), secret, offset, tail=" for auth")
    store = _Store()
    context_intel._extract_decisions(lead + sentence + ".\n" + "padding " * 10, store)
    saved = store.meta.get("session_decisions")
    assert saved, "decision was not stored"
    _assert_clean(saved, frags)


# --- codex backfill archive cap ---------------------------------------------------

def test_codex_backfill_archive_redacts_before_the_5mb_cap(m, monkeypatch, tmp_path):
    mod, _ = m
    archive_dir = tmp_path / "tool-archive" / "sess-cap"
    monkeypatch.setattr(mod, "_use_codex_session_adapter", lambda *a: True)
    monkeypatch.setattr(mod, "_archive_dir_for_session", lambda sid: archive_dir)
    cap = 5_242_880
    output = "a" * (cap - 18) + TOKEN + " tail " + "b" * 2000
    monkeypatch.setattr(
        mod.codex_session, "iter_tool_outputs",
        lambda *a, **k: iter([{
            "tool_use_id": "call_cap_1", "tool_name": "shell", "tool_type": "codex",
            "command_or_path": "cat big.log", "output": output,
            "timestamp": "2026-10-10T00:00:00Z",
        }]))
    fake = tmp_path / "fake.jsonl"
    fake.write_text("{}\n", encoding="utf-8")
    assert mod._codex_backfill_tool_archive(filepath=str(fake), session_id="sess-cap") == 1
    entry = (archive_dir / "call_cap_1.json").read_text(encoding="utf-8")
    assert "truncated by Token Optimizer archive cap" in entry
    assert TOKEN_FRAG not in entry and "ghp_" not in entry
