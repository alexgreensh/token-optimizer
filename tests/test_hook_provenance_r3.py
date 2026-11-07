"""Hook provenance, round 3: regressions for the two adversarial reviews of
post/healer-provenance (Devin and Sol, on 91e49b49). Each test is the
reviewer's repro turned around: it asserts the safe outcome, so it failed
before the matching fix landed.

Fixtures come from tests/test_hook_provenance.py.

Run: python3 -m pytest tests/test_hook_provenance_r3.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(REPO / "tests"))

from test_hook_provenance import (_add_shipped, _commands, _malformed_shipped,  # noqa: E402,F401
                                  _shipped_context, hp, installed, m)

_POSIX_ONLY = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")


def _run(code, *args, timeout=20, env=None):
    """Run ``code`` in a fresh interpreter with the scripts dir importable.
    A hang is a failure, not a stuck suite."""
    try:
        return subprocess.run([sys.executable, "-c", code, str(SCRIPTS), *map(str, args)],
                              capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, **(env or {})))
    except subprocess.TimeoutExpired as exc:
        pytest.fail("blocked for %ss: %s" % (timeout, exc))


# --- 1. Devin F1: a FIFO (or any blocking special file) at the ledger path ---

@_POSIX_ONLY
def test_fifo_ledger_load_is_unreadable_not_a_hang(tmp_path):
    fifo = tmp_path / "hook_provenance.json"
    os.mkfifo(str(fifo))
    out = _run("import sys; sys.path.insert(0, sys.argv[1]); import hook_provenance as hp\n"
               "print(hp.load_ledger(sys.argv[2])[1])", fifo, timeout=10)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "unreadable"


@_POSIX_ONLY
def test_fifo_ledger_never_blocks_record_or_heal_view(tmp_path):
    """record() and finish() go through _update, which used to block inside
    the lock (convoying every other writer); Provenance() loads the ledger on
    every heal pass."""
    fifo = tmp_path / "hook_provenance.json"
    os.mkfifo(str(fifo))
    code = ("import sys, stat, os; sys.path.insert(0, sys.argv[1]); import hook_provenance as hp\n"
            "led = sys.argv[2]\n"
            "assert hp.record(led, 'settings.json', ['echo $(x)'], 't') is False\n"
            "p = hp.Provenance(led, 'settings.json', [], [], False)\n"
            "assert p.status == 'unreadable'\n"
            "p.finish(['echo $(x)'], [('Stop', 'echo $(x)')])\n"
            "assert stat.S_ISFIFO(os.stat(led).st_mode)\n"
            "print('ok')\n")
    out = _run(code, fifo, timeout=15)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="needs /dev/zero")
def test_character_device_ledger_is_unreadable(tmp_path):
    link = tmp_path / "hook_provenance.json"
    link.symlink_to("/dev/zero")
    out = _run("import sys; sys.path.insert(0, sys.argv[1]); import hook_provenance as hp\n"
               "print(hp.load_ledger(sys.argv[2])[1])", link, timeout=10)
    assert out.stdout.strip() == "unreadable", out.stderr


# --- 2. Sol P1-1: a live/runtime hooks.json never adds deletion authority ----

_REAL_LOOKUP = '''import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
profile = Path(sys.argv[2])
other = profile / 'token-optimizer' / 'hooks' / 'hooks.json'
other.parent.mkdir(parents=True)
template = 'echo ${CLAUDE_PLUGIN_ROOT} $(printf NEVER_SHIPPED_FOREIGN)'
other.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': template}]}]}}))
import measure as m
import hook_provenance as hp
assert m._find_plugin_hooks_json() == other, m._find_plugin_hooks_json()
assert template not in hp.SHIPPED_HOOK_TEMPLATES
command = m._resolve_hook_command(template, Path(m.__file__).resolve().parents[3])
assert hp.load_ledger(hp.ledger_path_for(m))[1] == 'missing'
for event, matcher in (('PermissionRequest', 'FOREIGN_ONLY'), ('Stop', None)):
    group = {'hooks': [{'type': 'command', 'command': command}]}
    if matcher:
        group['matcher'] = matcher
    m.SETTINGS_PATH.write_text(json.dumps({'sentinel': True, 'hooks': {event: [group]}}))
    m._is_plugin_installed = lambda: True
    removed = m._fix_malformed_hook_commands()
    after = json.loads(m.SETTINGS_PATH.read_text())
    print(event, removed, command in json.dumps(after))
'''


def test_unshipped_live_hooks_json_command_is_never_removed(tmp_path):
    """Sol's real-lookup repro, no finder patch: a command that is only in the
    profile's runtime hooks.json (never shipped, never recorded) used to be
    proven and deleted. Only the frozen set and the ledger prove ownership;
    the live file may only keep the current command from churning."""
    profile = tmp_path / "profile"
    profile.mkdir()
    out = _run(_REAL_LOOKUP, profile, timeout=60,
               env={"CLAUDE_CONFIG_DIR": str(profile), "TOKEN_OPTIMIZER_RUNTIME": "claude",
                    "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap")})
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.split("\n")[:2] == ["PermissionRequest 0 True", "Stop 0 True"], out.stdout


def test_live_hooks_json_only_feeds_keep_current(m, hp, monkeypatch, tmp_path):
    live = tmp_path / "plug" / "hooks" / "hooks.json"
    live.parent.mkdir(parents=True)
    template = "echo ${CLAUDE_PLUGIN_ROOT} $(printf LIVE_ONLY)"
    live.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": template}]}]}}))
    monkeypatch.setattr(m, "_find_plugin_hooks_json", lambda: live)
    prov = hp.Provenance.from_facade(m)
    for root in (str(REPO), str(tmp_path / "plug")):
        resolved = m._resolve_hook_command(template, root)
        assert resolved not in prov.proven_templates
        assert prov.proof(resolved) is None
    assert m._resolve_hook_command(template, live.parent.parent) in prov.current_commands


# --- 3. Sol P1-2: read, proof key and write bound to one settings file -------

_RACE_CMD = "echo $(printf CONTEXT_BOUND_COMMAND)"
_B_ONLY = "echo DO_NOT_LOSE_PROFILE_B_HOOK"


def _prove(hp, m, settings, command, event="Stop", matcher=""):
    """Record ``command`` as written by Token Optimizer into ``settings``."""
    assert hp.record_contexts(hp.ledger_path_for(m), settings,
                              [(event, matcher, "command", command)], "setup_all_hooks")


def _race_profiles(m, hp, monkeypatch, tmp_path, prove_in):
    def payload(commands):
        return {"sentinel": True, "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": c} for c in commands]}]}}
    a, b = tmp_path / "profile-a.json", tmp_path / "profile-b.json"
    a.write_text(json.dumps(payload([_RACE_CMD])))
    b.write_text(json.dumps(payload([_RACE_CMD, _B_ONLY])))
    for path in prove_in:
        _prove(hp, m, {"a": a, "b": b}[path], _RACE_CMD)
    link = tmp_path / "settings.json"
    link.symlink_to(a)
    monkeypatch.setattr(m, "SETTINGS_PATH", link)
    monkeypatch.setattr(m, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: True)
    monkeypatch.setattr(m, "_find_plugin_hooks_json", lambda: REPO / "hooks" / "hooks.json")

    def retarget():
        link.unlink()
        link.symlink_to(b)
    return a, b, link, retarget


def _assert_untouched(a, b, a_before, b_before):
    assert a.read_bytes() == a_before, "profile A changed"
    assert b.read_bytes() == b_before, "profile B changed (A-derived payload written into B)"


def test_retarget_before_proof_lookup_cannot_borrow_other_files_proof(m, hp, monkeypatch, tmp_path):
    """Sol's repro: A's unrecorded hook judged with B's ledger key, and the
    A-derived payload written into B (B's unrelated hook lost)."""
    a, b, link, retarget = _race_profiles(m, hp, monkeypatch, tmp_path, prove_in=("b",))
    a_before, b_before = a.read_bytes(), b.read_bytes()
    real = hp.Provenance.from_facade.__func__
    fired = []

    def from_facade(cls, *args, **kwargs):
        if not fired:
            fired.append(retarget())
        return real(cls, *args, **kwargs)
    monkeypatch.setattr(hp.Provenance, "from_facade", classmethod(from_facade))
    assert m._fix_malformed_hook_commands() == 0
    assert fired
    _assert_untouched(a, b, a_before, b_before)


def test_retarget_at_settings_lock_refuses_the_write(m, hp, monkeypatch, tmp_path):
    """A's hook IS proven under A; the link moves to B while the healer waits
    for the settings lock. Writing the A-derived payload would wipe B."""
    a, b, link, retarget = _race_profiles(m, hp, monkeypatch, tmp_path, prove_in=("a",))
    a_before, b_before = a.read_bytes(), b.read_bytes()
    real_lock = m._settings_lock
    fired = []

    def lock(*args, **kwargs):
        if not fired:
            fired.append(retarget())
        return real_lock(*args, **kwargs)
    monkeypatch.setattr(m, "_settings_lock", lock)
    assert m._fix_malformed_hook_commands() == 0
    assert fired
    _assert_untouched(a, b, a_before, b_before)


def test_retarget_just_before_replace_is_refused(m, hp, monkeypatch, tmp_path):
    a, b, link, retarget = _race_profiles(m, hp, monkeypatch, tmp_path, prove_in=("a",))
    a_before, b_before = a.read_bytes(), b.read_bytes()
    real_write = m._write_settings_atomic_locked
    fired = []

    def write(*args, **kwargs):
        if not fired:
            fired.append(retarget())
        return real_write(*args, **kwargs)
    monkeypatch.setattr(m, "_write_settings_atomic_locked", write)
    assert m._fix_malformed_hook_commands() == 0
    assert fired
    _assert_untouched(a, b, a_before, b_before)


def test_stable_symlink_still_heals_its_target(m, hp, monkeypatch, tmp_path):
    a, b, link, _retarget = _race_profiles(m, hp, monkeypatch, tmp_path, prove_in=("a",))
    b_before = b.read_bytes()
    assert m._fix_malformed_hook_commands() == 1
    assert link.is_symlink() and link.resolve() == a.resolve()
    assert _commands(a) == [] and json.loads(a.read_text())["sentinel"] is True
    assert b.read_bytes() == b_before


# --- 4. Devin F2 / Sol P2-3: proof is per entry (event, matcher, type, command)

def _foreign_group(settings, command, event="PermissionRequest", matcher="FOREIGN_ONLY",
                   extra=()):
    data = json.loads(settings.read_text(encoding="utf-8"))
    data.setdefault("permissions", {"deny": ["Bash(rm:*)"]})
    data.setdefault("hooks", {}).setdefault(event, []).append({"matcher": matcher, "hooks": [
        {"type": "command", "command": c, "timeout": 37} for c in (command,) + tuple(extra)]})
    settings.write_text(json.dumps(data), encoding="utf-8")


def test_shipped_template_in_foreign_event_and_matcher_is_kept(m, hp, installed):
    """Sol: a user-created copy of an old template under a context we never
    shipped it in, no ledger at all. It used to be removed."""
    command = _malformed_shipped(m, hp)[0]
    hp.ledger_path_for(m).unlink()
    _foreign_group(installed, command)
    assert m._fix_malformed_hook_commands() == 0
    assert command in _commands(installed)


def test_ledger_record_from_one_context_does_not_prove_another(m, hp, installed):
    """Sol: a command genuinely written under Stop, then reused by the user
    under PermissionRequest[FOREIGN_ONLY]. Only the Stop entry is ours."""
    command = "echo $(printf user-owned)"
    before = hp.contexts_of(json.loads(installed.read_text(encoding="utf-8")))
    data = json.loads(installed.read_text(encoding="utf-8"))
    data["hooks"].setdefault("Stop", []).append({"hooks": [{"type": "command", "command": command}]})
    assert hp.record_written(m, before, data, [command], "setup_all_hooks")
    _foreign_group(installed, command)
    assert m._fix_malformed_hook_commands() == 0
    assert command in _commands(installed)


def test_devin_foreign_context_copy_kept_shipped_copy_removed(m, hp, installed):
    """Devin's end-to-end probe: the same old template bytes in their shipped
    context and in a foreign Notification[*] group with a foreign sibling.
    Only the shipped-context copy goes."""
    command = _malformed_shipped(m, hp)[0]
    _add_shipped(installed, m, hp, command)
    _foreign_group(installed, command, event="Notification", matcher="*",
                   extra=("echo foreign-sibling",))
    assert m._fix_malformed_hook_commands() == 1
    after = json.loads(installed.read_text(encoding="utf-8"))
    note = after["hooks"]["Notification"][0]
    assert [h["command"] for h in note["hooks"]] == [command, "echo foreign-sibling"]
    assert after["permissions"] == {"deny": ["Bash(rm:*)"]}
    event = _shipped_context(m, hp, command)[0]
    assert command not in [h.get("command") for g in after["hooks"].get(event, []) for h in g["hooks"]]


def test_hook_type_is_part_of_the_entry(m, hp, installed):
    command = _malformed_shipped(m, hp)[0]
    event, matcher, _type = _shipped_context(m, hp, command)
    data = json.loads(installed.read_text(encoding="utf-8"))
    group = {"hooks": [{"type": "prompt", "command": command}]}
    if matcher:
        group["matcher"] = matcher
    data["hooks"].setdefault(event, []).append(group)
    installed.write_text(json.dumps(data), encoding="utf-8")
    assert m._fix_malformed_hook_commands() == 0


def test_old_command_only_ledger_records_prove_nothing(m, hp, installed):
    """Migration, fail-safe: a pre-context record ("files", command digest
    only) cannot say which entry it was for, so it proves no settings.json
    entry. Its in-context cases are shipped templates, which still prove."""
    command = "echo $(printf recorded-before-contexts)"
    assert hp.record(hp.ledger_path_for(m), installed, [command], "setup_all_hooks")
    data = json.loads(installed.read_text(encoding="utf-8"))
    data["hooks"].setdefault("Stop", []).append({"hooks": [{"type": "command", "command": command}]})
    installed.write_text(json.dumps(data), encoding="utf-8")
    assert m._fix_malformed_hook_commands() == 0
    assert command in _commands(installed)
    template = _malformed_shipped(m, hp)[0]
    _add_shipped(installed, m, hp, template)
    assert m._fix_malformed_hook_commands() == 1


def test_record_written_never_claims_a_preserved_identical_entry(m, hp, installed):
    """A writer that adds one copy next to an identical pre-existing entry
    records one context; the pre-existing copy is never what it claims, and
    a command it did not generate is never recorded."""
    command = "echo $(printf twin)"
    entry = {"type": "command", "command": command}
    base = {"hooks": {"Stop": [{"hooks": [dict(entry)]}]}}
    before = hp.contexts_of(base)
    after = {"hooks": {"Stop": [{"hooks": [dict(entry), dict(entry)]}],
                       "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs $(x)"}]}]}}
    assert hp.added_contexts(before, after, [command, "unrelated"]) == [("Stop", "", "command", command)]
    assert hp.added_contexts(before, base, [command]) == []


