"""Issue #140: CLAUDE_PLUGIN_DATA identity leak + 2 robustness fixes."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"


def _load_run_py():
    """Load hooks/run.py as an isolated module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("run_py_under_test_140", HOOKS / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _load_plugin_env(monkeypatch: pytest.MonkeyPatch, data_base: Path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module("plugin_env")
    module._PLUGIN_DATA_BASE = data_base
    module._INSTALLED_PLUGINS = data_base.parent / "installed_plugins.json"
    # Use the real runtime_env to get the env var tuple, but override it
    # so tests control the env var list explicitly.
    module.resolve_plugin_data_dir.cache_clear()
    return module


# ---------------------------------------------------------------------------
# Fix (a): foreign CLAUDE_PLUGIN_DATA rejected; TOKEN_OPTIMIZER_PLUGIN_DATA
#          still honoured; hook + CLI resolve the same root.
# ---------------------------------------------------------------------------

def test_foreign_claude_plugin_data_rejected(monkeypatch, tmp_path):
    """A foreign plugin's CLAUDE_PLUGIN_DATA (not in installed_plugins.json)
    is rejected even though it sits under plugins/data/."""
    data_base = tmp_path / "data"
    our_identity = data_base / "token-optimizer-us"
    foreign = data_base / "token-optimizer-foreign"
    our_identity.mkdir(parents=True)
    foreign.mkdir(parents=True)

    # Write installed_plugins.json listing only OUR identity.
    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@us": []}}), encoding="utf-8"
    )

    # Set CLAUDE_PLUGIN_DATA to the FOREIGN identity — should be REJECTED.
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(foreign))
    monkeypatch.delenv("TOKEN_OPTIMIZER_PLUGIN_DATA", raising=False)

    module = _load_plugin_env(monkeypatch, data_base)
    # Override env vars tuple so we test exactly these two.
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    result = module.resolve_plugin_data_dir()
    # Foreign CLAUDE_PLUGIN_DATA is NOT a registered identity → skipped.
    # TOKEN_OPTIMIZER_PLUGIN_DATA is not set → fall through to glob.
    # Glob picks the only token-optimizer-* dir (our_identity is the only
    # non-registered one; foreign is not registered either, but lexical sort
    # picks "token-optimizer-foreign" first, and since both are not registered,
    # the first sorted one wins in the glob fallback).
    # Actually: the glob fallback doesn't check registration. Let me re-think.
    # After env vars are exhausted, _registered_plugin_data_dirs() returns
    # our_identity (the one in installed_plugins.json). So the result should
    # be our_identity.
    assert result == our_identity, (
        f"Expected {our_identity}, got {result} — foreign CLAUDE_PLUGIN_DATA "
        f"was not rejected"
    )


def test_dedicated_var_honored_when_claude_rejected(monkeypatch, tmp_path):
    """When CLAUDE_PLUGIN_DATA is foreign, TOKEN_OPTIMIZER_PLUGIN_DATA is still
    honoured unconditionally."""
    data_base = tmp_path / "data"
    dedicated = data_base / "token-optimizer-dedicated"
    foreign = data_base / "token-optimizer-foreign"
    dedicated.mkdir(parents=True)
    foreign.mkdir(parents=True)

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@dedicated": []}}), encoding="utf-8"
    )

    # CLAUDE_PLUGIN_DATA = foreign (rejected), TOKEN_OPTIMIZER_PLUGIN_DATA = dedicated (honoured).
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(foreign))
    monkeypatch.setenv("TOKEN_OPTIMIZER_PLUGIN_DATA", str(dedicated))

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    result = module.resolve_plugin_data_dir()
    assert result == dedicated, (
        f"Expected dedicated {dedicated}, got {result} — "
        f"TOKEN_OPTIMIZER_PLUGIN_DATA was not honoured"
    )


def test_legitimate_claude_plugin_data_still_accepted(monkeypatch, tmp_path):
    """When CLAUDE_PLUGIN_DATA points to a REGISTERED identity, it IS accepted."""
    data_base = tmp_path / "data"
    registered = data_base / "token-optimizer-registered"
    registered.mkdir(parents=True)

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@registered": []}}), encoding="utf-8"
    )

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(registered))
    monkeypatch.delenv("TOKEN_OPTIMIZER_PLUGIN_DATA", raising=False)

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    result = module.resolve_plugin_data_dir()
    assert result == registered, (
        f"Legitimate registered CLAUDE_PLUGIN_DATA should be accepted, got {result}"
    )


