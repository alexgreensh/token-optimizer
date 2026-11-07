"""Hook provenance, round 4: the ledger, its lock, recording failures, the
fossil and smart-compact paths, Codex context ownership and diagnostics
echoes. Each test asserts the safe outcome and failed before its fix.

Fixtures come from tests/test_hook_provenance.py.

Run: python3 -m pytest tests/test_hook_provenance_r4.py -v
"""
from __future__ import annotations

import errno
import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(REPO / "tests"))

from test_hook_provenance import hp, installed, m  # noqa: E402,F401


# --- a ledger from a newer version is never moved aside or overwritten ----------

def test_unknown_ledger_version_is_unreadable_and_kept_byte_for_byte(hp, tmp_path):
    ledger = tmp_path / "hook_provenance.json"
    newer = json.dumps({"version": 2, "files": {}, "contexts": {"k": {"d": {}}}}).encode()
    ledger.write_bytes(newer)
    assert hp.load_ledger(ledger)[1] == "unreadable"
    assert hp.record(ledger, "s.json", ["x"], "t") is False
    assert hp.record_contexts(ledger, "s.json", [("Stop", "", "command", "x")], "t") is False
    assert ledger.read_bytes() == newer
    assert not [p for p in tmp_path.iterdir() if "corrupt" in p.name]


def test_versionless_or_garbage_ledger_is_still_corrupt(hp, tmp_path):
    ledger = tmp_path / "hook_provenance.json"
    for raw in (b'{"files": {}}', b'{"version": "1", "files": {}}', b'{"version": false}', b"[1]", b"{nope"):
        ledger.write_bytes(raw)
        assert hp.load_ledger(ledger)[1] == "corrupt", raw


# --- only a lock another process holds is waited on ------------------------------

_POSIX_LOCK = pytest.mark.skipif(sys.platform == "win32", reason="injects fcntl.flock errors")


def _flock_raising(code):
    import fcntl

    def flock(fd, op):
        if op != fcntl.LOCK_UN:
            raise OSError(code, os.strerror(code))
    return flock


@_POSIX_LOCK
@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.ENOLCK, errno.EBADF])
def test_lock_error_that_is_not_busy_fails_at_once(hp, tmp_path, monkeypatch, code):
    import fcntl
    monkeypatch.setattr(fcntl, "flock", _flock_raising(code))
    t0 = time.monotonic()
    assert hp.record(tmp_path / "hook_provenance.json", "s.json", ["x"], "t") is False
    assert time.monotonic() - t0 < 0.5
    assert not (tmp_path / "hook_provenance.json").exists()


