#!/usr/bin/env python3
"""Regression tests for issue #78 — Copilot capability matrix stuck at "unknown".

Root cause: capability seeding runs `copilot --version`. In a WSL-root context
(the user runs `bash install.sh` from PowerShell, and Copilot spawns hooks there
too) the native-Windows `copilot` binary is not on PATH, so version detection
returns nothing and the matrix is seeded "unknown". On the "unknown" matrix,
postToolUse-context / allow / updated-input powers are gated OFF even on a
capable CLI — the user silently runs degraded.

Two guards pinned here:
1. A detection FAILURE must never DOWNGRADE a previously-resolved matrix back to
   "unknown" (otherwise the WSL-root sessionStart hook clobbers a good value).
2. `reseed_capabilities()` persists an externally-resolved version (used by
   copilot-doctor, which runs in the native shell where `copilot` IS on PATH) so
   the matrix self-heals.

Run: python3 -m pytest tests/test_copilot_capability_persistence.py -v
"""
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "token-optimizer", "scripts",
)
sys.path.insert(0, _SCRIPTS)

import copilot_hook_bridge as chb  # noqa: E402


@pytest.fixture
def to_dir(tmp_path, monkeypatch):
    """Point the capability cache at a temp dir and clear the env override."""
    monkeypatch.delenv("TOKEN_OPTIMIZER_COPILOT_CAPS_JSON", raising=False)
    monkeypatch.setattr(chb, "_to_dir", lambda: tmp_path)
    return tmp_path


def _write_cached(to_dir, cli_version, caps):
    (to_dir / "capabilities.json").write_text(
        json.dumps({"cli_version": cli_version, "caps": caps}), encoding="utf-8"
    )


def test_detection_failure_does_not_downgrade_known_version(to_dir, monkeypatch):
    # A previously-resolved 1.0.68 matrix (postToolUse ON) is cached.
    good = chb._seed_capabilities((1, 0, 68))
    assert good[chb.CAP_POSTTOOL_CTX] is True  # sanity: 1.0.68 enables it
    _write_cached(to_dir, "1.0.68", good)

    # Now the version can't be resolved (WSL-root hook, no `copilot` on PATH).
    monkeypatch.setattr(chb, "_copilot_cli_version", lambda: (None, ""))

    caps = chb.load_capabilities(refresh=True)
    # Must KEEP the resolved matrix, not downgrade to the "unknown" seed.
    assert caps[chb.CAP_POSTTOOL_CTX] is True
    # And must NOT have rewritten the file back to "unknown".
    on_disk = json.loads((to_dir / "capabilities.json").read_text(encoding="utf-8"))
    assert on_disk["cli_version"] == "1.0.68"


def test_fresh_install_with_no_binary_seeds_conservative_unknown(to_dir, monkeypatch):
    # No cached file + no resolvable version → conservative "unknown" seed.
    monkeypatch.setattr(chb, "_copilot_cli_version", lambda: (None, ""))
    caps = chb.load_capabilities(refresh=True)
    assert caps[chb.CAP_POSTTOOL_CTX] is False  # gated off until we know better
    on_disk = json.loads((to_dir / "capabilities.json").read_text(encoding="utf-8"))
    assert on_disk["cli_version"] == "unknown"


def test_reseed_capabilities_persists_resolved_version(to_dir):
    caps = chb.reseed_capabilities((1, 0, 68), "1.0.68")
    assert caps[chb.CAP_POSTTOOL_CTX] is True
    on_disk = json.loads((to_dir / "capabilities.json").read_text(encoding="utf-8"))
    assert on_disk["cli_version"] == "1.0.68"
    assert on_disk["caps"][chb.CAP_POSTTOOL_CTX] is True


def test_reseed_then_detection_failure_stays_healed(to_dir, monkeypatch):
    # Doctor reseeds to the real version...
    chb.reseed_capabilities((1, 0, 68), "1.0.68")
    # ...then a later WSL-root hook can't resolve the version.
    monkeypatch.setattr(chb, "_copilot_cli_version", lambda: (None, ""))
    caps = chb.load_capabilities(refresh=True)
    # The heal survives — no clobber back to "unknown".
    assert caps[chb.CAP_POSTTOOL_CTX] is True
    on_disk = json.loads((to_dir / "capabilities.json").read_text(encoding="utf-8"))
    assert on_disk["cli_version"] == "1.0.68"