def test_dedicated_var_wins_over_valid_claude_plugin_data(monkeypatch, tmp_path):
    """#140 review follow-up: TOKEN_OPTIMIZER_PLUGIN_DATA must win even when
    CLAUDE_PLUGIN_DATA ALSO resolves to a valid REGISTERED identity (the
    common in-plugin hook case). Pre-fix, the single for-loop iterated
    CLAUDE_PLUGIN_DATA first and returned on the first match, so a valid
    CLAUDE_PLUGIN_DATA silently shadowed the dedicated override."""
    data_base = tmp_path / "data"
    registered = data_base / "token-optimizer-registered"
    dedicated = data_base / "token-optimizer-dedicated"
    registered.mkdir(parents=True)
    dedicated.mkdir(parents=True)

    # Both identities are registered -- CLAUDE_PLUGIN_DATA is NOT foreign here.
    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {
            "token-optimizer@registered": [],
            "token-optimizer@dedicated": [],
        }}), encoding="utf-8"
    )

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(registered))
    monkeypatch.setenv("TOKEN_OPTIMIZER_PLUGIN_DATA", str(dedicated))

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    result = module.resolve_plugin_data_dir()
    assert result == dedicated, (
        f"Expected dedicated {dedicated} to win over valid registered "
        f"CLAUDE_PLUGIN_DATA {registered}, got {result}"
    )


def test_dedicated_var_rejects_shared_base_root(monkeypatch, tmp_path):
    """#140 review follow-up: TOKEN_OPTIMIZER_PLUGIN_DATA pointed straight at
    the shared plugins/data ROOT (not a real per-plugin subdir) must be
    rejected. is_relative_to() returns True on equal paths, so without an
    explicit inequality check the root itself would pass confinement --
    granting access to every plugin's data, not just this one's."""
    data_base = tmp_path / "data"
    other_identity = data_base / "token-optimizer-other"
    other_identity.mkdir(parents=True)
    # data_base itself already exists (tmp_path / "data") -- point the
    # dedicated var directly at the shared root.

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@other": []}}), encoding="utf-8"
    )

    monkeypatch.setenv("TOKEN_OPTIMIZER_PLUGIN_DATA", str(data_base))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    result = module.resolve_plugin_data_dir()
    assert result != data_base, (
        f"Dedicated var pointed at the shared root was NOT rejected: {result}"
    )
    # Rejected -> falls through to the registered-identity lookup.
    assert result == other_identity, f"Expected fallback to {other_identity}, got {result}"


def test_is_safe_subdir_rejects_exact_base_match(monkeypatch, tmp_path):
    """Direct unit test of the confinement primitive: a candidate that
    resolves to exactly `base` must be rejected, not merely a candidate
    that happens to share a name prefix with base."""
    module = _load_plugin_env(monkeypatch, tmp_path / "data")
    base = tmp_path / "data"
    base.mkdir(parents=True)
    assert module._is_safe_subdir(base, base) is False
    real_subdir = base / "token-optimizer-x"
    real_subdir.mkdir()
    assert module._is_safe_subdir(real_subdir, base) is True


def test_resolve_claude_plugin_data_env_rejects_foreign(monkeypatch, tmp_path):
    """The centralized resolver used by hooks/run.py and
    detectors/cache_instability.py: a foreign (unregistered) CLAUDE_PLUGIN_DATA
    resolves to None, never to the foreign path."""
    data_base = tmp_path / "data"
    our_identity = data_base / "token-optimizer-us"
    foreign = data_base / "token-optimizer-foreign"
    our_identity.mkdir(parents=True)
    foreign.mkdir(parents=True)

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@us": []}}), encoding="utf-8"
    )

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(foreign))
    module = _load_plugin_env(monkeypatch, data_base)

    assert module.resolve_claude_plugin_data_env() is None, (
        "Foreign CLAUDE_PLUGIN_DATA was not rejected by the centralized resolver"
    )


def test_resolve_claude_plugin_data_env_accepts_registered(monkeypatch, tmp_path):
    """The centralized resolver accepts a genuinely registered identity."""
    data_base = tmp_path / "data"
    identity = data_base / "token-optimizer-main"
    identity.mkdir(parents=True)

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@main": []}}), encoding="utf-8"
    )

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(identity))
    module = _load_plugin_env(monkeypatch, data_base)

    assert module.resolve_claude_plugin_data_env() == identity