@_POSIX_LOCK
def test_busy_lock_is_still_waited_for(hp, tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(fcntl, "flock", _flock_raising(errno.EWOULDBLOCK))
    t0 = time.monotonic()
    assert hp.record(tmp_path / "hook_provenance.json", "s.json", ["x"], "t") is False
    assert time.monotonic() - t0 >= 1.9


# --- the lock context manager yields once, whatever the body raises ---------------

@pytest.mark.parametrize("shape", ["directory", "busy"])
def test_body_exception_propagates_as_itself_when_lock_not_taken(hp, tmp_path, monkeypatch, shape):
    lock = tmp_path / "l.flock"
    if shape == "directory":
        lock.mkdir()
    else:
        monkeypatch.setattr(hp, "_try_lock", lambda fd: False)
    with pytest.raises(OSError, match="boom"):
        with hp._exclusive(lock, timeout=0.05) as ok:
            assert ok is False
            raise OSError("boom")


def test_body_exception_propagates_and_releases_when_lock_taken(hp, tmp_path):
    lock = tmp_path / "l.flock"
    with pytest.raises(OSError, match="boom"):
        with hp._exclusive(lock) as ok:
            assert ok is True
            raise OSError("boom")
    with hp._exclusive(lock, timeout=0.05) as ok:
        assert ok is True


# --- a record that cannot land is logged and queued, never silently lost ----------

def _heal_actions(m, action):
    path = m._settings_heal_dir() / "heal_log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    return [e for e in map(json.loads, filter(None, lines)) if e.get("action") == action]


def _settings_contexts(hp, settings):
    return set(hp.iter_contexts(json.loads(Path(settings).read_text(encoding="utf-8"))))


def _missing_smart_compact_events(m, settings):
    status = m._is_smart_compact_installed(json.loads(Path(settings).read_text(encoding="utf-8")))
    return [event for event, present in status.items() if not present]


def test_smart_compact_record_lost_to_a_held_lock_lands_on_the_next_heal_pass(m, hp, installed, monkeypatch):
    installed.write_text(json.dumps({"_test_marker": True}))  # a script install, no hooks yet
    assert _missing_smart_compact_events(m, installed)
    before = _settings_contexts(hp, installed)
    real_acquire = hp._acquire
    monkeypatch.setattr(hp, "_acquire", lambda *a, **k: None)  # lock never available
    m.setup_smart_compact()
    added = _settings_contexts(hp, installed) - before
    assert added
    failed = _heal_actions(m, "hook_provenance_record_failed")
    assert [(e["source"], e["entries"], e["queued"]) for e in failed] == [
        ("setup_smart_compact", len(added), True)]
    prov = hp.Provenance.from_facade(m)
    assert not [c for c in added if prov.proof(c[3], c[:3])]
    monkeypatch.setattr(hp, "_acquire", real_acquire)
    m._fix_malformed_hook_commands()  # any later run: the heal pass merges the queue
    prov = hp.Provenance.from_facade(m)
    assert all(prov.proof(c[3], c[:3]) == "ledger" for c in added)
    assert not hp.has_pending(hp.ledger_path_for(m))


def test_queued_record_never_claims_a_preserved_identical_foreign_entry(m, hp, installed, monkeypatch):
    installed.write_text(json.dumps({"_test_marker": True, "hooks": {}}))
    missing = _missing_smart_compact_events(m, installed)
    assert len(missing) >= 2, missing
    event = missing[0]
    user_cmd = m._smart_compact_hook_commands()[event]
    data = json.loads(installed.read_text())
    data["hooks"].setdefault(event, []).append({"hooks": [{"type": "command", "command": user_cmd}]})
    installed.write_text(json.dumps(data))  # the user's own copy, there first
    before = _settings_contexts(hp, installed)
    real_acquire = hp._acquire
    monkeypatch.setattr(hp, "_acquire", lambda *a, **k: None)
    m.setup_smart_compact()  # adds the others, keeps the user's copy
    added = _settings_contexts(hp, installed) - before
    assert {c[0] for c in added} == set(missing[1:])
    failed = _heal_actions(m, "hook_provenance_record_failed")
    assert [(e["source"], e["entries"]) for e in failed] == [("setup_smart_compact", len(added))]
    monkeypatch.setattr(hp, "_acquire", real_acquire)
    assert hp.record_written(m, {}, {}, [], "noop")  # a later writer run flushes the queue
    assert not hp.has_pending(hp.ledger_path_for(m))
    prov = hp.Provenance.from_facade(m)
    assert all(prov.proof(c[3], c[:3]) == "ledger" for c in added)
    assert prov.proof(user_cmd, (event, "", "command")) is None


def test_record_that_cannot_even_queue_is_logged_as_lost(m, hp, installed, monkeypatch):
    ledger = hp.ledger_path_for(m)
    ledger.with_name(ledger.name + ".pending").write_text("not a directory")
    monkeypatch.setattr(hp, "_acquire", lambda *a, **k: None)
    m.setup_smart_compact()
    failed = _heal_actions(m, "hook_provenance_record_failed")
    assert [(e["source"], e["queued"]) for e in failed] == [("setup_smart_compact", False)]
    assert "never removed" in failed[0]["reason"]


def test_unreadable_ledger_queues_and_lands_once_readable(hp, tmp_path):
    ledger = tmp_path / "hook_provenance.json"
    ledger.write_text(json.dumps({"version": 2}))  # another version: unreadable, kept
    context = ("Stop", "", "command", "x")
    assert hp.record_contexts(ledger, "s.json", [context], "t") is False
    assert hp.record_contexts(ledger, "s.json", [context], "t") is False
    assert len(list(hp._pending_dir(ledger).iterdir())) == 1  # a retry adds no second file
    ledger.unlink()
    assert hp.record(ledger, "s.json", ["other"], "t")
    data, status = hp.load_ledger(ledger)
    assert status == "ok" and hp.context_digest(context) in data["contexts"][hp.settings_key("s.json")]
    assert not hp.has_pending(ledger)


def test_cloud_record_failure_is_logged_in_the_profile_heal_log(hp, tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    after = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "cloud-cmd"}]}]}}
    monkeypatch.setattr(hp, "_acquire", lambda *a, **k: None)
    assert hp.record_cloud_write(settings, {}, after, after["hooks"]) is False
    log = tmp_path / "token-optimizer" / "settings_heal" / "heal_log.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["action"], e["source"], e["queued"]) for e in entries] == [
        ("hook_provenance_record_failed", "cloud_kit", True)]


