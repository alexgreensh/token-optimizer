"""Orphan plugin-data identities remain readable and are reclaimed safely."""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


_PLUGIN_ENV_CONSTANTS = ("_PLUGIN_DATA_BASE", "_INSTALLED_PLUGINS", "_PLUGIN_DATA_ENV_VARS")


def _plugin_env_constants():
    module = sys.modules.get("plugin_env")
    if module is None:
        return None
    return {name: getattr(module, name) for name in _PLUGIN_ENV_CONSTANTS}


@pytest.fixture(autouse=True)
def _restore_plugin_env_state():
    """plugin_env caches its result and this file overrides its constants. After each
    test, put the constants back to their pre-test values and drop the cache, so a
    later test that imports measure does not resolve paths from this file's temp
    folders. (Another autouse fixture may already hold monkeypatch, so this does not
    rely on monkeypatch's teardown running after it.)"""
    before = _plugin_env_constants()
    yield
    module = sys.modules.get("plugin_env")
    if module is not None:
        if before is not None:
            for name, value in before.items():
                setattr(module, name, value)
        module.resolve_plugin_data_dir.cache_clear()


def _load_plugin_env(monkeypatch: pytest.MonkeyPatch, data_base: Path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module("plugin_env")
    # monkeypatch restores these on teardown. Plain assignment left the test's
    # temp folders in the shared module, so a later test that imports measure
    # resolved its paths outside the sandbox.
    monkeypatch.setattr(module, "_PLUGIN_DATA_BASE", data_base)
    monkeypatch.setattr(module, "_INSTALLED_PLUGINS", data_base.parent / "installed_plugins.json")
    monkeypatch.setattr(module, "_PLUGIN_DATA_ENV_VARS", ("CLAUDE_PLUGIN_DATA",))
    module.resolve_plugin_data_dir.cache_clear()
    return module


def test_glob_fallback_is_stable_instead_of_mtime_selected(monkeypatch, tmp_path):
    data_base = tmp_path / "data"
    alpha = data_base / "token-optimizer-alpha"
    zeta = data_base / "token-optimizer-zeta"
    alpha.mkdir(parents=True)
    zeta.mkdir()
    now = time.time()
    os.utime(alpha, (now - 100, now - 100))
    os.utime(zeta, (now, now))
    module = _load_plugin_env(monkeypatch, data_base)

    assert module.resolve_plugin_data_dir() == alpha

    os.utime(alpha, (now + 100, now + 100))
    os.utime(zeta, (now - 100, now - 100))
    module.resolve_plugin_data_dir.cache_clear()
    assert module.resolve_plugin_data_dir() == alpha


def test_sibling_detection_excludes_active_identity(monkeypatch, tmp_path):
    data_base = tmp_path / "data"
    active = data_base / "token-optimizer-active"
    orphan = data_base / "token-optimizer-orphan"
    active.mkdir(parents=True)
    orphan.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active))
    module = _load_plugin_env(monkeypatch, data_base)

    assert module.find_sibling_plugin_data_dirs() == [orphan]
    assert module.snapshot_dir_candidates() == [
        active / "data",
        orphan / "data",
    ]


def test_orphan_reclamation_is_opt_in_age_gated_and_announced(
    monkeypatch, tmp_path, capsys
):
    data_base = tmp_path / "data"
    active = data_base / "token-optimizer-active"
    old_orphan = data_base / "token-optimizer-old"
    fresh_orphan = data_base / "token-optimizer-fresh"
    for path in (active, old_orphan, fresh_orphan):
        (path / "data").mkdir(parents=True)
        (path / "data" / "payload.json").write_text("private", encoding="utf-8")
    old = time.time() - (31 * 86400)
    for path in (old_orphan / "data" / "payload.json", old_orphan / "data", old_orphan):
        os.utime(path, (old, old))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active))
    module = _load_plugin_env(monkeypatch, data_base)
    module._INSTALLED_PLUGINS.write_text(
        json.dumps({"plugins": {"token-optimizer@active": []}}),
        encoding="utf-8",
    )

    assert module.reclaim_orphaned_plugin_data_dirs(min_age_days=30) == []
    assert old_orphan.exists()
    assert "not deleted" in capsys.readouterr().err

    monkeypatch.setenv("TOKEN_OPTIMIZER_RECLAIM_ORPHANED_DATA", "1")
    removed = module.reclaim_orphaned_plugin_data_dirs(min_age_days=30)

    assert removed == [old_orphan]
    assert active.exists()
    assert fresh_orphan.exists()
    assert not old_orphan.exists()
    assert "Reclaimed orphaned plugin data identity" in capsys.readouterr().err
    assert "token-optimizer-old" in (
        active / "data" / "orphan-reclamation.log"
    ).read_text(encoding="utf-8")


