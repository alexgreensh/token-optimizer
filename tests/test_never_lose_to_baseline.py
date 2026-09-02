"""Token Optimizer must never lose to baseline on huge outputs.

Measured problem: Claude Code 2.1.247 already truncates any tool result
larger than ~30KB down to a ~2.2KB "<persisted-output>" stub on its own.
Token Optimizer's Bash compression on a 41.1KB `ls -la /usr/bin` produced a
10,131-char compressed listing -- BIGGER than the 2,206-char stub baseline,
so on very large outputs TO LOST to baseline.

Invariant under test: for any output, the chars the model sees under TO
must be <= the chars it would see under baseline (raw output when at or
under the ~30KB threshold, the ~2.2KB stub above it). The raw output must
still be archived and retrievable via the archive pointer even when the
visible summary is tiny. The guard must fail open.
"""

from __future__ import annotations

import importlib
import json
import re
import shlex
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

SESSION_ID = "unit-c-never-lose-to-baseline"


def _load(name: str):
    importlib.invalidate_caches()
    return importlib.import_module(name)


def _ls_fixture(n_entries: int) -> str:
    """Synthetic `ls -la`-style output, one line per entry (~90 chars each)."""
    lines = []
    for i in range(n_entries):
        lines.append(
            f"-rwxr-xr-x  1 root  root  123456 Aug 27 2026  "
            f"usr-bin-tool-name-{i:04d}-with-some-padding"
        )
    return "\n".join(lines) + "\n"


def _generic_fixture(n_lines: int) -> str:
    """Synthetic unmatched-command output: unique filler lines, no error keywords."""
    return "\n".join(f"filler line {i:04d} xxxxxxxxxxxxxx" for i in range(n_lines)) + "\n"


def _run_main(monkeypatch, tmp_path, capsys, command: str, raw_stdout: str) -> str:
    """Run the REAL bash_compress.main() with a stubbed subprocess.run.

    Archiving is real (tmp snapshot dir), so the archive+pointer path and
    the baseline-size guard both execute exactly as in production. Returns the
    exact text main() wrote to stdout.
    """
    monkeypatch.syspath_prepend(str(SCRIPTS))
    bc = _load("bash_compress")
    ar = _load("archive_result")

    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir(parents=True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snapshot_dir))
    monkeypatch.setattr(ar, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(ar, "TRENDS_DB", snapshot_dir / "trends.db")
    monkeypatch.setenv("CLAUDE_SESSION_ID", SESSION_ID)

    class _FakeResult:
        stdout = raw_stdout
        stderr = ""
        returncode = 0

    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: _FakeResult())
    # Simulate a real shell invocation: each word is a separate argv element,
    # exactly like `bash_compress.py ls -la /usr/bin` from a launcher. The
    # R13a self-check re-joins argv, so a single fused string would be seen
    # as one (non-whitelisted) command and refused.
    monkeypatch.setattr(bc.sys, "argv", ["bash_compress.py", *shlex.split(command)])

    with pytest.raises(SystemExit) as exc_info:
        bc.main()
    assert exc_info.value.code == 0
    return capsys.readouterr().out


def _baseline(raw: str) -> int:
    """Chars Claude Code 2.1.247 would show for `raw` without Token Optimizer."""
    return _load("bash_compress")._baseline_visible_chars(len(raw))


def _archive_key_from_output(out: str) -> str | None:
    m = re.search(r"expand\s+([a-f0-9]{16})", out)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Invariant tests through the REAL main() pipeline
# ---------------------------------------------------------------------------

def test_small_output_to_wins_big(monkeypatch, tmp_path, capsys):
    """~5KB output: baseline shows it in full; TO must compress well below it."""
    raw = _generic_fixture(200)  # ~5KB of unmatched (generic-path) output
    assert 4000 <= len(raw) <= 7000, f"fixture size wrong: {len(raw)}"

    # A whitelisted command whose output has no dedicated pattern handler routes
    # through the generic compressor, exercising the baseline guard on the same
    # "unmatched output" path without tripping the R13a self-check.
    out = _run_main(monkeypatch, tmp_path, capsys, "git status --porcelain", raw)

    assert len(out) <= _baseline(raw), (
        f"baseline-size invariant violated on ~5KB output: TO={len(out)} "
        f"baseline={_baseline(raw)}"
    )
    assert len(out) < _baseline(raw) * 0.5, (
        f"~5KB output should compress big (TO={len(out)}, "
        f"baseline={_baseline(raw)})"
    )