def test_current_hooks_json_contexts_are_in_frozen_set(hp):
    """hooks.json changed without regenerating: run
    scripts/gen_shipped_hook_templates.py and commit the result."""
    live = set(hp.iter_contexts(json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))))
    assert live and live <= hp.SHIPPED_HOOK_CONTEXTS, sorted(live - hp.SHIPPED_HOOK_CONTEXTS)[:3]


def test_every_shipped_malformed_entry_still_healed_in_its_context(m, hp, installed):
    """Parity with main, per entry: every installer-written malformed command
    (29 on main) is removed in each context it shipped in, end to end."""
    prov = hp.Provenance.from_facade(m)
    malformed = sorted(c for c in prov.proven_contexts
                       if hp.looks_malformed(c[3], False) and c[3] not in prov.current_commands)
    assert len({c[3] for c in malformed}) >= 29
    for event, matcher, hook_type, command in malformed:
        assert prov.verdict(command, (event, matcher, hook_type)) == "remove"
    data = json.loads(installed.read_text(encoding="utf-8"))
    for event, matcher, hook_type, command in malformed:
        group = {"hooks": [{"type": hook_type, "command": command}]}
        if matcher:
            group["matcher"] = matcher
        data["hooks"].setdefault(event, []).append(group)
    installed.write_text(json.dumps(data), encoding="utf-8")
    assert m._fix_malformed_hook_commands() == len(malformed)