# --- the fossil reconcile and the uninstall paths touch only our own entries ------

FOREIGN = "python3 /home/me/mytool/measure.py collect --my-flag"


def _hook(command, **extra):
    return dict({"type": "command", "command": command}, **extra)


def _our_fossil(m, mp=None):
    mp = str(m.MEASURE_PY_PATH) if mp is None else mp
    return "python3 '{0}' collect --quiet && python3 '{0}' dashboard --quiet".format(mp)


def _reconcile(m, hooks):
    settings = {"hooks": json.loads(json.dumps(hooks))}
    result = {"rewritten": 0, "removed": 0, "stop_removed": 0}
    changed = m._apply_sessionend_fossil_reconcile(settings, result)
    return changed, result, settings["hooks"]


def test_users_own_measure_py_is_untouched_in_session_end_and_stop(m, installed):
    hooks = {"SessionEnd": [{"hooks": [_hook(FOREIGN)]}],
             "Stop": [{"hooks": [_hook(FOREIGN + " --stop")]}]}
    changed, result, after = _reconcile(m, hooks)
    assert (changed, result) == (False, {"rewritten": 0, "removed": 0, "stop_removed": 0})
    assert after == hooks


def test_users_own_measure_py_survives_the_real_reconcile_pass(m, installed):
    data = json.loads(installed.read_text())
    data["hooks"]["SessionEnd"] = [{"hooks": [_hook(FOREIGN)]}]
    data["hooks"]["Stop"] = data["hooks"].get("Stop", []) + [{"hooks": [_hook(FOREIGN + " --stop")]}]
    installed.write_text(json.dumps(data))
    m._reconcile_sessionend_fossils()
    after = json.loads(installed.read_text())["hooks"]
    assert after["SessionEnd"] == [{"hooks": [_hook(FOREIGN)]}]
    assert {"hooks": [_hook(FOREIGN + " --stop")]} in after["Stop"]


@pytest.mark.parametrize("where", ["running", "removed_plugin_version", "script_install"])
def test_our_fossil_is_still_rewritten(m, installed, where):
    mp = {"running": str(m.MEASURE_PY_PATH),
          "removed_plugin_version": str(m.CLAUDE_DIR / "plugins" / "cache" / "mkt" / "token-optimizer"
                                        / "5.1.0" / "skills" / "token-optimizer" / "scripts" / "measure.py"),
          "script_install": str(m.CLAUDE_DIR / "skills" / "token-optimizer" / "scripts" / "measure.py")}[where]
    changed, result, after = _reconcile(m, {"SessionEnd": [{"hooks": [_hook(_our_fossil(m, mp)), _hook("echo mine")]}]})
    assert changed and result["rewritten"] == 1
    assert [h["command"] for h in after["SessionEnd"][0]["hooks"]] == [m.HOOK_COMMAND, "echo mine"]


@pytest.mark.parametrize("variant", [
    lambda c: c + " ; echo mine",                 # the user appended to our command
    lambda c: c.replace("--quiet", "--loud", 1),  # an edited copy
    lambda c: c.replace("token-optimizer/scripts", "token-optimizer/other", 1),
])
def test_anything_but_our_exact_fossil_is_kept(m, installed, variant):
    command = variant(_our_fossil(m))
    changed, _, after = _reconcile(m, {"SessionEnd": [{"hooks": [_hook(command)]}]})
    assert not changed and after["SessionEnd"][0]["hooks"][0]["command"] == command


