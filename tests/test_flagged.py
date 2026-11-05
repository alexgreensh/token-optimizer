"""Regression tests for the flagged-item sweep.

Covers:
* The WindowsApps interpreter probe must not spawn the
  console-subsystem python.exe on the healthy path. A live GUI twin
  (pythonw.exe/pyw.exe) proves the Store install real via POSITIVE PROOF OF
  LIFE (it must WRITE a marker file; its bare exit code is never trusted).
  The console --version probe remains the fallback correctness authority.
* T6-M3 carry-forward: dashboard HTML writes in openclaw and opencode are
  atomic (sibling temp file + rename), never truncate-in-place, and the
  openclaw CLI reports a write failure cleanly instead of an uncaught throw.
* commands/health.md and commands/quick.md route the non-Claude-Code
  (Codex/OpenCode/standalone) invocation through python-launcher.sh instead
  of a bare `python3` console spawn.
* keepwarm-experiment's `claude --help` spawn routes a missing
  binary through fail() (clean ABORT) instead of a bare FileNotFoundError.

Each test fails on the pre-fix code (verified by temporary revert).
"""

from __future__ import annotations
from helpers.measure_split import exec_source_module as _split_exec_module

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"

# These tests drive python-launcher.sh (a POSIX shell script) through a real
# `/bin/bash` and exercise chmod'd `#!/bin/sh` fake interpreters -- genuinely
# POSIX-shell machinery the Windows CI runner has no `/bin/bash` for (the log
# shows FileNotFoundError [WinError 2]). Skip when bash is unavailable so they
# still run on any POSIX box; the os.name guard makes the Windows skip certain
# even if a git-bash happens to be on PATH but `/bin/bash` is not.
requires_bash = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="POSIX bash launcher test; no usable /bin/bash on this host",
)


# ---------------------------------------------------------------------------
# WindowsApps probe is flash-free on the healthy path.
# ---------------------------------------------------------------------------


def _launcher_defs() -> str:
    """All launcher function definitions (including the GUI-twin probe and
    find_interpreter), with the top-level cache exec and the discovery chain
    stripped so nothing runs until the driver calls it."""
    src = LAUNCHER.read_text(encoding="utf-8")
    src = src[: src.index('\nif py3=$(find_interpreter "python3")')]
    return src.replace(
        '\n_setup_interpreter_cache\n_exec_cached_interpreter "$@" || :\n', "\n"
    )


