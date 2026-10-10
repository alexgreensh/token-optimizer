"""Cross-platform security and invalidation tests for python-launcher caching."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only bash launcher harness; native Windows has no /bin/bash",
)

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"
MIRROR = REPO / "plugins" / "token-optimizer" / "hooks" / "python-launcher.sh"


def _cache_file_for(tmp_path: Path, path_value: str, platform: str) -> str:
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    uname_bin = tmp_path / "bin"
    uname_bin.mkdir(exist_ok=True)
    uname = uname_bin / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{platform}'\n", encoding="utf-8")
    uname.chmod(0o700)
    script = definitions + "\n_setup_interpreter_cache\nprintf '%s\\n' \"$_PY_CACHE_FILE\"\n"
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env.pop("XDG_CACHE_HOME", None)
    env.pop("TOKEN_OPTIMIZER_PY_CACHE", None)
    env["PATH"] = f"{uname_bin}:{path_value}"
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=str(LAUNCHER.parent),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_python3(dir_path: Path) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    stub = dir_path / "python3"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_candidate_set_changes_cache_key(monkeypatch, tmp_path):
    """The PATH half of the key covers what discovery can return: PATHs whose
    python candidate sets differ must map to different records."""
    va = _make_python3(tmp_path / "venv-a" / "bin")
    vb = _make_python3(tmp_path / "venv-b" / "bin")

    first = _cache_file_for(tmp_path, f"{va.parent}:/usr/bin:/bin", "Linux")
    second = _cache_file_for(tmp_path, f"{vb.parent}:/usr/bin:/bin", "Linux")

    assert first
    assert second
    assert first != second


def test_identical_candidate_set_shares_cache_key(monkeypatch, tmp_path):
    """PATH noise that cannot change discovery's answer (python-free dirs,
    even nonexistent ones) must share ONE record -- the whole-PATH checksum
    minted a new file per PATH value and ballooned to 174 records."""
    vc = _make_python3(tmp_path / "venv" / "bin")

    first = _cache_file_for(tmp_path, f"{tmp_path}/gone-a:{vc.parent}:/usr/bin:/bin", "Linux")
    second = _cache_file_for(tmp_path, f"{tmp_path}/gone-b:{vc.parent}:/usr/bin:/bin", "Linux")

    assert first
    assert second
    assert first == second


def test_msys_cache_engages_inside_per_user_home(tmp_path):
    cache_file = _cache_file_for(tmp_path, "/usr/bin:/bin", "MINGW64_NT-10.0")

    assert "_cache_dir_is_per_user" in LAUNCHER.read_text(encoding="utf-8")
    assert cache_file
    assert Path(cache_file).parent == (
        tmp_path / "home" / ".cache" / "token-optimizer" / "pylauncher"
    )


def test_msys_ownership_fallback_keeps_posix_owner_check_and_confinement():
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "[ -O \"$cache_dir\" ]" in source
    assert "MINGW*|MSYS*|CYGWIN*" in source
    assert "_cache_dir_is_per_user" in source
    assert 'cache_dir="/tmp/' not in source


def test_launcher_mirror_is_byte_identical():
    canonical = LAUNCHER.read_bytes()
    assert b"path_hash" in canonical
    assert canonical == MIRROR.read_bytes()


def test_cache_key_carries_probe_epoch(tmp_path):
    """poisoned-cache heal: the cache filename carries a probe-logic epoch, so
    a change to interpreter-liveness probing renames the key and every stale record
    is ignored once on upgrade. Without it, a user already bitten by the dead stub keeps a
    record naming the dead WindowsApps stub -- and the fixed probe runs only on a
    cache MISS, so on every cache HIT the dead stub is re-exec'd forever."""
    cache_file = _cache_file_for(tmp_path, "/usr/bin:/bin", "Linux")
    assert "/interpreter-e4-" in cache_file, (
        f"cache key must carry the probe-logic epoch (interpreter-e4-...); got {cache_file}"
    )