def test_stop_drops_our_fossil_and_keeps_the_users(m, installed):
    ours = "python3 '%s' collect --quiet" % m.MEASURE_PY_PATH
    changed, result, after = _reconcile(m, {"Stop": [{"hooks": [_hook(ours), _hook(FOREIGN)]}]})
    assert changed and result["stop_removed"] == 1
    assert after["Stop"] == [{"hooks": [_hook(FOREIGN)]}]


def test_a_users_flush_is_not_mistaken_for_ours(m, installed):
    """A user's own session-end-flush must not make our fossil 'redundant'."""
    users_flush = "python3 /home/me/mytool/measure.py session-end-flush --trigger end"
    changed, result, after = _reconcile(m, {"SessionEnd": [{"hooks": [_hook(users_flush), _hook(_our_fossil(m))]}]})
    assert result["rewritten"] == 1 and result["removed"] == 0
    assert [h["command"] for h in after["SessionEnd"][0]["hooks"]] == [users_flush, m.HOOK_COMMAND]


def test_session_end_uninstall_keeps_the_users_measure_py(m, installed):
    settings = {"hooks": {"SessionEnd": [{"hooks": [_hook(FOREIGN), _hook(m.HOOK_COMMAND)]}]}}
    assert m._remove_token_optimizer_session_end_hooks(settings) == 1
    assert settings["hooks"]["SessionEnd"] == [{"hooks": [_hook(FOREIGN)]}]


def test_setup_hook_upgrade_never_replaces_the_users_hook(m, installed):
    installed.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [_hook(FOREIGN)]}]}}))
    m.setup_hook(dry_run=False)
    commands = [h["command"] for g in json.loads(installed.read_text())["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert commands == [FOREIGN, m.HOOK_COMMAND]


@pytest.mark.parametrize("users", [
    "python3 /home/me/mytool/measure.py session-end-flush --trigger end",
    "python3 '/home/me/mytool/measure.py' session-end-flush --trigger end",
])
def test_setup_hook_installs_ours_beside_the_users_own_flush(m, installed, capsys, users):
    installed.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [_hook(users)]}]}}))
    m.setup_hook(dry_run=False)
    assert "Nothing to do" not in capsys.readouterr().out
    commands = [h["command"] for g in json.loads(installed.read_text())["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert commands == [users, m.HOOK_COMMAND]
    assert m._sessionend_has_current_flush({"hooks": {"SessionEnd": [{"hooks": [_hook(users)]}]}}) is False


@pytest.mark.parametrize("users", [
    "python3 /home/me/mytool/measure.py session-end-flush --trigger end",
    "python3 '/home/me/mytool/measure.py' session-end-flush --trigger end",
    "/home/me/mytool/measure.py session-end-flush",
])
def test_hook_labels_never_count_the_users_own_flush(m, installed, users):
    theirs = {"hooks": {"SessionEnd": [{"hooks": [_hook(users)]}]}}
    assert m._is_hook_installed(theirs, settings_only=True) is False
    assert m._is_hook_installed(theirs) is False
    assert m._is_hook_current(theirs) is False
    ours = {"hooks": {"SessionEnd": [{"hooks": [_hook(users), _hook(m.HOOK_COMMAND)]}]}}
    assert m._is_hook_installed(ours, settings_only=True) is True
    assert m._is_hook_current(ours) is True


def test_hook_labels_count_the_shipped_session_end_entry_setup_all_hooks_wrote(m, installed):
    data = json.loads(installed.read_text())
    assert data["hooks"]["SessionEnd"]
    assert m._is_hook_installed(data, settings_only=True) is True


def test_setup_hook_still_skips_when_ours_is_current(m, installed, capsys):
    installed.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [_hook(m.HOOK_COMMAND)]}]}}))
    m.setup_hook(dry_run=False)
    assert "Nothing to do" in capsys.readouterr().out
    commands = [h["command"] for g in json.loads(installed.read_text())["hooks"]["SessionEnd"] for h in g["hooks"]]
    assert commands == [m.HOOK_COMMAND]
    assert m._sessionend_has_current_flush(json.loads(installed.read_text())) is True


