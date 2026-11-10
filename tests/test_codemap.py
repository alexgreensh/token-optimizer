#!/usr/bin/env python3
"""Code map: index, lookup CLI, discovery hint, safe re-read outline, ledger.

Proves the shipped contract:
  * ON by default (lookup answers, SessionStart/SubagentStart hint, re-read outline)
    with one kill switch, TOKEN_OPTIMIZER_CODEMAP=0;
  * fail-open: any index problem prints a Grep/Read fallback (exit 0) or lets
    the Read through untouched;
  * stale files: answers follow the files on disk (mtime+size sync), and a
    changed file never gets a re-read outline.

Every test runs on synthetic fixtures with an isolated HOME and snapshot dir.
Run: python3 -m pytest tests/test_codemap.py -v
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
CODEMAP = SCRIPTS / "codemap.py"
HINT = SCRIPTS / "codemap_hint.py"
READ_CACHE = SCRIPTS / "read_cache.py"
SESSION = "22222222-2222-2222-2222-222222222222"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_FLAG_VARS = ("TOKEN_OPTIMIZER_CODEMAP", "TOKEN_OPTIMIZER_CODEMAP_REREAD", "CLAUDE_CODE_SAVE_HOOK_ADDITIONAL_CONTEXT")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    snap = tmp_path / "snap"
    home.mkdir()
    snap.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    for var in _FLAG_VARS:
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _project(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        "import json\n"
        "\n"
        "class Engine:\n"
        "    def start(self, n: int) -> int:\n"
        "        return helper(n) + 1\n"
        "\n"
        "def helper(n):\n"
        "    return n * 2\n",
        encoding="utf-8",
    )
    (pkg / "app.py").write_text(
        "from pkg.core import Engine, helper\n"
        "\n"
        "def run():\n"
        "    e = Engine()\n"
        "    return e.start(helper(3))\n",
        encoding="utf-8",
    )
    web = root / "web"
    web.mkdir()
    (web / "util.ts").write_text(
        "export function formatName(first: string, last: string): string {\n"
        "  return `${first} ${last}`;\n"
        "}\n",
        encoding="utf-8",
    )
    (web / "main.ts").write_text(
        "import { formatName } from './util';\n"
        "\n"
        "export function greet(): string {\n"
        "  return formatName('a', 'b');\n"
        "}\n",
        encoding="utf-8",
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "skip.js").write_text("function vendored() {}\n", encoding="utf-8")
    return root


def _cli(args, cwd: Path, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(CODEMAP), *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=60)


# --------------------------------------------------------------------------- #
# Lookup: on by default, answers match disk
# --------------------------------------------------------------------------- #

def test_lookup_is_on_by_default_and_answers(sandbox):
    proj = _project(sandbox / "proj")
    out = _cli(["find", "helper"], proj)
    assert out.returncode == 0, out.stderr
    assert "pkg/core.py:7-8  function helper" in out.stdout
    assert "turned off" not in out.stdout

    show = _cli(["show", "Engine.start"], proj)
    assert "     4\t    def start(self, n: int) -> int:" in show.stdout
    assert "     5\t        return helper(n) + 1" in show.stdout

    callers = _cli(["callers", "helper"], proj)
    assert "pkg/app.py: run@5" in callers.stdout
    assert "pkg/core.py: Engine.start@5" in callers.stdout

    outline = _cli(["outline", "pkg/core.py"], proj)
    assert "3-5  class Engine" in outline.stdout
    assert "  4-5  def Engine.start(self, n: int) -> int" in outline.stdout

    deps = _cli(["deps", "pkg/core.py"], proj)
    assert "imported by 1 file(s): pkg/app.py" in deps.stdout
    ts_deps = _cli(["deps", "web/main.ts"], proj)
    assert "imports 1 project file(s): web/util.ts" in ts_deps.stdout
    ts_callers = _cli(["callers", "formatName"], proj)
    assert "web/main.ts: greet@4" in ts_callers.stdout


def test_vendored_dirs_are_skipped(sandbox):
    proj = _project(sandbox / "proj")
    out = _cli(["find", "vendored"], proj)
    assert "no definition of 'vendored'" in out.stdout


def test_answers_are_hard_capped(sandbox):
    proj = _project(sandbox / "proj")
    big = "\n".join(f"def helper_{i}_{'x' * 40}(a, b, c):\n    return {i}\n" for i in range(400))
    (proj / "pkg" / "many.py").write_text(big, encoding="utf-8")
    calls = "\n".join(f"def c{i}():\n    return helper()\n" for i in range(600))
    (proj / "pkg" / "calls.py").write_text(calls, encoding="utf-8")
    import codemap
    for args in (["callers", "helper"], ["outline", "pkg/many.py"], ["find", "helper_"]):
        out = _cli(args, proj)
        assert len(out.stdout) <= codemap.MAX_OUT_CHARS + 200, args


def test_stale_file_is_reindexed_before_answering(sandbox):
    proj = _project(sandbox / "proj")
    assert "helper" in _cli(["find", "helper"], proj).stdout
    core = proj / "pkg" / "core.py"
    core.write_text(core.read_text(encoding="utf-8").replace("def helper(n):", "def renamed_helper(n):"),
                    encoding="utf-8")
    os.utime(core, ns=(time.time_ns(), time.time_ns() + 5_000_000))
    after = _cli(["find", "renamed_helper"], proj)
    assert "pkg/core.py:7-8  function renamed_helper" in after.stdout
    gone = _cli(["find", "helper"], proj).stdout
    assert "function helper " not in gone
    (proj / "pkg" / "app.py").unlink()
    assert "pkg/app.py" not in _cli(["callers", "renamed_helper"], proj).stdout


def test_kill_switch_turns_lookup_off(sandbox):
    proj = _project(sandbox / "proj")
    out = _cli(["find", "helper"], proj, {"TOKEN_OPTIMIZER_CODEMAP": "0"})
    assert out.returncode == 0
    assert "turned off" in out.stdout


# --------------------------------------------------------------------------- #
# Fail open
# --------------------------------------------------------------------------- #

def test_corrupt_index_is_rebuilt(sandbox):
    proj = _project(sandbox / "proj")
    assert "helper" in _cli(["find", "helper"], proj).stdout
    dbs = list((sandbox / "snap" / "codemap").glob("*.db"))
    assert dbs
    for db in dbs:
        for suffix in ("-wal", "-shm"):
            Path(str(db) + suffix).unlink(missing_ok=True)
        db.write_bytes(b"this is not a sqlite database" * 100)
    out = _cli(["find", "helper"], proj)
    assert out.returncode == 0
    assert "pkg/core.py:7-8  function helper" in out.stdout


def test_unusable_data_dir_falls_back_to_grep_read(sandbox):
    proj = _project(sandbox / "proj")
    blocker = sandbox / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    out = _cli(["find", "helper"], proj, {"TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(blocker)})
    assert out.returncode == 0
    assert "Use Grep/Read instead" in out.stdout


# --------------------------------------------------------------------------- #
# Discovery hint
# --------------------------------------------------------------------------- #

def _hint(payload, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(HINT)], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, timeout=30)


def test_subagent_hint_on_by_default(sandbox):
    proj = _project(sandbox / "proj")
    out = _hint({"hook_event_name": "SubagentStart", "agent_type": "Explore", "cwd": str(proj)})
    payload = json.loads(out.stdout)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "SubagentStart"
    assert "codemap.py" in hso["additionalContext"]
    assert "find|show|callers" in hso["additionalContext"]
    assert "—" not in hso["additionalContext"]  # no em-dashes in model/user-facing copy


def test_hint_silent_when_off_or_no_code(sandbox):
    proj = _project(sandbox / "proj")
    off = _hint({"hook_event_name": "SubagentStart", "cwd": str(proj)}, {"TOKEN_OPTIMIZER_CODEMAP": "0"})
    assert off.stdout.strip() == ""
    docs = sandbox / "docs"
    (docs / ".git").mkdir(parents=True)
    (docs / "notes.md").write_text("# notes\n", encoding="utf-8")
    none = _hint({"hook_event_name": "SubagentStart", "cwd": str(docs)})
    assert none.stdout.strip() == ""
    skip = _hint({"hook_event_name": "SubagentStart", "agent_type": "statusline-setup", "cwd": str(proj)})
    assert skip.stdout.strip() == ""


def test_session_text_and_background_warm(sandbox):
    import codemap_hint
    import codemap_index as ci
    proj = _project(sandbox / "proj")
    text = codemap_hint.session_text({"cwd": str(proj)})
    assert text.startswith("[Token Optimizer] Code map available")
    lock = ci.index_path(proj.resolve()).with_suffix(".building")
    db = ci.index_path(proj.resolve())
    deadline = time.time() + 30
    while time.time() < deadline and (lock.exists() or not db.exists()):
        time.sleep(0.2)
    assert db.exists()
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT v FROM meta WHERE k='complete'").fetchone()[0] == "1"
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Safe re-read outline (end to end through read_cache)
# --------------------------------------------------------------------------- #

_WORDS = "alpha beta gamma delta omega sigma kappa lambda theta zeta rho tau".split()


def _service_file(path: Path) -> Path:
    r = random.Random(7)
    lines = ['"""Service layer."""', "import json", ""]
    for c in range(8):
        lines.append(f"class {_WORDS[c].title()}Handler{c}(object):")
        lines.append(f'    """Handles {_WORDS[c]} work item {c}."""')
        for m in range(10):
            w, a = r.choice(_WORDS), r.choice(_WORDS)
            lines += [
                f"    def {w}_{a}_{m}(self, {a}_value, limit={m}, *, strict=False):",
                f"        result = [{a}_value for _ in range(limit)]",
                f"        if strict and len(result) > {r.randint(1, 50)}:",
                f'            raise ValueError("{w} overflow in {a}")',
                f'        return json.dumps({{"{w}": result, "n": {m}}})',
                "",
            ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _read(sandbox, path: Path, tool_use_id: str, extra_env=None, offset=0, limit=0):
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_READ_CACHE"] = "1"
    # Strict hook-context caps make the existing skeleton path decline
    # (hook_context_cap), which is exactly the gap the code map outline covers.
    env["CLAUDE_CODE_SAVE_HOOK_ADDITIONAL_CONTEXT"] = "1"
    env["TOKEN_OPTIMIZER_FIRST_READ_SHADOW"] = "0"
    if extra_env:
        env.update(extra_env)
    payload = {
        "tool_name": "Read", "hook_event_name": "PreToolUse",
        "tool_input": {"file_path": str(path), "offset": offset, "limit": limit},
        "session_id": SESSION, "tool_use_id": tool_use_id,
    }
    out = subprocess.run([sys.executable, str(READ_CACHE)], input=json.dumps(payload), env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    text = out.stdout.strip()
    return json.loads(text.splitlines()[-1]) if text else None


def _denied(resp) -> bool:
    return bool(resp) and resp.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_reread_outline_on_by_default_then_full_file(sandbox):
    proj = _project(sandbox / "proj")
    f = _service_file(proj / "pkg" / "service.py")
    assert not _denied(_read(sandbox, f, "t1"))
    second = _read(sandbox, f, "t2")
    assert _denied(second), second
    ctx = second["hookSpecificOutput"]["additionalContext"]
    assert "unchanged since your full Read" in ctx
    assert "class AlphaHandler0" in ctx and "10 methods: " in ctx
    # One outline per file version: asking again gets the full file.
    assert not _denied(_read(sandbox, f, "t3"))
    conn = sqlite3.connect(str(sandbox / "snap" / "trends.db"))
    try:
        rows = conn.execute("SELECT tokens_saved FROM savings_events WHERE event_type='codemap_outline'").fetchall()
        kinds = conn.execute("SELECT kind FROM codemap_events").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0][0] > 0
    assert ("reread_outline",) in kinds


def test_reread_outline_never_for_changed_file(sandbox):
    proj = _project(sandbox / "proj")
    f = _service_file(proj / "pkg" / "service.py")
    _read(sandbox, f, "t1")
    with f.open("a", encoding="utf-8") as fh:
        fh.write("\n# edited\n")
    resp = _read(sandbox, f, "t2")  # delta mode may serve a diff; never a code map outline
    assert "code map outline" not in json.dumps(resp or {})
    assert "unchanged since your full Read" not in json.dumps(resp or {})


def test_reread_outline_needs_prior_full_read_and_soft_block(sandbox):
    proj = _project(sandbox / "proj")
    f = _service_file(proj / "pkg" / "service.py")
    _read(sandbox, f, "t1", offset=1, limit=50)
    assert not _denied(_read(sandbox, f, "t2"))
    g = _service_file(proj / "pkg" / "service2.py")
    _read(sandbox, g, "u1", {"TOKEN_OPTIMIZER_READ_CACHE_MODE": "shadow"})
    assert not _denied(_read(sandbox, g, "u2", {"TOKEN_OPTIMIZER_READ_CACHE_MODE": "shadow"}))


def test_reread_outline_kill_switches(sandbox):
    proj = _project(sandbox / "proj")
    for var in ("TOKEN_OPTIMIZER_CODEMAP", "TOKEN_OPTIMIZER_CODEMAP_REREAD"):
        f = _service_file(proj / "pkg" / f"svc_{var.lower()}.py")
        _read(sandbox, f, f"{var}-1", {var: "0"})
        assert not _denied(_read(sandbox, f, f"{var}-2", {var: "0"}))


def test_reread_fails_open_when_index_unusable(sandbox):
    proj = _project(sandbox / "proj")
    f = _service_file(proj / "pkg" / "service.py")
    _read(sandbox, f, "t1")
    (sandbox / "snap" / "codemap").mkdir(exist_ok=True)
    import codemap_index as ci
    db = ci.index_path(proj.resolve())
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    db.mkdir()  # a directory where the index file should be: sqlite cannot open it
    assert not _denied(_read(sandbox, f, "t2"))


def test_try_outline_swallows_errors(sandbox, monkeypatch):
    import codemap_reread
    proj = _project(sandbox / "proj")
    f = _service_file(proj / "pkg" / "service.py")

    def boom(*_a, **_k):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(codemap_reread, "build_outline", boom)
    entry = {"ranges_seen": [[0, 0]]}
    assert codemap_reread.try_outline(
        str(f), entry, offset=0, limit=0, tokens_est=5000, reason_code="low_confidence",
        mode="soft_block", mtime_ns=f.stat().st_mtime_ns, max_context_chars=2600) is None


# --------------------------------------------------------------------------- #
# Ledger and savings tiers
# --------------------------------------------------------------------------- #

def test_ledger_books_only_unread_files_as_estimated(sandbox):
    import codemap_ledger as led
    now = time.time() - 3600
    a = led.log_lookup("find", 100, [("/p/a.py", 4000)], now=now)
    b = led.log_lookup("show", 300, [("/p/b.py", 6000)], now=now)
    assert a and b
    assert led.note_read("/p/b.py", now=now + 30) is True
    res = led.resolve_pending(now=now + 3600)
    assert res["resolved"] == 2
    assert res["avoided_tokens"] == 4000
    assert res["booked"] == 4000 - 400
    conn = sqlite3.connect(str(sandbox / "snap" / "trends.db"))
    try:
        rows = conn.execute("SELECT tokens_saved FROM savings_events WHERE event_type='codemap_lookup'").fetchall()
    finally:
        conn.close()
    assert rows == [(3600,)]
    summ = led.summary()
    assert summ["lookups"] == 2 and summ["lookups_followed_by_read"] == 1


def test_ledger_books_nothing_when_answers_cost_more(sandbox):
    import codemap_ledger as led
    now = time.time() - 3600
    led.log_lookup("callers", 500, [], now=now)
    assert led.resolve_pending(now=now + 3600)["booked"] == 0


def test_cli_lookup_is_logged(sandbox):
    proj = _project(sandbox / "proj")
    _cli(["show", "helper"], proj)
    conn = sqlite3.connect(str(sandbox / "snap" / "trends.db"))
    try:
        row = conn.execute("SELECT command, session_id, answer_tokens, covered, status FROM codemap_events").fetchone()
    finally:
        conn.close()
    assert row[0] == "show" and row[1] == SESSION and row[2] > 0 and row[4] == "pending"
    assert json.loads(row[3])[0][0].endswith("pkg/core.py")


def test_codemap_lookup_is_estimated_tier():
    from _measure import savings
    import measure
    rows = [("structure_map", 1, 1000, 0.003), ("codemap_lookup", 2, 5000, 0.015),
            ("codemap_outline", 1, 700, 0.002)]
    by_cat, total_tokens, _c, _e, popped, _r = savings._net_realized_savings(rows, {})
    assert "codemap_lookup" not in by_cat and popped["codemap_lookup"]["tokens_saved"] == 5000
    assert total_tokens == 1700
    assert "codemap_lookup" in measure._RUNWAY_ESTIMATED_EVENT_TYPES
    assert "codemap_outline" in measure._COUNTED_SE_EVENT_TYPES
