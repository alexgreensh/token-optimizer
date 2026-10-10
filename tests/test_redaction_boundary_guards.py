"""Every persistence boundary that redacts must have a test that notices the
redaction being removed.

An independent review mutated each boundary (deleted the redact call) and the
suite stayed green for five of eight. Each test here plants a credential, runs
the real writer, and reads back what reached disk / the returned record. The
secret is assembled at runtime (push protection scans test files).

Boundaries: compression_log.log_compression_event, measure._log_compression_event,
the antigravity/copilot/grok ``_safe_topic``, codex_session._extract_topic,
hermes_session title, measure._extract_topic.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SECRET = "ghp_" + "Q7r8T9u0V1w2X3y4Z5a6B7c8D9e0F1g2H3i4"
FRAG = "Q7r8T9u0V1w2"


def _rows(db: Path) -> str:
    conn = sqlite3.connect(str(db))
    try:
        return repr(conn.execute(
            "SELECT command_pattern, detail FROM compression_events").fetchall())
    finally:
        conn.close()


def test_compression_log_redacts_command_pattern_and_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    sys.modules.pop("compression_log", None)
    cl = importlib.import_module("compression_log")
    monkeypatch.setattr(cl, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(cl, "TRENDS_DB", tmp_path / "snap" / "trends.db")
    cl.log_compression_event(
        "sess-1", "bash_compress", "x" * 400, "y" * 100,
        command_pattern=f"cat {SECRET}.txt", detail=f"label {SECRET}")
    blob = _rows(tmp_path / "snap" / "trends.db")
    assert "[CREDENTIAL REDACTED" in blob, "the row must exist and carry the marker"
    assert FRAG not in blob and "ghp_" not in blob
    sys.modules.pop("compression_log", None)


def test_measure_log_compression_event_redacts_command_pattern_and_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    monkeypatch.setattr(mod, "SNAPSHOT_DIR", tmp_path / "snap")
    monkeypatch.setattr(mod, "TRENDS_DB", tmp_path / "snap" / "trends.db")
    mod._log_compression_event(
        "sess-1", "bash_compress", "x" * 400, "y" * 100,
        command_pattern=f"cat {SECRET}.txt", detail=f"label {SECRET}")
    blob = _rows(tmp_path / "snap" / "trends.db")
    assert "[CREDENTIAL REDACTED" in blob, "the row must exist and carry the marker"
    assert FRAG not in blob and "ghp_" not in blob
    sys.modules.pop("measure", None)


@pytest.mark.parametrize("module", ["antigravity_session", "copilot_session", "grok_session"])
def test_adapter_safe_topic_redacts(module):
    mod = importlib.import_module(module)
    topic = mod._safe_topic(f"fix auth with {SECRET} please")
    assert topic is not None
    assert FRAG not in topic and "ghp_" not in topic
    assert "[CREDENTIAL REDACTED" in topic


@pytest.mark.parametrize("module", ["antigravity_session", "copilot_session", "grok_session"])
def test_adapter_safe_topic_drops_when_the_redactor_refuses(module, monkeypatch):
    """A redactor that raises must drop the topic, never pass the raw text."""
    import credential_patterns
    mod = importlib.import_module(module)

    def refuse(_text):
        raise RuntimeError("redactor unavailable")
    monkeypatch.setattr(credential_patterns, "redact_credentials", refuse)
    assert mod._safe_topic(f"fix auth with {SECRET}") is None


def test_codex_extract_topic_redacts():
    import codex_session
    topic = codex_session._extract_topic(f"deploy with {SECRET}")
    assert topic is not None
    assert FRAG not in topic and "ghp_" not in topic


def test_hermes_title_is_redacted_into_the_topic():
    import hermes_session
    row = {"id": "h-1", "model": "claude-sonnet-5", "input_tokens": 1000, "output_tokens": 100,
           "message_count": 4, "title": f"rotate {SECRET} today"}
    norm = hermes_session.normalize_session(row)
    assert norm is not None
    assert norm["topic"] is not None
    assert FRAG not in norm["topic"] and "ghp_" not in norm["topic"]


def test_hermes_title_dropped_when_the_redactor_refuses(monkeypatch):
    import credential_patterns
    import hermes_session

    def refuse(_text):
        raise RuntimeError("redactor unavailable")
    monkeypatch.setattr(credential_patterns, "redact_credentials", refuse)
    row = {"id": "h-1", "model": "claude-sonnet-5", "input_tokens": 1000, "output_tokens": 100,
           "message_count": 4, "title": f"rotate {SECRET} today"}
    assert hermes_session.normalize_session(row)["topic"] is None


def test_measure_extract_topic_redacts(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    sys.modules.pop("measure", None)
    mod = importlib.import_module("measure")
    topic = mod._extract_topic(f"deploy with {SECRET}")
    assert topic is not None
    assert FRAG not in topic and "ghp_" not in topic
    sys.modules.pop("measure", None)