# --- 5. Sol P2-4: a stalled live writer keeps the ledger lock ----------------

_STALLED_WRITER = '''import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hook_provenance as hp
real = hp._write_atomic
def paused(path, data):
    Path(sys.argv[3]).touch()
    while not Path(sys.argv[4]).exists():
        time.sleep(.02)
    return real(path, data)
hp._write_atomic = paused
print(hp.record(sys.argv[2], 'settings.json', ['writer-A'], 'A'))
'''


def test_stalled_live_writer_never_lets_a_second_success_be_lost(hp, tmp_path):
    """Sol's repro: A holds the lock, then stalls past the old 10s lease
    (sleeping laptop, debugger, slow disk) while still alive. B must not be
    told it succeeded and then lose its record to A's older snapshot: B
    either fails honestly (lock still held) or both records survive."""
    ledger = tmp_path / hp.LEDGER_NAME
    ready, release = tmp_path / "ready", tmp_path / "release"
    proc = subprocess.Popen([sys.executable, "-c", _STALLED_WRITER, str(SCRIPTS), str(ledger),
                             str(ready), str(release)], stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        time.sleep(11)
        b_ok = hp.record(ledger, "settings.json", ["writer-B"], "B")
        release.touch()
        out, _ = proc.communicate(timeout=30)
        assert out.strip() == "True"
        entries = hp.load_ledger(ledger)[0]["files"][hp.settings_key("settings.json")]
        assert hp.command_digest("writer-A") in entries
        if b_ok:
            assert hp.command_digest("writer-B") in entries, "B reported success but its record was lost"
        assert hp.record(ledger, "settings.json", ["writer-B"], "B")
        entries = hp.load_ledger(ledger)[0]["files"][hp.settings_key("settings.json")]
        assert {hp.command_digest("writer-A"), hp.command_digest("writer-B")} <= set(entries)
    finally:
        release.touch()
        if proc.poll() is None:
            proc.kill()


def test_dead_writer_releases_the_lock(hp, tmp_path):
    """The lock lives as long as its owner: a writer killed mid-write never
    leaves the ledger locked."""
    ledger = tmp_path / hp.LEDGER_NAME
    ready = tmp_path / "ready"
    proc = subprocess.Popen([sys.executable, "-c", _STALLED_WRITER, str(SCRIPTS), str(ledger),
                             str(ready), str(tmp_path / "never")])
    deadline = time.monotonic() + 15
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert ready.exists()
    proc.kill()
    proc.wait(timeout=10)
    started = time.monotonic()
    assert hp.record(ledger, "settings.json", ["after-kill"], "C")
    assert time.monotonic() - started < 3


@_POSIX_ONLY
def test_fifo_at_lock_path_never_blocks(hp, tmp_path):
    ledger = tmp_path / hp.LEDGER_NAME
    for name in (hp.LEDGER_NAME + ".lock", hp.LEDGER_NAME + ".flock"):
        os.mkfifo(str(tmp_path / name))
    out = _run("import sys; sys.path.insert(0, sys.argv[1]); import hook_provenance as hp\n"
               "print(hp.record(sys.argv[2], 'settings.json', ['x'], 't'))", ledger, timeout=15)
    assert out.stdout.strip() == "False", out.stderr


# --- 6. Sol P2-5: every hook-writing path records exactly what it writes -----

_CLOUD_RUN = "/cache/token-optimizer-cloud/token-optimizer-cloud-run.sh hooks/run.py"
_CLOUD_WRITE = '''import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("cloud_kit", sys.argv[1])
ck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ck)
print(ck.write_settings_hooks(Path(sys.argv[2]), json.loads(sys.argv[3])))
'''


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("TOKEN_OPTIMIZER_SNAPSHOT_DIR", "CLAUDE_PLUGIN_DATA", "CLAUDE_CONFIG_DIR")}
    env.update(extra)
    return env