def _fake_console_python(store: Path, exit_code: int = 0) -> Path:
    """A stand-in WindowsApps python.exe. Every invocation is a would-be
    console-window flash, so it logs itself to $PROBE_LOG.

    Post-fix the probe keys on the CANDIDATE's own liveness, so this fake models
    a real console python by response, keyed on exit_code:
      * alive (exit_code 0): writes the ``-c`` proof-of-life marker to $3, and
        prints a real ``Python X.Y.Z`` version string on ``--version``.
      * dead (nonzero): the Store redirector -- ignores ``-c`` (no marker) and
        prints a localized "not found / install from Store" message on
        ``--version`` that carries NO version number (exit code is not trusted).
    """
    path = store / "python.exe"
    body = "#!/bin/sh\n" 'echo "console-spawn $*" >> "$PROBE_LOG"\n'
    if exit_code == 0:
        body += 'if [ "$1" = "-c" ]; then printf ok > "$3"; exit 0; fi\n'
        body += 'if [ "$1" = "--version" ]; then echo "Python 3.11.0"; exit 0; fi\n'
    else:
        body += 'if [ "$1" = "--version" ]; then echo "Python was not found; install from the Store"; exit 0; fi\n'
    body += f"exit {exit_code}\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_gui_twin(store: Path, *, alive: bool) -> Path:
    """A stand-in pythonw.exe. Alive: emulates a real GUI interpreter running
    ``-c 'import sys; open(sys.argv[1], "w").write("ok")' <file>`` ($3 is the
    marker path). Dead: exits 0 silently WITHOUT writing -- the AppExecutionAlias
    stub behaviour whose bare exit code must never be trusted."""
    path = store / "pythonw.exe"
    body = "#!/bin/sh\n"
    if alive:
        body += 'printf "ok" > "$3"\n'
    body += "exit 0\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_find_interpreter(store: Path, probe_log: Path):
    """Drive find_interpreter with the fake WindowsApps dir FIRST on PATH.
    The rest of PATH stays intact -- the launcher's probe helpers need
    coreutils (tr/mktemp/cat) -- so assertions compare against the store
    candidate specifically, not against 'nothing found at all'.
    Returns (accepted_path_or_None, probe_log_lines)."""
    driver = _launcher_defs() + (
        "\n_is_safe_prefix() { return 0; }\n"
        # Shim `timeout` (absent on macOS) so the probe's fail-closed no-timeout
        # guard does not short-circuit; strips `--kill-after=1s 2s`, runs unbounded.
        'timeout() { shift 2; "$@"; }\n'
        f'PATH="{store}:$PATH"\n'
        "set +e\n"
        'found=$(find_interpreter "python")\n'
        "rc=$?\n"
        'printf "RC=%s FOUND=%s\\n" "$rc" "$found"\n'
    )
    probe_log.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["/bin/bash", "-c", driver],
        env={**os.environ, "PROBE_LOG": str(probe_log)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    m = re.search(r"RC=(\d+) FOUND=(.*)", result.stdout)
    assert m, f"driver produced no verdict: {result.stdout!r} {result.stderr!r}"
    rc, found = int(m.group(1)), m.group(2).strip()
    log = [l for l in probe_log.read_text(encoding="utf-8").splitlines() if l]
    return (found if rc == 0 else None), log


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    d = tmp_path / "WindowsApps"
    d.mkdir()
    return d


@requires_bash
def test_live_console_candidate_is_accepted(store, tmp_path):
    """A healthy Store install must be accepted. Post-fix the CANDIDATE itself
    supplies proof of life (marker write, else a real --version string), because a
    sibling GUI twin can belong to a different package and must not vouch for it.

    The liveness probe fix deliberately traded the no-flash fix's
    no-flash-on-the-healthy-path property for this correctness: probing the
    console candidate can spawn it once on a cache miss.
    That is why this test no longer asserts an empty PROBE_LOG -- a one-time spawn
    is expected and accepted; a silently-dead cached interpreter is far worse."""
    py = _fake_console_python(store)
    _fake_gui_twin(store, alive=True)
    found, _log = _run_find_interpreter(store, tmp_path / "probe.log")
    assert found == str(py), f"live install must be accepted; log={_log}"


@requires_bash
def test_dead_gui_twin_exit0_is_not_trusted(store, tmp_path):
    """A dead alias can exit 0 silently. Its exit code must NOT accept the
    candidate: with no marker written, the console probe is the authority,
    and here it reports the stub dead (nonzero) -> candidate rejected."""
    py = _fake_console_python(store, exit_code=1)
    _fake_gui_twin(store, alive=False)
    found, log = _run_find_interpreter(store, tmp_path / "probe.log")
    assert found != str(py), (
        "a GUI twin that exits 0 without writing the marker must never "
        "prove the install alive"
    )
    assert log, "the console fallback probe must have run for the dead twin"


@requires_bash
def test_no_gui_twin_console_candidate_accepted(store, tmp_path):
    """No GUI twin present: the candidate's own proof of life still accepts it
    (marker write on -c, or a real --version string). The probe runs the console
    candidate, so PROBE_LOG is non-empty."""
    py = _fake_console_python(store, exit_code=0)
    found, log = _run_find_interpreter(store, tmp_path / "probe.log")
    assert found == str(py)
    assert log, "the candidate probe must run when there is no GUI twin"


@requires_bash
def test_no_gui_twin_console_probe_still_rejects_stub(store, tmp_path):
    """No GUI twin + failing --version: the dead-stub reject semantics of the
    original probe are preserved (a silently-dead interpreter is never
    accepted)."""
    py = _fake_console_python(store, exit_code=1)
    found, _log = _run_find_interpreter(store, tmp_path / "probe.log")
    assert found != str(py)


def test_candidate_probe_keeps_c6_timeout_semantics():
    """Both candidate probe sites (the -c marker write and the --version
    string check) carry the `timeout --kill-after=1s 2s` escalation, alongside the
    pre-existing pythonw-swap liveness probe (3 total in the launcher). Post-fix
    the probes run against `$binpath` (the candidate), never a `$twin`."""
    src = LAUNCHER.read_text(encoding="utf-8")
    assert src.count("--kill-after=1s 2s") == 3
    assert 'timeout --kill-after=1s 2s "$binpath" -c' in src
    # The --version discriminator carries the same bounded semantics.
    assert 'timeout --kill-after=1s 2s "$binpath" --version' in src
    # The twin proof-of-life probe is gone (the launcher probe fix); never reference $twin.
    assert '"$twin" -c' not in src


# ---------------------------------------------------------------------------
# T6-M3: atomic dashboard writes (openclaw + opencode), clean CLI failure.
# ---------------------------------------------------------------------------


def test_openclaw_dashboard_write_is_atomic():
    src = (REPO / "openclaw" / "src" / "dashboard.ts").read_text(encoding="utf-8")
    body = src[src.index("export function writeDashboard") :]
    body = body[: body.index("\n}") + 2]
    assert "renameSync(tmpPath, DASHBOARD_PATH)" in body, (
        "openclaw writeDashboard must rename a sibling temp file onto the "
        "final path (atomic), not truncate-in-place"
    )
    assert "writeFileSync(DASHBOARD_PATH" not in body, (
        "openclaw writeDashboard must never open the destination directly"
    )
    assert ".tmp.${process.pid}" in body, "temp file must be pid-suffixed"


def test_openclaw_dist_carries_atomic_dashboard_write():
    """The shipped dist must match src (rebuilt with the vendored tsc)."""
    dist = (REPO / "openclaw" / "dist" / "dashboard.js").read_text(encoding="utf-8")
    assert "fs.renameSync(tmpPath, DASHBOARD_PATH)" in dist
    assert "fs.writeFileSync(DASHBOARD_PATH" not in dist


def test_openclaw_cli_reports_dashboard_write_failure_cleanly():
    """A Windows rename onto a locked destination must exit with a one-line
    diagnostic, not an uncaught stack trace out of cmdDashboard."""
    for rel in ("src/cli.ts", "dist/cli.js"):
        text = (REPO / "openclaw" / rel).read_text(encoding="utf-8")
        assert "Could not write dashboard" in text, f"missing catch in {rel}"


def test_opencode_dashboard_write_is_atomic():
    src = (REPO / "opencode" / "src" / "dashboard" / "generator.ts").read_text(
        encoding="utf-8"
    )
    body = src[src.index("export function writeDashboard") :]
    assert "renameSync(tmpPath, outputPath)" in body
    assert "writeFileSync(outputPath, html" not in body
    assert ".tmp.${process.pid}" in body


# ---------------------------------------------------------------------------
# Command docs route the non-Claude runtime through the launcher.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", ["health.md", "quick.md"])
def test_command_doc_routes_non_claude_python_through_launcher(doc):
    text = (REPO / "commands" / doc).read_text(encoding="utf-8")
    assert 'TO_LAUNCHER="$_root/hooks/python-launcher.sh"' in text, (
        f"{doc} must resolve the launcher beside the selected measure.py"
    )
    run_lines = [l for l in text.splitlines() if "Codex / OpenCode / standalone" in l]
    assert run_lines, f"{doc} lost its Codex/OpenCode invocation line"
    for line in run_lines:
        assert 'bash "$TO_LAUNCHER"' in line, (
            f"{doc} documents a bare console spawn for non-Claude runtimes "
            f"(the no-flash concern): {line!r}"
        )


@requires_bash
@pytest.mark.parametrize("doc", ["health.md", "quick.md"])
def test_command_doc_resolver_block_is_valid_bash(doc):
    """The fenced resolver block must stay parseable shell."""
    text = (REPO / "commands" / doc).read_text(encoding="utf-8")
    block = text.split("```bash", 1)[1].split("```", 1)[0]
    result = subprocess.run(
        ["/bin/bash", "-n"], input=block, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"{doc} resolver block: {result.stderr}"


# ---------------------------------------------------------------------------
# keepwarm-experiment guards its `claude --help` spawn.
# ---------------------------------------------------------------------------


def _load_keepwarm():
    spec = importlib.util.spec_from_file_location(
        "keepwarm_experiment", REPO / "scripts" / "keepwarm-experiment.py"
    )
    mod = importlib.util.module_from_spec(spec)
    _split_exec_module(mod)
    return mod


def test_keepwarm_missing_claude_aborts_cleanly(monkeypatch, capsys):
    """A missing `claude` binary must route through fail() (ABORT + result
    json + exit 1), not raise FileNotFoundError out of main()."""
    mod = _load_keepwarm()
    monkeypatch.setattr(mod, "CLAUDE_BIN", "/nonexistent/claude-fix3-test")
    monkeypatch.setattr(sys, "argv", ["keepwarm-experiment.py"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ABORT" in err
    assert "cannot run" in err
