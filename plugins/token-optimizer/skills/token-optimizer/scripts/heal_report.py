"""Doctor and health rows for the settings self-heal.

Two kinds of rows:

* "Settings self-heal": restore history from heal_log.jsonl (moved here
  verbatim from audit.doctor so that legacy file could shrink).
* "Hook left alone": every hook in settings.json that looks like a broken
  Token Optimizer hook but that Token Optimizer cannot prove it wrote (the
  healer's "hook_unproven" verdict, hook_provenance.py). One plain line each:
  what it is, where it lives, that it was left alone and can be deleted.

The hook list is computed live from settings.json with the healer's own
verdict, not replayed from the heal log: the log is capped and does not know
when the user deletes a hook, so it would show stale or missing entries.
Everything here is read-only and never raises into doctor/health.
"""
from __future__ import annotations

import json

import hook_provenance as hp
import settings_owned

_SHOWN = 110


def unproven_hooks(facade):
    """[(event, command, settings_path)] for hooks the healer leaves alone and
    reports. Empty on any error."""
    try:
        data, path = facade._read_settings_json()
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            return []
        prov = hp.Provenance.from_facade(facade)
        found, seen = [], set()
        for event, groups in hooks.items():
            for group in groups if isinstance(groups, list) else ():
                for hook in (group.get("hooks") or []) if isinstance(group, dict) else ():
                    command = hook.get("command") if isinstance(hook, dict) else None
                    # One row per entry: the same command in two events is two hooks to look at.
                    key = hp.entry_context(event, group, hook) or (event, command)
                    if not isinstance(command, str) or key in seen:
                        continue
                    if prov.verdict_entry(event, group, hook) == hp.UNPROVEN and prov.reportable(command):
                        seen.add(key)
                        found.append((str(event), command, str(path)))
        return found
    except Exception:
        return []


def describe(event, command, settings_path):
    """One plain line: what it is, where it lives, what to do."""
    shown = hp.echo_command(command, _SHOWN)
    return (f"{event} hook `{shown}` in {settings_path} looks broken but was not written by "
            "Token Optimizer, so Token Optimizer left it alone. Delete it there if you don't want it.")


def doctor_rows(facade):
    """(status, name, detail) rows for doctor. Informational: callers append
    them without touching the score."""
    rows = []
    try:
        heal_log = facade._settings_heal_dir() / "heal_log.jsonl"
        if heal_log.is_file():
            entries = [json.loads(line) for line in heal_log.read_text(encoding="utf-8").strip().split("\n")
                       if line.strip()]
            restores = [e for e in entries if e.get("action") == "restore"]
            if restores:
                last = restores[-1]
                line = last.get("notice") or settings_owned.restore_notice(last.get("restored_keys") or ["hooks"])
                rows.append(("OK", "Settings self-heal",
                             f"{len(restores)} restore(s). Last: {line} at {last.get('timestamp', '?')[:19]}"))
            else:
                rows.append(("OK", "Settings self-heal", "no restores needed"))
        else:
            rows.append(("OK", "Settings self-heal", "no heal history yet"))
    except Exception:
        pass  # doctor must never fail on heal history
    for event, command, path in unproven_hooks(facade):
        rows.append(("WARN", "Hook left alone", describe(event, command, path)))
    return rows


def health_lines(facade):
    """Lines for `measure.py health` (empty when there is nothing to say)."""
    found = unproven_hooks(facade)
    restored = settings_owned.last_restore_line(facade)
    lines = ["", "SETTINGS SELF-HEAL", "  " + restored] if restored else []
    if not found:
        return lines
    return lines + ["", f"HOOKS LEFT ALONE ({len(found)})"] + ["  - " + describe(*entry) for entry in found]
