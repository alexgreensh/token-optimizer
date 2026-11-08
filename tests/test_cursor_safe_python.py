"""Cursor hook must never persist a PATH-hijackable bare `python3`.

Clone of test_copilot_safe_python.py against cursor_install: the resolver must
emit an ABSOLUTE, trusted path or fail, never a bare name that $PATH would
resolve at hook time. Environment-independent by construction (controlled files,
not assumptions about the host interpreter's ownership).
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"



@pytest.fixture(autouse=True)
def _trusted_python_env(tmp_path, monkeypatch):
    """A CONTROLLED trusted interpreter for resolver/install tests.

    The host's real interpreter is not guaranteed to pass the trust gate --
    hosted-CI tool caches extract python world-writable (measured on runner
    33618210157), so falling back to sys.executable/$PATH makes these tests
    environment-dependent. Point TOKEN_OPTIMIZER_PYTHON at a tmp interpreter
    with clean modes (0755 file in a 0755 euid-owned dir) instead. Tests that
    need a specific resolution override the env or mock the gate themselves.
    """
    d = tmp_path / "trusted-bin"
    d.mkdir(mode=0o755)
    f = d / "python3"
    f.write_bytes(b"#!/bin/sh\n")
    os.chmod(f, 0o755)
    os.chmod(d, 0o755)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", str(f))


@pytest.fixture()
def c():
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("cursor_install", None)
    yield importlib.import_module("cursor_install")


def test_resolver_returns_absolute_existing_file(c):
    r = c._resolve_safe_python()
    assert os.path.isabs(r), f"not absolute: {r}"
    assert os.path.isfile(r), f"not a real file: {r}"


def test_hook_command_bakes_the_resolved_absolute_path(c, tmp_path):
    resolved = c._resolve_safe_python()
    entries = c._hook_entries(tmp_path / "cursor_hook_bridge.py")
    cmd = entries["preToolUse"][0]["command"]
    assert resolved in cmd, f"resolved path not in hook command: {cmd}"
    assert " python3 " not in f" {cmd} " and " python " not in f" {cmd} ", cmd


def test_hook_entries_cover_all_events(c, tmp_path):
    entries = c._hook_entries(tmp_path / "cursor_hook_bridge.py")
    assert set(entries) == {
        "sessionStart", "preToolUse", "postToolUse", "preCompact", "stop", "sessionEnd",
        "afterAgentResponse", "postToolUseFailure", "afterShellExecution",
        "subagentStart", "subagentStop",
    }
    # The Shell matcher keeps non-Shell tool calls out of the hot path.
    assert entries["preToolUse"][0]["matcher"] == "Shell"
    assert all(e["timeout"] == c.HOOK_TIMEOUT_SEC for es in entries.values() for e in es)


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_trust_gate_rejects_hijackable_paths(c):
    # world-writable DIR -> anyone can swap the file
    d = tempfile.mkdtemp()
    os.chmod(d, 0o777)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert c._py_path_is_trusted(f) is False
    # world-writable FILE in an owned dir -> anyone can rewrite its bytes
    d2 = tempfile.mkdtemp()
    os.chmod(d2, 0o755)
    f2 = os.path.join(d2, "python3")
    open(f2, "w").close()
    os.chmod(f2, 0o777)
    assert c._py_path_is_trusted(f2) is False


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_trust_gate_accepts_owned_unwritable_file(c):
    d = tempfile.mkdtemp()
    os.chmod(d, 0o755)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert c._py_path_is_trusted(f) is True


def test_trust_gate_accepts_system_prefix(c):
    for p in ("/usr/bin/python3", "/opt/homebrew/bin/python3",
              "/opt/hostedtoolcache/Python/3.12/x64/bin/python"):
        if os.path.isfile(p):
            assert c._py_path_is_trusted(p) is True, p


def test_override_env_is_honored_when_trusted(c, monkeypatch):
    resolved = c._resolve_safe_python()
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", resolved)
    assert c._resolve_safe_python() == os.path.realpath(resolved)
    # A bogus override is ignored: the resolver must never persist it. On hosts
    # with no trusted fallback (hosted-CI python is world-writable) it raises
    # instead -- either outcome proves the bogus path was not honoured.
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", "/nonexistent/python3")
    try:
        fallback = c._resolve_safe_python()
    except RuntimeError as exc:
        # No trusted fallback on this host; the bogus override must still be
        # named as rejected, never silently honoured.
        assert "/nonexistent/python3" in str(exc)
    else:
        assert os.path.isfile(fallback)
        assert fallback != "/nonexistent/python3"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_sys_executable_must_pass_the_trust_gate(c, monkeypatch):
    """P0-1: sys.executable is only persisted when the trust gate accepts it.
    A writable venv interpreter (gate rejects) must fall through to the $PATH
    search, never be persisted as-is."""
    calls = []

    def fake_gate(p):
        calls.append(p)
        return os.path.abspath(p) != os.path.abspath(sys.executable)

    monkeypatch.setattr(c, "_py_path_is_trusted", fake_gate)
    # no override: the resolver must reach sys.executable and gate it
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", "")
    resolved = c._resolve_safe_python()
    assert sys.executable in calls, "gate never consulted for sys.executable"
    assert os.path.isabs(resolved) and os.path.isfile(resolved)


def test_sys_executable_returned_when_trusted(c, monkeypatch):
    monkeypatch.setattr(c, "_py_path_is_trusted", lambda p: True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", "")
    assert c._resolve_safe_python() == os.path.realpath(sys.executable)


def _fake_stat(uid_file, mode_file, uid_dir, mode_dir, gid=None, gid_dir=None):
    import stat as _s
    class R:
        def __init__(self, uid, mode, gid=None):
            self.st_uid = uid
            self.st_gid = os.getegid() if gid is None else gid
            self.st_mode = _s.S_IFREG | mode
    def fake(path, *a, **k):
        return (R(uid_dir, mode_dir, gid_dir) if str(path).endswith("bin")
                else R(uid_file, mode_file, gid))
    return fake


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_gate_accepts_ci_and_admin_owned_layouts(c, monkeypatch, tmp_path):
    """Hosted-CI and admin layouts must be trusted: root-owned interpreter in a
    root-owned 0775 dir (hostedtoolcache-as-root), and euid-owned interpreter
    in an euid-owned 0775 dir (runner/Homebrew group-writable-by-own-group)."""
    f = str(tmp_path / "bin" / "python3")
    # root-owned file+dir, dir group-writable (0775): CI root cache layout
    monkeypatch.setattr(c.os, "stat", _fake_stat(0, 0o755, 0, 0o775))
    assert c._py_path_is_trusted(f) is True
    # euid-owned file+dir, dir group-writable (0775): runner / Homebrew layout
    monkeypatch.setattr(c.os, "stat", _fake_stat(os.geteuid(), 0o755, os.geteuid(), 0o775))
    assert c._py_path_is_trusted(f) is True


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_gate_rejects_third_party_group_writable_dir(c, monkeypatch, tmp_path):
    """A group-writable dir owned by ANOTHER account lets that account swap the
    interpreter even though the file bytes are clean."""
    f = str(tmp_path / "bin" / "python3")
    other = (os.geteuid() or 1000) + 1
    monkeypatch.setattr(c.os, "stat", _fake_stat(os.geteuid(), 0o755, other, 0o775))
    assert c._py_path_is_trusted(f) is False
    # world-writable dir is rejected regardless of owner
    monkeypatch.setattr(c.os, "stat", _fake_stat(0, 0o755, 0, 0o777))
    assert c._py_path_is_trusted(f) is False


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_gate_rejects_foreign_group_writable_dir(c, monkeypatch, tmp_path):
    """An euid-owned dir that is group-writable by a FOREIGN group lets that
    group swap the interpreter; the dir's gid must be checked, not just its
    owner (the file rule already checks gid; the dir rule must match)."""
    f = str(tmp_path / "bin" / "python3")
    other_gid = (os.getegid() or 1000) + 1
    monkeypatch.setattr(c.os, "stat",
                        _fake_stat(os.geteuid(), 0o755, os.geteuid(), 0o775,
                                   gid_dir=other_gid))
    assert c._py_path_is_trusted(f) is False


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_gate_accepts_hosted_ci_self_group_writable_interpreter(c, monkeypatch, tmp_path):
    """Hosted CI (measured on ubuntu runner 33617174377): setup-python extracts
    the interpreter 0775 under the runner's OWN group. That file must be
    trusted; a foreign-group-writable or world-writable file must not."""
    f = str(tmp_path / "bin" / "python3.14")
    # self-group-writable file in an euid-owned 0775 dir: the hosted-CI layout
    monkeypatch.setattr(c.os, "stat",
                        _fake_stat(os.geteuid(), 0o775, os.geteuid(), 0o775))
    assert c._py_path_is_trusted(f) is True
    # foreign-group-writable file: rejected
    other_gid = (os.getegid() or 1000) + 1
    monkeypatch.setattr(c.os, "stat",
                        _fake_stat(os.geteuid(), 0o775, os.geteuid(), 0o755, gid=other_gid))
    assert c._py_path_is_trusted(f) is False
    # world-writable file: rejected
    monkeypatch.setattr(c.os, "stat",
                        _fake_stat(os.geteuid(), 0o777, os.geteuid(), 0o755))
    assert c._py_path_is_trusted(f) is False


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX symlink test")
def test_resolver_persists_realpath_not_symlink(c, tmp_path, monkeypatch):
    """B2-P1-1: the resolver must persist the resolved realpath, not the
    original symlink path. The gate validates realpath(cand) (the target + its
    parent dir); persisting the symlink itself leaves a swap window where an
    attacker with write access to the symlink's parent dir can redirect the
    symlink to a malicious interpreter after install."""
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir(mode=0o755)
    real_py = real_bin / "python3"
    real_py.write_bytes(b"#!/bin/sh\n")
    os.chmod(real_py, 0o755)
    # Symlink parent dir must also be clean (owned, not world-writable)
    link_dir = tmp_path / "link-bin"
    link_dir.mkdir(mode=0o755)
    link_py = link_dir / "python3"
    os.symlink(str(real_py), str(link_py))
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", str(link_py))
    resolved = c._resolve_safe_python()
    # The persisted path must be the REALPATH (the target), not the symlink
    assert resolved == str(real_py), (
        f"resolver persisted {resolved} (symlink), expected {real_py} (realpath)"
    )
    assert not os.path.islink(resolved), "persisted path is still a symlink"


def test_trust_gate_rejects_null_byte_path(c):
    """B2-P3-1: a null-byte path raises ValueError from os.path.realpath, which
    must be caught alongside OSError so one bad candidate is rejected (not
    crashing the entire resolver search)."""
    reason = c._py_trust_reason("/usr/bin/python3\x00/tmp/evil")
    assert reason is not None, "null-byte path was not rejected"
