"""Replace a completed ping response with inert text, preserving usage only."""
import json


class UnsafeResponse(ValueError):
    """A malformed or untrusted response cannot be sent to the CLI."""


def _event(name, data):
    return ('event: ' + name + '\ndata: ' + json.dumps(data, separators=(',', ':')) + '\n\n').encode('utf-8')


def sanitize_stream(payload, expected_model):
    """Never forward upstream content; accept only a complete Messages SSE."""
    if not isinstance(payload, bytes) or len(payload) > 8 * 1024 * 1024:
        raise UnsafeResponse('response is missing or too large')
    try:
        text = payload.decode('utf-8').replace('\r\n', '\n')
        start = None
        usage = {}
        finished = False
        delta_seen = False
        for chunk in text.split('\n\n'):
            if not chunk.strip():
                continue
            if finished:
                raise UnsafeResponse('events after message stop')
            lines = chunk.split('\n')
            name = next((line[6:].strip() for line in lines if line.startswith('event:')), None)
            data = '\n'.join(line[5:].lstrip() for line in lines if line.startswith('data:'))
            if name == 'ping' and not data:
                continue
            obj = json.loads(data)
            if not isinstance(obj, dict) or obj.get('type') != name:
                raise UnsafeResponse('invalid event')
            if start is None and name not in ('message_start', 'ping', 'error'):
                raise UnsafeResponse('event before message start')
            if name == 'message_start':
                if start is not None:
                    raise UnsafeResponse('multiple message starts')
                start = obj.get('message')
                if not isinstance(start, dict) or start.get('model') != expected_model:
                    raise UnsafeResponse('unexpected response model')
                usage.update(start.get('usage') or {})
            elif name == 'content_block_start':
                if (obj.get('content_block') or {}).get('type') == 'server_tool_use':
                    raise UnsafeResponse('server-side tool use')
            elif name == 'message_delta':
                delta_seen = True
                usage.update(obj.get('usage') or {})
            elif name == 'message_stop':
                finished = True
            elif name == 'error':
                raise UnsafeResponse('upstream error')
        if start is None or not delta_seen or not finished:
            raise UnsafeResponse('incomplete response')
        identifier = start.get('id')
        if not isinstance(identifier, str) or not identifier:
            raise UnsafeResponse('missing message id')
        keys = ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
        for key in keys:
            value = usage.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise UnsafeResponse('invalid usage')
        usage = {key: usage.get(key, 0) for key in keys}
        message = {'id': identifier, 'type': 'message', 'role': 'assistant', 'content': [],
                   'model': expected_model, 'stop_reason': None, 'stop_sequence': None,
                   'usage': usage}
        result = b''.join((
            _event('message_start', {'type': 'message_start', 'message': message}),
            _event('content_block_start', {'type': 'content_block_start', 'index': 0,
                                           'content_block': {'type': 'text', 'text': ''}}),
            _event('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                           'delta': {'type': 'text_delta', 'text': 'ok'}}),
            _event('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
            _event('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                                      'usage': {'output_tokens': usage.get('output_tokens', 0)}}),
            _event('message_stop', {'type': 'message_stop'}),
        ))
        return result, usage
    except (ValueError, TypeError, UnicodeError, KeyError) as exc:
        if isinstance(exc, UnsafeResponse):
            raise
        raise UnsafeResponse('invalid upstream response') from exc
