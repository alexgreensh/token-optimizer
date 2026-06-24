"""Detector: respondToBashCommands settings check.

When respondToBashCommands is not set to false in ~/.claude/settings.json,
Claude Code generates a model reply after every /slash-command and !bash output
(added as the default in v2.1.186), spending output tokens on unrequested replies.

This detector reads settings.json directly -- no session turn parsing required.
Settings are cached at module load so batch runs do not re-read the file per session.
"""

import functools
import json
import sys
from pathlib import Path

try:
    from runtime_env import claude_home as _get_claude_home
except ImportError:
    _get_claude_home = None


@functools.lru_cache(maxsize=1)
def _load_settings(settings_path):
    """Read and parse settings.json. Cached so batch runs hit the file once."""
    if not settings_path.exists():
        return {}
    try:
        parsed = json.loads(settings_path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except PermissionError as e:
        print(
            f"[token-optimizer] respond_to_bash: cannot read {settings_path}: {e}",
            file=sys.stderr,
        )
        return None  # None signals "unknown" -- caller must not fire
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}  # treat as empty settings -- finding fires


def detect_respond_to_bash(_session_data):
    """Return a finding if respondToBashCommands is not explicitly false."""
    if _get_claude_home is not None:
        settings_path = _get_claude_home() / "settings.json"
    else:
        settings_path = Path.home() / ".claude" / "settings.json"

    settings = _load_settings(settings_path)
    if settings is None:  # PermissionError -- cannot determine setting
        return []

    if settings.get("respondToBashCommands") is False:
        return []

    return [{
        "name": "respond_to_bash_commands",
        "confidence": 0.9,
        "always_show": True,
        "evidence": (
            "respondToBashCommands is not disabled in settings.json. "
            "Since v2.1.186, Claude Code generates a model reply after every "
            "/command and !bash output by default, spending output tokens on "
            "unrequested replies."
        ),
        "suggestion": (
            f'Set "respondToBashCommands": false in {settings_path} '
            "to stop Claude from generating unsolicited replies to command outputs."
        ),
    }]