def test_smart_compact_uninstall_removes_ours_and_keeps_the_users(m, installed, capsys):
    users = ["python3 /home/me/mytool/measure.py compact-capture --trigger stop", "echo compact-restore notes"]
    installed.write_text(json.dumps({"_test_marker": True}))
    m.setup_smart_compact()
    data = json.loads(installed.read_text())
    ours = sum(len(g["hooks"]) for groups in data["hooks"].values() for g in groups)
    data["hooks"]["Stop"].append({"hooks": [_hook(c) for c in users]})
    installed.write_text(json.dumps(data))
    m.setup_smart_compact(uninstall=True)
    after = json.loads(installed.read_text()).get("hooks", {})
    left = [h["command"] for groups in after.values() for g in groups for h in g["hooks"]]
    assert left == users
    assert "%d hook(s) removed" % ours in capsys.readouterr().out


# --- Codex: our command is ours only in the entry we wrote it to -----------------

from test_codex_hook_provenance import ci, chp, home, semver_root  # noqa: E402,F401


def _codex_write(home, hooks):
    path = home / "hooks.json"
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
    return path


def test_codex_uninstall_keeps_our_command_copied_into_another_event(home, semver_root):
    managed = ci._managed_hooks()
    ours = managed["Stop"][0]["hooks"][0]["command"]
    users = {"matcher": "UserMatcher", "hooks": [{"type": "command", "command": ours}]}
    path = _codex_write(home, {"Stop": managed["Stop"], "Notification": [users]})
    proof = ci._codex_proof(path)
    assert proof.group_is_ours("Stop", managed["Stop"][0])
    assert not proof.group_is_ours("Notification", users)
    ci.uninstall(Path("."), is_global=True)
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"] == {"Notification": [users]}


def test_codex_our_command_under_another_matcher_is_not_ours(home, semver_root):
    group = json.loads(json.dumps(ci._managed_hooks()["Stop"][0]))
    group["matcher"] = "mine"
    path = _codex_write(home, {"Stop": [group]})
    ci.install(Path("."), is_global=True, skip_compact_prompt=True)
    stops = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]
    assert group in stops and len(stops) == 2  # the user's copy stays; ours is added


def test_codex_command_only_record_proves_nothing(home, semver_root, hp):
    """A ledger written before entries were recorded must not widen deletion."""
    copied = ci._managed_hooks()["Stop"][0]["hooks"][0]["command"]
    legacy = "python3 /legacy/token-optimizer/hooks/run.py hooks/stop_runner.py"
    path = _codex_write(home, {"Notification": [{"hooks": [{"type": "command", "command": c}]}
                                                for c in (copied, legacy)]})
    ledger = ci._codex_proof(path).ledger_path
    assert hp.record(ledger, path, [copied, legacy], "older-install")  # the "files" section
    proof = ci._codex_proof(path)
    assert not proof.owns(("Notification", "", "command", copied))
    assert not proof.owns(("Notification", "", "command", legacy))
    ci.uninstall(Path("."), is_global=True)
    assert len(json.loads(path.read_text(encoding="utf-8"))["hooks"]["Notification"]) == 2


def test_codex_install_records_entries_not_bare_commands(home, semver_root, hp):
    ci.install(Path("."), is_global=True, skip_compact_prompt=True)
    path = home / "hooks.json"
    ledger, status = hp.load_ledger(ci._codex_proof(path).ledger_path)
    assert status == "ok" and not ledger["files"]
    data = json.loads(path.read_text(encoding="utf-8"))
    present = {c for event, gs in data["hooks"].items() for g in gs for c in chp.contexts_in(event, g)}
    assert set(ledger["contexts"][hp.settings_key(path)]) == {hp.context_digest(c) for c in present}


def test_shipped_codex_templates_carry_their_entry(hp):
    data = chp.shipped()
    for key, templates in data["templates"].items():
        assert templates and all(isinstance(t, list) and len(t) == 4 for t in templates), key


# --- Codex: the ledger, its lock and its queue never land in a user's repo --------

def test_codex_project_install_and_heal_leave_only_hooks_json_in_the_repo(home, semver_root, tmp_path, hp):
    project = tmp_path / "repo"
    project.mkdir()
    ci.install(project, skip_compact_prompt=True)
    path = project / ".codex" / "hooks.json"
    ci.heal_quoted_launcher_commands([path])  # seeds the ledger on every run
    ci.uninstall(project)
    assert sorted(p.name for p in path.parent.iterdir()) == ["hooks.json"]
    ledger = ci._codex_proof(path).ledger_path
    assert ledger.parent == home / "token-optimizer"
    assert hp.settings_key(path) in hp.load_ledger(ledger)[0]["contexts"]


