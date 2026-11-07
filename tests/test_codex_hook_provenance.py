"""Codex hooks: install/reinstall/uninstall/heal touch a hook only when its
exact entry (event, matcher, type, command) is provably Token Optimizer's
(the provenance ledger, the current generator's output, or a shipped
generator's output instantiated for this install). Foreign hooks survive,
even ones that mention our script names or copy our command elsewhere.

Run: python3 -m pytest tests/test_codex_hook_provenance.py -v
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_compact_prompt  # noqa: E402
import codex_doctor  # noqa: E402
import codex_hook_provenance as chp  # noqa: E402
import codex_install as ci  # noqa: E402
import codex_statusline  # noqa: E402
import hook_provenance as hp  # noqa: E402


@pytest.fixture()
def home(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix=".t_codex_prov_", dir=str(Path.home())))
    for mod in (ci, codex_compact_prompt, codex_statusline, codex_doctor):
        monkeypatch.setattr(mod, "codex_home", lambda: tmp)
    return tmp


def _install_root(path):
    """A marketplace version dir as shipped: on Windows, install copies its
    hooks/windows-launcher.py next to the version dirs."""
    (path / "hooks").mkdir(parents=True, exist_ok=True)
    (path / "hooks" / "windows-launcher.py").write_bytes((REPO / "hooks" / "windows-launcher.py").read_bytes())
    return path


@pytest.fixture()
def semver_root(monkeypatch, tmp_path):
    root = _install_root(tmp_path / "market" / "token-optimizer" / "5.13.67")
    monkeypatch.setattr(ci, "_repo_root", lambda: root)
    return root


def _commands(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [c for gs in data.get("hooks", {}).values() for g in gs for c in chp.hook_commands(g)]


def _install(**kw):
    return ci.install(Path("."), is_global=True, skip_compact_prompt=True, **kw)


def _foreign(root):
    """Foreign hooks a marker/regex rule would have taken for ours: each names
    our scripts, folders, markers or env vars, or is one byte off ours."""
    ours = ci._managed_hooks()["Stop"][0]["hooks"][0]["command"]
    return [
        "python3 ~/.codex/token-optimizer/skills/token-optimizer/scripts/measure.py session-end-flush",
        "bash /opt/me/hooks/python-launcher.sh /opt/me/hooks/run.py hooks/stop_runner.py",
        'set "TOKEN_OPTIMIZER_RUNTIME_ROOT=C:\\mine" && python C:\\mine\\token-optimizer\\hooks\\run.py hooks/stop_runner.py',
        "TOKEN_OPTIMIZER_RUNTIME=codex python3 my_hook.py token-optimizer/scripts/windows-launcher",
        ours + " ",
        ours.replace(str(root), str(root) + "-fork"),
        ours.replace("5.13.67", "9.9.9"),
    ]


# --- exact-bytes proof -----------------------------------------------------------

@pytest.mark.parametrize("parent", ["plain", "Dana Q"])
@pytest.mark.parametrize("version", ["5.13.67", "5.13.40", None])
def test_shipped_templates_reproduce_the_generator_exactly(monkeypatch, tmp_path, parent, version):
    """Harvest fidelity, on whatever platform runs the suite (native Windows
    on CI): every command the generator writes for this install is proven by
    the frozen templates alone, with no ledger and no current-output shortcut."""
    root = tmp_path / parent / "token-optimizer" / (version or "devroot")
    monkeypatch.setattr(ci, "_repo_root", lambda: root)
    contexts = chp.current_contexts(ci._managed_hooks)
    assert contexts
    proof = chp.CodexProof(None, root, current_contexts=())
    assert [c for c in contexts if not proof.owns(c)] == []


def test_near_misses_of_our_commands_are_not_ours(semver_root):
    proof = ci._codex_proof()
    assert all(proof.owns(c) for c in chp.current_contexts(ci._managed_hooks))
    for command in _foreign(semver_root):
        assert not proof.owns(("Stop", "", "command", command)), command


def test_no_text_heuristics_in_codex_ownership():
    src = (SCRIPTS / "codex_hook_provenance.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"re", "shlex", "fnmatch"}
    install_src = (SCRIPTS / "codex_install.py").read_text(encoding="utf-8")
    for gone in ("_WIN_ROOT_SET_RE", "_RUNNER_SCRIPT_RE", "_RUNNER_ENTRY_RE", "_WIN_TO_DIR_RE"):
        assert gone not in install_src


# --- install / reinstall / uninstall end to end -----------------------------------

def test_foreign_hooks_naming_our_scripts_survive_install_and_uninstall(home, semver_root):
    foreign = _foreign(semver_root)
    path = home / "hooks.json"
    path.write_text(json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": c}]} for c in foreign]}}), encoding="utf-8")
    _install(enable_prompt_hooks=True)
    after = _commands(path)
    assert all(c in after for c in foreign)
    ours = chp.current_commands(lambda: ci._managed_hooks(enable_prompt_hooks=True))
    assert all(after.count(c) == 1 for c in ours)
    _install(enable_prompt_hooks=True)  # reinstall: replaced, never duplicated
    assert sorted(_commands(path)) == sorted(after)
    ci.uninstall(Path("."), is_global=True)
    assert sorted(_commands(path)) == sorted(foreign)


def test_mixed_group_loses_only_our_hook(home, semver_root):
    ours = ci._managed_hooks()["Stop"][0]["hooks"][0]
    mine = {"type": "command", "command": "python3 token-optimizer/scripts/measure.py mine"}
    path = home / "hooks.json"
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [ours, mine]}]}}), encoding="utf-8")
    ci.uninstall(Path("."), is_global=True)
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"] == [{"hooks": [mine]}]


def test_install_records_what_it_writes(home, semver_root):
    _install(enable_prompt_hooks=True)
    path = home / "hooks.json"
    ledger, status = hp.load_ledger(ci._codex_proof(path).ledger_path)
    assert status == "ok"
    recorded = set(ledger["contexts"][hp.settings_key(path)])
    data = json.loads(path.read_text(encoding="utf-8"))
    present = {c for event, gs in data["hooks"].items() for g in gs for c in chp.contexts_in(event, g)}
    assert {hp.context_digest(c) for c in present} == recorded


def test_upgrade_without_ledger_replaces_last_version_by_template(home, monkeypatch, tmp_path):
    """A pre-ledger install of a shipped version under the same parent is
    proven by template, so upgrading replaces it instead of duplicating.
    On Windows an old launcher command equal to the fresh one modulo the
    --baked-root version leaf is kept in place (no trust review); compare by
    launcher signature, which is the command itself on POSIX."""
    parent = tmp_path / "market" / "token-optimizer"
    monkeypatch.setattr(ci, "_repo_root", lambda: parent / "5.13.40")
    old = ci._managed_hooks()
    path = home / "hooks.json"
    path.write_text(json.dumps({"hooks": old}), encoding="utf-8")
    monkeypatch.setattr(ci, "_repo_root", lambda: _install_root(parent / "5.13.67"))
    _install()
    expected = [c for gs in ci._managed_hooks().values() for g in gs for c in chp.hook_commands(g)]
    old_commands = [c for gs in old.values() for g in gs for c in chp.hook_commands(g)]
    assert set(old_commands).isdisjoint(expected)
    written = _commands(path)
    assert len(written) == len(expected) and len(set(written)) == len(written)
    assert sorted(map(ci._launcher_signature, written)) == sorted(map(ci._launcher_signature, expected))


@pytest.mark.parametrize("garbage", [b"{oops", b"[]", b'{"version": 1, "files": {"k": 3}}'])
def test_corrupt_codex_ledger_falls_back_to_templates_only(home, semver_root, garbage):
    recorded_only = ("Stop", "", "command", "python3 /legacy/token-optimizer/hooks/run.py hooks/stop_runner.py")
    path = home / "hooks.json"
    ledger = ci._codex_proof(path).ledger_path
    assert hp.record_contexts(ledger, path, [recorded_only], "test")
    assert ci._codex_proof(path).owns(recorded_only)
    ledger.write_bytes(garbage)
    proof = ci._codex_proof(path)
    assert not proof.owns(recorded_only)
    assert all(proof.owns(c) for c in chp.current_contexts(ci._managed_hooks))


def test_planted_marker_rule_would_delete_foreign_hooks(home, semver_root, monkeypatch):
    """Teeth: the old substring/marker rule deletes these foreign hooks; the
    exact rule keeps them. If ownership ever regresses to text, this fails."""
    foreign = _foreign(semver_root)
    path = home / "hooks.json"
    body = json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": c}]} for c in foreign]}})
    path.write_text(body, encoding="utf-8")
    ci.uninstall(Path("."), is_global=True)
    assert sorted(_commands(path)) == sorted(foreign)
    markers = ("token-optimizer/scripts", "_runner.py", "TOKEN_OPTIMIZER_RUNTIME", "token-optimizer\\")
    monkeypatch.setattr(chp.CodexProof, "owns", lambda self, c: c is not None and any(m in c[3] for m in markers))
    path.write_text(body, encoding="utf-8")
    ci.uninstall(Path("."), is_global=True)
    assert len(_commands(path)) < len(foreign)


# --- doctor -------------------------------------------------------------------------

def test_doctor_lists_each_unproven_hook_once_in_plain_words(home, semver_root):
    ours = ci._managed_hooks()["Stop"][0]
    foreign = "python3 ~/.codex/token-optimizer/skills/token-optimizer/scripts/measure.py session-end-flush"
    unrelated = "echo hello"
    path = home / "hooks.json"
    path.write_text(json.dumps({"hooks": {"Stop": [ours, {"hooks": [
        {"type": "command", "command": foreign}, {"type": "command", "command": unrelated}]}]}}),
        encoding="utf-8")
    checks = codex_doctor._unproven_hook_checks()
    assert len(checks) == 1
    detail = checks[0]["detail"]
    assert checks[0]["status"] == "WARN" and foreign[:60] in detail and str(path) in detail
    assert "left alone" in detail and "delete" in detail
    assert codex_doctor._global_hook_check()["status"] == "OK"


@pytest.mark.parametrize("path", [SCRIPTS / "codex_hook_provenance.py",
                                  REPO / "scripts" / "gen_shipped_codex_hook_templates.py"])
def test_new_codex_modules_parse_as_python_39(path):
    ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))
    assert len(path.read_text(encoding="utf-8").splitlines()) < 1000


# --- Windows: an install from before a Python change is still ours ----------------

from pathlib import PureWindowsPath  # noqa: E402

WIN_ROOT = PureWindowsPath(r"C:\Users\me\.codex\plugins\cache\mkt\token-optimizer\5.13.67")
OLD_PY, NEW_PY = r"C:\Python312\python.exe", r"C:\Python313\python.exe"


def _win_proof(executable, root=WIN_ROOT):
    return chp.CodexProof(None, root, windows=True, executable=executable)


def _old_interpreter_entries(old_py=OLD_PY):
    """Entries the generator wrote for 5.13.40 under the old interpreter,
    each one that interpreter's own proof calls ours."""
    old = _win_proof(old_py)
    entries = [c for c in old._instances("5.13.40") if old_py in c[3] and old.owns(c)]
    assert entries, "no shipped Windows template embeds the interpreter"
    return entries