def test_run_py_consent_rejects_foreign_claude_plugin_data(monkeypatch, tmp_path):
    """#140 sibling site: hooks/run.py's consent read (hooks/run.py:119) used
    to trust raw CLAUDE_PLUGIN_DATA with no identity check. A foreign
    plugin's CLAUDE_PLUGIN_DATA -- same shared plugins/data root, but an
    UNREGISTERED identity -- must not misdirect the consent read into that
    foreign config.json. Proven by making the foreign config claim consent
    (True) while the legitimate fallback path has none (empty): the leak bug
    would read the foreign True; the fix must not.
    """
    data_base = tmp_path / "data"
    foreign = data_base / "token-optimizer-foreign"
    (foreign / "config").mkdir(parents=True)
    # Foreign plugin's config claims consent -- if the leak bug were present,
    # _check_consent would read THIS file and wrongly return True.
    (foreign / "config" / "config.json").write_text(
        json.dumps({"enterprise_consent_shown": True}), encoding="utf-8"
    )

    # installed_plugins.json registers a DIFFERENT identity than `foreign`,
    # so CLAUDE_PLUGIN_DATA=foreign is unambiguously the leaked-in case.
    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@ours": []}}), encoding="utf-8"
    )

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(foreign))
    monkeypatch.delenv("TOKEN_OPTIMIZER_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows resolves home via USERPROFILE (+ HOMEDRIVE/HOMEPATH), not HOME,
    # so the legacy ~/.claude fallback would otherwise land on the real profile.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", tmp_path.drive or "")
    monkeypatch.setenv("HOMEPATH", str(tmp_path)[len(tmp_path.drive):] if tmp_path.drive else str(tmp_path))

    # Legacy path (what a rejected/absent CLAUDE_PLUGIN_DATA falls through to)
    # holds an explicit opt-out -- since unit D (v5.12.4 race fix), a
    # flags-ABSENT config fails OPEN, so the only state whose honest answer
    # is still "no consent" is a PRESENT enterprise_consent_shown: false
    # (exactly what `measure.py consent --reset` persists). The discriminating
    # power is unchanged: the foreign config claims True, the leak bug would
    # read that True; the fix must read the legitimate False instead.
    legacy_config = tmp_path / ".claude" / "token-optimizer" / "config.json"
    legacy_config.parent.mkdir(parents=True)
    legacy_config.write_text(
        json.dumps({"enterprise_consent_shown": False}), encoding="utf-8"
    )

    run_mod = _load_run_py()
    result = run_mod._check_consent(tmp_path / "plugin-root")

    assert result is False, (
        "consent read was misdirected into the foreign plugin's config.json "
        "(a leaked CLAUDE_PLUGIN_DATA must not bypass the real consent state)"
    )


def test_run_py_consent_accepts_registered_claude_plugin_data(monkeypatch, tmp_path):
    """Companion regression guard: a genuinely REGISTERED CLAUDE_PLUGIN_DATA
    must still be read normally through the identity-checked resolver."""
    data_base = tmp_path / "data"
    ours = data_base / "token-optimizer-ours"
    (ours / "config").mkdir(parents=True)
    (ours / "config" / "config.json").write_text(
        json.dumps({"enterprise_consent_shown": True}), encoding="utf-8"
    )

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@ours": []}}), encoding="utf-8"
    )

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(ours))
    monkeypatch.delenv("TOKEN_OPTIMIZER_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows resolves home via USERPROFILE (+ HOMEDRIVE/HOMEPATH), not HOME,
    # so the legacy ~/.claude fallback would otherwise land on the real profile.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", tmp_path.drive or "")
    monkeypatch.setenv("HOMEPATH", str(tmp_path)[len(tmp_path.drive):] if tmp_path.drive else str(tmp_path))

    run_mod = _load_run_py()
    result = run_mod._check_consent(tmp_path / "plugin-root")

    assert result is True, (
        "a genuinely registered CLAUDE_PLUGIN_DATA's consent config was not honored"
    )


def test_cache_instability_state_dir_rejects_foreign_claude_plugin_data(monkeypatch, tmp_path):
    """#140 sibling site: detectors/cache_instability.py's _state_dir()
    (line ~93) WRITES detector state -- the worst of the sibling sites, since
    a leaked CLAUDE_PLUGIN_DATA there doesn't just misread, it misdirects a
    WRITE into another plugin's directory. A foreign, unregistered
    CLAUDE_PLUGIN_DATA must not be used as the state dir; the detector must
    fall back to the legacy ~/.claude/token-optimizer/data location instead.
    """
    data_base = tmp_path / "data"
    foreign = data_base / "token-optimizer-foreign"
    foreign.mkdir(parents=True)

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@ours": []}}), encoding="utf-8"
    )

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(foreign))
    monkeypatch.delenv("TOKEN_OPTIMIZER_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows resolves home via USERPROFILE (+ HOMEDRIVE/HOMEPATH), not HOME,
    # so the legacy ~/.claude fallback would otherwise land on the real profile.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOMEDRIVE", tmp_path.drive or "")
    monkeypatch.setenv("HOMEPATH", str(tmp_path)[len(tmp_path.drive):] if tmp_path.drive else str(tmp_path))

    monkeypatch.syspath_prepend(str(SCRIPTS))
    ci = importlib.import_module("detectors.cache_instability")

    result = ci._state_dir()
    assert result != foreign / "data", (
        f"cache_instability._state_dir() was misdirected into the foreign "
        f"plugin's data dir: {result}"
    )
    assert result == tmp_path / ".claude" / "token-optimizer" / "data", (
        f"expected fallback to the legacy default, got {result}"
    )


def test_cache_instability_state_dir_accepts_registered_claude_plugin_data(monkeypatch, tmp_path):
    """Companion regression guard: a genuinely registered CLAUDE_PLUGIN_DATA
    must still be used as the detector's state dir."""
    data_base = tmp_path / "data"
    ours = data_base / "token-optimizer-ours"
    ours.mkdir(parents=True)

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@ours": []}}), encoding="utf-8"
    )

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(ours))
    monkeypatch.delenv("TOKEN_OPTIMIZER_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", raising=False)

    monkeypatch.syspath_prepend(str(SCRIPTS))
    ci = importlib.import_module("detectors.cache_instability")

    result = ci._state_dir()
    assert result == ours / "data"