def test_cloud_settings_write_records_only_what_it_adds(hp, tmp_path):
    """Sol's repro inverted: cloud_kit.write_settings_hooks wrote hooks and no
    ledger. It runs under python -I with no sibling imports on sys.path, so
    this loads it by path exactly that way."""
    claude = tmp_path / "cloud-profile"
    claude.mkdir()
    path = claude / "settings.json"
    twin = {"type": "command", "command": _CLOUD_RUN + " --twin"}
    path.write_text(json.dumps({"keep": 1, "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "theirs $(x)"}]}],
        "Stop": [{"hooks": [dict(twin)]}]}}))
    hook_map = {"Stop": [{"hooks": [{"type": "command", "command": _CLOUD_RUN}, dict(twin)]}]}
    out = subprocess.run([sys.executable, "-I", "-c", _CLOUD_WRITE, str(SCRIPTS / "cloud_kit.py"),
                          str(path), json.dumps(hook_map)], capture_output=True, text=True,
                         timeout=60, env=_clean_env())
    assert out.returncode == 0 and out.stdout.strip() == "True", out.stderr
    ledger = claude / "token-optimizer" / "settings_heal" / hp.LEDGER_NAME
    data, status = hp.load_ledger(ledger)
    assert status == "ok"
    entries = data["contexts"][hp.settings_key(path)]
    assert set(entries) == {hp.context_digest(("Stop", "", "command", _CLOUD_RUN))}
    assert {v["source"] for v in entries.values()} == {"cloud_kit"}
    # The healer in that profile reads the same ledger under the same key.
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import measure as m, hook_provenance as hp\n"
             "p = hp.Provenance.from_facade(m)\n"
             "print(hp.ledger_path_for(m) == __import__('pathlib').Path(sys.argv[2]),"
             " p.proof(sys.argv[3], ('Stop', '', 'command')))")
    out = subprocess.run([sys.executable, "-c", probe, str(SCRIPTS), str(ledger), _CLOUD_RUN],
                         capture_output=True, text=True, timeout=60,
                         env=_clean_env(CLAUDE_CONFIG_DIR=str(claude), TOKEN_OPTIMIZER_RUNTIME="claude"))
    assert out.stdout.strip() == "True ledger", out.stdout + out.stderr