# --- command echoes in the heal log and doctor never carry a likely secret --------

SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


def _unproven_with_secret(m, installed):
    command = 'OPENAI_API_KEY=%s bash -c "$(cat /opt/me/run)" %s/measure.py collect' % (SECRET, "/opt/me")
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("Stop", []).append({"hooks": [{"type": "command", "command": command}]})
    installed.write_text(json.dumps(data))
    return command


def test_heal_log_and_doctor_redact_a_secret_in_an_unproven_hook(m, hp, installed):
    command = _unproven_with_secret(m, installed)
    m._fix_malformed_hook_commands()
    logged = _heal_actions(m, "hook_unproven")
    assert [e["sha256"] for e in logged] == [hp.command_digest(command)]
    assert SECRET not in json.dumps(logged) and "REDACTED" in logged[0]["command"]
    import heal_report
    rows = heal_report.doctor_rows(m) + [("", "", line) for line in heal_report.health_lines(m)]
    left_alone = [r for r in rows if "left it alone" in r[2]]
    assert left_alone and not [r for r in rows if SECRET in r[2]]


def test_echo_shows_nothing_when_redaction_is_unavailable(hp, monkeypatch):
    monkeypatch.setitem(sys.modules, "credential_patterns", None)
    assert hp.echo_command("TOKEN=%s run" % SECRET, 200) == "(command not shown)"


def test_codex_doctor_redacts_a_secret_in_an_unrecognized_hook(home, semver_root):
    import codex_doctor
    command = "GITHUB_TOKEN=ghp_%s python3 ~/.codex/token-optimizer/measure.py stop" % ("a1" * 18)
    _codex_write(home, {"Stop": [{"hooks": [{"type": "command", "command": command}]}]})
    details = [c["detail"] for c in codex_doctor._unproven_hook_checks()]
    assert len(details) == 1 and "ghp_" + "a1" * 18 not in details[0] and "REDACTED" in details[0]


# --- the doctor lists each unproven entry, not each unproven command ---------------

def test_doctor_lists_the_same_unproven_command_once_per_entry(m, hp, installed):
    import heal_report
    command = 'bash -c "$(cat /opt/me/run)" /opt/me/measure.py collect'
    data = json.loads(installed.read_text())
    for event in ("Stop", "Notification"):
        data["hooks"].setdefault(event, []).append({"hooks": [_hook(command), _hook(command)]})
    installed.write_text(json.dumps(data))
    assert [e for e, c, _ in heal_report.unproven_hooks(m) if c == command] == ["Stop", "Notification"]


# --- measure.py cleanup never takes a user's own tool that shares our names --------

def test_cleanup_keeps_a_users_own_measure_py_and_removes_ours(m, installed):
    users = [FOREIGN, "python3 /home/me/mytool/measure.py session-end-flush --trigger end",
             "python3 /home/me/tools/read_cache.py --clear-compacted token-optimizer"]
    stop = "python3 /home/me/mytool/measure.py session-end-flush --trigger stop"
    settings = {"hooks": {"SessionEnd": [{"hooks": [_hook(c) for c in users] + [_hook(m.HOOK_COMMAND)]}],
                          "Stop": [{"hooks": [_hook(stop)]}]}}
    m._remove_our_settings_entries(settings)
    assert settings["hooks"] == {"SessionEnd": [{"hooks": [_hook(c) for c in users]}],
                                 "Stop": [{"hooks": [_hook(stop)]}]}


# --- a script install's clone is an install, whichever copy runs -------------------

def _clone_measure_py(m):
    return str(m.CLAUDE_DIR / "token-optimizer" / "skills" / "token-optimizer" / "scripts" / "measure.py")


