from _measure.first_call_context import observe, summarize
import json
import sqlite3


def test_observes_first_parent_call_and_private_component_sizes():
    rows = [
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "abcdefgh"}},
        {"type": "attachment", "attachment": {"type": "hook_success", "toolUseID": "1", "hookEvent": "SessionStart", "command": "token-optimizer hook", "stdout": json.dumps({"hookSpecificOutput": {"additionalContext": "abc"}})}},
        {"type": "attachment", "attachment": {"type": "hook_additional_context", "toolUseID": "2", "hookEvent": "SessionStart", "content": [{"text": "abc"}]}},
        {"type": "assistant", "timestamp": "2026-11-01T00:00:00Z", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 7}}},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "later"}},
    ]
    item = observe(rows, len)
    assert item == {"month": "2026-11", "total": 32, "components": {"first_user": 5, "skills": 8, "SessionStart_token_optimizer": 3, "SessionStart_other_or_unknown": 0}}
    assert summarize([item])[0]["p25"] == 32
    assert summarize([item])[0]["components"]["skills"] == {"median": 8, "observed": 1}


def test_missing_usage_and_sidechain_are_not_first_parent_call():
    rows = [{"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 8}}},
            {"type": "assistant", "message": {"usage": {}}}]
    assert observe(rows, len) is None


def test_instruction_files_are_split_without_exposing_paths():
    rows = [{"type": "attachment", "attachment": {"type": "instructions", "files": [
        {"path": "/private/CLAUDE.md", "content": "abc"},
        {"path": "/private/memory/MEMORY.md", "content": "four"},
        {"path": "/private/rules/other.md", "content": "five!"},
    ]}}, {"type": "assistant", "timestamp": "2026-11-01", "message": {"usage": {"input_tokens": 100}}}]
    result = observe(rows, len)
    assert result["components"] == {"claude_md": 3, "memory_files": 4, "other_instructions": 5}
    assert "/private" not in repr(result)


def test_unmatched_hook_output_is_not_attributed():
    rows = [{"type": "attachment", "attachment": {"type": "hook_success", "hookEvent": "SessionStart", "command": "token_optimizer", "stdout": json.dumps({"hookSpecificOutput": {"additionalContext": "not delivered"}})}},
            {"type": "attachment", "attachment": {"type": "hook_additional_context", "hookEvent": "SessionStart", "content": "different"}},
            {"type": "assistant", "timestamp": "2026-11-01", "message": {"usage": {"input_tokens": 1}}}]
    assert observe(rows, len)["components"] == {"SessionStart_other_or_unknown": 9}


def test_generic_runner_name_is_not_sufficient_to_identify_plugin():
    rows = [{"type": "attachment", "attachment": {"type": "hook_success", "hookEvent": "SessionStart", "command": "other-plugin/hooks/sessionstart_runner.py", "stdout": json.dumps({"hookSpecificOutput": {"additionalContext": "abc"}})}},
            {"type": "attachment", "attachment": {"type": "hook_additional_context", "hookEvent": "SessionStart", "content": "abc"}},
            {"type": "assistant", "timestamp": "2026-11-01", "message": {"usage": {"input_tokens": 1}}}]
    assert observe(rows, len)["components"] == {"SessionStart_other_or_unknown": 3}


def test_registered_optimizer_hook_is_identified():
    from _measure.first_call_context import _optimizer_commands
    command = next(c for c in _optimizer_commands() if "sessionstart_runner.py" in c)
    rows = [{"type": "attachment", "attachment": {"type": "hook_success", "hookEvent": "SessionStart", "command": command, "stdout": json.dumps({"hookSpecificOutput": {"additionalContext": "abc"}})}},
            {"type": "attachment", "attachment": {"type": "hook_additional_context", "hookEvent": "SessionStart", "content": "abc"}},
            {"type": "assistant", "timestamp": "2026-11-01", "message": {"usage": {"input_tokens": 1}}}]
    assert observe(rows, len)["components"]["SessionStart_token_optimizer"] == 3


def test_collect_reads_parent_transcripts_and_dated_trims(tmp_path):
    from _measure.first_call_context import collect
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"type": "assistant", "timestamp": "2026-11-05T00:00:00Z", "message": {"usage": {"input_tokens": 800}}}) + "\n")
    db = tmp_path / "trends.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE session_log(jsonl_path TEXT, is_sidechain INTEGER)")
        conn.executemany("INSERT INTO session_log VALUES (?, ?)", [(str(path), 0), (str(path), 0), ("missing", 0), ("ignored", 1)])
    (tmp_path / "structural_trims.jsonl").write_text(json.dumps({"kind":"trim", "id":"1", "ts":"2026-11-03T00:00:00Z", "tokens_before":200, "tokens_after":100}) + "\n")
    result = collect(db, tmp_path)
    assert result["months"][0]["median"] == 800
    assert result["months"][0]["n"] == 1
    assert result["missing"] == 1
    assert result["trim_dates"] == ["2026-11-03"]