def test_windows_hot_path_avoids_utility_processes(tmp_path):
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    script = definitions + r'''
OSTYPE=msys
uname() { echo unexpected-uname >&2; return 99; }
cksum() { echo unexpected-cksum >&2; return 99; }
tr() { echo unexpected-tr >&2; return 99; }
_setup_interpreter_cache
[ -n "$_PY_CACHE_FILE" ] || exit 1
_is_msys_platform || exit 2
_path_contains_windowsapps /c/Users/a/WiNdOwSaPpS/python3 || exit 3
'''
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env.pop("XDG_CACHE_HOME", None)
    env.pop("TOKEN_OPTIMIZER_PY_CACHE", None)
    result = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not result.stderr


@pytest.mark.parametrize("ostype", ["msys", "msys2", "cygwin"])
def test_known_windows_ostype_never_runs_uname(tmp_path, ostype):
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    script = definitions + '\nOSTYPE="$1"\nuname() { exit 99; }\n_is_msys_platform\n'
    result = subprocess.run(["/bin/bash", "-c", script, "probe", ostype], capture_output=True)
    assert result.returncode == 0


@pytest.mark.parametrize("value", ["", "a\nb", "a\\b", "x'\"$`!", "שלום/é/路径", "a" * 32000])
def test_builtin_checksum_is_deterministic_and_bounded(value):
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    script = definitions + r'''
_cache_checksum "$1"
first=$_CACHE_CHECKSUM
_cache_checksum "$1"
[ "$first" = "$_CACHE_CHECKSUM" ] || exit 1
printf '%s\n' "$first"
'''
    result = subprocess.run(["/bin/bash", "-c", script, "hash", value], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 0 <= int(result.stdout.strip()) <= 0xFFFFFFFF


def test_checksum_preserves_byte_boundaries_and_locale():
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    script = definitions + r'''
LC_ALL=C
_cache_checksum "$1"; first=$_CACHE_CHECKSUM
LC_ALL=C.UTF-8
_cache_checksum "$1"; [ "$first" = "$_CACHE_CHECKSUM" ] || exit 1
_cache_checksum ab; first=$_CACHE_CHECKSUM
_cache_checksum $'a\nb'; [ "$first" != "$_CACHE_CHECKSUM" ] || exit 2
'''
    result = subprocess.run(["/bin/bash", "-c", script, "hash", "שלום/é/路径"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_posix_checksum_failure_disables_only_cache(tmp_path):
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    script = definitions + r'''
_is_msys_platform() { return 1; }
cksum() { return 99; }
_setup_interpreter_cache
[ -z "$_PY_CACHE_FILE" ]
'''
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_windows_checksum_keys_change_with_candidates_and_plugin_root(tmp_path):
    """The key must change when the candidate set or the plugin root changes.
    (A bare PATH-string shuffle that keeps the same candidates shares a key
    on purpose -- covered by test_identical_candidate_set_shares_cache_key.)"""
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    script = definitions + r'''
OSTYPE=msys
_setup_interpreter_cache
printf '%s\n' "$_PY_CACHE_FILE"
'''
    roots = [tmp_path / "plugin a" / "hooks", tmp_path / "plugin b" / "hooks"]
    for root in roots:
        root.mkdir(parents=True)
    cand_a = _make_python3(tmp_path / "cand a")
    cand_b = _make_python3(tmp_path / "cand b")
    keys = []
    for root, path in [
        (roots[0], str(cand_a.parent)),
        (roots[0], str(cand_b.parent)),
        (roots[1], str(cand_a.parent)),
    ]:
        env = os.environ.copy()
        env.update(HOME=str(tmp_path), PATH=f"{path}:/usr/bin:/bin")
        env.pop("XDG_CACHE_HOME", None)
        env.pop("TOKEN_OPTIMIZER_PY_CACHE", None)
        result = subprocess.run(["/bin/bash", "-c", script, str(root / "python-launcher.sh")], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        keys.append(result.stdout.strip())
    assert all(keys)
    assert len(set(keys)) == 3


@pytest.mark.parametrize("output", ["", "1:2 5", "not-a-checksum", "42x 5"])
def test_posix_malformed_checksum_disables_cache(output):
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    script = definitions + r'''
_is_msys_platform() { return 1; }
# Pass test data through a function variable, never shell interpolation.
CHECKSUM_OUTPUT=$1
cksum() { printf '%s\n' "$CHECKSUM_OUTPUT"; }
_setup_interpreter_cache
[ -z "$_PY_CACHE_FILE" ]
'''
    result = subprocess.run(["/bin/bash", "-c", script, "checksum", output], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