def test_reclamation_never_deletes_a_registered_sibling(monkeypatch, tmp_path):
    data_base = tmp_path / "data"
    active = data_base / "token-optimizer-active"
    registered = data_base / "token-optimizer-registered"
    for path in (active, registered):
        path.mkdir(parents=True)
    old = time.time() - (90 * 86400)
    os.utime(registered, (old, old))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RECLAIM_ORPHANED_DATA", "1")
    module = _load_plugin_env(monkeypatch, data_base)
    module._INSTALLED_PLUGINS.write_text(
        json.dumps({
            "plugins": {
                "token-optimizer@active": [],
                "token-optimizer@registered": [],
            }
        }),
        encoding="utf-8",
    )

    assert module.reclaim_orphaned_plugin_data_dirs(min_age_days=30) == []
    assert registered.exists()


def test_reclamation_revalidates_identity_immediately_before_delete(
    monkeypatch, tmp_path
):
    data_base = tmp_path / "data"
    active = data_base / "token-optimizer-active"
    orphan = data_base / "token-optimizer-orphan"
    active.mkdir(parents=True)
    orphan.mkdir()
    old = time.time() - (90 * 86400)
    os.utime(orphan, (old, old))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RECLAIM_ORPHANED_DATA", "1")
    module = _load_plugin_env(monkeypatch, data_base)
    module._INSTALLED_PLUGINS.write_text(
        json.dumps({"plugins": {"token-optimizer@active": []}}),
        encoding="utf-8",
    )
    original_latest = module._latest_tree_mtime
    calls = 0

    def register_during_scan(path):
        nonlocal calls
        calls += 1
        result = original_latest(path)
        if calls == 1:
            module._INSTALLED_PLUGINS.write_text(
                json.dumps({
                    "plugins": {
                        "token-optimizer@active": [],
                        "token-optimizer@orphan": [],
                    }
                }),
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(module, "_latest_tree_mtime", register_during_scan)

    assert module.reclaim_orphaned_plugin_data_dirs(min_age_days=30) == []
    assert orphan.exists()


def test_expand_checks_sibling_archives_before_reporting_miss(
    monkeypatch, tmp_path, capsys
):
    active = tmp_path / "active"
    sibling = tmp_path / "sibling"
    entry = sibling / "tool-archive" / "session-1" / "tool-1.json"
    entry.parent.mkdir(parents=True)
    entry.write_text('{"response":"recovered from sibling"}', encoding="utf-8")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(active))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    measure = importlib.import_module("measure")
    monkeypatch.setattr(measure, "SNAPSHOT_DIR", active)
    monkeypatch.setattr(
        measure,
        "snapshot_dir_candidates",
        lambda: [active, sibling],
    )
    monkeypatch.setattr(measure, "_log_reexpand_debit", lambda *args: None)

    measure.expand_archived("tool-1", session_id="session-1")

    assert "recovered from sibling" in capsys.readouterr().out


def test_helper_overrides_are_undone_when_the_monkeypatch_ends(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module("plugin_env")
    before = _plugin_env_constants()
    with monkeypatch.context() as inner:
        _load_plugin_env(inner, tmp_path / "data")
        assert module._PLUGIN_DATA_BASE == tmp_path / "data"
    assert _plugin_env_constants() == before


def test_helper_leaves_a_cached_result_behind_within_its_own_test(monkeypatch, tmp_path):
    # First of an ordered pair: the next test checks the file's teardown dropped this.
    active = tmp_path / "data" / "token-optimizer-active"
    active.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(active))
    module = _load_plugin_env(monkeypatch, tmp_path / "data")
    assert module.resolve_plugin_data_dir() == active
    assert module.resolve_plugin_data_dir.cache_info().currsize == 1


def test_cached_result_does_not_outlive_the_test_that_set_it(monkeypatch):
    # Runs right after the test above in file order. Imports the module itself, so it
    # also passes alone or in any order. A leftover cache would still hold the previous
    # test's temp folder and disagree with a fresh resolve; a cache another file filled
    # under its own constants agrees with it.
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = importlib.import_module("plugin_env")
    if module.resolve_plugin_data_dir.cache_info().currsize:
        assert module.resolve_plugin_data_dir() == module.resolve_plugin_data_dir.__wrapped__()
