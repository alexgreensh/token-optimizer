"""Hook provenance: the malformed-hook healer removes only entries Token
Optimizer can prove it wrote (ledger record or byte-identical shipped
template for this install) that are also malformed. Everything else is left
alone and reported once.

Companion to tests/test_malformed_detector_installer_resolver.py, which keeps
the historical foreign-preserve corpus. This file covers the ledger itself,
installer recording, migration seeding, report-once logging, Sol's foreign
executions end to end, a planted fuzzy matcher the corpus must catch, Windows
spellings, concurrency and Python 3.9 syntax.

Run: python3 -m pytest tests/test_hook_provenance.py -v
"""
from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
OWN = f"{REPO}/skills/token-optimizer/scripts/measure.py"


@pytest.fixture()
def m(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tempfile.mkdtemp(prefix="to-hp-"))
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("measure", None)
    import measure
    monkeypatch.setattr(measure, "CONFIG_DIR", tmp_path / "to-config")
    monkeypatch.setattr(measure, "_find_plugin_hooks_json", lambda: REPO / "hooks" / "hooks.json")
    return measure


@pytest.fixture()
def hp():
    sys.path.insert(0, str(SCRIPTS))
    import hook_provenance
    return hook_provenance


@pytest.fixture()
def installed(m, monkeypatch, tmp_path):
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({"_test_marker": True}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(REPO))
    monkeypatch.setattr(m, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(m, "SETTINGS_PATH", settings)
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)
    monkeypatch.setattr(m, "_is_running_from_plugin_cache", lambda: False)
    monkeypatch.setattr(m, "_is_running_from_synced_plugin", lambda: False)
    monkeypatch.setattr(m, "_read_config_flag",
                        lambda key, default=0: 0 if key == "last_hook_heal_check" else default)
    assert m.setup_all_hooks(dry_run=False, verbose=False)["added"] > 0
    return settings


def _commands(settings):
    data = json.loads(Path(settings).read_text(encoding="utf-8"))
    return [h.get("command", "") for groups in data.get("hooks", {}).values()
            for g in groups for h in g.get("hooks", [])]


def _add(settings, *commands, event="PreToolUse"):
    data = json.loads(settings.read_text(encoding="utf-8"))
    data.setdefault("hooks", {}).setdefault(event, []).append(
        {"matcher": "Bash", "hooks": [{"type": "command", "command": c} for c in commands]})
    settings.write_text(json.dumps(data), encoding="utf-8")


def _record(m, hp, *commands, event="PreToolUse", matcher="Bash"):
    """Record commands as Token Optimizer-written in the context ``_add`` uses."""
    contexts = [(event, matcher, "command", c) for c in commands]
    m._settings_heal_ensure_dir()
    return hp.record_contexts(hp.ledger_path_for(m), m.SETTINGS_PATH, contexts, "test")


def _shipped_context(m, hp, command):
    """The first shipped (event, matcher, type) this exact command proves in."""
    return sorted(c[:3] for c in hp.Provenance.from_facade(m).proven_contexts if c[3] == command)[0]


def _add_shipped(settings, m, hp, command):
    """Place a shipped command where hooks.json shipped it."""
    event, matcher, hook_type = _shipped_context(m, hp, command)
    data = json.loads(settings.read_text(encoding="utf-8"))
    group = {"hooks": [{"type": hook_type, "command": command}]}
    if matcher:
        group["matcher"] = matcher
    data.setdefault("hooks", {}).setdefault(event, []).append(group)
    settings.write_text(json.dumps(data), encoding="utf-8")


def _contexts(hp, settings):
    return set(hp.iter_contexts(json.loads(Path(settings).read_text(encoding="utf-8"))))


def _heal_log(m):
    path = m._settings_heal_dir() / "heal_log.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _malformed_shipped(m, hp, root=REPO):
    """Every shipped template that, resolved for `root`, the healer must heal."""
    return sorted({m._resolve_hook_command(t, root) for t in hp.SHIPPED_HOOK_TEMPLATES
                   if hp.looks_malformed(m._resolve_hook_command(t, root), False)})


# --- frozen template set --------------------------------------------------------

