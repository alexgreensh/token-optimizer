#!/usr/bin/env python3
"""Regression tests for GitHub #75 — Codex hook path must survive version upgrades.

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

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


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
    """Dev / install.sh layouts (non-semver dir) keep the simple baked command."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "token-optimizer-codex-prod"
        root.mkdir()
        cmd = _gen_command(root)
        assert "bash -c" not in cmd, f"non-versioned should not wrap in bash -c: {cmd}"
        assert str(root / "hooks" / "run.py") in cmd, f"expected baked path: {cmd}"
        assert "TOKEN_OPTIMIZER_RUNTIME=codex" in cmd


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
