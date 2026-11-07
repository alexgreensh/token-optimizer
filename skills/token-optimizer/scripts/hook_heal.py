"""The settings.json heal transaction: the malformed-hook healer's plan and
its one bound write (lifecycle._fix_malformed_hook_commands).

Ownership comes from hook_provenance (the ledger and the frozen shipped
contexts); this module only decides, per entry in its context, and writes
under the settings lock. Lives beside measure.py and must stay importable
on Python 3.9 with the standard library only.
"""
from __future__ import annotations

import json
from pathlib import Path

import hook_provenance as hp


def _resolved(path):
    try:
        return Path(path).resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return None


def _read_settings_at(dest):
    """The settings object stored at exactly ``dest``, or None when it is
    missing, not a regular file, unreadable, not JSON or not an object."""
    try:
        data = json.loads(hp._read_regular(dest).decode("utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def plan_removals(settings, prov):
    """``(new_hooks, removed, kept_contexts, unproven)`` for one settings
    object. Pure: decides per hook entry, in its context; mutates nothing."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return None, 0, [], []
    removed, new_hooks, kept_contexts, unproven = 0, {}, [], []
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        new_groups = []
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else None
            if not isinstance(entries, list):
                new_groups.append(group)
                continue
            kept = []
            for hook in entries:
                if not isinstance(hook, dict):
                    kept.append(hook)
                    continue
                command = hook.get("command", "")
                verdict = prov.verdict_entry(event, group, hook)
                if verdict == hp.REMOVE:
                    removed += 1
                    continue
                if verdict == hp.UNPROVEN:
                    unproven.append((event, command))
                context = hp.entry_context(event, group, hook)
                if context is not None:
                    kept_contexts.append(context)
                kept.append(hook)
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                new_groups.append(new_group)
        if new_groups:
            new_hooks[event] = new_groups
    return new_hooks, removed, kept_contexts, unproven


def _write_bound(facade, dest, prov):
    """Under the settings lock: confirm SETTINGS_PATH still resolves to
    ``dest``, fresh-read ``dest``, re-plan with the proof keyed to ``dest``
    and write only if the file is still the one read (identity check right
    before os.replace, which re-resolves). Any drift writes nothing.
    Returns ``(removed, plan)``; ``plan`` is None when nothing was decided."""
    with facade._settings_lock() as acquired:
        if not acquired or _resolved(facade.SETTINGS_PATH) != dest:
            return 0, None
        identity = facade._settings_file_identity(dest)
        fresh = _read_settings_at(dest)
        if fresh is None:
            return 0, None
        plan = plan_removals(fresh, prov)
        if not plan[1] or _resolved(facade.SETTINGS_PATH) != dest:
            return 0, plan
        new_settings = dict(fresh)
        new_settings["hooks"] = plan[0]
        if not facade._write_settings_atomic_locked(new_settings, expect_identity=identity):
            return 0, plan
        return plan[1], plan


def heal_settings(facade):
    """The malformed-hook heal pass (lifecycle._fix_malformed_hook_commands).

    SETTINGS_PATH is resolved ONCE; that destination is what is read, what
    keys the proof, and what is written. A symlink retargeted mid-pass can
    never get one file judged with another file's proof, or an A-derived
    payload written into B. Returns the number of removed entries.
    """
    dest = _resolved(facade.SETTINGS_PATH)
    current = _read_settings_at(dest) if dest is not None else None
    if current is None:
        return 0
    prov = hp.Provenance.from_facade(facade, settings_path=dest)
    plan = plan_removals(current, prov)
    if plan[0] is None:
        return 0
    removed = 0
    if plan[1]:
        removed, fresh_plan = _write_bound(facade, dest, prov)
        plan = fresh_plan or plan
    # Seed on first run, report unproven entries once; never a removal.
    prov.finish(plan[2], plan[3], log=facade._settings_heal_log)
    if not removed:
        return 0
    try:  # next session re-runs the full hook heal cycle
        facade._write_config_flag("last_hook_heal_check", 0)
    except Exception:
        pass
    # Script installs need the current hooks back now; plugin installs get
    # them from hooks.json.
    if not facade._is_plugin_installed():
        warn = "  [Token Optimizer] Warning: hook re-provisioning failed after cleanup: %s"
        try:
            result = facade.setup_all_hooks(dry_run=False, verbose=False)
            if result.get("error"):
                print(warn % result["error"], file=facade.sys.stderr)
        except Exception as exc:
            print(warn % exc, file=facade.sys.stderr)
    return removed

