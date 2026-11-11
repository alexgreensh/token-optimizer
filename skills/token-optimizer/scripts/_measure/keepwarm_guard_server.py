"""A single-use, loopback-only guard for a keep-warm fork."""
from contextlib import contextmanager
from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlsplit

from .keepwarm_guard import Guard

_MAX_REQUEST = 64 * 1024 * 1024
_MAX_RESPONSE = 8 * 1024 * 1024


def official_forward(body, headers):
    """Send only the guarded request to Anthropic's fixed HTTPS endpoint."""
    encoded = json.dumps(body, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    allowed = ('x-api-key', 'anthropic-version', 'anthropic-beta', 'user-agent')
    sent = {key: headers[key] for key in allowed if key in headers}
    if not sent.get('x-api-key') or not sent.get('anthropic-version'):
        raise ValueError('required API headers unavailable')
    sent['content-type'] = 'application/json'
    conn = HTTPSConnection('api.anthropic.com', 443, timeout=60)
    try:
        conn.request('POST', '/v1/messages?beta=true', body=encoded, headers=sent)
        response = conn.getresponse()
        if response.status != 200 or 'text/event-stream' not in (response.getheader('Content-Type') or '').lower():
            raise ValueError('upstream request failed')
        payload = response.read(_MAX_RESPONSE + 1)
        if len(payload) > _MAX_RESPONSE:
            raise ValueError('upstream response too large')
        return payload
    finally:
        conn.close()


@contextmanager
def server(parent, api_key, forward=official_forward, port=0):
    guard = Guard(parent, api_key, forward)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                self.connection.settimeout(5)
                url = urlsplit(self.path)
                host = self.headers.get('Host', '')
                if (url.path != '/v1/messages' or url.query not in ('', 'beta=true') or
                        host != '127.0.0.1:' + str(self.server.server_port) or
                        self.headers.get('Transfer-Encoding')):
                    raise ValueError('invalid request')
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > _MAX_REQUEST:
                    raise ValueError('invalid request')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('incomplete request')
                body = json.loads(raw)
                if not isinstance(body, dict) or body.get('stream') is not True:
                    raise ValueError('invalid request')
                response, usage = guard.process(body, dict(self.headers))
                self.server.usage = usage
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            except Exception:
                self.send_response(400)
                self.send_header('Content-Length', '0')
                self.end_headers()

    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    httpd.daemon_threads = True
    httpd.usage = None
    httpd.url = 'http://127.0.0.1:' + str(httpd.server_port)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
