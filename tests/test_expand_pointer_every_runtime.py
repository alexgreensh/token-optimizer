"""Expand pointers must retrieve from every runtime, not only Claude Code.

The run that archives an output knows which runtime's archive it wrote to; the
model's later shell may resolve a different runtime. On Copilot, Cursor,
OpenCode, Antigravity, Grok and Devin the pointer comes from bash_compress.py
(the command rewrite) and the archive lands under that runtime's home. A bare
`measure.py expand <id>` only finds it if the next shell re-detects the same
runtime, which hangs on env vars the user may not export and on a time-boxed
process-tree scan that fails open to Claude Code. Every non-Claude pointer now
pins `--runtime NAME`, as Codex and Hermes already do.

The JS plugins (OpenCode's and OpenClaw's own) and the Devin filter never print
a measure.py expand pointer at all; the static tests below pin that.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

RUNTIMES = ["copilot", "cursor", "opencode", "antigravity", "grok", "devin"]


def _env(home: Path, **extra) -> dict:
    keep = ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    # No process-tree scan: the bare shell's runtime must come from env alone,
    # which is what a scan that timed out or found nothing leaves behind.
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONIOENCODING="utf-8",
               TOKEN_OPTIMIZER_NO_PROC_SCAN="1", **extra)
    return env


def _archive_via_bash_compress(home: Path, runtime: str | None) -> str:
    listing = home / "d"
    listing.mkdir(parents=True, exist_ok=True)
    for i in range(300):
        (listing / f"file_{i}.txt").touch()
    extra = {"TOKEN_OPTIMIZER_RUNTIME": runtime} if runtime else {}
    out = subprocess.run([sys.executable, str(SCRIPTS / "bash_compress.py"), "ls", "-la", "d"],
                         cwd=str(home), env=_env(home, **extra), capture_output=True,
                         text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    return next(line.strip() for line in out.stdout.splitlines()
                if re.search(r"measure\.py .*expand [0-9a-f]{16}$", line.strip()))


def _run_pointer(home: Path, pointer: str) -> subprocess.CompletedProcess:
    argv = pointer.split()
    assert Path(argv[1]).name == "measure.py"
    return subprocess.run([sys.executable, *argv[1:]], env=_env(home),
                          capture_output=True, text=True, timeout=180)


@pytest.mark.skipif(os.name != "posix", reason="bash_compress wraps POSIX commands")
@pytest.mark.parametrize("runtime", RUNTIMES)
def test_pointer_pins_its_runtime_and_retrieves_from_a_bare_shell(tmp_path, runtime):
    home = tmp_path / "home"
    pointer = _archive_via_bash_compress(home, runtime)
    assert f" --runtime {runtime} expand " in pointer, pointer
    # The archive really is outside the Claude Code archive.
    assert not list((home / ".claude").rglob("tool-archive/*/*.json"))
    out = _run_pointer(home, pointer)
    assert out.returncode == 0, out.stderr
    assert "file_150.txt" in out.stdout


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_pinned_pointer_is_a_recovery_command(runtime):
    sys.path.insert(0, str(SCRIPTS))
    import recovery_output
    from refetch_fingerprint import measure_py_path, shell_path

    pointer = f"python3 {shell_path(measure_py_path())} --runtime {runtime} expand 0123456789abcdef"
    assert recovery_output.is_expand_command(pointer)
    assert recovery_output.is_recovery_output("Bash", {"command": pointer})


@pytest.mark.skipif(os.name != "posix", reason="bash_compress wraps POSIX commands")
def test_claude_code_pointer_stays_unpinned_and_works(tmp_path):
    home = tmp_path / "home"
    pointer = _archive_via_bash_compress(home, None)
    assert "--runtime" not in pointer
    out = _run_pointer(home, pointer)
    assert out.returncode == 0, out.stderr
    assert "file_150.txt" in out.stdout


def test_devin_install_never_prints_a_measure_expand_pointer():
    """Devin ships a closed payload without measure.py or archive_result.py,
    and its filter swaps the archive note for its own `recover` command (an
    absolute path to the installed filter), falling back to raw output if an
    expand mention survives. No runtime pin can matter there."""
    sys.path.insert(0, str(SCRIPTS))
    import devin_source

    assert "measure.py" not in devin_source.PAYLOAD
    assert "archive_result.py" not in devin_source.PAYLOAD
    src = (SCRIPTS / "devin_compress.py").read_text(encoding="utf-8")
    assert "shortened.replace(_ARCHIVE_NOTE, _CAPTURE_NOTE)" in src
    assert "if 'expand key' in shortened:\n            shortened = text" in src
    assert "'recover', '--capture-id'" in src


@pytest.mark.parametrize("plugin", ["opencode", "openclaw"])
def test_js_plugins_never_print_a_measure_expand_pointer(plugin):
    """OpenCode's and OpenClaw's own plugins are TypeScript; neither archives
    tool output to the Python tool-archive or tells the model to run
    measure.py expand. (A Python run detected as OpenCode is covered by the
    parametrized pin test above.)"""
    offenders = []
    for path in (REPO / plugin / "src").rglob("*.ts"):
        if path.name.endswith(".test.ts"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "tool-archive" in text or re.search(r"measure\.py[^\n]*\bexpand\b", text):
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, offenders


# ------------------------------------------- unpinned expand: cross-runtime fallback

def _measure(home: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "measure.py"), *args], env=_env(home),
                          capture_output=True, text=True, timeout=180)


def _key(pointer: str) -> str:
    return pointer.rsplit(" ", 1)[1]


@pytest.mark.skipif(os.name != "posix", reason="bash_compress wraps POSIX commands")
@pytest.mark.parametrize("runtime", ["copilot", "codex"])
def test_bare_expand_falls_back_to_another_runtimes_archive(tmp_path, runtime):
    """The unpinned-Claude gap: an archive written under another runtime, then
    a bare `measure.py expand <id>` whose shell resolves Claude Code."""
    home = tmp_path / "home"
    key = _key(_archive_via_bash_compress(home, runtime))
    out = _measure(home, "expand", key)
    assert out.returncode == 0, out.stderr
    assert "file_150.txt" in out.stdout
    # With --session too (the same id lookup, scoped).
    out = _measure(home, "expand", key, "--session", "unknown")
    assert out.returncode != 0  # "unknown" never names a session dir, as today


@pytest.mark.skipif(os.name != "posix", reason="bash_compress wraps POSIX commands")
def test_bare_expand_of_a_missing_id_still_reports_not_found(tmp_path):
    home = tmp_path / "home"
    _archive_via_bash_compress(home, "copilot")
    out = _measure(home, "expand", "0123456789abcdef")
    assert out.returncode == 1
    assert "Tool result not found: 0123456789abcdef" in out.stderr
    # Ids are validated before any lookup: traversal never reaches the fallback.
    out = _measure(home, "expand", "../../../etc/passwd")
    assert out.returncode == 1 and "Invalid tool_use_id" in out.stderr


@pytest.mark.skipif(os.name != "posix", reason="bash_compress wraps POSIX commands")
def test_explicit_runtime_expand_stays_strict(tmp_path):
    home = tmp_path / "home"
    key = _key(_archive_via_bash_compress(home, "copilot"))
    out = _measure(home, "--runtime", "claude", "expand", key)
    assert out.returncode == 1
    assert "file_150.txt" not in out.stdout
    out = _measure(home, "--runtime", "grok", "expand", key)
    assert out.returncode == 1 and "file_150.txt" not in out.stdout


@pytest.mark.skipif(os.name != "posix", reason="bash_compress wraps POSIX commands")
def test_fallback_never_follows_a_symlinked_entry(tmp_path):
    home = tmp_path / "home"
    key = _key(_archive_via_bash_compress(home, "copilot"))
    entry = next((home / ".copilot").rglob(f"{key}.json"))
    secret = tmp_path / "outside.json"
    secret.write_text('{"response": "outside-the-archive"}', encoding="utf-8")
    entry.unlink()
    entry.symlink_to(secret)
    out = _measure(home, "expand", key)
    assert "outside-the-archive" not in out.stdout
    assert out.returncode == 1


def test_bare_expand_is_still_a_recovery_command():
    sys.path.insert(0, str(SCRIPTS))
    import recovery_output
    from refetch_fingerprint import measure_py_path, shell_path

    bare = f"python3 {shell_path(measure_py_path())} expand 0123456789abcdef"
    assert recovery_output.is_expand_command(bare)
    assert recovery_output.is_recovery_output("Bash", {"command": bare})