def test_clone_fossil_is_rewritten_when_another_copy_runs(m, installed):
    assert str(m.MEASURE_PY_PATH) != _clone_measure_py(m)  # this checkout runs, not the clone
    changed, result, after = _reconcile(m, {"SessionEnd": [{"hooks": [_hook(_our_fossil(m, _clone_measure_py(m)))]}]})
    assert changed and result["rewritten"] == 1
    assert after["SessionEnd"][0]["hooks"][0]["command"] == m.HOOK_COMMAND


def test_clone_hook_is_removed_by_setup_hook_uninstall_from_another_copy(m, installed):
    clone_hook = "python3 '%s' session-end-flush --trigger end" % _clone_measure_py(m)
    settings = {"hooks": {"SessionEnd": [{"hooks": [_hook(clone_hook), _hook(FOREIGN)]}]}}
    assert m._remove_token_optimizer_session_end_hooks(settings) == 1
    assert settings["hooks"]["SessionEnd"] == [{"hooks": [_hook(FOREIGN)]}]


# --- a wrapper in front of our measure.py is not a command we wrote ---------------

@pytest.mark.parametrize("wrapper", ["sudo", "env", "nice", "/usr/bin/sudo", "py", "/opt/me/python-wrapper",
                                     "pypy3", "python3.13t", "/opt/me/pypy-wrapper", "/usr/bin/python3.13x",
                                     "/usr/bin/pythonista", "/usr/bin/python3.13-dbg-wrapper", "C:/Py/py.exe"])
def test_wrapped_measure_py_is_not_our_shape(m, installed, wrapper):
    import owned_hooks
    sh = owned_hooks._sh
    command = "%s %s session-end-flush --trigger end >/dev/null 2>&1" % (sh(wrapper), sh(str(m.MEASURE_PY_PATH)))
    assert not owned_hooks.matches_shape(m, command, owned_hooks.SESSION_END_SHAPES)


@pytest.mark.parametrize("python", ["/usr/bin/python3", "/opt/homebrew/bin/python3.14",
                                    "C:/Program Files/Python312/python.exe", "C:/Py/pythonw.exe",
                                    "/usr/local/bin/python3.13t", "/usr/local/bin/python3.14t",
                                    "C:/Py313/python3.13t.exe", "C:/Py313/pythonw3.13t.exe",
                                    "/usr/local/bin/python3.13d", "/usr/local/bin/python3.13td",
                                    "/usr/bin/python3.12-dbg", "/usr/bin/python3-dbg", "C:/Py/python_d.exe",
                                    "C:/Py313/python3.13t_d.exe", "/usr/bin/pypy3", "/usr/bin/pypy",
                                    "/opt/pypy/bin/pypy3.10", "C:/pypy3.10/pypy3.exe", "C:/pypy/pypy.exe"])
def test_generated_interpreters_still_match(m, installed, python):
    import owned_hooks
    sh = owned_hooks._sh
    command = "%s %s session-end-flush --trigger end >/dev/null 2>&1" % (sh(python), sh(str(m.MEASURE_PY_PATH)))
    assert owned_hooks.matches_shape(m, command, owned_hooks.SESSION_END_SHAPES)


# --- nothing that is not a queued record keeps the queue pending -------------------

def test_stray_files_in_the_queue_never_keep_it_pending(hp, tmp_path):
    ledger = tmp_path / "hook_provenance.json"
    qdir = hp._pending_dir(ledger)
    qdir.mkdir()
    (qdir / ".DS_Store").write_bytes(b"\0")
    (qdir / "notes.txt").write_text("mine")
    (qdir / ("a" * 64)).mkdir()  # record-named, not a file
    assert not hp.has_pending(ledger)
    bad = qdir / ("b" * 64)
    bad.write_text("{not json")
    assert hp.has_pending(ledger)
    assert hp.record(ledger, "s.json", ["x"], "t")
    assert not hp.has_pending(ledger) and not bad.exists()
    assert (qdir / ".DS_Store").exists() and (qdir / "notes.txt").exists()  # not ours: never deleted


# --- measure.py cleanup removes proven entries only -------------------------------

CLEANUP_USERS = ["curl -fsS http://localhost:8080/ensure-health", "mytool ensure-health --all",
                 "kubectl get pods # quality-cache warm", "/usr/local/bin/session-end-flush",
                 "bash launcher.sh read_cache.py --clear-compacted --quiet"]