def test_current_hooks_json_commands_are_in_frozen_set(hp):
    """hooks.json changed without regenerating the frozen set: run
    scripts/gen_shipped_hook_templates.py and commit the result."""
    data = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    live = {h["command"] for gs in data["hooks"].values() for g in gs for h in g["hooks"]}
    assert live <= hp.SHIPPED_HOOK_TEMPLATES, sorted(live - hp.SHIPPED_HOOK_TEMPLATES)[:3]


def test_every_installer_written_malformed_hook_is_still_healed(m, hp):
    """Parity with main: main's text detector healed the older resolver
    spellings setup_all_hooks wrote (29 measured on main @ 02f4387c). Each is
    a shipped template and must still be removed; current ones never are."""
    healed = _malformed_shipped(m, hp)
    assert len(healed) >= 29
    assert all(m._is_malformed_to_hook(c) for c in healed)
    data = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for groups in data["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert not m._is_malformed_to_hook(m._resolve_hook_command(hook["command"], REPO))


@pytest.mark.parametrize("module", ["hook_provenance.py", "hook_heal.py"])
def test_removal_path_has_no_text_heuristics(hp, module):
    """Guard against regrowth: the provenance module and the heal transaction
    must not tokenize, pattern-match or normalize commands."""
    tree = ast.parse((Path(hp.__file__).parent / module).read_text(encoding="utf-8"))
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"re", "shlex", "fnmatch", "glob"}
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"lower", "casefold", "strip", "lstrip", "rstrip", "startswith", "endswith"}


# --- installer recording (invariant 6) ---------------------------------------

def test_setup_all_hooks_records_every_command_it_writes(m, hp, installed):
    ledger, status = hp.load_ledger(hp.ledger_path_for(m))
    assert status == "ok"
    entries = ledger["contexts"][hp.settings_key(installed)]
    assert {hp.context_digest(c) for c in _contexts(hp, installed)} == set(entries)
    assert {v["source"] for v in entries.values()} == {"setup_all_hooks"}


def test_other_installers_record_their_commands(m, hp, installed):
    installed.write_text(json.dumps({"_test_marker": True}), encoding="utf-8")
    m.setup_hook(dry_run=False)
    m.setup_smart_compact(dry_run=False)
    ledger, _ = hp.load_ledger(hp.ledger_path_for(m))
    entries = ledger["contexts"][hp.settings_key(installed)]
    sources = {v["source"] for v in entries.values()}
    assert {"setup_hook", "setup_smart_compact"} <= sources
    for context in _contexts(hp, installed):
        assert hp.context_digest(context) in entries, context


def test_codex_install_records_its_managed_commands(hp, tmp_path):
    path = tmp_path / "codex" / "hooks.json"
    path.parent.mkdir()
    data = {"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "ours --stop"}]},
        {"hooks": [{"type": "command", "command": "theirs --stop"}]},
    ]}}
    written = []
    import codex_hook_provenance as chp
    ours = ("Stop", "", "command", "ours --stop")
    proof = chp.CodexProof(path, tmp_path / "token-optimizer" / "5.13.67", current_contexts=[ours],
                           ledger_dir=tmp_path / "codex-state")
    hp.write_codex_hooks(path, data, proof, lambda p, d: written.append((p, d)))
    assert written == [(path, data)]
    ledger, status = hp.load_ledger(proof.ledger_path)
    assert status == "ok"
    assert set(ledger["contexts"][hp.settings_key(path)]) == {hp.context_digest(ours)}


# --- migration seeding and report-once (invariants 3, 4) ---------------------

def test_first_run_seeds_only_exact_template_matches(m, hp, installed):
    ledger_path = hp.ledger_path_for(m)
    ledger_path.unlink()  # an install that predates the ledger
    foreign = "python3 /opt/vendor/measure.py --quiet"
    claude = f"python3 $(which python3) {OWN} ensure-health"
    _add(installed, foreign, claude)
    before = installed.read_bytes()
    assert m._fix_malformed_hook_commands() == 0
    assert installed.read_bytes() == before
    ledger, _ = hp.load_ledger(ledger_path)
    key = hp.settings_key(installed)
    seeded = set(ledger["contexts"][key])
    expected = {hp.context_digest(c) for c in _contexts(hp, installed)
                if c in hp.Provenance.from_facade(m).proven_contexts}
    assert seeded == expected and expected
    assert {v["cmd"] for v in ledger["contexts"][key].values()}.isdisjoint(
        {hp.command_digest(foreign), hp.command_digest(claude)})
    assert {v["source"] for v in ledger["contexts"][key].values()} == {"migration-seed"}
    assert key in ledger["contexts_seeded"]
    stamp = ledger_path.read_bytes()
    assert m._fix_malformed_hook_commands() == 0
    ledger2, _ = hp.load_ledger(ledger_path)
    assert ledger2["contexts"] == ledger["contexts"]
    assert ledger_path.read_bytes() == stamp  # nothing new to seed or report


