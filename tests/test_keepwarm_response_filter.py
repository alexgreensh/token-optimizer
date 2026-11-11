"""A ping may read cache, but model responses must never reach CLI tools."""
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))


def _event(name, payload):
    return 'event: ' + name + '\ndata: ' + json.dumps(payload) + '\n\n'


def _stream(block_type='tool_use'):
    return ''.join([
        _event('message_start', {'type': 'message_start', 'message': {'id': 'msg_synthetic', 'model': 'model',
            'usage': {'input_tokens': 1, 'cache_read_input_tokens': 20000, 'cache_creation_input_tokens': 0}}}),
        _event('content_block_start', {'type': 'content_block_start', 'index': 0,
            'content_block': {'type': block_type, 'id': 'toolu_1', 'name': 'Write', 'input': {}}}),
        _event('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
            'usage': {'output_tokens': 5}}),
        _event('message_stop', {'type': 'message_stop'}),
    ]).encode()


def test_filter_discards_tool_call_but_preserves_usage():
    from _measure.keepwarm_response_filter import sanitize_stream
    result, usage = sanitize_stream(_stream(), 'model')
    assert b'tool_use' not in result and b'Write' not in result
    assert b'"stop_reason":"end_turn"' in result
    assert usage['cache_read_input_tokens'] == 20000
    assert usage['cache_creation_input_tokens'] == 0
    assert usage['output_tokens'] == 5


def test_filter_rejects_server_side_tools_and_incomplete_streams():
    from _measure.keepwarm_response_filter import sanitize_stream, UnsafeResponse
    with pytest.raises(UnsafeResponse): sanitize_stream(_stream('server_tool_use'), 'model')
    with pytest.raises(UnsafeResponse): sanitize_stream(_stream().split(b'event: message_stop')[0], 'model')
    with pytest.raises(UnsafeResponse): sanitize_stream(_stream(), 'other-model')
    with pytest.raises(UnsafeResponse): sanitize_stream(_stream() + _event('message_delta', {'type': 'message_delta', 'usage': {}}).encode(), 'model')


def test_filter_does_not_forward_unrecognized_usage_fields():
    from _measure.keepwarm_response_filter import sanitize_stream
    data = _stream().replace(b'"input_tokens": 1', b'"secret": "never-forward", "input_tokens": 1')
    result, usage = sanitize_stream(data, 'model')
    assert b'never-forward' not in result
    assert 'secret' not in usage
