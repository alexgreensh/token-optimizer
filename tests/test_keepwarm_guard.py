"""Keep-warm's local guard must fail closed before forwarding a request."""
import copy
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from test_keepwarm_prefix import _fixture
from test_keepwarm_response_filter import _stream


def test_guard_only_forwards_matching_prefix_and_strips_tool_response():
    from _measure.keepwarm_prefix import reconstruct
    from _measure.keepwarm_guard import Guard
    rows, fork, _ = _fixture()
    calls = []
    # Adapt the synthetic response model to match the recorded model.
    def send_matching(body, headers):
        calls.append((body, headers))
        return _stream().replace(b'"model": "model"', b'"model": "synthetic-model"')
    guard = Guard(reconstruct(rows), 'synthetic-key', send_matching)
    body, usage = guard.process(fork, {'x-api-key': 'synthetic-key', 'anthropic-beta': 'test'})
    assert b'tool_use' not in body and b'Write' not in body
    assert usage['cache_read_input_tokens'] == 20000
    assert len(calls) == 1
    with pytest.raises(ValueError): guard.process(fork, {'x-api-key': 'synthetic-key', 'anthropic-beta': 'test'})


def test_guard_does_not_send_wrong_credentials_or_prefix():
    from _measure.keepwarm_prefix import reconstruct
    from _measure.keepwarm_guard import Guard
    rows, fork, _ = _fixture()
    calls = []
    guard = Guard(reconstruct(rows), 'synthetic-key', lambda *args: calls.append(args))
    with pytest.raises(ValueError): guard.process(fork, {'x-api-key': 'wrong', 'anthropic-beta': 'test'})
    fork = copy.deepcopy(fork)
    fork['tools'][0]['name'] = 'Write'
    with pytest.raises(ValueError): guard.process(fork, {'x-api-key': 'synthetic-key', 'anthropic-beta': 'test'})
    assert calls == []