def test_refetch_guard_renderability_read_is_capped(monkeypatch, tmp_path):
    """#140 review P2: _entry_is_renderable() used to fh.read() the WHOLE
    archive entry file (up to ~5MB, the archive_result.py entry ceiling)
    before json.loads -- uncapped on this hot PreToolUse path. Proves the cap
    is applied BEFORE parsing: with the cap patched down to a small value, an
    entry file larger than the cap is read truncated and therefore correctly
    treated as not-renderable (fail-open, same as any other corrupt/oversized
    file), while an entry within the cap still renders normally.
    """
    monkeypatch.syspath_prepend(str(SCRIPTS))
    rg = importlib.import_module("refetch_guard")

    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(parents=True)

    # Shrink the cap so a small, well-formed entry becomes "too large" without
    # needing a multi-MB fixture file.
    monkeypatch.setattr(rg, "_ENTRY_RENDER_MAX_BYTES", 40)

    big_payload = json.dumps({"response": "x" * 500})
    assert len(big_payload) > 40
    (archive_dir / "big-entry.json").write_text(big_payload, encoding="utf-8")

    assert rg._entry_is_renderable(archive_dir, "big-entry") is False, (
        "renderability read was not capped -- a truncated read should fail "
        "json.loads and report not-renderable, not silently read past the cap"
    )

    small_payload = json.dumps({"response": "ok"})
    assert len(small_payload) <= 40
    (archive_dir / "small-entry.json").write_text(small_payload, encoding="utf-8")
    assert rg._entry_is_renderable(archive_dir, "small-entry") is True, (
        "the cap must not be so aggressive it breaks a normal small entry"
    )


