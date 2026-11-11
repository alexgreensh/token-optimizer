"""Local guard rejects unsafe requests and never exposes model tool calls."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'))
from test_keepwarm_prefix import _fixture
from test_keepwarm_response_filter import _stream


def test_local_server_forwards_once_and_returns_inert_sse():
    from _measure.keepwarm_prefix import reconstruct
    from _measure.keepwarm_guard_server import server
    rows, fork, _ = _fixture()
    fork['stream'] = True
    calls = []
    def forward(body, headers):
        calls.append(body)
        return _stream().replace(b'"model": "model"', b'"model": "synthetic-model"')
    with server(reconstruct(rows), 'synthetic-key', forward) as live:
        request = urllib.request.Request(live.url + '/v1/messages?beta=true',
            data=json.dumps(fork).encode(), headers={'x-api-key': 'synthetic-key', 'anthropic-beta': 'test'})
        with urllib.request.urlopen(request, timeout=5) as response:
            result = response.read()
        assert b'tool_use' not in result and b'Write' not in result
        assert live.usage['cache_read_input_tokens'] == 20000
        with pytest.raises(urllib.error.HTTPError): urllib.request.urlopen(request, timeout=5)
    assert len(calls) == 1


def test_local_server_rejects_wrong_key_without_forwarding():
    from _measure.keepwarm_prefix import reconstruct
    from _measure.keepwarm_guard_server import server
    rows, fork, _ = _fixture()
    fork['stream'] = True
    calls = []
    with server(reconstruct(rows), 'synthetic-key', lambda *args: calls.append(args)) as live:
        request = urllib.request.Request(live.url + '/v1/messages', data=json.dumps(fork).encode(),
                                         headers={'x-api-key': 'wrong', 'anthropic-beta': 'test'})
        with pytest.raises(urllib.error.HTTPError): urllib.request.urlopen(request, timeout=5)
    assert not calls


def test_official_forward_uses_fixed_https_and_never_forwards_extra_headers(monkeypatch):
    from _measure import keepwarm_guard_server as transport
    sent = []
    class Response:
        status = 200
        def getheader(self, name): return 'text/event-stream'
        def read(self, limit): return _stream()
    class Connection:
        def __init__(self, host, port, timeout):
            sent.append(('connect', host, port))
        def request(self, method, path, body, headers):
            sent.append(('request', method, path, headers))
        def getresponse(self): return Response()
        def close(self): sent.append(('closed',))
    monkeypatch.setattr(transport, 'HTTPSConnection', Connection)
    transport.official_forward({'model': 'model'}, {'x-api-key': 'synthetic-key',
        'anthropic-version': '2023-06-01', 'cookie': 'do-not-forward', 'authorization': 'secret'})
    assert sent[0] == ('connect', 'api.anthropic.com', 443)
    assert sent[1][1:3] == ('POST', '/v1/messages?beta=true')
    assert 'cookie' not in sent[1][3] and 'authorization' not in sent[1][3]
