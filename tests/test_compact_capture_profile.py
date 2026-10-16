"""Diagnostic profile of the before-compact checkpoint save (issue #253 follow-up).

WHY THIS FILE EXISTS
--------------------
Issue #253: the PreCompact ``compact-capture`` save takes ~1 s on macOS but
7-9 s on Windows (reporter timings, real user machine). PR #256 raised the
budgets so the save finishes; it did not make the save faster. This file is
the profiling step named in that PR's follow-ups: run the save the way the
host runs it and print where the wall clock goes, on every OS CI covers,
so the Windows/macOS delta stops being a guess.

HOW IT MEASURES
---------------
No mocks and no in-process imports of product code (importing measure.py in
the pytest process would freeze its import-time paths and measure nothing
about a real hook). Each scenario copies ``hooks/`` and
``skills/token-optimizer/scripts/`` into a throwaway plugin root, points a
sandboxed HOME / CLAUDE_CONFIG_DIR at throwaway dirs, and runs the real
chain as a subprocess::

    python hooks/run.py skills/token-optimizer/scripts/measure.py \
        compact-capture --trigger auto --quiet

exactly as hooks.json does (minus the bash launcher, which only locates the
interpreter). Checkpoints and __pycache__ land in the sandbox, never in the
working tree (the suite tree-guard watches that) and never in a real home.

HOW TO READ THE OUTPUT
----------------------
Every scenario reports one ``[profile]`` line per measurement. pytest shows
them twice: in the captured-output section on ``-s``/failures, and ALWAYS in
the ``warnings summary`` section of a plain ``pytest -q`` run (each line is
emitted as a unique UserWarning), which is how the numbers reach the CI log
on windows-latest without any workflow changes.

These tests never fail on timing. They fail only when the save itself breaks
(no checkpoint written, non-zero exit), so a red run still means a real bug.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

# Per-subprocess ceiling. run.py caps its own child at 120 s, so anything
# longer is a hang, not a slow save.
RUN_TIMEOUT_S = 180

# Transcript sizes. 32 MB is a heavy-but-normal session (the issue reporter's
# class of session; PR #256's receipts used a 71 MB real one). 2 MB is the
# small-session floor for the scaling delta.
SMALL_BYTES = 2 * 1024 * 1024
LARGE_BYTES = 32 * 1024 * 1024

_ENV_STRIP_PREFIXES = (
    "CODEX_", "HERMES_", "COPILOT_", "CURSOR_", "OPENCODE", "ANTIGRAVITY_",
    "GROK_", "DEVIN_", "TOKEN_OPTIMIZER_",
)
_ENV_STRIP_EXACT = {
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CLAUDE_CONFIG_DIR",
    "CODEX_HOME", "HERMES_HOME", "COPILOT_HOME", "XDG_CACHE_HOME", "HOME",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
}


# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------
def _copy_plugin_tree(dst: Path) -> Path:
    """Copy hooks/ + skills/token-optimizer/scripts/ into a throwaway root.

    module_runner writes __pycache__ next to the scripts it runs; running
    against the real tree would dirty the working copy (the suite tree-guard
    fails the run on tracked-file mutation, and untracked pyc litter is how
    'works on my machine' starts).
    """
    plugin = dst / "plugin"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(HOOKS, plugin / "hooks", ignore=ignore)
    shutil.copytree(SCRIPTS, plugin / "skills" / "token-optimizer" / "scripts", ignore=ignore)
    return plugin


def _child_env(home: Path, plugin: Path) -> dict:
    """Minimal, deterministic hook environment."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in _ENV_STRIP_EXACT
        and not any(k.startswith(p) for p in _ENV_STRIP_PREFIXES)
    }
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(claude_dir)
    env["CLAUDE_PLUGIN_ROOT"] = str(plugin)
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    return env


def _run_hook(plugin: Path, env: dict, args, stdin_payload: dict, timeout=RUN_TIMEOUT_S):
    """Run hooks/run.py exactly as a host would (a subprocess, never a shell)."""
    cmd = [sys.executable, str(plugin / "hooks" / "run.py"),
           "skills/token-optimizer/scripts/measure.py", *args]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        input=json.dumps(stdin_payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    return proc, time.perf_counter() - t0


def _run_module(plugin: Path, env: dict, args, timeout=RUN_TIMEOUT_S):
    """Run measure.py through module_runner directly (skips run.py's child)."""
    scripts_dir = plugin / "skills" / "token-optimizer" / "scripts"
    cmd = [sys.executable, str(plugin / "hooks" / "module_runner.py"),
           str(scripts_dir), "measure", *args]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env,
                          timeout=timeout, input="")
    return proc, time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Synthetic transcript