def test_stale_path_rewrite_records_only_entries_already_proven_ours(m, hp, installed, monkeypatch, tmp_path):
    """_fix_stale_settings_paths substitutes text inside ANY hook naming an
    old plugin-cache root, foreign ones included, so its output can be part
    foreign. A rewritten entry is recorded only when the entry it replaced
    was already proven ours in that context."""
    stale = tmp_path / "x" / "plugins" / "cache" / "org" / "token-optimizer" / "1.0.0"
    ours = "python3 %s/skills/token-optimizer/scripts/measure.py ensure-health $(true)" % stale
    foreign = "python3 %s/vendor/tool.py $(echo hi)" % stale
    data = json.loads(installed.read_text(encoding="utf-8"))
    data["hooks"].setdefault("Notification", []).append(
        {"matcher": "x", "hooks": [{"type": "command", "command": ours}]})
    data["hooks"].setdefault("Notification", []).append(
        {"matcher": "y", "hooks": [{"type": "command", "command": foreign}]})
    installed.write_text(json.dumps(data), encoding="utf-8")
    assert hp.record_contexts(hp.ledger_path_for(m), installed,
                              [("Notification", "x", "command", ours)], "setup_all_hooks")
    monkeypatch.setattr(m, "_is_running_from_plugin_cache", lambda: True)
    assert m._fix_stale_settings_paths() == 1
    root = str(Path(m.__file__).resolve().parents[3])
    new_ours, new_foreign = ours.replace(str(stale), root), foreign.replace(str(stale), root)
    assert {new_ours, new_foreign} <= set(_commands(installed))
    entries = hp.load_ledger(hp.ledger_path_for(m))[0]["contexts"][hp.settings_key(installed)]
    assert hp.context_digest(("Notification", "x", "command", new_ours)) in entries
    assert hp.context_digest(("Notification", "y", "command", new_foreign)) not in entries
    assert entries[hp.context_digest(("Notification", "x", "command", new_ours))]["source"] == "fix_stale_settings_paths"


