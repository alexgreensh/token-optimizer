#!/usr/bin/env python3
"""Regression tests for ``setup-hook --uninstall`` (issue #78, workstream B1).

Before the fix, the ``setup-hook --uninstall`` handler called
``setup_hook(dry_run=dry)`` with NO uninstall param, so ``--uninstall``
silently ran INSTALL. The core SessionEnd tracking hook thus had no working
CLI removal.

The fix adds real uninstall to ``setup_hook``: it removes ONLY Token
Optimizer's own SessionEnd hook entries (commands containing ``measure.py``
plus ``collect`` or ``session-end-flush``), leaving the user's other
SessionEnd hooks and all other hook events intact. Mirrors the proven
``setup-smart-compact --uninstall`` filter pattern. Supports ``--dry-run``.

Run directly:  python3 tests/test_setup_hook_uninstall.py
Exits non-zero on first failure.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402


def _to_hook(cmd: str) -> dict:
    return {"type": "command", "command": cmd, "async": True}


def test_remove_strips_only_to_session_end_hooks():
    settings = {
        "hooks": {
            "SessionEnd": [
                {
                    "hooks": [
                        _to_hook("python3 '/x/measure.py' collect --quiet && python3 '/x/measure.py' dashboard --quiet"),
                        _to_hook("echo user-own-keep-me"),
                    ]
                }
            ],
            "PreCompact": [{"hooks": [_to_hook("echo keep-other-event")]}],
        }
    }
    removed = measure._remove_token_optimizer_session_end_hooks(settings)
    assert removed == 1, f"expected 1 removed, got {removed}"
    se = settings["hooks"]["SessionEnd"]
    assert len(se) == 1
    assert len(se[0]["hooks"]) == 1
    assert se[0]["hooks"][0]["command"] == "echo user-own-keep-me"
    # Other events untouched.
    assert "PreCompact" in settings["hooks"]
    assert settings["hooks"]["PreCompact"][0]["hooks"][0]["command"] == "echo keep-other-event"


def test_remove_drops_empty_session_end_group_and_key():
    settings = {
        "hooks": {
            "SessionEnd": [
                {"hooks": [_to_hook("python3 '/x/measure.py' collect --quiet")]},
            ],
            "Stop": [{"hooks": [_to_hook("echo keep-stop")]}],
        }
    }
    removed = measure._remove_token_optimizer_session_end_hooks(settings)
    assert removed == 1
    assert "SessionEnd" not in settings["hooks"], "empty SessionEnd key must be dropped"
    assert "Stop" in settings["hooks"]


def test_remove_recognizes_session_end_flush_command():
    settings = {
        "hooks": {
            "SessionEnd": [
                {"hooks": [_to_hook("python3 '/x/measure.py' session-end-flush --trigger stop --quiet")]}
            ]
        }
    }
    removed = measure._remove_token_optimizer_session_end_hooks(settings)
    assert removed == 1
    assert "SessionEnd" not in settings["hooks"]


def test_remove_no_op_when_nothing_installed():
    settings = {"hooks": {"SessionEnd": [{"hooks": [_to_hook("echo user-only")]}]}}
    removed = measure._remove_token_optimizer_session_end_hooks(settings)
    assert removed == 0
    assert settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == "echo user-only"


def test_remove_no_op_when_no_hooks_key():
    settings = {}
    removed = measure._remove_token_optimizer_session_end_hooks(settings)
    assert removed == 0


def test_remove_does_not_touch_other_measure_py_subcommands():
    """A measure.py command that is NOT collect/session-end-flush is left alone."""
    settings = {
        "hooks": {
            "SessionEnd": [
                {"hooks": [_to_hook("python3 '/x/measure.py' dashboard --quiet")]}
            ]
        }
    }
    removed = measure._remove_token_optimizer_session_end_hooks(settings)
    assert removed == 0, "dashboard-only command is not the SessionEnd collect hook"
    assert settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"].endswith("dashboard --quiet")


def test_remove_handles_malformed_entries_safely():
    settings = {
        "hooks": {
            "SessionEnd": [
                "not-a-dict",
                {"hooks": [_to_hook("python3 '/x/measure.py' collect --quiet"), "raw-string-hook"]},
            ]
        }
    }
    removed = measure._remove_token_optimizer_session_end_hooks(settings)
    assert removed == 1
    se = settings["hooks"]["SessionEnd"]
    # The non-dict group is preserved; the mixed group keeps the raw string.
    assert "not-a-dict" in se
    mixed = [g for g in se if isinstance(g, dict)]
    assert mixed and mixed[0]["hooks"] == ["raw-string-hook"]


def test_is_to_hook_rejects_false_positives():
    """REDTEAM-REGEX: loose substring matching must not catch unrelated commands.

    Before the fix, ``_is_token_optimizer_session_end_hook`` used bare
    ``"measure.py" in cmd`` + ``"collect" in cmd`` substring tests, which
    matched (a) prose inside an echo, (b) a different script whose name merely
    ends in ``measure.py``, and (c) a dot-glued token. The tightened
    ``_TO_SESSION_END_CMD_RE`` requires ``measure.py`` as a word followed
    (modulo an optional closing quote) by the ``collect``/``session-end-flush``
    subcommand as the first positional argument.
    """
    is_to = measure._is_token_optimizer_session_end_hook
    # False positives that must NOT match.
    assert not is_to(_to_hook("echo 'run measure.py to collect data'")), "prose in echo"
    assert not is_to(_to_hook("python3 /path/to/other_measure.py collect")), "longer filename"
    assert not is_to(_to_hook("measure.py.collect")), "dot-glued token"
    # Real TO hook commands that MUST still match.
    assert is_to(_to_hook("python3 measure.py collect")), "bare invocation"
    assert is_to(_to_hook("python3 /path/to/measure.py session-end-flush --trigger stop")), "path + flush"
    assert is_to(_to_hook("python3 '/abs/measure.py' collect --quiet")), "quoted path"
    assert is_to(_to_hook(measure.HOOK_COMMAND)), "production HOOK_COMMAND"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