def test_unproven_entries_logged_once_and_never_counted(m, hp, installed, capsys):
    claude = f"python3 $(which python3) {OWN} ensure-health"
    unrelated = "python3 $(which python3) /opt/other/tool.py"
    _add(installed, claude, unrelated)
    assert m._fix_malformed_hook_commands() == 0
    assert m._fix_malformed_hook_commands() == 0
    entries = [e for e in _heal_log(m) if e["action"] == "hook_unproven"]
    assert len(entries) == 1
    assert entries[0]["sha256"] == hp.command_digest(claude)
    assert entries[0]["command"] == hp.echo_command(claude, 200) and entries[0]["event"] == "PreToolUse"
    assert claude in _commands(installed) and unrelated in _commands(installed)
    out = capsys.readouterr()
    assert "Removed" not in out.out + out.err


# --- ledger failure modes (invariant 2) ----------------------------------------

def _ours_and_template(m, hp, installed):
    ledger_only = f"python3 $(which python3) {OWN} ensure-health"
    assert _record(m, hp, ledger_only)
    template = _malformed_shipped(m, hp)[0]
    return ledger_only, template


def test_missing_ledger_falls_back_to_templates_only(m, hp, installed):
    ledger_only, template = _ours_and_template(m, hp, installed)
    hp.ledger_path_for(m).unlink()
    assert m._is_malformed_to_hook(ledger_only) is False
    assert m._is_malformed_to_hook(template) is True


@pytest.mark.parametrize("garbage", [b"{not json", b"\xff\xfe", b"[]",
                                     b'{"version": 1, "files": {"k": []}}',
                                     b'{"version": "2", "files": {}}'])
def test_corrupt_ledger_template_only_then_reset_once(m, hp, installed, garbage):
    ledger_only, template = _ours_and_template(m, hp, installed)
    path = hp.ledger_path_for(m)
    path.write_bytes(garbage)
    assert hp.load_ledger(path)[1] == "corrupt"
    assert m._is_malformed_to_hook(ledger_only) is False
    assert m._is_malformed_to_hook(template) is True
    _add(installed, ledger_only)
    _add_shipped(installed, m, hp, template)
    assert m._fix_malformed_hook_commands() == 1  # the template only
    assert ledger_only in _commands(installed) and template not in _commands(installed)
    assert len(list(path.parent.glob(path.name + ".corrupt-*"))) == 1
    assert hp.load_ledger(path)[1] == "ok"
    assert [e["action"] for e in _heal_log(m)].count("hook_provenance_reset") == 1


def test_unreadable_ledger_is_never_overwritten(m, hp, installed):
    ledger_only, template = _ours_and_template(m, hp, installed)
    path = hp.ledger_path_for(m)
    path.unlink()
    path.mkdir()  # reads raise IsADirectoryError / PermissionError
    assert hp.load_ledger(path)[1] == "unreadable"
    assert m._is_malformed_to_hook(ledger_only) is False
    assert m._is_malformed_to_hook(template) is True
    assert _record(m, hp, "x") is False
    assert path.is_dir()


def test_concurrent_writers_lose_nothing(hp, tmp_path):
    """Eight processes record 25 distinct commands each into one ledger."""
    ledger = tmp_path / "heal" / hp.LEDGER_NAME
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import hook_provenance as hp\n"
            "w = sys.argv[3]\n"
            "for i in range(25):\n"
            "    assert hp.record(sys.argv[2], 'settings.json', ['cmd-%s-%d' % (w, i)], 'w' + w)\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS), str(ledger), str(w)])
             for w in range(8)]
    assert [p.wait(timeout=120) for p in procs] == [0] * 8
    data, status = hp.load_ledger(ledger)
    assert status == "ok"
    want = {hp.command_digest("cmd-%d-%d" % (w, i)) for w in range(8) for i in range(25)}
    assert set(data["files"][hp.settings_key("settings.json")]) == want
    assert not list(ledger.parent.glob(".hook_provenance-*"))  # no temp leftovers


