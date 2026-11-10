from _measure.first_call_context import observe, summarize


def test_observes_first_parent_call_and_private_component_sizes():
    rows = [
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "abcdefgh"}},
        {"type": "attachment", "attachment": {"type": "hook_success", "toolUseID": "1", "command": "token-optimizer hook"}},
        {"type": "attachment", "attachment": {"type": "hook_additional_context", "toolUseID": "1", "hookEvent": "SessionStart", "content": [{"text": "abc"}]}},
        {"type": "assistant", "timestamp": "2026-11-01T00:00:00Z", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 7}}},
        {"type": "attachment", "attachment": {"type": "skill_listing", "content": "later"}},
    ]
    item = observe(rows, len)
    assert item == {"month": "2026-11", "total": 32, "components": {"first_user": 5, "skills": 8, "SessionStart_token_optimizer": 3}}
    assert summarize([item])[0]["p25"] == 32
    assert summarize([item])[0]["components"]["skills"] == {"median": 8, "observed": 1}


def test_missing_usage_and_sidechain_are_not_first_parent_call():
    rows = [{"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 8}}},
            {"type": "assistant", "message": {"usage": {}}}]
    assert observe(rows, len) is None
