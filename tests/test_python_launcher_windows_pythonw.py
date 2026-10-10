"""Acceptance tests for Windows pythonw.exe preference in
``hooks/python-launcher.sh``.

Covers the FABLE red-team amendments:
* swap at BOTH exec sites, at exec time (cache record still names python.exe,
  but exec uses pythonw.exe -- proven by the cached-path round-trip);
* skip WindowsApps pythonw aliases;
* stdout-pipe guard (pythonw GUI-subsystem can null sys.stdout -> dark hook
  protocol): swap only when fd 1 is a pipe, never a tty;
* non-Windows behaviour byte-for-byte unchanged (python.exe used, no swap);
* py.exe (``py -3``) is never swapped -- documented, not fixed;
* mirror stays byte-identical with the canonical launcher.
"""

from __future__ import annotations

import os
import pytest
import subprocess
import sys
import time
from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only bash launcher harness; native Windows has no /bin/bash",
)

try:
    import pty  # POSIX-only; imports termios, absent on Windows.
except ImportError:  # pragma: no cover - exercised only on Windows CI.
    pty = None

# Skip marker for tests that drive a real PTY (fd 1 = tty). pty is unavailable
# on Windows (no termios), so these must skip cleanly there while the rest of
# the module still collects and runs.
requires_pty = pytest.mark.skipif(
    pty is None, reason="pty/termios unavailable on this platform (Windows)"
)

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"
MIRROR = REPO / "plugins" / "token-optimizer" / "hooks" / "python-launcher.sh"


def _defs() -> str:
    """Function definitions from the launcher, up to (not including) the
    top-level ``_setup_interpreter_cache`` invocation. Includes every helper
    the exec sites depend on."""
    source = LAUNCHER.read_text(encoding="utf-8")
    return source[: source.index("\n_setup_interpreter_cache\n")]


def _fake_interp(tmp_path: Path, name: str, marker: str) -> Path:
    """A stand-in interpreter 'binary' (an executable shell script) that
    reads stdin, prints a marker line, and exits with FAKE_PY_EXIT. Lets us
    prove which interpreter the launcher actually exec'd and that the
    stdin->stdout round-trip survives, without needing a real Windows box.

    A liveness probe (`-c ""` with an empty program, as the launcher uses to
    test pythonw.exe) short-circuits to exit 0 so the twin looks healthy and
    the swap proceeds; the real exec then runs the round-trip and uses
    FAKE_PY_EXIT. Without this, a nonzero FAKE_PY_EXIT would make the probe
    reject the twin as broken and no swap would ever happen."""
    path = tmp_path / name
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ] && [ "$2" = "" ]; then exit 0; fi\n'
        "input=$(cat)\n"
        f"printf '{marker} echo=%s\\n' \"$input\"\n"
        'exit "${FAKE_PY_EXIT:-0}"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run(script: str, stdin: str, env: dict, *, use_pty: bool = False):
    """Run a bash driver script. Returns (stdout, returncode, stderr)."""
    full_env = os.environ.copy()
    full_env.update(env)
    if use_pty:
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            ["/bin/bash", "-c", script],
            stdin=subprocess.PIPE,
            stdout=slave,
            stderr=slave,
            env=full_env,
            close_fds=True,
        )
        os.close(slave)
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin.encode("utf-8"))
            proc.stdin.close()
        except BrokenPipeError:
            pass
        chunks: list[bytes] = []
        deadline = time.time() + 60
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            import select

            ready, _, _ = select.select([master], [], [], remaining)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        rc = proc.wait()
        try:
            os.close(master)
        except OSError:
            pass
        out = b"".join(chunks).decode("utf-8", errors="replace")
        # PTYs echo input + CR; strip the echoed stdin line and CRs.
        out = out.replace("\r\n", "\n").replace("\r", "\n")
        return out, rc, ""
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        input=stdin.encode("utf-8"),
        env=full_env,
        capture_output=True,
        timeout=60,
    )
    return (
        result.stdout.decode("utf-8", errors="replace"),
        result.returncode,
        result.stderr.decode("utf-8", errors="replace"),
    )


