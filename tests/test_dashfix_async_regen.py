import json
import time
import urllib.error
import urllib.request

from test_daemon_threading import live_daemon


def post(daemon, path, body, token=None, origin=None):
    request = urllib.request.Request(daemon.base + path, data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json', 'Origin': origin or daemon.base,
                 'X-TO-Token': daemon.token if token is None else token}, method='POST')
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def test_async_regeneration_and_security(live_daemon):
    d = live_daemon
    start = time.monotonic()
    job = post(d, '/api/regenerate', {})
    assert time.monotonic() - start < 1
    assert job['ok'] and job['job_id']
    try:
        post(d, '/api/regenerate', {})
        assert False, 'a second regeneration started'
    except urllib.error.HTTPError as error:
        assert error.code == 409
    for kwargs in ({'token': 'wrong'}, {'origin': 'https://evil.example'}):
        try:
            post(d, '/api/regenerate', {}, **kwargs)
            assert False, 'security check did not reject request'
        except urllib.error.HTTPError as error:
            assert error.code == 403
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        state = post(d, '/api/regenerate/status', {'job_id': job['job_id']})
        if state['state'] != 'running':
            break
        time.sleep(.1)
    assert state['state'] == 'done'


def test_failure_is_friendly(live_daemon, tmp_path):
    fake = next(tmp_path.glob('install/**/scripts/measure.py'))
    fake.write_text('import sys\nsys.stderr.write("private stack details")\nsys.exit(1)\n')
    job = post(live_daemon, '/api/regenerate', {})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = post(live_daemon, '/api/regenerate/status', {'job_id': job['job_id']})
        if state['state'] != 'running':
            break
        time.sleep(.1)
    assert state['state'] == 'failed'
    assert state['progress'] == 'Could not update the dashboard. Please try again.'
    assert 'private stack details' not in json.dumps(state)


def test_opaque_origin_cannot_obtain_token(live_daemon):
    request = urllib.request.Request(live_daemon.base + '/api/token', headers={'Origin': 'null'})
    try:
        urllib.request.urlopen(request, timeout=2)
        assert False, 'opaque origin could obtain the token'
    except urllib.error.HTTPError as error:
        assert error.code == 403
