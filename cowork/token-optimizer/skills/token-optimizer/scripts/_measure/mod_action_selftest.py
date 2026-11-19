"""Exercise the real mod booking CLI in an isolated temporary environment."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main(facade):
    try:
        with tempfile.TemporaryDirectory(prefix='to-mod-selftest-') as temporary:
            root = Path(temporary)
            transcript = root / 'selftest-session.jsonl'
            transcript.write_text(json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z', 'message': {'model': 'claude-sonnet-4-5', 'usage': {'cache_read_input_tokens': 20000, 'cache_creation': {'ephemeral_1h_input_tokens': 100}}}}) + '\n', encoding='utf-8')
            stat = transcript.stat()
            event = {'action': 'warm-prepare', 'action_id': 'selftest', 'session_id': 'selftest-session', 'timestamp': time.time(), 'transcript_path': str(transcript), 'byte_size': stat.st_size, 'transcript_mtime': stat.st_mtime}
            env = dict(os.environ, HOME=str(root), CLAUDE_CONFIG_DIR=str(root), TOKEN_OPTIMIZER_SNAPSHOT_DIR=str(root / 'data'), TOKEN_OPTIMIZER_RUNTIME='claude')
            cmd = [sys.executable, str(Path(facade.__file__).resolve()), 'mod-action']
            for action in ('warm-prepare', 'warm-cancel'):
                event['action'] = action
                result = subprocess.run(cmd, input=json.dumps(event), text=True, capture_output=True, env=env, timeout=30)
                if result.returncode or not json.loads(result.stdout).get('ok'):
                    raise RuntimeError(action + ' failed')
            rows = [json.loads(line) for line in (root / 'data' / 'keepwarm_ledger.jsonl').read_text().splitlines()]
            if [row['status'] for row in rows] != ['firing', 'cancelled']:
                raise RuntimeError('ledger verification failed')
        print('PASS')
        return 0
    except Exception as exc:
        print('FAIL: ' + type(exc).__name__ + ': ' + str(exc))
        return 1