def _selection_driver(candidate: str, *, msys: bool, safe: bool) -> str:
    """Drive _maybe_swap_to_pythonw in isolation and return _PYW_INTERP."""
    # bash: return 0 == success/true, return 1 == failure/false.
    overrides = (
        f"_is_msys_platform() {{ return {0 if msys else 1}; }}\n"
        f"_is_safe_prefix() {{ return {0 if safe else 1}; }}\n"
        "_setup_interpreter_cache() { :; }\n"
        "_write_interpreter_cache() { :; }\n"
    )
    body = overrides + (
        f'_maybe_swap_to_pythonw "{candidate}"\n'
        'printf "%s\\n" "$_PYW_INTERP"\n'
    )
    return _defs() + "\n" + body


# ---------------------------------------------------------------------------
# Selection logic (no exec): _maybe_swap_to_pythonw picks the right binary.
# ---------------------------------------------------------------------------


def test_selects_pythonw_when_present_off_windowsapps(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    pw = _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    out, rc, _err = _run(_selection_driver(str(py), msys=True, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(pw)


def test_falls_back_to_python_when_no_pythonw(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    out, rc, _err = _run(_selection_driver(str(py), msys=True, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(py)


def test_skips_windowsapps_pythonw_alias(tmp_path):
    store = tmp_path / "WindowsApps"
    store.mkdir()
    py = _fake_interp(store, "python.exe", "PYTHON_SELECTED")
    _fake_interp(store, "pythonw.exe", "PYTHONW_SELECTED")  # present but an alias
    out, rc, _err = _run(_selection_driver(str(py), msys=True, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(py)


def test_skips_pythonw_outside_safe_prefix(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    out, rc, _err = _run(_selection_driver(str(py), msys=True, safe=False), "", {})
    assert rc == 0
    assert out.strip() == str(py)


def test_python3_exe_swaps_to_pythonw(tmp_path):
    py = _fake_interp(tmp_path, "python3.exe", "PYTHON_SELECTED")
    pw = _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    out, rc, _err = _run(_selection_driver(str(py), msys=True, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(pw)


def test_py_launcher_swaps_to_pyw(tmp_path):
    """py.exe swaps to its GUI-subsystem twin pyw.exe (`pyw -3` execs
    pythonw.exe), so py-launcher-only installs no longer flash a console."""
    pyl = _fake_interp(tmp_path, "py.exe", "PY_SELECTED")
    pyw = _fake_interp(tmp_path, "pyw.exe", "PYW_SELECTED")
    out, rc, _err = _run(_selection_driver(str(pyl), msys=True, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(pyw)


def test_py_launcher_ignores_pythonw_twin(tmp_path):
    """py.exe's twin is pyw.exe, not pythonw.exe: a pythonw.exe in the same
    directory (a different install's twin) must never be selected for py.exe."""
    pyl = _fake_interp(tmp_path, "py.exe", "PY_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    out, rc, _err = _run(_selection_driver(str(pyl), msys=True, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(pyl)


def test_non_windows_is_noop_even_with_pythonw_present(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    out, rc, _err = _run(_selection_driver(str(py), msys=False, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(py)


# ---------------------------------------------------------------------------
# stdout guard: a tty on fd 1 keeps python.exe (pythonw could null a tty and
# a console is already attached, so there is no flash to avoid).
# ---------------------------------------------------------------------------


@requires_pty
def test_tty_stdout_keeps_python_exe(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    out, rc, _err = _run(
        _selection_driver(str(py), msys=True, safe=True), "", {}, use_pty=True
    )
    assert rc == 0
    # PTY echoes the candidate path; assert python.exe selected, pythonw not.
    assert str(py) in out
    assert "pythonw.exe" not in out


# ---------------------------------------------------------------------------
# Integration: the launcher actually exec's pythonw.exe and the hook protocol
# stdin -> stdout round-trip survives, exit code propagates, and the process
# is waited on (no early EOF).
# ---------------------------------------------------------------------------


def _discovered_driver(fake_py: Path) -> str:
    overrides = (
        "_is_msys_platform() { return 0; }\n"
        "_is_safe_prefix() { return 0; }\n"
        "_setup_interpreter_cache() { :; }\n"
        "_write_interpreter_cache() { :; }\n"
    )
    body = overrides + (
        'set +e\n'
        f'_exec_discovered_interpreter "{fake_py}" "" "$@"\n'
        "rc=$?\n"
        "set -e\n"
        'if [ "$rc" -ne 0 ]; then printf "EXEC_FAILED rc=%s\\n" "$rc" >&2; exit "$rc"; fi\n'
        'printf "EXEC_DID_NOT_REPLACE\\n" >&2\n'
        "exit 1\n"
    )
    return _defs() + "\n" + body


def _cached_driver(fake_py: Path, cache_file: Path) -> str:
    record = f"INTERP\t{fake_py}\n"
    cache_file.write_text(record, encoding="utf-8")
    overrides = (
        "_is_msys_platform() { return 0; }\n"
        "_is_safe_prefix() { return 0; }\n"
        "_setup_interpreter_cache() { :; }\n"
        "_write_interpreter_cache() { :; }\n"
    )
    body = overrides + (
        f'_PY_CACHE_FILE="{cache_file}"\n'
        'set +e\n'
        '_exec_cached_interpreter "$@"\n'
        "rc=$?\n"
        "set -e\n"
        'if [ "$rc" -ne 0 ]; then printf "EXEC_FAILED rc=%s\\n" "$rc" >&2; exit "$rc"; fi\n'
        'printf "EXEC_DID_NOT_REPLACE\\n" >&2\n'
        "exit 1\n"
    )
    return _defs() + "\n" + body


def test_discovered_path_execs_pythonw_and_round_trips_stdout(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    out, rc, err = _run(
        _discovered_driver(py),
        '{"session_id":"x"}',
        {"FAKE_PY_EXIT": "0"},
    )
    assert "EXEC_DID_NOT_REPLACE" not in err
    assert "EXEC_FAILED" not in err
    assert "PYTHONW_SELECTED" in out
    assert "PYTHON_SELECTED" not in out
    assert '{"session_id":"x"}' in out  # stdin survived the round-trip
    assert rc == 0


def test_discovered_path_propagates_nonzero_exit(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    out, rc, err = _run(
        _discovered_driver(py),
        '{"session_id":"x"}',
        {"FAKE_PY_EXIT": "7"},
    )
    assert "PYTHONW_SELECTED" in out
    assert rc == 7


def test_cached_path_swaps_to_pythonw_at_exec_time(tmp_path):
    """The regression: a cache record holding python.exe must NOT keep
    flashing forever. The swap happens at exec time on cache hits too."""
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    cache = tmp_path / "interpreter.cache"
    out, rc, err = _run(
        _cached_driver(py, cache),
        '{"session_id":"x"}',
        {"FAKE_PY_EXIT": "0"},
    )
    assert "EXEC_DID_NOT_REPLACE" not in err
    assert "EXEC_FAILED" not in err
    assert "PYTHONW_SELECTED" in out
    assert "PYTHON_SELECTED" not in out
    assert '{"session_id":"x"}' in out
    assert rc == 0


def test_cache_record_still_names_python_exe_after_swap(tmp_path):
    """The cache key/record is unchanged by the swap: discovery writes
    python.exe, exec uses pythonw.exe. Proves the swap is exec-only."""
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    cache = tmp_path / "interpreter.cache"
    # Use the REAL _write_interpreter_cache so we observe what gets stored.
    driver = (
        "_is_msys_platform() { return 0; }\n"
        "_is_safe_prefix() { return 0; }\n"
        "_setup_interpreter_cache() { :; }\n"
        f'_PY_CACHE_FILE="{cache}"\n'
        'set +e\n'
        f'_exec_discovered_interpreter "{py}" "" "$@"\n'
        "rc=$?\n"
        "set -e\n"
        'if [ "$rc" -ne 0 ]; then printf "EXEC_FAILED rc=%s\\n" "$rc" >&2; exit "$rc"; fi\n'
        'printf "EXEC_DID_NOT_REPLACE\\n" >&2\n'
        "exit 1\n"
    )
    out, rc, err = _run(_defs() + "\n" + driver, '{"session_id":"x"}', {"FAKE_PY_EXIT": "0"})
    assert "PYTHONW_SELECTED" in out
    assert rc == 0
    record = cache.read_text(encoding="utf-8")
    assert "python.exe" in record
    assert "pythonw.exe" not in record


def test_non_windows_execs_python_exe_unchanged(tmp_path):
    """Non-Windows behaviour is byte-for-byte unchanged: python.exe is used,
    pythonw.exe is never selected."""
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _fake_interp(tmp_path, "pythonw.exe", "PYTHONW_SELECTED")
    driver = (
        "_is_msys_platform() { return 1; }\n"
        "_is_safe_prefix() { return 0; }\n"
        "_setup_interpreter_cache() { :; }\n"
        "_write_interpreter_cache() { :; }\n"
        'set +e\n'
        f'_exec_discovered_interpreter "{py}" "" "$@"\n'
        "rc=$?\n"
        "set -e\n"
        'if [ "$rc" -ne 0 ]; then printf "EXEC_FAILED rc=%s\\n" "$rc" >&2; exit "$rc"; fi\n'
        'printf "EXEC_DID_NOT_REPLACE\\n" >&2\n'
        "exit 1\n"
    )
    out, rc, err = _run(_defs() + "\n" + driver, '{"session_id":"x"}', {"FAKE_PY_EXIT": "0"})
    assert "PYTHON_SELECTED" in out
    assert "PYTHONW_SELECTED" not in out
    assert rc == 0


# ---------------------------------------------------------------------------
# Broken-twin guard: a corrupt pythonw.exe that passes -f/-x/-s but exits
# nonzero on a liveness probe must NOT be swapped to. The launcher keeps
# python.exe so the hook still runs, instead of exec'ing a dead twin that
# bricks every hook (exit 127) and makes the fallback ladder unreachable.
# ---------------------------------------------------------------------------


def _broken_pythonw(tmp_path: Path) -> Path:
    """A pythonw.exe twin that is executable and non-empty (passes -f/-x/-s)
    but exits nonzero when actually run -- a stand-in for a corrupt/garbage
    install twin or a 0xC000-style Windows stub."""
    path = tmp_path / "pythonw.exe"
    path.write_text("#!/bin/sh\nexit 13\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_broken_pythonw_twin_not_selected(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _broken_pythonw(tmp_path)
    out, rc, _err = _run(_selection_driver(str(py), msys=True, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(py)


def test_broken_pythonw_twin_keeps_python_exe_and_hook_runs(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _broken_pythonw(tmp_path)
    out, rc, err = _run(
        _discovered_driver(py),
        '{"session_id":"x"}',
        {"FAKE_PY_EXIT": "0"},
    )
    assert "EXEC_DID_NOT_REPLACE" not in err
    assert "EXEC_FAILED" not in err
    assert "PYTHON_SELECTED" in out
    assert "PYTHONW" not in out
    assert '{"session_id":"x"}' in out  # the hook still ran, stdin survived
    assert rc == 0


def test_broken_pythonw_twin_keeps_python_exe_on_cache_hit(tmp_path):
    py = _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    _broken_pythonw(tmp_path)
    cache = tmp_path / "interpreter.cache"
    out, rc, err = _run(
        _cached_driver(py, cache),
        '{"session_id":"x"}',
        {"FAKE_PY_EXIT": "0"},
    )
    assert "EXEC_DID_NOT_REPLACE" not in err
    assert "EXEC_FAILED" not in err
    assert "PYTHON_SELECTED" in out
    assert "PYTHONW" not in out
    assert '{"session_id":"x"}' in out
    assert rc == 0


def test_cache_record_naming_pythonw_exe_is_rejected(tmp_path):
    """A cache record that names /pythonw.exe is rejected outright: the
    swap is exec-only, so such a record is poisoned or a stale broken-twin
    artefact. Discovery re-runs instead of exec'ing a possibly-dead twin."""
    _fake_interp(tmp_path, "python.exe", "PYTHON_SELECTED")
    bad_pythonw = _broken_pythonw(tmp_path)
    cache = tmp_path / "interpreter.cache"
    # Poison the cache with a pythonw.exe path that passes -x/-s and is in a
    # safe prefix (overrides make _is_safe_prefix always pass), so the ONLY
    # thing rejecting it is the /pythonw.exe guard.
    cache.write_text(f"INTERP\t{bad_pythonw}\n", encoding="utf-8")
    out, rc, err = _run(
        _cached_driver(bad_pythonw, cache),
        '{"session_id":"x"}',
        {"FAKE_PY_EXIT": "0"},
    )
    # _exec_cached_interpreter returns 1 -> driver prints EXEC_FAILED and
    # exits 1. The dead twin is NOT exec'd (no PYTHONW output).
    assert "EXEC_FAILED" in err
    assert "PYTHONW" not in out
    assert "PYTHON_SELECTED" not in out
    assert rc != 0


# ---------------------------------------------------------------------------
# Documentation + mirror invariants.
# ---------------------------------------------------------------------------



def test_documents_py_launcher_pyw_swap():
    """The old "py-launcher-only installs still flash" carve-out was flipped:
    py.exe now swaps to pyw.exe. The source must document the swap and must
    no longer claim the flash is unfixed."""
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "pyw.exe" in source
    assert "still flash" not in source


def test_mirror_remains_byte_identical():
    assert LAUNCHER.read_bytes() == MIRROR.read_bytes()
    assert b"_maybe_swap_to_pythonw" in LAUNCHER.read_bytes()


# ---------------------------------------------------------------------------
# Both pythonw probe sites must use `timeout --kill-after=1s 2s`.
# A bare `timeout 2s` sends SIGTERM at 2s but a GUI-subsystem pythonw (or a
# Store AppExecutionAlias stub) can ignore SIGTERM and hold the budget past
# expiry, stalling the hook. --kill-after=1s escalates to SIGKILL 1s after
# SIGTERM so the probe always terminates within ~3s.
# ---------------------------------------------------------------------------

def test_pythonw_liveness_probe_uses_kill_after():
    """The _maybe_swap_to_pythonw liveness probe must use
    --kill-after=1s so a SIGTERM-ignoring pythonw twin is SIGKILLed."""
    source = LAUNCHER.read_text(encoding="utf-8")
    # The probe is the line with `timeout ... "$pythonw" -c ""`.
    assert 'timeout --kill-after=1s 2s "$pythonw" -c ""' in source, (
        "the pythonw liveness probe must use `timeout --kill-after=1s 2s` "
        "so a SIGTERM-ignoring twin is SIGKILLed within ~3s"
    )


def test_windowsapps_probe_uses_kill_after():
    """The find_interpreter WindowsApps --version probe must use
    --kill-after=1s so a SIGTERM-ignoring Store stub is SIGKILLed."""
    source = LAUNCHER.read_text(encoding="utf-8")
    assert 'timeout --kill-after=1s 2s "$binpath" --version' in source, (
        "the WindowsApps --version probe must use `timeout --kill-after=1s 2s` "
        "so a SIGTERM-ignoring stub is SIGKILLed within ~3s"
    )


def test_kill_after_present_in_mirror():
    """The mirror must carry the same --kill-after=1s probes."""
    source = MIRROR.read_bytes()
    assert b"--kill-after=1s 2s" in source, (
        "the mirror launcher must carry --kill-after=1s on both probe sites"
    )
    # Exactly three occurrences (one per probe site): the _maybe_swap_to_pythonw
    # liveness probe, the GUI-twin proof-of-life probe, and the console
    # --version fallback probe (both now in _probe_windowsapps_candidate).
    # Updated from 2 when the flash-free twin probe was added.
    assert source.count(b"--kill-after=1s 2s") == 3, (
        "expected exactly three --kill-after=1s probe sites in the mirror; "
        f"got {source.count(b'--kill-after=1s 2s')}"
    )


# ---------------------------------------------------------------------------
# _is_safe_prefix version-number globs must be anchored to the
# path-component boundary. In bash case patterns, * crosses /, so the old
# Python[23]* matched Python3-evil/python.exe (spoofed dir name). The fix
# uses extglob +([0-9])/* to require digits-only until the path separator.
# ---------------------------------------------------------------------------

def _safe_prefix_driver(binpath: str) -> str:
    """Drive _is_safe_prefix in isolation and print the result.
    Disables set -e inside the subshell so the expected return 1 is captured."""
    return _defs() + (
        f'(set +e; _is_safe_prefix "{binpath}"; echo "safe=$?")\n'
    )


def test_safe_prefix_blocks_program_files_dir_spoof():
    """Python3-evil under Program Files must NOT pass _is_safe_prefix."""
    out, rc, _err = _run(
        _safe_prefix_driver("/c/Program Files/Python3-evil/python.exe"),
        "",
        {},
    )
    assert "safe=1" in out, (
        f"Python3-evil spoof must be rejected; got {out!r}"
    )


def test_safe_prefix_blocks_program_files_x86_dir_spoof():
    """Python3-evil under Program Files (x86) must NOT pass."""
    out, rc, _err = _run(
        _safe_prefix_driver("/c/Program Files (x86)/Python3-evil/python.exe"),
        "",
        {},
    )
    assert "safe=1" in out, (
        f"Python3-evil spoof under x86 must be rejected; got {out!r}"
    )


def test_safe_prefix_blocks_root_python_dir_spoof():
    """Python31-evil at drive root must NOT pass _is_safe_prefix."""
    out, rc, _err = _run(
        _safe_prefix_driver("/c/Python31-evil/python.exe"),
        "",
        {},
    )
    assert "safe=1" in out, (
        f"Python31-evil spoof must be rejected; got {out!r}"
    )


def test_safe_prefix_allows_legit_program_files_python():
    """Python313 under Program Files still passes (no false reject)."""
    out, rc, _err = _run(
        _safe_prefix_driver("/c/Program Files/Python313/python.exe"),
        "",
        {},
    )
    assert "safe=0" in out, (
        f"legit Python313 install must pass; got {out!r}"
    )


def test_safe_prefix_allows_legit_root_python():
    """Python313 at drive root still passes (no false reject)."""
    out, rc, _err = _run(
        _safe_prefix_driver("/c/Python313/python.exe"),
        "",
        {},
    )
    assert "safe=0" in out, (
        f"legit root Python313 install must pass; got {out!r}"
    )


def test_safe_prefix_extglob_enabled_in_launcher():
    """The launcher must enable extglob for +([0-9]) to work."""
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "shopt -s extglob" in source, (
        "launcher must enable extglob so +([0-9]) patterns work in case"
    )


def test_safe_prefix_extglob_in_mirror():
    """The mirror must also enable extglob."""
    source = MIRROR.read_text(encoding="utf-8")
    assert "shopt -s extglob" in source, (
        "mirror launcher must enable extglob so +([0-9]) patterns work"
    )


# ---------------------------------------------------------------------------
# WindowsApps skip must be case-insensitive. Windows is a
# case-insensitive FS, so the directory can appear as WindowsApps,
# windowsapps, WINDOWSAPPS, Windowsapps, etc. The old explicit-variant
# patterns (*/WindowsApps/*|*/windowsapps/*) only covered two casings.
# ---------------------------------------------------------------------------

def _windowsapps_driver(binpath: str) -> str:
    """Drive _path_contains_windowsapps in isolation."""
    return _defs() + (
        f'(set +e; _path_contains_windowsapps "{binpath}"; echo "wa=$?")\n'
    )


@pytest.mark.parametrize("casing", [
    "WindowsApps",
    "windowsapps",
    "WINDOWSAPPS",
    "Windowsapps",
    "WINDOWsapPS",
])
def test_windowsapps_detected_case_insensitive(casing):
    """Any casing of the WindowsApps directory must be detected."""
    path = f"/c/Users/x/AppData/Local/Microsoft/{casing}/python.exe"
    out, rc, _err = _run(_windowsapps_driver(path), "", {})
    assert "wa=0" in out, (
        f"casing {casing!r} must be detected as WindowsApps; got {out!r}"
    )


def test_windowsapps_not_detected_for_unrelated_path():
    """A path that does not contain windowsapps (any casing) must not
    be detected."""
    out, rc, _err = _run(
        _windowsapps_driver("/c/Users/x/AppData/Local/Microsoft/NotApps/python.exe"),
        "",
        {},
    )
    assert "wa=1" in out, (
        f"unrelated path must not be detected; got {out!r}"
    )


def test_windowsapps_helper_in_both_launchers():
    """Both launchers must define _path_contains_windowsapps."""
    for launcher, label in [(LAUNCHER, "canonical"), (MIRROR, "mirror")]:
        source = launcher.read_text(encoding="utf-8")
        assert "_path_contains_windowsapps" in source, (
            f"{label} launcher must define _path_contains_windowsapps"
        )
        assert "[wW][iI][nN][dD][oO][wW][sS][aA][pP][pP][sS]" in source, (
            f"{label} launcher must match casing without spawning tr"
        )

@pytest.mark.parametrize("name,twin", [("python", "pythonw.exe"), ("python3", "pythonw.exe"), ("py", "pyw.exe")])
def test_suffixless_windows_interpreter_swaps(tmp_path, name, twin):
    py = _fake_interp(tmp_path, name, "CONSOLE")
    pw = _fake_interp(tmp_path, twin, "GUI")
    out, rc, _err = _run(_selection_driver(str(py), msys=True, safe=True), "", {})
    assert rc == 0
    assert out.strip() == str(pw)


@pytest.mark.parametrize("cached", [False, True])
def test_suffixless_exec_preserves_stdin_stdout_and_exit(tmp_path, cached):
    py = _fake_interp(tmp_path, "python3", "CONSOLE")
    _fake_interp(tmp_path, "pythonw.exe", "GUI")
    driver = _cached_driver(py, tmp_path / "interp.cache") if cached else _discovered_driver(py)
    out, rc, err = _run(driver, 'hello\n{"data":"שלום"}\n', {"FAKE_PY_EXIT": "7"})
    assert rc == 7
    assert 'GUI echo=hello\n{"data":"שלום"}' in out
    assert not err
