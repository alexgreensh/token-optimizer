"""Read-only Claude Code tool-search configuration diagnosis.

No network probe, cost projection or automatic setting change. Proxy support,
managed policy and model/deployment fallback cannot be inferred from env alone.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

_DOC = 'https://code.claude.com/docs/en/agent-sdk/tool-search'


def diagnose_tool_search(environ, settings, runtime='claude'):
    """Return an optional doctor (status, name, detail) tuple, without secrets."""
    if runtime != 'claude':
        return None
    if not isinstance(environ, Mapping):
        environ = {}
    settings_env = settings.get('env', {}) if isinstance(settings, dict) else {}
    if not isinstance(settings_env, dict):
        settings_env = {}

    def effective(key):
        value = environ[key] if key in environ else settings_env.get(key, '')
        return value.strip() if isinstance(value, str) else ''

    base = effective('ANTHROPIC_BASE_URL')
    mode = effective('ENABLE_TOOL_SEARCH')
    beta_off = effective('CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS')
    label = 'Tool search'
    caveat = ('Only enable through a proxy after confirming tool_reference support; '
              'model, deployment and managed policy can still disable it. ' + _DOC)
    if beta_off and beta_off.lower() not in ('0', 'false', 'no'):
        return ('!!', label, 'Tool search is disabled by CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS. ' + _DOC)

    valid_mode = mode in ('', 'true', 'false', 'auto')
    if mode.startswith('auto:'):
        match = re.fullmatch(r'auto:(\d{1,3})', mode)
        valid_mode = bool(match and 0 <= int(match.group(1)) <= 100)
    if not valid_mode:
        return ('!!', label, 'ENABLE_TOOL_SEARCH is invalid; effective behavior is unknown. ' + caveat)

    custom = False
    if base:
        try:
            parsed = urlsplit(base)
            custom = not (parsed.scheme == 'https' and parsed.hostname == 'api.anthropic.com'
                          and parsed.port in (None, 443) and not parsed.username and not parsed.password)
        except ValueError:
            custom = True
    if mode == 'false':
        return ('!!', label, 'Tool search is explicitly off; tool definitions load upfront. ' + caveat)
    if custom and not mode:
        return ('!!', label, 'A non-default ANTHROPIC_BASE_URL can disable tool search by default; '
                'tool definitions may load upfront. ' + caveat)
    if mode:
        return ('OK', label, 'ENABLE_TOOL_SEARCH=' + mode + '; proxy compatibility is unverified, '
                'and model/deployment/managed-policy fallbacks may apply. ' + _DOC)
    return None
