"""Run actual dashboard handlers on an in-memory HTTP transport."""
from __future__ import annotations
import ast
import email.message
import http.server
import inspect
import io
from pathlib import Path
import sys
import textwrap
from types import SimpleNamespace


def response(measure, kind, case):
    if kind == 'daemon':
        source = measure._generate_daemon_script()
        nodes = [node for node in ast.parse(source).body
                 if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef))
                 or isinstance(node, ast.ClassDef) and node.name == 'Handler']
        namespace = {'__name__': 'fixture_generated_daemon', '__file__': str(measure.DAEMON_LOG_DIR / 'fixture-daemon.py')}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<generated-daemon-fixture>', 'exec'), namespace)
        handler = namespace['Handler']
    else:
        source = textwrap.dedent(inspect.getsource(measure._serve_dashboard))
        node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef) and node.name == 'DashboardHandler')
        namespace = dict(vars(measure))
        namespace.update(handler=http.server.SimpleHTTPRequestHandler, http=__import__('http'),
                         serve_dir=str(measure.DASHBOARD_PATH.parent), filename=measure.DASHBOARD_PATH.name,
                         _measure_facade=measure)
        if '_measure' in sys.modules:
            from _measure import _compat
            namespace['_compat'] = _compat
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<dashboard-handler-fixture>', 'exec'), namespace)
        handler = namespace['DashboardHandler']
    token = Path(measure.DAEMON_TOKEN_PATH)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('fixture-token')
    token.chmod(0o600)
    assert token.read_text() == 'fixture-token'
    method, path, body, authorized = {
        'health': ('GET', '/api/health', b'', True),
        'token': ('GET', '/api/token', b'', True),
        'savings': ('GET', '/api/savings', b'', True),
        'authorized': ('POST', '/api/mcp/disable', b'{"name":"fixture"}', True),
        'unauthorized': ('POST', '/api/mcp/disable', b'{"name":"fixture"}', False),
        'malformed': ('POST', '/api/mcp/disable', b'{broken', True),
    }[case]
    request = object.__new__(handler)
    request.path = path
    request.request_version = 'HTTP/1.1'
    request.requestline = method + ' ' + path + ' HTTP/1.1'
    request.command = method
    request.client_address = ('127.0.0.1', 4243)
    request.server = SimpleNamespace(server_port=measure.DAEMON_PORT,
                                     server_address=('127.0.0.1', measure.DAEMON_PORT))
    request.close_connection = False
    request.headers = email.message.Message()
    request.headers['Host'] = 'localhost:' + str(measure.DAEMON_PORT)
    request.headers['Origin'] = 'http://localhost:' + str(measure.DAEMON_PORT)
    request.headers['X-TO-Token'] = 'fixture-token' if authorized else 'wrong-token'
    request.headers['Content-Length'] = str(len(body))
    request.rfile = io.BytesIO(body)
    request.wfile = io.BytesIO()
    request._headers_buffer = []
    getattr(request, 'do_' + method)()
    return request.wfile.getvalue()