def test_cleanup_keeps_hooks_that_only_name_our_subcommands(m, installed):
    settings = {"hooks": {"SessionEnd": [{"hooks": [_hook(c) for c in CLEANUP_USERS]}],
                          "UserPromptSubmit": [{"hooks": [_hook(c) for c in CLEANUP_USERS]}]}}
    before = json.loads(json.dumps(settings))
    assert m._remove_our_settings_entries(settings) == []
    assert settings == before


def test_cleanup_still_removes_everything_setup_all_hooks_and_the_installers_wrote(m, hp, installed):
    installed.write_text(json.dumps({"_test_marker": True}))
    m.setup_all_hooks(dry_run=False, verbose=False)
    m.setup_smart_compact()
    data = json.loads(installed.read_text())
    data["hooks"].setdefault("UserPromptSubmit", []).append(
        {"hooks": [_hook("python3 '%s' quality-cache --quiet" % m.MEASURE_PY_PATH)]})
    data["hooks"].setdefault("Stop", []).append({"hooks": [_hook(c) for c in CLEANUP_USERS]})
    m._remove_our_settings_entries(data)
    left = [h["command"] for gs in data["hooks"].values() for g in gs for h in g["hooks"]]
    assert left == CLEANUP_USERS


def test_cleanup_removes_a_shipped_entry_baked_for_a_removed_plugin_version(m, hp, installed):
    old_root = m.CLAUDE_DIR / "plugins" / "cache" / "mkt" / "token-optimizer" / "5.0.1"  # never on disk
    context = sorted(hp.instantiate_contexts(hp.SHIPPED_HOOK_CONTEXTS, [str(old_root)]), key=repr)[0]
    event, matcher, hook_type, command = context
    group = {"hooks": [{"type": hook_type, "command": command}]}
    if matcher:
        group["matcher"] = matcher
    settings = {"hooks": {event: [group]}}
    m._remove_our_settings_entries(settings)
    assert settings["hooks"] == {}


# --- measure.py cleanup removes hooks-starter.json entries pasted as shipped -------

STARTER = REPO / "skills" / "token-optimizer" / "examples" / "hooks-starter.json"


def _starter_hooks():
    return json.loads(STARTER.read_text(encoding="utf-8"))["hooks"]


def _commands(hooks):
    return [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]


def test_cleanup_removes_a_pasted_hooks_starter_and_keeps_its_echo_prompts(m, installed):
    settings = {"hooks": _starter_hooks()}
    m._remove_our_settings_entries(settings)
    assert all(c.startswith("echo ") for c in _commands(settings.get("hooks", {})))
    assert len(_commands(settings.get("hooks", {}))) == sum(
        c.startswith("echo ") for c in _commands(_starter_hooks()))


def test_cleanup_removes_every_historical_starter_entry_and_keeps_lookalikes(m, installed):
    import shipped_starter_hooks
    assert len(shipped_starter_hooks.STARTER_HOOK_CONTEXTS) >= 40
    for event, matcher, hook_type, command in sorted(shipped_starter_hooks.STARTER_HOOK_CONTEXTS):
        group = {"matcher": matcher, "hooks": [{"type": hook_type, "command": command}]} if matcher \
            else {"hooks": [{"type": hook_type, "command": command}]}
        moved = dict(group, matcher="Other") if matcher != "Other" else dict(group, matcher="Else")
        tweaked = {"hooks": [{"type": hook_type, "command": command.replace(".claude/token-optimizer", ".claude/mytool")
                                                        .replace("$MEASURE_PY", "$MY_PY")}]}
        if matcher:
            tweaked["matcher"] = matcher
        settings = {"hooks": {event: [group, moved, tweaked]}}
        m._remove_our_settings_entries(settings)
        assert settings["hooks"] == {event: [moved, tweaked]}, (event, matcher, command)


def test_frozen_starter_set_has_every_current_starter_entry_that_runs_our_code():
    import shipped_starter_hooks
    for event, groups in _starter_hooks().items():
        for group in groups:
            for hook in group["hooks"]:
                if not hook["command"].startswith("echo "):
                    context = (event, group.get("matcher", ""), hook.get("type"), hook["command"])
                    assert context in shipped_starter_hooks.STARTER_HOOK_CONTEXTS, context