def test_sessionend_fossil_rewrite_records_hook_command_in_its_entry(m, hp, installed):
    own = m._get_measure_py_path()
    fossil = "python3 %s collect --quiet && python3 %s dashboard --quiet" % (own, own)
    data = json.loads(installed.read_text(encoding="utf-8"))
    data["hooks"]["SessionEnd"] = [{"hooks": [{"type": "command", "command": fossil, "timeout": 30}]}]
    data["hooks"].setdefault("Notification", []).append(
        {"hooks": [{"type": "command", "command": m.HOOK_COMMAND}]})  # preserved twin elsewhere
    installed.write_text(json.dumps(data), encoding="utf-8")
    assert m._reconcile_sessionend_fossils()["rewritten"] == 1
    entries = hp.load_ledger(hp.ledger_path_for(m))[0]["contexts"][hp.settings_key(installed)]
    mine = hp.context_digest(("SessionEnd", "", "command", m.HOOK_COMMAND))
    assert mine in entries and entries[mine]["source"] == "sessionend_fossil_reconcile"
    assert hp.context_digest(("Notification", "", "command", m.HOOK_COMMAND)) not in entries


def test_codex_launcher_repair_never_rewrites_or_records_a_foreign_handler(hp, tmp_path):
    """Pin (already true since d7fed0d1): the Codex launcher repair touches a
    group only when EVERY handler in it is proven ours, and records only
    the commands it rewrote there."""
    sys.path.insert(0, str(SCRIPTS))
    import codex_install
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    ours = '"C:\\\\Py\\\\python.exe" "C:\\\\TO\\\\%s.py" --baked-root "C:\\\\TO\\\\5.13.0" stop' % (
        codex_install._LAUNCHER_MARKER)
    ours2, foreign = ours.replace("stop", "start"), ours.replace("stop", "theirs")
    path.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": ours}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": ours2},
                                    {"type": "command", "command": foreign}]}]}}))
    led = path.parent / hp.CODEX_LEDGER_NAME
    assert hp.record(led, path, [ours, ours2], "codex_install")
    assert codex_install.heal_quoted_launcher_commands(paths=[path]) == 1
    after = json.loads(path.read_text())
    fixed = after["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert fixed == codex_install._WIN_UNQUOTED_PREFIX + " " + ours
    assert [h["command"] for h in after["hooks"]["SessionStart"][0]["hooks"]] == [ours2, foreign]
    recorded = set(hp.load_ledger(led)[0]["files"][hp.settings_key(path)])
    assert hp.command_digest(fixed) in recorded
    assert hp.command_digest(foreign) not in recorded
    assert hp.command_digest(codex_install._WIN_UNQUOTED_PREFIX + " " + foreign) not in recorded