def test_40kb_output_lands_under_stub(monkeypatch, tmp_path, capsys):
    """~40KB output (the measured 41.1KB `ls -la /usr/bin` shape): the final
    visible output must be <= the ~2.2KB stub baseline, and the raw output
    must remain archived + retrievable via the archive pointer."""
    raw = _ls_fixture(450)  # ~40KB
    assert 30_000 < len(raw) < 60_000, f"fixture size wrong: {len(raw)}"
    bc = _load("bash_compress")

    out = _run_main(monkeypatch, tmp_path, capsys, "ls -la /usr/bin", raw)

    assert len(out) <= _baseline(raw), (
        f"baseline-size invariant violated: TO={len(out)} baseline={_baseline(raw)}"
    )
    assert len(out) <= bc.CC_PERSISTED_OUTPUT_STUB_CHARS, (
        f"TO output must fit under the ~2.2KB stub: {len(out)} > "
        f"{bc.CC_PERSISTED_OUTPUT_STUB_CHARS}"
    )

    # Raw output archived + retrievable via the pointer even at stub size.
    key = _archive_key_from_output(out)
    assert key, f"archive pointer lost from capped output: {out[:200]!r}"
    entry = tmp_path / "snapshots" / "tool-archive" / SESSION_ID / f"{key}.json"
    assert entry.is_file(), f"archived raw missing: {entry}"
    record = json.loads(entry.read_text(encoding="utf-8"))
    assert record["original_chars"] == len(raw)
    assert "usr-bin-tool-name-0449-with-some-padding" in record.get("response", ""), (
        "archived copy does not contain the full raw output"
    )


def test_200kb_output_lands_under_stub(monkeypatch, tmp_path, capsys):
    """~200KB output: same invariant as the 40KB case."""
    raw = _ls_fixture(2200)  # ~200KB
    assert len(raw) > 150_000, f"fixture size wrong: {len(raw)}"
    bc = _load("bash_compress")

    out = _run_main(monkeypatch, tmp_path, capsys, "ls -la /usr/bin", raw)

    assert len(out) <= _baseline(raw), (
        f"baseline-size invariant violated: TO={len(out)} baseline={_baseline(raw)}"
    )
    assert len(out) <= bc.CC_PERSISTED_OUTPUT_STUB_CHARS, (
        f"TO output must fit under the ~2.2KB stub: {len(out)} > "
        f"{bc.CC_PERSISTED_OUTPUT_STUB_CHARS}"
    )
    assert _archive_key_from_output(out), "archive pointer lost from capped output"


# ---------------------------------------------------------------------------
# Pure-function tests for the guard
# ---------------------------------------------------------------------------

def test_baseline_visible_chars_boundary():
    bc = _load("bash_compress")
    assert bc._baseline_visible_chars(100) == 100
    assert bc._baseline_visible_chars(bc.CC_PERSISTED_OUTPUT_THRESHOLD_CHARS) \
        == bc.CC_PERSISTED_OUTPUT_THRESHOLD_CHARS
    assert bc._baseline_visible_chars(bc.CC_PERSISTED_OUTPUT_THRESHOLD_CHARS + 1) \
        == bc.CC_PERSISTED_OUTPUT_STUB_CHARS
    assert bc._baseline_visible_chars(200_000) == bc.CC_PERSISTED_OUTPUT_STUB_CHARS


def test_guard_passthrough_when_already_under_baseline():
    bc = _load("bash_compress")
    text = "preview " * 200  # ~1.6KB
    raw = "raw " * 1500      # ~6KB
    assert bc._enforce_baseline_invariant(text, raw, None) == text


def test_guard_defers_raw_output_for_huge_results():
    """Emit raw on huge outputs is baseline-exact: CC stubs raw output at its
    own layer to the same ~2.2KB stub it would show without TO (defer)."""
    bc = _load("bash_compress")
    raw = "z" * 40_000
    assert bc._enforce_baseline_invariant(raw, raw, None) == raw


def test_guard_caps_lossy_preview_when_no_pointer():
    """Archiving failed: only a lossy preview exists. Cap its head to the stub
    so the model still sees <= baseline chars."""
    bc = _load("bash_compress")
    preview = "p" * 10_000
    raw = "r" * 40_000
    out = bc._enforce_baseline_invariant(preview, raw, None)
    assert len(out) == bc.CC_PERSISTED_OUTPUT_STUB_CHARS
    assert set(out) == {"p"}


def test_guard_shrinks_preview_but_keeps_pointer_intact():
    """The measured regression: preview+pointer far over the stub. Guard must
    shrink the preview and keep the pointer, so the raw stays retrievable and
    CC does not re-stub (and swallow) our output."""
    bc = _load("bash_compress")
    ar = _load("archive_result")
    raw = "r" * 40_000
    preview = "p" * 10_000
    key = "unitc-test-key-01"

    final = ar.build_archive_pointer(preview, len(raw), key)
    assert len(final) > bc.CC_PERSISTED_OUTPUT_STUB_CHARS  # the regression

    out = bc._enforce_baseline_invariant(final, raw, key)
    assert len(out) <= bc.CC_PERSISTED_OUTPUT_STUB_CHARS
    assert out.endswith(ar.build_archive_pointer("", len(raw), key)), (
        "archive pointer must survive the cap"
    )
    assert out.startswith("p"), "preview head must be kept"
    assert key in out, "expand key must survive the cap"
