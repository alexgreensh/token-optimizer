"""Issue #215 follow-ups for ``hooks/python-launcher.sh``:

(A) A cache hit must launch ZERO external utility processes and fork ZERO bash
    subshells. The issue measured ~14 bash subshell forks plus uname/cksum on
    every hook invocation; on MSYS each of those is a full process (~0.2s).
    The only external processes a hit may still run are the interpreter exec
    itself and -- when a GUI twin exists -- the ``timeout`` + ``pythonw``
    liveness probe pair the pythonw swap provably needs (an unprobed exec of a
    dead twin would brick every hook; that probe is a requirement, not waste).

    External launches are counted by a PATH shim dir: the driver runs with
    PATH pointed at a directory of counting stubs, so ANY external utility the
    launcher invokes is intercepted and logged (utilities absent from the shim
    fail to resolve, which would break the test rather than pass silently --
    the pinned names below include every binary the launcher could call).
    Bash subshells are counted with ``set -o functrace`` + a DEBUG trap that
    logs ``$BASH_SUBSHELL`` per simple command: any command run at depth > 0
    proves a ``$(...)`` / ``(...)`` / pipeline fork happened.

(B) The interpreter cache must stay bounded. The pre-fix key was a checksum of
    the WHOLE PATH string, so any PATH churn minted a new record (one machine
    accumulated 174). The key now covers only what can change discovery's
    answer -- the ordered set of PATH entries that actually contain a python3 /
    python / py candidate -- plus a hard cap with oldest-first eviction. These
    tests prove both halves: 500 PATHs differing only in non-candidate dirs
    leave ONE record, and 500 PATHs with distinct candidate sets leave at most
    _PY_CACHE_MAX_FILES records while the newest still resolves.

Run: python3 -m pytest tests/test_python_launcher_cache_hit_processes.py -q
"""

from __future__ import annotations

import os
import re
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

# Every external utility the launcher could conceivably invoke on the
# cache-hit path (or anywhere). Each gets a counting stub in the shim dir.
_STUB_NAMES = (
    "uname cksum tr dirname basename realpath readlink stat mktemp cygpath "
    "cat rm mkdir ls sort uniq head tail sed awk grep find chmod touch mv cp "
    "ln timeout hostname wc cut egrep fgrep date id whoami sh dash env"
).split()


def _defs() -> str:
    """Launcher function definitions (everything before the top-level
    ``_setup_interpreter_cache`` invocation), same extraction the sibling
    launcher tests use."""
    source = LAUNCHER.read_text(encoding="utf-8")
    return source[: source.index("\n_setup_interpreter_cache\n")]


def _max_cache_files() -> int:
    src = LAUNCHER.read_text(encoding="utf-8")
    m = re.search(r"^_PY_CACHE_MAX_FILES=(\d+)$", src, re.MULTILINE)
    assert m, "_PY_CACHE_MAX_FILES constant missing from launcher"
    return int(m.group(1))