# --- Sol's foreign executions, end to end (invariant 3) ----------------------

def _sol_foreign_commands(tmp_path):
    prefix = str(tmp_path / "vendor ")
    driver = tmp_path / "foreign_driver_do_not_remove.py"
    cmds = [
        # v8/v8b: spellings that only mention our path
        'python3 "' + prefix + OWN + '" $(printf harmless)',
        "python3 '" + prefix + OWN + "' $(printf harmless)",
        'python3 "' + prefix + '"' + OWN + ' $(printf harmless)',
        'python3 "\'' + OWN + '" $(printf harmless)',
        "python3 " + shlex.quote(OWN.replace("/", "\\")) + " $(printf harmless)",
        "python3 " + str(driver) + ' --source "' + OWN + '" $(printf harmless)',
        # v8c P1: quoted literals the shell never expands
        "python3 '$HOME/../../skills/token-optimizer/scripts/measure.py' measure.py $(printf harmless)",
        "python3 '~/skills/token-optimizer/scripts/measure.py' measure.py $(printf harmless)",
        "python3 '${CLAUDE_PLUGIN_ROOT}/skills/token-optimizer/scripts/measure.py' measure.py $(printf h)",
        "'~/.claude/token-optimizer/skills/token-optimizer/scripts/measure.py' $(printf harmless)",
        # v8c P1: option values are not the executed script
        "python3 -X " + OWN + " /opt/vendor/foreign.py $(printf harmless)",
        "python3 -W " + OWN + " /opt/vendor/foreign.py $(printf harmless)",
        "python3 -X " + OWN + " -c \"print('FOREIGN_HOOK_EXECUTED')\" $(printf harmless)",
        "python3 -X " + OWN + " -m foreign_module $(printf harmless)",
        "python3 -c " + OWN + " $(printf harmless)",
        "python3 -m " + OWN + " $(printf harmless)",
        # v8c P1: an executable merely named python3
        str(tmp_path / "foreign-bin" / "python3") + " " + OWN + " $(printf harmless)",
    ]
    return cmds


def test_sol_foreign_executions_survive_the_real_healer(m, hp, installed, tmp_path):
    foreign = _sol_foreign_commands(tmp_path)
    ours = _malformed_shipped(m, hp)[0]
    data = json.loads(installed.read_text(encoding="utf-8"))
    data.update({"permissions": {"deny": ["Bash(rm:*)"]}, "env": {"KEEP": "1"}})
    data["hooks"]["SessionStart"].append({"matcher": "startup", "hooks": [
        {"type": "command", "command": c, "timeout": 17} for c in foreign]})
    installed.write_text(json.dumps(data), encoding="utf-8")
    _add_shipped(installed, m, hp, ours)
    assert m._fix_malformed_hook_commands() == 1
    after = json.loads(installed.read_text(encoding="utf-8"))
    assert after["permissions"] == data["permissions"] and after["env"] == data["env"]
    remaining = _commands(installed)
    assert all(c in remaining for c in foreign)
    assert ours not in remaining
    for c in foreign:
        assert m._is_malformed_to_hook(c) is False, c


def test_planted_fuzzy_matcher_is_caught(m, hp, installed, monkeypatch):
    """Teeth check: each variant differs from a proven malformed hook only by
    a quote, separator, case or path prefix, so it is NOT ours. A matcher that
    normalizes any of those would delete them; the exact rule must not."""
    ours = _malformed_shipped(m, hp)[0]
    root = str(REPO)
    variants = [
        ours.replace('"$b"', "'$b'"),
        ours.replace(root, root.replace("/", "\\")),
        ours.replace(root, root.upper()),
        ours.replace(root, root + "-evil"),
        ours + " ",
    ]
    assert len(set(variants)) == len(variants) and ours not in variants
    for variant in variants:  # same event/matcher/type as ours: only the bytes differ
        _add_shipped(installed, m, hp, ours)
        data = json.loads(installed.read_text(encoding="utf-8"))
        event = _shipped_context(m, hp, ours)[0]
        data["hooks"][event][-1]["hooks"][0]["command"] = variant
        installed.write_text(json.dumps(data), encoding="utf-8")
    before = installed.read_bytes()
    assert m._fix_malformed_hook_commands() == 0
    assert installed.read_bytes() == before

    real_proof = hp.Provenance.proof

    def norm(c):
        return c.replace("'", '"').replace("\\", "/").lower().replace("-evil", "").strip()

    def fuzzy(self, command, context=None):
        if real_proof(self, command, context):
            return "template"
        wanted = norm(command)
        return "template" if any(norm(p[3]) == wanted and (context is None or p[:3] == tuple(context))
                                 for p in self.proven_contexts) else None

    monkeypatch.setattr(hp.Provenance, "proof", fuzzy)
    assert m._fix_malformed_hook_commands() == len(variants)
    assert not any(v in _commands(installed) for v in variants)


