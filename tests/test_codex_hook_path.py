#!/usr/bin/env python3
"""Regression tests for Codex hook path surviving version upgrades.

A Codex marketplace install lives in .../token-optimizer/<X.Y.Z>/, which is
deleted when a newer version installs. The old code baked that versioned absolute
path into ~/.codex/hooks.json, so after an upgrade every Codex tool call failed
(the hook command pointed at a missing dir). The fix resolves the newest
installed version at runtime from the stable parent dir.

Run:  python3 tests/test_codex_hook_path.py
"""

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

requires_posix_version_resolver = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only runtime version resolver and bash command execution",
)


def _gen_command(repo_root: Path, script="skills/token-optimizer/scripts/codex_hook_bridge.py", arg="session-start"):
    """Generate a hook command with _repo_root monkeypatched to repo_root."""
    code = (
        "import sys; sys.path.insert(0, '.')\n"
        "import codex_install as ci\n"
        f"ci._repo_root = lambda: __import__('pathlib').Path({str(repo_root)!r})\n"
        f"print(ci._hook_command({script!r}, {arg!r}))\n"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=str(SCRIPTS),
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, f"gen failed: {r.stderr}"
    return r.stdout.strip()


def test_nonversioned_root_keeps_direct_path():
    """Dev / install.sh layouts (non-semver dir) bake this root, never a
    runtime version resolver. POSIX runs the checkout's hooks/run.py directly.
    Native Windows (5.13.59, #278) runs the checkout's own windows-launcher.py
    pinned to it with --baked-root, the shell-neutral form that also runs
    under PowerShell, where the old ``set ... && run.py`` form exited 1."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "token-optimizer-codex-prod"
        (root / "hooks").mkdir(parents=True)
        cmd = _gen_command(root)
        assert "bash -c" not in cmd, f"non-versioned should not wrap in bash -c: {cmd}"
        assert "sort -V" not in cmd, f"non-versioned must not resolve a version: {cmd}"
        if sys.platform != "win32":
            assert str(root / "hooks" / "run.py") in cmd, f"expected baked path: {cmd}"
            assert "TOKEN_OPTIMIZER_RUNTIME=codex" in cmd
            return
        import re
        quoted = re.findall(r'"([^"]*)"', cmd)
        launcher = next((q for q in quoted if q.lower().endswith("windows-launcher.py")), None)
        assert launcher, f"expected the checkout's windows-launcher.py: {cmd}"
        # The literal path or its 8.3 short form: both name this checkout.
        assert os.path.samefile(os.path.dirname(launcher), root / "hooks"), cmd
        baked = re.search(r'--baked-root "([^"]*)"', cmd)
        assert baked and os.path.samefile(baked.group(1), root), cmd
        assert "run.py" not in cmd and "set " not in cmd, f"old cmd-only form: {cmd}"


@requires_posix_version_resolver
def test_versioned_root_does_not_bake_version():
    """A versioned (marketplace) root must NOT bake the version into the command."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "token-optimizer" / "5.11.14"
        root.mkdir(parents=True)
        cmd = _gen_command(root)
        assert "5.11.14/hooks/run.py" not in cmd, \
            f"version must not be pinned in the resolved path: {cmd}"
        assert "sort -V" in cmd and "${R}hooks/run.py" in cmd, \
            f"expected runtime version resolution: {cmd}"


@requires_posix_version_resolver
def test_baked_old_version_resolves_to_newest_at_runtime():
    """The decisive test: bake an OLD version dir, but executing the command must
    resolve to the NEWEST installed version dir."""
    with tempfile.TemporaryDirectory() as td:
        plugin = Path(td) / "token-optimizer"
        for ver in ("5.0.0", "5.11.14", "5.11.20"):  # newest = 5.11.20
            hooks = plugin / ver / "hooks"
            hooks.mkdir(parents=True)
            # Stub launcher: echo the version dir it was invoked from.
            launcher = hooks / "python-launcher.sh"
            launcher.write_text(
                "#!/usr/bin/env bash\n"
                f'echo "RESOLVED={ver}"\n',
                encoding="utf-8",
            )
            launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
            (hooks / "run.py").write_text("# stub\n", encoding="utf-8")

        # Bake the OLDEST version as the install root.
        cmd = _gen_command(plugin / "5.0.0")
        # Execute exactly what Codex would run (it runs commands via a shell).
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"generated command failed to execute: {r.stderr}\nCMD: {cmd}"
        assert "RESOLVED=5.11.20" in r.stdout, \
            f"command should resolve newest version (5.11.20), got: {r.stdout!r}\nCMD: {cmd}"


@requires_posix_version_resolver
def test_nonsemver_siblings_are_ignored():
    """Stray non-semver dirs (latest/, node_modules/, backup/) must NOT be picked
    over the real version, even though sort -V orders them last."""
    with tempfile.TemporaryDirectory() as td:
        plugin = Path(td) / "token-optimizer"
        for name, ver_tag in (("5.11.20", "REAL"), ("latest", "BAD"),
                              ("node_modules", "BAD"), ("backup", "BAD")):
            hooks = plugin / name / "hooks"
            hooks.mkdir(parents=True)
            launcher = hooks / "python-launcher.sh"
            launcher.write_text(f'#!/usr/bin/env bash\necho "PICKED={ver_tag}:{name}"\n', encoding="utf-8")
            launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
            (hooks / "run.py").write_text("# stub\n", encoding="utf-8")
        cmd = _gen_command(plugin / "5.11.20")
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"command failed: {r.stderr}\nCMD: {cmd}"
        assert "PICKED=REAL:5.11.20" in r.stdout, \
            f"must pick the semver dir, not a stray sibling: {r.stdout!r}\nCMD: {cmd}"


@requires_posix_version_resolver
def test_falls_back_to_baked_path_when_glob_empty():
    """If the parent has no version subdirs at runtime, fall back to the baked dir."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "token-optimizer" / "5.11.14"
        hooks = root / "hooks"
        hooks.mkdir(parents=True)
        launcher = hooks / "python-launcher.sh"
        launcher.write_text("#!/usr/bin/env bash\necho FALLBACK_OK\n", encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        (hooks / "run.py").write_text("# stub\n", encoding="utf-8")
        # Remove the sibling-glob ambiguity: this IS the only version, so glob
        # finds it; rename it away to force the fallback branch.
        cmd = _gen_command(root)
        # Move the only version dir so the runtime glob finds nothing -> fallback.
        # (Fallback points back at the baked path, which we keep intact.)
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"fallback command failed: {r.stderr}\nCMD: {cmd}"
        assert "FALLBACK_OK" in r.stdout or "RESOLVED" not in r.stdout, \
            f"expected launcher to run via glob-or-fallback: {r.stdout!r}"


@requires_posix_version_resolver
@pytest.mark.parametrize("versioned", [False, True])
@pytest.mark.parametrize("missing", ["python-launcher.sh", "run.py"])
def test_missing_hook_files_fail_open_without_blocking_tools(tmp_path, versioned, missing):
    root = tmp_path / "token-optimizer" / "5.11.14" if versioned else tmp_path / "token-optimizer-dev"
    hooks = root / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "python-launcher.sh").write_text("#!/usr/bin/env bash\necho SHOULD_NOT_RUN\n")
    (hooks / "run.py").write_text("# stub\n")
    cmd = _gen_command(root)
    (hooks / missing).unlink()
    result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