def test_hook_and_cli_resolve_same_root(monkeypatch, tmp_path):
    """Hook env (only CLAUDE_PLUGIN_DATA) and CLI env (no env vars) both
    resolve to the same registered identity via different paths."""
    data_base = tmp_path / "data"
    identity = data_base / "token-optimizer-main"
    identity.mkdir(parents=True)

    (data_base.parent / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"token-optimizer@main": []}}), encoding="utf-8"
    )

    # Simulate hook: CLAUDE_PLUGIN_DATA set (no TOKEN_OPTIMIZER_PLUGIN_DATA).
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(identity))
    monkeypatch.delenv("TOKEN_OPTIMIZER_PLUGIN_DATA", raising=False)

    module = _load_plugin_env(monkeypatch, data_base)
    module._PLUGIN_DATA_ENV_VARS = ("CLAUDE_PLUGIN_DATA", "TOKEN_OPTIMIZER_PLUGIN_DATA")
    hook_result = module.resolve_plugin_data_dir()

    # Simulate CLI: no env vars set. Should fall through to
    # _registered_plugin_data_dirs().
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    module.resolve_plugin_data_dir.cache_clear()
    cli_result = module.resolve_plugin_data_dir()

    assert hook_result == identity, f"Hook resolved to {hook_result}"
    assert cli_result == identity, f"CLI resolved to {cli_result}"
    assert hook_result == cli_result, (
        f"Hook and CLI diverged: hook={hook_result}, cli={cli_result}"
    )


# ---------------------------------------------------------------------------
# Fix (b): refetch_guard denies only when target is renderable; scans older
#          entries on a bad match.
# ---------------------------------------------------------------------------

def test_refetch_guard_renderable_check(monkeypatch, tmp_path):
    """_lookup_archived only returns a hit when the archive entry file is
    actually renderable (exists, readable JSON, non-empty response)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    rg = importlib.import_module("refetch_guard")

    snapshot_dir = tmp_path / "snapshots"
    archive_dir = snapshot_dir / "tool-archive" / "session-1"
    archive_dir.mkdir(parents=True)

    monkeypatch.setattr(rg, "resolve_snapshot_dir", lambda: snapshot_dir)

    # Write a manifest with two entries: newest (bad file) then older (good file).
    manifest = archive_dir / "manifest.jsonl"
    manifest.write_text(
        json.dumps({
            "tool_name": "mcp__test__query",
            "args_hash": "abc123",
            "tool_use_id": "bad-entry",
            "tokens_est": 500,
        }) + "\n" +
        json.dumps({
            "tool_name": "mcp__test__query",
            "args_hash": "abc123",
            "tool_use_id": "good-entry",
            "tokens_est": 300,
        }) + "\n",
        encoding="utf-8",
    )

    # Write the good entry file.
    good_entry = archive_dir / "good-entry.json"
    good_entry.write_text(json.dumps({"response": "valid content"}), encoding="utf-8")

    # The bad entry file does NOT exist (or is empty/corrupt).
    # This tests that _lookup_archived skips the bad newest entry and returns
    # the older good one.

    tool_use_id, tokens = rg._lookup_archived(
        "session-1", "mcp__test__query", "abc123"
    )
    assert tool_use_id == "good-entry", (
        f"Expected good-entry, got {tool_use_id} — bad entry was not skipped"
    )
    assert tokens == 300


def test_refetch_guard_empty_response_is_not_renderable(monkeypatch, tmp_path):
    """An entry with an empty response field is NOT renderable."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    rg = importlib.import_module("refetch_guard")

    snapshot_dir = tmp_path / "snapshots"
    archive_dir = snapshot_dir / "tool-archive" / "session-1"
    archive_dir.mkdir(parents=True)

    monkeypatch.setattr(rg, "resolve_snapshot_dir", lambda: snapshot_dir)

    # Write manifest with one entry.
    manifest = archive_dir / "manifest.jsonl"
    manifest.write_text(
        json.dumps({
            "tool_name": "mcp__test__query",
            "args_hash": "abc123",
            "tool_use_id": "empty-response",
            "tokens_est": 500,
        }) + "\n",
        encoding="utf-8",
    )

    # Write entry with empty response.
    entry = archive_dir / "empty-response.json"
    entry.write_text(json.dumps({"response": ""}), encoding="utf-8")

    tool_use_id, tokens = rg._lookup_archived(
        "session-1", "mcp__test__query", "abc123"
    )
    assert tool_use_id is None, (
        f"Empty response should not be renderable, got {tool_use_id}"
    )