# --- Windows spellings -------------------------------------------------------------

def test_windows_backslash_own_hook_proven_by_exact_bytes(m, hp, monkeypatch, tmp_path):
    """A Windows install root is substituted two ways by shipped installers:
    natively (backslashes) and by the current resolver (forward slashes).
    Both exact strings prove; any other spelling of the same path does not."""
    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    root = "C:\\Users\\Dana Q\\.claude\\token-optimizer"
    old = next(t for t in sorted(hp.SHIPPED_HOOK_TEMPLATES)
               if "${D%/*}" in t and "read_cache.py --quiet" in t)
    native = old.replace("${CLAUDE_PLUGIN_ROOT}", root)
    forward = m._resolve_hook_command(old, root)
    assert "\\" in native and "C:/Users/Dana Q/" in forward
    settings = "C:\\Users\\Dana Q\\.claude\\settings.json"
    ledger = tmp_path / hp.LEDGER_NAME
    proven = hp.instantiate_contexts(hp.SHIPPED_HOOK_CONTEXTS, [root], m._resolve_hook_command)
    prov = hp.Provenance(ledger, settings, proven, set(), darwin=False)
    ctx = sorted(c[:3] for c in proven if c[3] == native)[0]
    assert ctx == sorted(c[:3] for c in proven if c[3] == forward)[0]
    assert prov.verdict(native, ctx) == "remove" and prov.verdict(forward, ctx) == "remove"
    for other in (native.replace("C:\\", "c:\\"), native.replace("\\.claude", "/.claude"),
                  forward.replace("C:/", "C:\\"), native.replace("Dana Q", "Dana  Q")):
        assert prov.verdict(other, ctx) == "unproven", other
    claude = "python3 $(which python3) " + root + "\\skills\\token-optimizer\\scripts\\measure.py"
    here = ("PreToolUse", "Bash", "command")
    assert prov.verdict(claude, here) == "unproven"
    assert hp.record_contexts(ledger, settings, [here + (claude,)], "setup_all_hooks")
    prov = hp.Provenance(ledger, settings, proven, set(), darwin=False)
    assert prov.verdict(claude, here) == "remove"
    assert prov.verdict(claude, ("Stop", "", "command")) == "unproven"
    assert prov.verdict(claude.replace("\\", "/"), here) == "unproven"


def test_current_installer_commands_never_removed_even_if_resolver_drifts(m, hp, installed, monkeypatch):
    """If hooks.json ever gains a subshell the malformation test does not
    know, what setup_all_hooks writes today still stays (no remove/re-add
    loop): the current-command guard wins over the malformation test."""
    monkeypatch.setattr(hp, "CURRENT_RESOLVER", "no-such-resolver")
    assert all(hp.looks_malformed(c, False) for c in _commands(installed) if 'D="$(ls' in c)
    before = installed.read_bytes()
    assert m._fix_malformed_hook_commands() == 0
    assert installed.read_bytes() == before


# --- Python 3.9 ------------------------------------------------------------------

@pytest.mark.parametrize("path", [SCRIPTS / "hook_provenance.py", SCRIPTS / "shipped_hook_templates.py",
                                  SCRIPTS / "shipped_starter_hooks.py",
                                  REPO / "scripts" / "gen_shipped_hook_templates.py"])
def test_new_modules_parse_as_python_39(path):
    ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))
    assert len(path.read_text(encoding="utf-8").splitlines()) < 1000
