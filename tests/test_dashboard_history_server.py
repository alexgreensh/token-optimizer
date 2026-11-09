"""History expansion through the standalone HTTP server."""
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def test_standalone_server_creates_token_and_saves_history_choice(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / 'skills/token-optimizer/scripts'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    dashboard = tmp_path / 'dashboard.html'
    dashboard.write_text('old')
    program = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import measure as m
root = Path(sys.argv[2]); port = int(sys.argv[3])
m.RUNTIME_DIR = root
m.SNAPSHOT_DIR = root
m.DAEMON_TOKEN_PATH = root / 'daemon-token'
m.TRENDS_DB = root / 'trends.db'
m.generate_standalone_dashboard = lambda **kw: str(root / 'dashboard.html')
m._serve_dashboard(str(root / 'dashboard.html'), port=port, host='127.0.0.1')
'''
    proc = subprocess.Popen([sys.executable, '-c', program, str(scripts), str(tmp_path), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = 'http://127.0.0.1:%d' % port
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + '/api/token', timeout=1) as response:
                    token = json.load(response)['token']
                break
            except (urllib.error.URLError, OSError):
                if proc.poll() is not None:
                    raise AssertionError(proc.stderr.read().decode())
                time.sleep(0.05)
        else:
            raise AssertionError('standalone server did not start')
        assert len(token) >= 16
        request = urllib.request.Request(base + '/api/regenerate', data=b'{"days":180}', method='POST',
            headers={'Origin': base, 'X-TO-Token': token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            assert json.load(response)['ok'] is True
        settings = json.loads((tmp_path / 'token-optimizer/dashboard-history.json').read_text())
        assert settings == {'days': 180}
    finally:
        proc.terminate()
        proc.wait(timeout=10)