def test_refetch_guard_corrupt_json_not_renderable(monkeypatch, tmp_path):
    """An entry with corrupt JSON is NOT renderable."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    rg = importlib.import_module("refetch_guard")

    snapshot_dir = tmp_path / "snapshots"
    archive_dir = snapshot_dir / "tool-archive" / "session-1"
    archive_dir.mkdir(parents=True)

    monkeypatch.setattr(rg, "resolve_snapshot_dir", lambda: snapshot_dir)

    manifest = archive_dir / "manifest.jsonl"
    manifest.write_text(
        json.dumps({
            "tool_name": "mcp__test__query",
            "args_hash": "abc123",
            "tool_use_id": "corrupt",
            "tokens_est": 500,
        }) + "\n",
        encoding="utf-8",
    )

    # Write corrupt JSON.
    (archive_dir / "corrupt.json").write_text("not json{{{", encoding="utf-8")

    tool_use_id, tokens = rg._lookup_archived(
        "session-1", "mcp__test__query", "abc123"
    )
    assert tool_use_id is None, (
        f"Corrupt JSON should not be renderable, got {tool_use_id}"
    )


# ---------------------------------------------------------------------------
# Fix (c): archive_result serves full result when entry was pruned post-write.
# ---------------------------------------------------------------------------

def test_archive_result_post_prune_serves_full_result(monkeypatch, tmp_path):
    """When _cleanup_archives_if_due deletes the just-written entry, the hook
    does NOT emit a pointer — it returns without printing, so the original
    tool output reaches context."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    ar = importlib.import_module("archive_result")

    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setattr(ar, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(ar, "TRENDS_DB", snapshot_dir / "trends.db")

    # Force retention to 1 byte so the ceiling gate (max_bytes > 0) triggers.
    # Setting it to 0 would disable the retention ceiling entirely.
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES", "1")

    # Disable the rate-limit marker so cleanup always runs.
    cleanup_marker = snapshot_dir / ".archive-cleanup.last"
    cleanup_marker.parent.mkdir(parents=True, exist_ok=True)

    # Mock stdin with a large MCP result.
    hook_input = {
        "tool_name": "mcp__test__big_query",
        "tool_use_id": "test-entry-1",
        "tool_response": {"text": "x" * 5000},
        "session_id": "test-session",
        "tool_input": {"query": "hello"},
    }

    # Mock read_stdin_hook_input to return our input.
    monkeypatch.setattr(
        ar, "read_stdin_hook_input", lambda _max_bytes: hook_input
    )

    # Disable SessionStore (no DB in test).
    monkeypatch.setattr(ar, "SessionStore", None)

    # Ensure the cleanup rate-limit marker is old so cleanup runs.
    cleanup_marker.touch()
    os.utime(cleanup_marker, (0, 0))

    # Capture stdout.
    import sys
    from io import StringIO

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    # Run archive_result — it should write the entry, run cleanup (which
    # deletes it because max_bytes=0), detect the entry is gone, and return
    # without printing an updatedMCPToolOutput.
    ar.archive_result(quiet=True)

    output = captured.getvalue()
    # Should NOT emit a replacement pointer since the entry was pruned.
    assert "updatedMCPToolOutput" not in output, (
        f"Pointer emitted after entry was pruned: {output[:200]}"
    )
    # Should NOT emit the expand instruction.
    assert "expand" not in output.lower(), (
        f"expand instruction emitted after entry was pruned: {output[:200]}"
    )


def test_bash_compress_post_prune_serves_full_result_not_lossy(monkeypatch, tmp_path, capsys):
    """#140 review P0 (data loss): when the archived Bash entry is pruned by a
    concurrent retention pass right after write, bash_compress.py's main()
    fallback used to clear the archive key but keep serving the LOSSY
    `compressed` preview -- the untouched full raw command output was in
    scope the whole time. This repros with a real 1-byte retention ceiling
    (TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES=1, same mechanism
    test_archive_result_post_prune_serves_full_result uses below) and asserts
    the FULL command output reaches stdout, not the compressed preview.

    Calls the REAL bash_compress.main() (not a hand-rolled mirror of its
    fallback logic) so a regression in the actual fixed lines is caught.
    subprocess.run is stubbed to avoid depending on any real OS command or
    platform (mirrors test_bash_compress_main_passes_creationflags in
    test_107_hook_scripts_no_window.py) -- consent/env/prune are NOT stubbed:
    the prune is a real cleanup_old_archives() call that really deletes the
    just-written entry file from disk, only its TIMING (landing between the
    real archive_original() write and main()'s post-write existence re-check)
    is test-orchestrated to make an inherently racy concurrent-process
    scenario deterministic.
    """
    monkeypatch.syspath_prepend(str(SCRIPTS))
    bc = importlib.import_module("bash_compress")
    ar = importlib.import_module("archive_result")

    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setattr(ar, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(ar, "TRENDS_DB", snapshot_dir / "trends.db")
    # Force the retention ceiling so cleanup_old_archives() really prunes
    # everything, including the entry main() is about to write.
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES", "1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session-bash-prune")

    # Fake command output: > 2000 chars (clears the GENERIC compressor's
    # floor). Every line shares the same vocabulary and differs only by a
    # numeric (therefore non-distinctive) index, so NO line carries a rare
    # identifier-shaped token. That matters: the rarity-based needle
    # preservation re-injects any line with a distinctive token, so a flashy
    # unique marker line would SURVIVE compression and defeat this fixture. A
    # plain middle filler line is dropped by the head+tail generic compressor
    # and NOT rescued, so serving the lossy preview vs. the raw output stays
    # trivially distinguishable.
    body_lines = [f"line {i:04d} of unique filler content padded out further" for i in range(150)]
    middle_line = body_lines[75]  # a middle line the head+tail compressor drops
    raw_stdout = "\n".join(body_lines) + "\n"
    assert len(raw_stdout) > 2000

    # Sanity: confirm this fixture really compresses lossily via the same
    # compress() main() calls, so the test doesn't pass vacuously.
    compressed_preview = bc.compress("git status --porcelain", raw_stdout, returncode=0, stderr="")
    assert compressed_preview != raw_stdout
    assert middle_line not in compressed_preview, (
        "test fixture's compressed preview unexpectedly kept the middle line"
    )

    class _FakeResult:
        stdout = raw_stdout
        stderr = ""
        returncode = 0

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _FakeResult())
    monkeypatch.setattr(bc.sys, "argv", ["bash_compress.py", "git", "status", "--porcelain"])

    # Real prune, timed to land between archive_original()'s write and
    # main()'s post-write archive_entry_exists() re-check -- simulates a
    # concurrent PostToolUse hook's retention pass racing this one, without
    # relying on actual OS-level concurrency (which would be flaky).
    real_entry_exists = ar.archive_entry_exists
    pruned = {"done": False}

    def _prune_then_check(session_id, key):
        if not pruned["done"]:
            pruned["done"] = True
            ar.cleanup_old_archives()  # real deletion, real retention ceiling
        return real_entry_exists(session_id, key)

    monkeypatch.setattr(ar, "archive_entry_exists", _prune_then_check)

    with pytest.raises(SystemExit) as exc_info:
        bc.main()
    assert exc_info.value.code == 0

    out = capsys.readouterr().out
    assert pruned["done"], "test setup never triggered the prune path"
    assert middle_line in out, (
        "post-prune fallback served the lossy compressed preview instead of "
        "the full raw command output -- silent, unrecoverable data loss"
    )
    assert out == raw_stdout, (
        f"expected the exact raw command output on stdout, got: {out[:200]!r}"
    )


def test_archive_result_emits_pointer_when_entry_survives(monkeypatch, tmp_path):
    """Normal path: entry survives cleanup, pointer IS emitted."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    ar = importlib.import_module("archive_result")

    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setattr(ar, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(ar, "TRENDS_DB", snapshot_dir / "trends.db")

    # Normal retention (100MB, won't prune anything).
    monkeypatch.setenv("TOKEN_OPTIMIZER_ARCHIVE_RETENTION_MAX_BYTES", "104857600")

    hook_input = {
        "tool_name": "mcp__test__query",
        "tool_use_id": "test-entry-2",
        "tool_response": {"text": "x" * 5000},
        "session_id": "test-session",
        "tool_input": {"query": "hello"},
    }

    monkeypatch.setattr(
        ar, "read_stdin_hook_input", lambda _max_bytes: hook_input
    )
    monkeypatch.setattr(ar, "SessionStore", None)

    # Ensure cleanup runs.
    cleanup_marker = snapshot_dir / ".archive-cleanup.last"
    cleanup_marker.parent.mkdir(parents=True, exist_ok=True)
    cleanup_marker.touch()
    os.utime(cleanup_marker, (0, 0))

    import sys
    from io import StringIO

    captured = StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    ar.archive_result(quiet=True)

    output = captured.getvalue()
    assert "updatedMCPToolOutput" in output, (
        f"Pointer NOT emitted when entry survived: {output[:200]}"
    )
    assert "expand" in output.lower(), (
        f"expand instruction missing: {output[:200]}"
    )
