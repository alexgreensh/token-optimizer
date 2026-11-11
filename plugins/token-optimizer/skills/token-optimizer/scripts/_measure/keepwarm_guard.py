"""Single-use request guard for an isolated keep-warm fork."""
import hmac
import threading
from .keepwarm_prefix import prepare
from .keepwarm_response_filter import sanitize_stream


class Guard:
    """Verify one fork request before forwarding, and never forward model output."""

    def __init__(self, parent, api_key, forward):
        if not isinstance(api_key, str) or not api_key:
            raise ValueError('API key unavailable')
        self.parent = parent
        self._key = api_key
        self.forward = forward
        self._lock = threading.Lock()
        self._used = False

    def process(self, fork, headers):
        headers = {str(key).lower(): value for key, value in headers.items()}
        key = headers.get('x-api-key')
        if not isinstance(key, str) or not hmac.compare_digest(key, self._key):
            raise ValueError('request not authorized')
        beta_header = headers.get('anthropic-beta', '')
        if not isinstance(beta_header, str):
            raise ValueError('invalid beta header')
        betas = [item.strip() for item in beta_header.split(',') if item.strip()]
        request = prepare(fork, self.parent, betas)
        with self._lock:
            if self._used:
                raise ValueError('request already used')
            self._used = True
        payload = self.forward(request, headers)
        return sanitize_stream(payload, self.parent['body']['model'])
