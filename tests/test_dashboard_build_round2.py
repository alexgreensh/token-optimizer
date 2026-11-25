"""Round two of the dashboard build read savings.

  * the cache TTL waste report is asked for twice per build (30 days and the
    dashboard's history length); each transcript must be parsed once, with
    the same report as a cold run for each window;
  * the project CLAUDE.md walk must skip the account's real home when HOME
    is redirected, as it already skips HOME;
  * the weekly price must not parse one transcript twice because the session
    log spells its path differently from discovery.
"""
from __future__ import annotations

import collections
import json
import os
import time
from pathlib import Path

from test_dashboard_build_repeats import NOW, _call, _price_fixture, _write, measure  # noqa: F401

MODEL = "claude-opus-4-6"


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _turns(age_days, h1):
    """A session with a cache rebuild after a two-hour gap, `age_days` old."""
    t0 = time.time() - age_days * 86400
    base = {"model": MODEL, "input_tokens": 10, "output_tokens": 5}
    return [
        dict(base, timestamp=_iso(t0), cache_read=0, cache_creation=40000,
             cache_creation_1h=40000 if h1 else 0, cache_creation_5m=0 if h1 else 40000,
             request_id="a%s" % age_days),
        dict(base, timestamp=_iso(t0 + 60), cache_read=40000, cache_creation=500,
             gap_since_prev_seconds=60, request_id="b%s" % age_days),
        dict(base, timestamp=_iso(t0 + 7260), cache_read=0, cache_creation=41000,
             cache_creation_5m=41000,
             gap_since_prev_seconds=7200, request_id="c%s" % age_days),
    ]


def _ttl_fixture(measure, tmp_path, monkeypatch):
    d = tmp_path / "projects" / "-repo"
    sessions = {_write(d / ("s%d.jsonl" % age), [{"age": age}]): _turns(age, age % 2)
                for age in (1, 3, 12, 20)}
    files = list(sessions)
    parses = collections.Counter()

    def fake_parse(fp):
        parses[Path(fp).name] += 1
        return [dict(t) for t in sessions[Path(fp)]]

    monkeypatch.setattr(measure, "detect_runtime", lambda: "claude")
    monkeypatch.setattr(measure, "_find_all_jsonl_files",
                        lambda days=None: [(p, p.stat().st_mtime, "-repo") for p in files])
    monkeypatch.setattr(measure, "parse_session_turns", fake_parse)
    return files, sessions, parses


def _clear(measure):
    getattr(measure, "_ttl_file_memo", {}).clear()


def _dump(r):
    return json.dumps(r, sort_keys=True, default=str)


def test_ttl_waste_parses_each_unchanged_transcript_once(measure, tmp_path, monkeypatch):
    files, sessions, parses = _ttl_fixture(measure, tmp_path, monkeypatch)
    _clear(measure)
    measure._get_cache_ttl_waste(days=30)
    measure._get_cache_ttl_waste(days=7)
    measure._get_cache_ttl_waste(days=30, collect_sessions=True)
    assert max(parses.values()) == 1 and len(parses) == 4

    # A transcript that changed is parsed again, on its own.
    parses.clear()
    with open(files[0], "a") as fh:
        fh.write("{}\n")
    measure._get_cache_ttl_waste(days=7)
    assert dict(parses) == {files[0].name: 1}


def test_ttl_waste_matches_a_cold_run_for_each_window(measure, tmp_path, monkeypatch):
    _ttl_fixture(measure, tmp_path, monkeypatch)
    cold = {}
    for days in (7, 30, None):
        _clear(measure)
        cold[days] = _dump(measure._get_cache_ttl_waste(days=days))
    _clear(measure)
    warm = {days: _dump(measure._get_cache_ttl_waste(days=days)) for days in (30, 7, None, 7)}
    assert warm == cold
    # The windows really differ, so the memo is not hiding the cutoff.
    r7, r30 = json.loads(cold[7]), json.loads(cold[30])
    assert r7["wasted_tokens"] < r30["wasted_tokens"]
    assert r30["wasted_tokens"] > 0


def test_project_walk_skips_the_real_home_when_home_is_redirected(measure, tmp_path, monkeypatch):
    real_home = tmp_path / "realhome"
    project = real_home / "work" / "proj"
    (real_home / ".claude").mkdir(parents=True)
    (real_home / ".claude" / "CLAUDE.md").write_text("real home instructions\n" * 50)
    (real_home / "CLAUDE.md").write_text("real home root\n" * 50)
    project.mkdir(parents=True)
    (project / "CLAUDE.md").write_text("project rules\n" * 5)
    monkeypatch.setattr(measure, "_real_account_home", lambda: real_home)
    monkeypatch.chdir(project)

    opened = []
    real_open = Path.open

    def tracking_open(self, *a, **k):
        opened.append(str(self))
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", tracking_open)
    components = measure.measure_components()
    paths = {c.get("path") for c in components.values() if isinstance(c, dict)}
    assert str(project / "CLAUDE.md") in paths  # project files are still counted
    for leaked in (real_home / ".claude" / "CLAUDE.md", real_home / "CLAUDE.md"):
        assert str(leaked) not in paths
        assert str(leaked) not in opened


def test_weekly_price_parses_a_symlinked_spelling_once(measure, tmp_path, monkeypatch):
    conn, start, end, (recent, settled, parent, child), kids = _price_fixture(measure, tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "projects", target_is_directory=True)
    discovered = [alias / "-repo" / p.name for p in (recent, settled, parent)]
    monkeypatch.setattr(measure, "_find_all_jsonl_files",
                        lambda days=None: [(p, p.stat().st_mtime, "-repo") for p in discovered])
    monkeypatch.setattr(measure, "_find_subagent_jsonl_files", lambda p: kids.get(str(p), []))
    parses = collections.Counter()
    real_parse = measure._parse_session_jsonl

    def counting_parse(filepath, window_start=None, window_end=None):
        parses[os.path.realpath(str(filepath))] += 1
        return real_parse(filepath, window_start=window_start, window_end=window_end)

    monkeypatch.setattr(measure, "_parse_session_jsonl", counting_parse)
    measure._parse_session_jsonl_cache.clear()
    priced = measure._price_parent_activity_window(conn, start, end, "anthropic")
    conn.close()
    assert max(parses.values()) == 1
    assert priced["sessions"] == 2 and priced["coverage"]["total_sessions"] == 2
