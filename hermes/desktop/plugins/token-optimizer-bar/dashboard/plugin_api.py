"""Read-only, minimal status for the local Token Optimizer desktop widget."""
import json
import os
import sys
import time
from pathlib import Path
from fastapi import APIRouter, Query

router = APIRouter()
HOME = Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')


def status_data(context_used=None, context_max=None, model='', session_id=''):
    out = {'quality': None, 'limits': {}, 'source': 'local-account-limits'}
    if context_used and context_max and 0 < context_used <= 100_000_000 and 0 < context_max <= 100_000_000:
        scripts = HOME / 'token-optimizer' / 'skills' / 'token-optimizer' / 'scripts'
        if scripts.is_dir():
            try:
                if str(scripts) not in sys.path:
                    sys.path.insert(0, str(scripts))
                from _measure.quality_continuity import session_quality
                q = session_quality(context_used, context_max, model[:200])
                if q.get('measured'):
                    out['quality'] = {'score': q.get('score'), 'grade': q.get('grade'), 'band': q.get('band')}
            except (ImportError, ValueError, TypeError, KeyError):
                pass
    try:
        state = json.loads((HOME / 'limit-watches' / 'account-limits.json').read_text())
        matches = set()
        for key in state.get('_activity', {}).get('counters', {}):
            fields = key.split('|')
            if session_id and len(fields) >= 3 and fields[0] == session_id and (not model or fields[1] == model):
                provider = fields[2]
                if provider == 'openai-codex':
                    matches.add('openai-codex')
                elif provider.startswith('claude-subscription'):
                    matches.add('anthropic')
        out['provider'] = next(iter(matches)) if len(matches) == 1 else None
        for provider, labels in {'openai-codex': ('Session', 'Weekly'), 'anthropic': ('Current session', 'Current week')}.items():
            windows = state.get(provider, {}).get('windows', {})
            entries = []
            for name in labels:
                item = windows.get(name, {})
                used, checked = item.get('used'), item.get('checked_at')
                if isinstance(used, (int, float)) and isinstance(checked, (int, float)) and 0 <= used <= 100:
                    entries.append({'used': used, 'checked_at': checked, 'reset': item.get('reset'), 'stale': time.time() - checked > 900})
                else:
                    entries.append(None)
            out['limits'][provider] = {'five_hour': entries[0], 'weekly': entries[1]}
    except (OSError, ValueError, TypeError):
        pass
    return out


@router.post('/status')
def status(body: dict):
    used, maximum = body.get('context_used'), body.get('context_max')
    if not isinstance(used, (int, float)) or isinstance(used, bool): used = None
    if not isinstance(maximum, (int, float)) or isinstance(maximum, bool): maximum = None
    model = str(body.get('model') or '')[:200]
    session_id = str(body.get('session_id') or '')[:200]
    return status_data(used, maximum, model, session_id)