# ---------------------------------------------------------------------------
_WORDS = ("the quick brown fox jumps over the lazy dog refactor function "
          "handle edge case tests pass checkpoint save transcript").split()


def _write_transcript(path: Path, target_bytes: int, session_id: str) -> int:
    """Deterministic Claude-Code-shaped JSONL. Returns the line count."""
    import random
    rnd = random.Random(20261017)
    lines = []
    total = 0
    i = 0
    while total < target_bytes:
        i += 1
        r = rnd.random()
        stamp = f"2026-10-16T10:{i % 60:02d}:{(i * 7) % 60:02d}Z"
        if r < 0.3:
            rec = {"type": "user", "uuid": f"u{i}", "sessionId": session_id,
                   "timestamp": stamp,
                   "message": {"role": "user",
                               "content": " ".join(rnd.choice(_WORDS) for _ in range(20))}}
        elif r < 0.75:
            content = [{"type": "text",
                        "text": "I decided to use the cached path. "
                                + " ".join(rnd.choice(_WORDS) for _ in range(40))}]
            if rnd.random() < 0.5:
                content.append({"type": "tool_use", "id": f"toolu_{i}",
                                "name": "Edit",
                                "input": {"file_path": f"/src/app/module{i % 37}.py"}})
            rec = {"type": "assistant", "uuid": f"a{i}", "sessionId": session_id,
                   "timestamp": stamp,
                   "message": {"role": "assistant", "model": "claude-opus-4-6",
                               "content": content}}
        else:
            big = " ".join(rnd.choice(_WORDS) for _ in range(1200))
            rec = {"type": "user", "uuid": f"t{i}", "sessionId": session_id,
                   "timestamp": stamp,
                   "message": {"role": "user",
                               "content": [{"type": "tool_result",
                                            "tool_use_id": f"toolu_{i - 1}",
                                            "content": big}]}}
        line = json.dumps(rec)
        total += len(line) + 1
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def _seed_quality_cache(home: Path, session_id: str) -> None:
    """A fresh per-session quality cache, as UserPromptSubmit would leave it."""
    cache_dir = home / ".claude" / "token-optimizer"
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "score": 82.5, "grade": "B",
        "breakdown": {"context_fill_degradation": {"fill_pct": 41.0,
                                                   "model": "claude-opus-4-6"}},
        "signals": {"decisions": 0.8, "errors": 0.9},
        "topic": "fix bug", "model": "claude-opus-4-6",
        "total_messages": 100, "decisions_found": 3, "compactions": 0,
    }
    (cache_dir / f"quality-cache-{session_id}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def _checkpoints(home: Path) -> list:
    cp_dir = home / ".claude" / "token-optimizer" / "checkpoints"
    return list(cp_dir.glob("*.md")) if cp_dir.is_dir() else []


def _fresh_checkpoint(home: Path, since: float):
    """A checkpoint file written at or after ``since`` (mtime), or None.

    Count-based checks misfire: two captures of one session in the same
    second share a filename ({sid}-{YYYYMMDD-HHMMSS}-{trigger}.md), so the
    second save REPLACES the first instead of adding a file.
    """
    for cp in _checkpoints(home):
        try:
            if cp.stat().st_mtime >= since:
                return cp
        except OSError:
            continue
    return None


def _emit(lines):
    """Surface profile lines in the CI log even when pytest captures stdout.

    A unique-message UserWarning appears in the ``warnings summary`` section
    of a plain ``pytest -q`` run; captured stdout does not. Do both.
    """
    for line in lines:
        print(line)
    warnings.warn("\n".join(lines), stacklevel=2)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------
@pytest.fixture()
def sandbox(tmp_path):
    """One copied plugin tree + sandboxed home per test."""
    home = tmp_path / "home"
    home.mkdir()
    plugin = _copy_plugin_tree(tmp_path)
    env = _child_env(home, plugin)
    return {"home": home, "plugin": plugin, "env": env, "tmp": tmp_path}


def _make_session(sandbox, name, size_bytes):
    sid = f"{name}-1111-2222-3333-444455556666"
    jsonl = (sandbox["home"] / ".claude" / "projects" / "-proj" / f"{sid}.jsonl")
    n = _write_transcript(jsonl, size_bytes, sid)
    return sid, jsonl, n


def _capture(sandbox, sid, jsonl, cwd):
    payload = {"session_id": sid, "transcript_path": str(jsonl), "cwd": str(cwd)}
    return _run_hook(sandbox["plugin"], sandbox["env"],
                     ["compact-capture", "--trigger", "auto", "--quiet"], payload)


def test_profile_baseline_import_and_dispatch(sandbox):
    """Cost floor: interpreter + measure.py import + CLI dispatch, no save.

    Everything a capture pays BEFORE any save work: process start, source or
    pycache load of the 2.7 MB measure.py and its sibling imports, runtime
    detection, argv dispatch. A capture slower than this baseline is slow in
    the save itself; a capture close to it is slow in startup.
    """
    times = []
    for _ in range(3):
        proc, dt = _run_module(sandbox["plugin"], sandbox["env"], ["version"])
        assert proc.returncode == 0, proc.stderr[-500:]
        times.append(dt)
    chain_times = []
    for _ in range(3):
        proc, dt = _run_hook(sandbox["plugin"], sandbox["env"], ["version"], {})
        assert proc.returncode == 0, proc.stderr[-500:]
        chain_times.append(dt)
    _emit([f"[profile] baseline.version_import_dispatch "
           f"runs={','.join(f'{t:.3f}' for t in times)}s median={sorted(times)[1]:.3f}s "
           f"(one python: interpreter + 2.7MB measure.py import + dispatch, no save)",
           f"[profile] baseline.via_run_py "
           f"runs={','.join(f'{t:.3f}' for t in chain_times)}s median={sorted(chain_times)[1]:.3f}s "
           f"(two pythons: run.py + module_runner child, as hooks.json runs it)"])


def test_profile_capture_scaling(sandbox):
    """The save itself, warm everything, small vs large transcript.

    Warm pycache (a priming version run), warm quality cache (seeded the way
    UserPromptSubmit leaves it), cwd outside any git repo. The small->large
    delta is the cost of reading the transcript end to end; flat would mean
    the save is dominated by fixed startup, steep by per-byte work.
    """
    # Prime __pycache__ so this measures the save, not one-time compile.
    proc, _ = _run_module(sandbox["plugin"], sandbox["env"], ["version"])
    assert proc.returncode == 0, proc.stderr[-500:]

    lines = []
    for label, size in (("small2MB", SMALL_BYTES), ("large32MB", LARGE_BYTES)):
        sid, jsonl, n = _make_session(sandbox, label, size)
        _seed_quality_cache(sandbox["home"], sid)
        runs = []
        for _ in range(2):
            t_before = time.time()
            proc, dt = _capture(sandbox, sid, jsonl, sandbox["tmp"])
            assert proc.returncode == 0, proc.stderr[-500:]
            assert _fresh_checkpoint(sandbox["home"], t_before) is not None, (
                f"no checkpoint written ({label}); stdout={proc.stdout[-300:]} "
                f"stderr={proc.stderr[-300:]}")
            runs.append(dt)
        lines.append(f"[profile] capture.{label} lines={n} "
                     f"runs={','.join(f'{t:.3f}' for t in runs)}s "
                     f"(warm pycache, warm quality cache, non-repo cwd)")
    _emit(lines)


def test_profile_capture_cold_pycache(sandbox):
    """First run after install/update: measure.py compiled from source.

    module_runner runs measure.py as a module so __pycache__ normally hides
    this; the first hook after every plugin update pays it once. The cold vs
    warm delta is the pure parse/compile slice of the 2.7 MB source.
    """
    sid, jsonl, n = _make_session(sandbox, "coldpyc", SMALL_BYTES)
    _seed_quality_cache(sandbox["home"], sid)
    cold_proc, cold = _capture(sandbox, sid, jsonl, sandbox["tmp"])
    assert cold_proc.returncode == 0, cold_proc.stderr[-500:]
    warm_proc, warm = _capture(sandbox, sid, jsonl, sandbox["tmp"])
    assert warm_proc.returncode == 0, warm_proc.stderr[-500:]
    _emit([f"[profile] capture.cold_pycache first={cold:.3f}s second={warm:.3f}s "
           f"delta={cold - warm:.3f}s (one-time 2.7MB source compile)"])


def test_profile_capture_cold_quality_cache(sandbox):
    """Stale/missing quality cache: the save re-parses the whole transcript.

    At a real PreCompact the cache is fresh (refreshed on UserPromptSubmit and
    throttled PostToolUse), so this is the worst case, not the common one -
    but a long tool run can leave the cache stale, and then the capture pays
    _parse_jsonl_for_quality over the full file on top of everything else.
    """
    sid, jsonl, n = _make_session(sandbox, "coldqc", SMALL_BYTES)
    proc, _ = _run_module(sandbox["plugin"], sandbox["env"], ["version"])
    assert proc.returncode == 0, proc.stderr[-500:]
    # Warm first (cache seeded), then cold (cache deleted).
    _seed_quality_cache(sandbox["home"], sid)
    _, warm = _capture(sandbox, sid, jsonl, sandbox["tmp"])
    cache = sandbox["home"] / ".claude" / "token-optimizer" / f"quality-cache-{sid}.json"
    cache.unlink()
    t_before = time.time()
    proc, cold = _capture(sandbox, sid, jsonl, sandbox["tmp"])
    assert proc.returncode == 0, proc.stderr[-500:]
    assert _fresh_checkpoint(sandbox["home"], t_before) is not None, (
        "no checkpoint on cold quality cache")
    _emit([f"[profile] capture.cold_quality_cache warm={warm:.3f}s cold={cold:.3f}s "
           f"delta={cold - warm:.3f}s (full-transcript quality re-parse, {n} lines)"])


def test_profile_git_spawn_pair():
    """The two `git rev-parse` spawns every capture pays for checkpoint metadata.

    Timed directly so the process-creation price is visible as its own line
    (Windows CreateProcess + AV image scan vs POSIX fork/exec). Skipped where
    no git is installed.
    """
    git = shutil.which("git")
    if not git:
        pytest.skip("git not installed")
    kw = {"capture_output": True, "text": True, "cwd": str(REPO),
          "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    t0 = time.perf_counter()
    br = subprocess.run([git, "rev-parse", "--abbrev-ref", "HEAD"], timeout=10, **kw)
    t1 = time.perf_counter()
    sha = subprocess.run([git, "rev-parse", "--short", "HEAD"], timeout=10, **kw)
    t2 = time.perf_counter()
    assert br.returncode == 0 and sha.returncode == 0
    _emit([f"[profile] git.spawn_pair branch={t1 - t0:.3f}s sha={t2 - t1:.3f}s "
           f"total={t2 - t0:.3f}s (2 process spawns per capture)"])


def test_profile_transcript_read_full_vs_tail(sandbox):
    """Full-file read vs reverse-seek tail read of the same transcript.

    _extract_session_state keeps only the last 500 entries (deque maxlen) but
    reads and json-parses EVERY line to get them. This measures both shapes
    on the same file and OS: the ratio is the headroom a tail read buys, and
    the full-read number is the per-byte price the save currently pays (worst
    under AV scan-on-read, which meters every byte the loop pulls).
    """
    sid, jsonl, n = _make_session(sandbox, "readshape", LARGE_BYTES)

    def full_read(tail_lines=500):
        from collections import deque
        recs = deque(maxlen=tail_lines)
        with open(jsonl, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return recs

    def tail_read(tail_lines=500, chunk=1 << 16):
        with open(jsonl, "rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            buf = b""
            while pos > 0 and buf.count(b"\n") <= tail_lines:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
        recs = []
        for line in buf.decode("utf-8", errors="replace").splitlines()[-tail_lines:]:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return recs

    assert len(full_read()) == len(tail_read()) == 500
    full = min(_time_call(full_read) for _ in range(3))
    tail = min(_time_call(tail_read) for _ in range(3))
    ratio = full / tail if tail > 0 else float("inf")
    _emit([f"[profile] transcript.read_shape file={os.stat(jsonl).st_size >> 20}MB "
           f"lines={n} full={full:.3f}s tail500={tail:.3f}s ratio={ratio:.1f}x"])


def _time_call(fn):
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0