def _write_stub(path: Path, name: str, body: str = "") -> Path:
    """A counting stub: logs its own name to $COUNT_LOG, then runs `body`."""
    path.write_text(
        "#!/bin/sh\n"
        f'echo "{name}" >> "$COUNT_LOG"\n'
        + body,
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _make_shim_dir(tmp_path: Path, *, pythonw: bool = False) -> Path:
    """A PATH dir of counting stubs for every launcher-callable utility.

    - most stubs just log and exit 1 (the launcher treats a failed probe as
      "not usable" and never depends on their output on the hit path);
    - ``timeout`` logs, drops ``--kill-after=1s 2s``, and runs the rest so the
      pythonw liveness probe can succeed;
    - the fake interpreters log and exit 0 so the launcher's final ``exec``
      produces a clean exit.
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    for name in _STUB_NAMES:
        if name == "timeout":
            _write_stub(shim / name, name, 'shift 2; "$@"\n')
        else:
            _write_stub(shim / name, name, "exit 1\n")
    _write_stub(shim / "python3", "python3", "exit 0\n")
    if pythonw:
        _write_stub(shim / "pythonw.exe", "pythonw.exe", "exit 0\n")
    return shim


def _hit_driver(tmp_path: Path) -> str:
    """Drive the REAL cache-hit path end to end:
    _setup_interpreter_cache (computes _PY_CACHE_FILE from the real key) ->
    plant a record at that key -> _exec_cached_interpreter (which exec's).
    A DEBUG trap under functrace logs BASH_SUBSHELL per command for the
    subshell count; exec replaces the process so counting stops at exec.
    """
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    return _defs() + r'''
OSTYPE=msys
MSYSTEM=MINGW64
_is_safe_prefix() { return 0; }
set -o functrace
trap 'printf "%s\n" "$BASH_SUBSHELL" >> "$DEPTH_LOG"' DEBUG
_setup_interpreter_cache
[ -n "$_PY_CACHE_FILE" ] || { echo "cache did not engage" >&2; exit 9; }
printf 'INTERP\t%s\n' "$FAKE_INTERP" > "$_PY_CACHE_FILE"
_exec_cached_interpreter "hooks/run.py" "x.py" "--quiet"
echo "exec did not replace the process" >&2
exit 7
'''


def _hit_env(tmp_path: Path, shim: Path, fake_interp: Path, count_log: Path,
             depth_log: Path) -> dict:
    home = tmp_path / "home"
    cache = home / ".cache" / "token-optimizer" / "pylauncher"
    cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        PATH=str(shim),          # every external lookup hits a counting stub
        OSTYPE="msys",
        TOKEN_OPTIMIZER_PY_CACHE=str(cache),
        FAKE_INTERP=str(fake_interp),
        COUNT_LOG=str(count_log),
        DEPTH_LOG=str(depth_log),
    )
    env.pop("XDG_CACHE_HOME", None)
    env.pop("TOKEN_OPTIMIZER_PYTHON", None)
    return env


def _run_hit(tmp_path: Path, *, pythonw: bool):
    shim = _make_shim_dir(tmp_path, pythonw=pythonw)
    count_log = tmp_path / "count.log"
    depth_log = tmp_path / "depth.log"
    fake_interp = shim / "python3"
    script = _hit_driver(tmp_path)
    env = _hit_env(tmp_path, shim, fake_interp, count_log, depth_log)
    result = subprocess.run(
        ["/bin/bash", "-c", script, str(tmp_path / "hooks" / "python-launcher.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    counts = count_log.read_text().split() if count_log.exists() else []
    depths = (
        [int(x) for x in depth_log.read_text().split()]
        if depth_log.exists() else []
    )
    return result, counts, depths


def test_cache_hit_has_zero_subshells_and_only_the_interp_exec(tmp_path):
    """No-twin hit: the ONLY external process launched is the interpreter
    exec itself (the payload -- counted once as 'python3'), and ZERO bash
    subshells fork on the entire setup+hit path."""
    result, counts, depths = _run_hit(tmp_path, pythonw=False)
    assert result.returncode == 0, (
        f"cache-hit driver failed rc={result.returncode} stderr={result.stderr!r}"
    )
    assert depths, "DEBUG trap logged nothing -- functrace counting broken"
    assert max(depths) == 0, (
        "bash subshell(s) forked on the cache-hit path (each is a process on "
        f"MSYS). commands at depth>0: {sum(1 for d in depths if d > 0)}"
    )
    assert counts == ["python3"], (
        f"external launches on a cache hit must be exactly the interpreter "
        f"exec; counted: {counts}"
    )


def test_cache_hit_pythonw_twin_costs_only_the_probe(tmp_path):
    """Twin hit: a pythonw.exe beside python3 runs the mandatory liveness
    probe (timeout + pythonw) before the exec swaps to it -- 3 externals
    total, still zero subshells."""
    result, counts, depths = _run_hit(tmp_path, pythonw=True)
    assert result.returncode == 0, (
        f"cache-hit driver failed rc={result.returncode} stderr={result.stderr!r}"
    )
    assert depths and max(depths) == 0, (
        f"bash subshell(s) forked on the cache-hit path; depths: {set(depths)}"
    )
    assert counts == ["timeout", "pythonw.exe", "pythonw.exe"], (
        "a twin hit may launch only the liveness probe (timeout + pythonw "
        f"probe) and the final pythonw exec; counted: {counts}"
    )


# ---------------------------------------------------------------------------
# Bounded cache
# ---------------------------------------------------------------------------


def _bounded_driver(tmp_path: Path, iterations: int, path_expr: str,
                    fake_interp: str) -> str:
    """Loop the REAL _setup_interpreter_cache + _write_interpreter_cache with
    `path_expr` evaluated per-iteration (may reference $i)."""
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    return _defs() + f'''
OSTYPE=msys
MSYSTEM=MINGW64
_is_safe_prefix() {{ return 0; }}
i=0
while [ "$i" -lt {iterations} ]; do
    PATH="{path_expr}"
    _setup_interpreter_cache
    _write_interpreter_cache "{fake_interp}" ""
    i=$((i + 1))
done
printf 'LAST=%s\\n' "$_PY_CACHE_FILE"
'''


def _bounded_env(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    cache = home / ".cache" / "token-optimizer" / "pylauncher"
    cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        HOME=str(home),
        OSTYPE="msys",
        TOKEN_OPTIMIZER_PY_CACHE=str(cache),
    )
    env.pop("XDG_CACHE_HOME", None)
    env.pop("TOKEN_OPTIMIZER_PYTHON", None)
    return env


def _cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".cache" / "token-optimizer" / "pylauncher"


def _make_candidate_dir(parent: Path, name: str, *files: str) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    for f in files:
        p = d / f
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    return d


def test_500_distinct_candidate_sets_leave_bounded_cache(tmp_path):
    """The 174-file regression: 500 PATH values each with a DIFFERENT python3
    candidate dir must not leave 500 records. The hard cap evicts oldest
    first; the record for the current PATH must still exist afterwards."""
    cands = tmp_path / "cands"
    junk = tmp_path / "junk"
    junk.mkdir()
    n = 500
    for i in range(n):
        _make_candidate_dir(cands, f"cand_{i}", "python3")
    fake = str(tmp_path / "interp" / "python3")
    # /usr/bin:/bin tail: the write path legitimately uses mv/ls/rm utilities;
    # a real PATH always has them. The tail is constant so it cannot mask the
    # per-iteration candidate change.
    script = _bounded_driver(
        tmp_path, n, f"{cands}/cand_$i:{junk}:/usr/bin:/bin", fake
    )
    env = _bounded_env(tmp_path)
    result = subprocess.run(
        ["/bin/bash", "-c", script, str(tmp_path / "hooks" / "python-launcher.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"bounded driver failed rc={result.returncode} stderr={result.stderr!r}"
    )
    records = list(_cache_dir(tmp_path).glob("interpreter-e*-*.cache"))
    cap = _max_cache_files()
    assert len(records) <= cap, (
        f"{len(records)} cache records survived 500 distinct PATH values; "
        f"the cap is {cap}"
    )
    m = re.search(r"LAST=(\S+)", result.stdout)
    assert m, f"driver did not report the last cache file: {result.stdout!r}"
    assert Path(m.group(1)).exists(), (
        "the record for the CURRENT PATH must survive -- evicting the just-"
        "written entry would make every hit a miss"
    )


def test_500_paths_with_same_candidates_leave_one_record(tmp_path):
    """The key must ignore PATH noise that cannot change discovery's answer:
    500 PATHs = one shared candidate dir + a unique (even nonexistent) junk
    dir each, all map to ONE record."""
    cand = _make_candidate_dir(tmp_path, "shared", "python3")
    junk = tmp_path / "junk"
    junk.mkdir()
    n = 500
    fake = str(cand / "python3")
    script = _bounded_driver(
        tmp_path, n, f"{junk}/junk_$i:{cand}:/usr/bin:/bin", fake
    )
    env = _bounded_env(tmp_path)
    result = subprocess.run(
        ["/bin/bash", "-c", script, str(tmp_path / "hooks" / "python-launcher.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"driver failed rc={result.returncode} stderr={result.stderr!r}"
    )
    records = list(_cache_dir(tmp_path).glob("interpreter-e*-*.cache"))
    assert len(records) == 1, (
        f"PATH churn that cannot change discovery must share ONE record; "
        f"found {len(records)}"
    )


def test_candidate_order_changes_the_key(tmp_path):
    """A candidate change that CAN change discovery's answer must change the
    key: PATH dir order decides which python3 wins, so a:b and b:a (both
    holding python3) must map to different records."""
    a = _make_candidate_dir(tmp_path, "a", "python3")
    b = _make_candidate_dir(tmp_path, "b", "python3")

    def _key(path_val: str) -> str:
        script = _defs() + "\nOSTYPE=msys\n_setup_interpreter_cache\n" \
            'printf "%s\\n" "$_PY_CACHE_FILE"\n'
        env = _bounded_env(tmp_path)
        env["PATH"] = f"{path_val}:/usr/bin:/bin"
        r = subprocess.run(
            ["/bin/bash", "-c", script,
             str(tmp_path / "hooks" / "python-launcher.sh")],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    (tmp_path / "hooks").mkdir(exist_ok=True)
    key_ab = _key(f"{a}:{b}")
    key_ba = _key(f"{b}:{a}")
    assert key_ab and key_ba and key_ab != key_ba, (
        "reordering candidate dirs changes which interpreter discovery "
        "returns, so it must change the cache key"
    )
