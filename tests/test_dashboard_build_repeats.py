"""A dashboard build must not re-read transcripts it has no use for.

Two costs dominated a large-history build:
  * the weekly activity price parsed every transcript ever recorded to find
    activity inside a seven-day window;
  * the skill-listing / unused-connector check read every recent transcript
    once per caller, and a build has several callers.

Each test below checks the read counts, and checks that the outputs equal
the pre-change behaviour: the weekly price with the skip turned off, and
the transcript check against a frozen copy of the original single-pass scan.
"""
from __future__ import annotations

import shutil
import collections
import importlib.util
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers.measure_split import exec_source_module as _split_exec_module

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def measure(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snapshot"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = mod
    _split_exec_module(mod)
    yield mod
    sys.modules.pop("measure", None)


@pytest.fixture()
def tp(measure):
    from _measure import transcript_product
    getattr(transcript_product, "_EVENTS", {}).clear()
    yield transcript_product
    getattr(transcript_product, "_EVENTS", {}).clear()


# Frozen copy of transcript_product.scan before per-file memoization.
def _reference_scan(tp, paths, budget=None, now=None, days=30):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    listing, samples, used, seen_calls = [], {}, set(), set()
    incomplete = False
    for path in paths:
        path = Path(path)
        tools, instructions = {}, {}
        have_listing = False
        files = [path] + sorted((path.parent / path.stem / 'subagents').glob('*.jsonl'))
        for source in files:
            try:
                with source.open(encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        if not any(tag in line for tag in ('"tool_use"', '"skill_listing"', '"deferred_tools_delta"', '"mcp_instructions_delta"')):
                            continue
                        try:
                            record = json.loads(line)
                        except (ValueError, TypeError):
                            incomplete = True
                            continue
                        if not isinstance(record, dict):
                            continue
                        a = record.get('attachment') or {}
                        if not isinstance(a, dict):
                            continue
                        kind = a.get('type')
                        stamp = tp._time(record.get('timestamp'))
                        recent = stamp is not None and now - timedelta(days=7) <= stamp <= now
                        if source == path and recent and not have_listing and kind == 'skill_listing' and a.get('isInitial') and isinstance(a.get('content'), str):
                            listing.append((len(a['content']), a.get('skillCount')))
                            have_listing = True
                        if source == path and recent and kind in ('deferred_tools_delta', 'mcp_instructions_delta'):
                            names = a.get('addedNames') or []
                            values = a.get('addedLines' if kind == 'deferred_tools_delta' else 'addedBlocks') or []
                            target = tools if kind == 'deferred_tools_delta' else instructions
                            for name in a.get('removedNames') or []:
                                target.pop(name, None)
                            if kind == 'deferred_tools_delta':
                                for value in values:
                                    if isinstance(value, str):
                                        name = value.split(None, 1)[0] if value.strip() else ''
                                        if tp._server(name):
                                            target[name] = len(value)
                            elif len(names) == len(values):
                                for name, value in zip(names, values):
                                    if isinstance(name, str) and isinstance(value, str):
                                        target[name] = len(value)
                            else:
                                incomplete = True
                        if record.get('type') != 'assistant':
                            continue
                        message = record.get('message') or {}
                        content = message.get('content') if isinstance(message, dict) else []
                        if not isinstance(content, list):
                            continue
                        for item in content:
                            if not isinstance(item, dict) or item.get('type') != 'tool_use':
                                continue
                            server = tp._server(item.get('name'))
                            call_id = item.get('id')
                            if server and (stamp is None or cutoff <= stamp <= now):
                                if not call_id or call_id not in seen_calls:
                                    used.add(tp._normalize(server))
                                if call_id:
                                    seen_calls.add(call_id)
            except OSError:
                incomplete = True
        session = {}
        for name, size in tools.items():
            server = tp._server(name)
            if server:
                session.setdefault(tp._normalize(server), [0, 0])[0] += size
        for name, size in instructions.items():
            session.setdefault(tp._normalize(name), [0, 0])[1] += size
        for server, sizes in session.items():
            samples.setdefault(server, []).append(sizes)
    median = int(statistics.median(size for size, _ in listing)) if listing else None
    try:
        budget = int(budget)
        if budget <= 0:
            budget = None
    except (TypeError, ValueError, OverflowError):
        budget = None
    at_cap = None if median is None or budget is None else median >= budget * .98
    basis = 'configured budget' if at_cap is not None else 'unknown'
    if at_cap is None and median:
        plateau = [(size, count) for size, count in listing if abs(size - median) <= median * .01 and isinstance(count, int) and count > 0]
        counts = [count for _, count in plateau]
        if len(counts) >= 5 and max(counts) >= min(counts) * 1.1:
            at_cap, basis = True, 'observed plateau'
    unused = []
    if not incomplete:
        for server in sorted(set(samples) - used):
            rows = samples[server]
            tool = int(statistics.median(r[0] for r in rows))
            instruction = int(statistics.median(r[1] for r in rows))
            unused.append({'server': server, 'tool_chars': tool, 'instruction_chars': instruction,
                           'estimated_tokens_per_call': (tool + instruction) // 4, 'sessions': len(rows)})
    return {'listing': {'median_chars': median, 'samples': len(listing), 'budget_chars': budget, 'at_cap': at_cap, 'basis': basis},
            'unused_connectors': unused, 'incomplete': incomplete}


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _attach(ts, **a):
    return {"type": "attachment", "timestamp": _z(ts), "attachment": a}


def _call(ts, call_id, name, rid=None):
    rid = rid or "r-" + str(call_id)
    return {"type": "assistant", "timestamp": _z(ts) if ts else None, "requestId": rid,
            "message": {"id": "m-" + rid, "role": "assistant", "model": "claude-opus-4-6",
                        "content": [{"type": "tool_use", "id": call_id, "name": name, "input": {}}],
                        "usage": {"input_tokens": 100, "output_tokens": 20}}}


def _write(path, records, extra_lines=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _history(root, variant="clean"):
    """Transcripts covering every branch scan() weighs against `now`."""
    d = root / "projects" / "-repo"
    recent, old, ancient = NOW - timedelta(days=2), NOW - timedelta(days=12), NOW - timedelta(days=45)
    paths = []
    for i in range(6):
        stamp = recent if i % 2 else old
        recs = [
            _attach(stamp, type="skill_listing", isInitial=True, content="s" * (5000 + i), skillCount=30 + i),
            _attach(stamp, type="skill_listing", isInitial=True, content="later" * 3, skillCount=1),
            _attach(stamp, type="deferred_tools_delta", addedNames=[],
                    addedLines=["mcp__alpha__x " + "a" * (100 + i), "mcp__beta__y " + "b" * 80, "Read builtin", 7]),
            _attach(stamp, type="deferred_tools_delta", removedNames=["mcp__beta__y"], addedLines=[]),
            _attach(stamp, type="mcp_instructions_delta", addedNames=["alpha", "gamma"],
                    addedBlocks=["i" * (40 + i), "g" * 30]),
            _call(stamp, "toolu_shared", "mcp__alpha__x"),
            _call(None, f"toolu_nostamp_{i}", "mcp__delta__z") if i == 3 else _call(ancient, f"toolu_old_{i}", "mcp__delta__z"),
        ]
        if variant == "recent_mismatch" and i == 1:
            recs.append(_attach(recent, type="mcp_instructions_delta", addedNames=["a", "b"], addedBlocks=["x"]))
        if i == 4:
            recs.append(_attach(old, type="mcp_instructions_delta", addedNames=["a", "b"], addedBlocks=["x"]))
        extra = ["{not json \"tool_use\""] if variant == "bad_json" and i == 2 else ()
        p = _write(d / f"s{i}.jsonl", recs, extra)
        if i in (1, 4):
            _write(d / f"s{i}" / "subagents" / "agent-1.jsonl",
                   [_call(recent, "toolu_shared", "mcp__beta__y"), _call(recent, f"toolu_sub_{i}", "mcp__epsilon__q")])
        paths.append(p)
    return paths


@pytest.mark.parametrize("variant", ["clean", "bad_json", "recent_mismatch"])
def test_scan_matches_the_original_single_pass(tp, tmp_path, variant):
    paths = _history(tmp_path, variant)
    for days in (30, 7):
        expected = _reference_scan(tp, paths, budget="5004", now=NOW, days=days)
        assert tp.scan(paths, budget="5004", now=NOW, days=days) == expected
        assert tp.scan(paths, budget="5004", now=NOW, days=days) == expected  # memoized pass
        assert tp.scan(paths, now=NOW + timedelta(days=6), days=days) == _reference_scan(
            tp, paths, now=NOW + timedelta(days=6), days=days)  # same memo, later clock


def _count_jsonl_opens(monkeypatch):
    opened = collections.Counter()
    real_open = Path.open

    def counting_open(self, *a, **kw):
        if self.suffix == ".jsonl":
            opened[str(self)] += 1
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", counting_open)
    return opened


def test_repeat_check_reads_each_unchanged_transcript_once(measure, tp, tmp_path, monkeypatch):
    """A build calls from_facade from several places; only the first reads."""
    paths = _history(tmp_path)
    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_find_all_jsonl_files",
                        lambda days=None: [(p, p.stat().st_mtime, "-repo") for p in paths])
    opened = _count_jsonl_opens(monkeypatch)
    first = tp.from_facade(measure)
    files_read = sum(opened.values())
    assert files_read == len(paths) + 2  # six parents, two subagent files
    assert max(opened.values()) == 1
    opened.clear()
    assert tp.from_facade(measure) == first
    assert sum(opened.values()) == 0

    # A transcript that grows mid-build is read again, alone.
    with paths[1].open("a", encoding="utf-8") as f:
        f.write(json.dumps(_call(datetime.now(timezone.utc), "toolu_new", "mcp__zeta__w")) + "\n")
    opened.clear()
    grown = tp.from_facade(measure)
    assert dict(opened) == {str(paths[1]): 1}
    assert "zeta" not in [r["server"] for r in grown["unused_connectors"]]


def _price_fixture(measure, tmp_path):
    """Three sessions: one recent, one settled long ago, one settled parent
    whose subagent ran inside the window."""
    start = NOW - timedelta(days=3)
    end = NOW
    d = tmp_path / "projects" / "-repo"
    recent = _write(d / "recent.jsonl", [_call(NOW - timedelta(days=1), "t1", "Read", rid="q1")])
    settled = _write(d / "settled.jsonl", [_call(NOW - timedelta(days=20), "t2", "Read", rid="q2")])
    parent = _write(d / "parent.jsonl", [_call(NOW - timedelta(days=20), "t3", "Read", rid="q3")])
    child = _write(d / "parent" / "subagents" / "agent-1.jsonl",
                   [_call(NOW - timedelta(days=1), "t4", "Read", rid="q4")])
    for p, when in ((recent, NOW - timedelta(days=1)), (settled, NOW - timedelta(days=20)),
                    (parent, NOW - timedelta(days=20)), (child, NOW - timedelta(days=1))):
        os.utime(p, (when.timestamp(), when.timestamp()))
    conn = measure._init_trends_db()
    for p in (recent, settled, parent):
        conn.execute("INSERT INTO session_log (jsonl_path,date,input_tokens,output_tokens,api_calls) VALUES (?,?,?,?,?)",
                     (str(p), "2026-08-01", 1, 0, 1))
    conn.commit()
    monkeypatch_children = {str(parent): [child]}
    return conn, start, end, (recent, settled, parent, child), monkeypatch_children


def test_weekly_price_skips_transcripts_settled_before_the_window(measure, tmp_path, monkeypatch):
    from _measure import weekly_activity
    conn, start, end, (recent, settled, parent, child), kids = _price_fixture(measure, tmp_path)
    monkeypatch.setattr(measure, "_find_all_jsonl_files", lambda days=None: [])
    monkeypatch.setattr(measure, "_find_subagent_jsonl_files", lambda p: kids.get(str(p), []))
    parses = collections.Counter()
    real_parse = measure._parse_session_jsonl

    def counting_parse(filepath, window_start=None, window_end=None):
        parses[Path(filepath).name] += 1
        return real_parse(filepath, window_start=window_start, window_end=window_end)

    monkeypatch.setattr(measure, "_parse_session_jsonl", counting_parse)
    measure._parse_session_jsonl_cache.clear()
    fast = measure._price_parent_activity_window(conn, start, end, "anthropic")
    assert parses == {"recent.jsonl": 1, "parent.jsonl": 1, "agent-1.jsonl": 1}

    # The skip only drops work: with it off, every file is parsed and the
    # priced result is the same, value for value.
    parses.clear()
    measure._parse_session_jsonl_cache.clear()
    monkeypatch.setattr(weekly_activity, "_settled_before", lambda paths, start: False)
    full = measure._price_parent_activity_window(conn, start, end, "anthropic")
    conn.close()
    assert parses["settled.jsonl"] == 1
    assert json.dumps(fast, sort_keys=True) == json.dumps(full, sort_keys=True)
    # The recent session and the settled parent (through its subagent) count.
    assert fast["sessions"] == 2 and fast["coverage"]["total_sessions"] == 2


def test_settled_check_reads_a_file_it_cannot_stat(tmp_path):
    from importlib import import_module
    sys.path.insert(0, str(SCRIPTS))
    weekly_activity = import_module("_measure.weekly_activity")
    old = _write(tmp_path / "old.jsonl", [])
    os.utime(old, (1, 1))
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert weekly_activity._settled_before([old], start) is True
    assert weekly_activity._settled_before([old, tmp_path / "gone.jsonl"], start) is False
    os.utime(old, (start.timestamp() - 3600, start.timestamp() - 3600))
    assert weekly_activity._settled_before([old], start) is False  # inside the day of slack


# Fields that carry the build's own clock, the live process table or the
# snapshot directory; they differ between any two builds.
_VOLATILE = ("generated_at", "_cached_ts", "running_sessions", "period_end", "period_start",
             "/window/start", "/window/end", "computed_at")


def _leaves(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _leaves(v, f"{prefix}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _leaves(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def _build_history(home, now):
    proj = home / ".claude" / "projects" / "-Users-fixture-repo"
    for s in range(40):
        days_ago = (s * 2.3) % 95 + 0.3  # away from the 7, 30 and window-start edges
        start = now - timedelta(days=days_ago)
        sid = f"{s:08d}-0000-4000-8000-{s:012d}"
        recs = [{"type": "user", "timestamp": _z(start), "sessionId": sid, "cwd": "/Users/fixture/repo",
                 "message": {"role": "user", "content": f"task {s}"}}]
        if s % 3 == 0:
            recs += [_attach(start, type="skill_listing", isInitial=True, content="x" * (8000 + s), skillCount=40 + s % 5),
                     _attach(start, type="deferred_tools_delta", addedNames=[],
                             addedLines=["mcp__alpha__a " + "y" * 200, "mcp__beta__b " + "z" * 300])]
        t = start
        for turn in range(6 + s % 9):
            t += timedelta(seconds=97 + turn * 13)
            rec = _call(t, f"toolu_{s}_{turn}", "mcp__alpha__a" if turn == 1 else "Read", rid=f"req_{s}_{turn}")
            rec["message"]["usage"] = {"input_tokens": 10 + turn, "output_tokens": 300 + 7 * s,
                                       "cache_read_input_tokens": 20000 + 100 * turn,
                                       "cache_creation_input_tokens": 1500}
            recs.append(rec)
        p = _write(proj / f"{sid}.jsonl", recs)
        os.utime(p, (t.timestamp(), t.timestamp()))
        if s % 4 == 0:
            c = _write(proj / sid / "subagents" / "agent-a1.jsonl",
                       [_call(start + timedelta(minutes=i), f"sub_{s}_{i}", "mcp__beta__b", rid=f"sub_{s}_{i}")
                        for i in range(3)])
            os.utime(c, (t.timestamp(), t.timestamp()))


def _one_build(tmp_path, monkeypatch, arm, resets_at):
    # Same HOME path and contents for both arms: a build writes into HOME, and
    # the CLAUDE.md lookup climbs from cwd, so cwd stays inside tmp_path too.
    home = tmp_path / "home"
    shutil.rmtree(home, ignore_errors=True)
    shutil.copytree(tmp_path / "home.pristine", home, copy_function=shutil.copy2)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOKEN_OPTIMIZER_RUNTIME", "claude")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / f"snapshot-{arm}"))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("measure", None)
    spec = importlib.util.spec_from_file_location("measure", SCRIPTS / "measure.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["measure"] = m
    _split_exec_module(m)
    from _measure import transcript_product, weekly_activity
    getattr(transcript_product, "_EVENTS", {}).clear()
    assert str(m.CLAUDE_DIR).startswith(str(tmp_path))
    m.collect_sessions(quiet=True)
    m._keepwarm_read_meters = lambda rate_limits_path=None, now=None: {
        "available": True, "stale": False, "age_s": 60, "ts": time.time(),
        "five_hour_pct": 20.0, "seven_day_pct": 56.0, "seven_day_resets_at": resets_at}
    m._SESSION_WEIGHT_MIN_ANCHOR_SESSIONS = 2
    m._AFTER_MIN_SESSIONS = 1
    if arm == "before":
        monkeypatch.setattr(transcript_product, "scan",
                            lambda paths, budget=None, now=None, days=30: _reference_scan(
                                transcript_product, paths, budget=budget, now=now, days=days))
        monkeypatch.setattr(weekly_activity, "_settled_before", lambda paths, start: False, raising=False)
    returns = collections.defaultdict(list)
    for owner, name in ((transcript_product, "from_facade"), (weekly_activity, "price")):
        real = getattr(owner, name)

        def recorded(*a, _real=real, _name=name, **kw):
            out = _real(*a, **kw)
            returns[_name].append(json.loads(json.dumps(out, sort_keys=True, default=str)))
            return out
        monkeypatch.setattr(owner, name, recorded)
    captured = {}
    real_sanitize = m._sanitize_dashboard_paths

    def capture(data):
        out = real_sanitize(data)
        captured["data"] = json.loads(json.dumps(out, sort_keys=True, default=str))
        return out
    m._sanitize_dashboard_paths = capture
    m.generate_standalone_dashboard(quiet=True, force=True)
    sys.modules.pop("measure", None)
    snap = str(tmp_path / f"snapshot-{arm}")
    leaves = {k: (v.replace(snap, "<snapshot>") if isinstance(v, str) else v)
              for k, v in _leaves(captured["data"]) if not any(tag in k for tag in _VOLATILE)}
    return leaves, dict(returns)


def test_dashboard_data_is_unchanged_by_the_read_savings(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    _build_history(tmp_path / "home.pristine", now)
    resets_at = (now + timedelta(days=3, hours=6)).timestamp()
    environ = dict(os.environ)
    before, before_returns = _one_build(tmp_path, monkeypatch, "before", resets_at)
    monkeypatch.undo()
    os.environ.clear()
    os.environ.update(environ)  # a build may set process env flags of its own
    after, after_returns = _one_build(tmp_path, monkeypatch, "after", resets_at)
    assert before_returns["price"] and before_returns["from_facade"]
    assert before_returns["price"][0] is not None
    assert after_returns == before_returns
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert changed == [], [(k, before.get(k), after.get(k)) for k in changed][:30]
    assert len(before) > 1000