@pytest.mark.parametrize("old_py", [OLD_PY, r"C:\Program Files\Python312\python.exe"])
def test_windows_install_from_before_a_python_change_is_ours(old_py):
    new = _win_proof(NEW_PY)
    for context in _old_interpreter_entries(old_py):
        assert new.owns(context), context


@pytest.mark.parametrize("wrapper", [r"C:\tools\sudo.exe", r"C:\Windows\py.exe", "python.exe", r"C:\me\python-wrapper.exe"])
def test_windows_command_led_by_a_wrapper_is_not_ours(wrapper):
    new = _win_proof(NEW_PY)
    for context in _old_interpreter_entries():
        assert not new.owns(context[:3] + (context[3].replace(OLD_PY, wrapper),)), (wrapper, context)


def test_posix_proof_never_reads_the_interpreter_from_the_command():
    posix = chp.CodexProof(None, WIN_ROOT, windows=False, executable=NEW_PY)
    assert posix._interpreters('"%s" x' % OLD_PY) == [NEW_PY]


@pytest.mark.parametrize("command,flagged", [
    ("python ~/tools/run.py", False),
    ("python3 ~/tools/my_runner.py --go", False),
    ("python3 ~/old/token-optimizer-copy/hooks/stop_runner.py", True),
    (r"py C:\old\hooks\run.py hooks/stop_runner.py", True),
])
def test_doctor_flags_only_hooks_naming_our_hook_scripts(home, semver_root, command, flagged):
    path = home / "hooks.json"
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}}),
                    encoding="utf-8")
    assert len(codex_doctor._unproven_hook_checks()) == int(flagged)


def test_a_command_naming_many_interpreters_stays_cheap_to_check():
    """A crafted command that names hundreds of interpreter paths must not make
    the ownership check build a template set for each one."""
    import time
    proof = _win_proof(NEW_PY)
    pythons = " ".join(rf"C:\Py{n}\python.exe" for n in range(400))
    names = proof._interpreters(pythons)
    assert names[0] == NEW_PY
    assert len(names) <= chp._MAX_NAMED_INTERPRETERS + 1
    started = time.monotonic()
    assert not proof.owns(("Stop", "", "command", pythons))
    assert time.monotonic() - started < 2.0
