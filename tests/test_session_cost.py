"""`measure.py session-cost`: "what did the Garden session yesterday cost?"

Users ask in plain words; Claude runs this command through the token-dashboard
skill. It must find the right trends.db on every layout, never write to it,
match sessions by words / id / day, and give the dashboard's own dollar figure.

Expected dollars are worked by hand from pricing/prices.json (anthropic cards):
  claude-opus-5-5  (generation card opus_5_5): $4 in, $20 out, $0.20 cache read,
                   $5 cache write (5m)
  claude-haiku-5-5 (haiku_5_5): $0.10 in, $0.50 out, $0.01 cache read
  The opus FAMILY card is $5 / $25 / $0.50 / $6.25: a session priced there is wrong.

Garden session (yesterday, noon): 3 parent calls on Opus 5.5, each
  1,000 in + 2,000 out + 100,000 cache read + 10,000 cache write
  = 1000*4 + 2000*20 + 100000*0.2 + 10000*5 = 114,000 / 1e6 = $0.114 per call
  -> main thread 3 * 0.114 = $0.342
and one Haiku 5.5 subagent with 2 calls, each 2,000 in + 1,000 out + 50,000 cache read
  = 2000*0.1 + 1000*0.5 + 50000*0.01 = 1,200 / 1e6 = $0.0012 per call
  -> subagents $0.0024
Session total: $0.3444. Components: input 3*0.004 + 2*0.0002 = $0.0124,
  output 3*0.04 + 2*0.0005 = $0.121, cache read 3*0.02 + 2*0.0005 = $0.061,
  cache write 3*0.05 = $0.15.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_cost_accuracy_200 import _assistant, _load  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE = SCRIPTS / "measure.py"

SID_GARDEN = "1a2b3c4d-0000-4000-8000-000000000001"
SID_SYNC = "2b3c4d5e-0000-4000-8000-000000000002"
SID_TRENDS = "3c4d5e6f-0000-4000-8000-000000000003"
SID_HEBREW = "4d5e6f70-0000-4000-8000-000000000004"
SID_LATE = "5e6f7081-0000-4000-8000-000000000005"

GARDEN_MAIN = 3 * 0.114
GARDEN_SUBS = 2 * 0.0012
GARDEN_TOTAL = GARDEN_MAIN + GARDEN_SUBS  # $0.3444
# Sync session: 2 Opus 5.5 calls of 1,000 in + 1,000 out = (4000 + 20000)/1e6 each.
SYNC_TOTAL = 2 * 0.024


def _noon(days_ago):
    return (datetime.now() - timedelta(days=days_ago)).replace(hour=12, minute=0, second=0, microsecond=0)


def _transcript(path, topic, records, sidechain=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    first = records[0]["timestamp"]
    user = {"type": "user", "timestamp": first, "message": {"content": topic}}
    if sidechain:
        user["isSidechain"] = True
        for r in records:
            r["isSidechain"] = True
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [user, *records]) + "\n",
                    encoding="utf-8")
    last = datetime.fromisoformat(records[-1]["timestamp"].replace("Z", "+00:00")).timestamp()
    os.utime(path, (last, last))


def build_history(projects):
    """The synthetic user every test asks about."""
    nav = _noon(1)
    _transcript(projects / "-Users-u-work-garden-site" / f"{SID_GARDEN}.jsonl",
                "Garden site doodles for the homepage",
                [_assistant(f"nav-{i}", nav + timedelta(minutes=i), model="claude-opus-5-5",
                            inp=1000, out=2000, cr=100_000, cc=10_000) for i in range(3)])
    _transcript(projects / "-Users-u-work-garden-site" / SID_GARDEN / "subagents" / "agent-a1.jsonl",
                "Explore the repo",
                [_assistant(f"navsub-{i}", nav + timedelta(minutes=1, seconds=10 * (i + 1)),
                            model="claude-haiku-5-5", inp=2000, out=1000, cr=50_000)
                 for i in range(2)], sidechain=True)
    # Earlier today, never yesterday (a run just after midnight stays on today).
    now = datetime.now().replace(second=0, microsecond=0)
    today = max(now - timedelta(minutes=30), now.replace(hour=0, minute=1))
    _transcript(projects / "-Users-u-total-recall" / f"{SID_SYNC}.jsonl",
                "TR sync: fix the indexing wedge",
                [_assistant(f"sync-{i}", today + timedelta(minutes=i), model="claude-opus-5-5",
                            inp=1000, out=1000) for i in range(2)])
    _transcript(projects / "-Users-u-total-recall" / f"{SID_TRENDS}.jsonl",
                "trends cleanup and sync of the docs",
                [_assistant("trends-0", today, model="claude-opus-5-5", inp=1000, out=1000)])
    heb = _noon(3)
    _transcript(projects / "-Users-u-clients" / f"{SID_HEBREW}.jsonl",
                "סיכום פגישה עם נבינה על האתר",
                [_assistant("heb-0", heb, model="claude-opus-5-5", inp=1000, out=1000)])


@pytest.fixture()
def history(tmp_path, monkeypatch):
    measure = _load(tmp_path, monkeypatch)
    measure.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    build_history(tmp_path / "claude_home" / "projects")
    measure.collect_sessions(days=30, quiet=True)
    return measure


def _q(measure, *argv):
    return measure.session_cost_query(list(argv))


def _ids(res):
    return [s["session_id"] for s in res["sessions"]]


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def test_plain_words_and_yesterday_find_the_garden_session(history):
    res = _q(history, "what", "did", "the", "Garden", "session", "yesterday", "cost?")
    assert res["query"]["words"] == ["garden"]
    assert res["query"]["date"] == (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert _ids(res) == [SID_GARDEN]


def test_all_words_must_match_and_short_words_are_whole_tokens(history):
    res = _q(history, "tr", "sync")
    # "trends cleanup and sync" has "sync" but "tr" only inside "trends".
    assert _ids(res) == [SID_SYNC]
    assert res["total_cost_usd"] == pytest.approx(SYNC_TOTAL, abs=1e-6)


def test_today_word_and_date_flag(history):
    assert set(_ids(_q(history, "today"))) == {SID_SYNC, SID_TRENDS}
    assert set(_ids(_q(history, "--date", "today"))) == {SID_SYNC, SID_TRENDS}
    assert _ids(_q(history, "garden", "--date", "today")) == []


def test_id_prefix_and_full_id(history):
    assert _ids(_q(history, SID_GARDEN[:8])) == [SID_GARDEN]
    assert _ids(_q(history, "--id", SID_GARDEN)) == [SID_GARDEN]
    assert _ids(_q(history, "cost", "of", "session", SID_SYNC[:13])) == [SID_SYNC]


def test_unicode_hebrew_and_case_insensitive(history):
    assert _ids(_q(history, "נבינה")) == [SID_HEBREW]
    assert _ids(_q(history, "GARDEN", "Site")) == [SID_GARDEN]


def test_no_match_says_so_and_offers_the_closest(history):
    res = _q(history, "garden", "zebra")
    assert res["match_count"] == 0 and res["sessions"] == []
    assert res["note"] and "collected" in res["note"]
    # The session matching one of the two words comes first.
    assert res["closest"][0]["session_id"] == SID_GARDEN


def test_session_not_collected_yet_is_explained(history, tmp_path):
    _transcript(tmp_path / "claude_home" / "projects" / "-repo" / f"{SID_LATE}.jsonl",
                "brand new", [_assistant("late-0", datetime.now(), model="claude-opus-5-5")])
    res = _q(history, "--id", SID_LATE[:8])
    assert res["match_count"] == 0
    assert "not collected yet" in res["note"]
    assert res["uncollected_transcripts"]


# --------------------------------------------------------------------------- #
# Pricing: hand-worked, and identical to the dashboard's own number
# --------------------------------------------------------------------------- #

def test_garden_cost_matches_the_hand_worked_dollars(history):
    (s,) = _q(history, "garden", "yesterday")["sessions"]
    c = s["cost_usd"]
    assert c["total"] == pytest.approx(GARDEN_TOTAL, abs=1e-6)
    assert c["main_thread"] == pytest.approx(GARDEN_MAIN, abs=1e-6)
    assert c["subagents"] == pytest.approx(GARDEN_SUBS, abs=1e-6)
    assert c["input"] == pytest.approx(0.0124, abs=1e-6)
    assert c["output"] == pytest.approx(0.121, abs=1e-6)
    assert c["cache_read"] == pytest.approx(0.061, abs=1e-6)
    assert c["cache_write"] == pytest.approx(0.15, abs=1e-6)
    assert "long_context_adjustment" not in c
    assert s["tokens"] == {"input": 3 * 1000 + 2 * 2000, "output": 3 * 2000 + 2 * 1000,
                           "cache_read": 3 * 100_000 + 2 * 50_000, "cache_write": 3 * 10_000}
    assert s["api_calls"] == 3 and s["subagents"]["included"]
    assert {m["model"] for m in s["models"]} == {"claude-opus-5-5", "claude-haiku-5-5"}
    assert "not a bill" in _q(history, "garden")["basis"]


def test_a_multi_day_session_gives_the_asked_days_share(history, tmp_path):
    """Two Opus 5.5 calls of 1,000 in + 1,000 out ($0.024 each), one two days
    ago and one yesterday: total $0.048, of which yesterday $0.024."""
    _transcript(tmp_path / "claude_home" / "projects" / "-Users-u-longrun" / f"{SID_LATE}.jsonl",
                "Longrun accelerator work",
                [_assistant("long-0", _noon(2), model="claude-opus-5-5", inp=1000, out=1000),
                 _assistant("long-1", _noon(1), model="claude-opus-5-5", inp=1000, out=1000)])
    history.collect_sessions(days=30, quiet=True)
    (s,) = _q(history, "longrun", "yesterday")["sessions"]
    assert len(s["days"]) == 2
    assert s["cost_usd"]["total"] == pytest.approx(0.048, abs=1e-6)
    assert s["cost_usd"]["on_date"] == pytest.approx(0.024, abs=1e-6)
    (one_day,) = _q(history, "garden", "yesterday")["sessions"]
    assert "on_date" not in one_day["cost_usd"]


def test_same_number_as_the_dashboard(history):
    """Call both: the dashboard's session_cost_usd and session-cost's total."""
    res = _q(history, "--days", "30")
    trends = history._collect_trends_from_db(days=30)
    dash = {}
    for day in trends["daily"]:
        for sd in day["session_details"]:
            dash[Path(sd["jsonl_path"]).stem] = sd["session_cost_usd"]
    ours = {s["session_id"]: s["cost_usd"]["total"] for s in _q(history, "--days", "30", "--limit", "50")["sessions"]}
    assert set(ours) == set(dash) and len(ours) == res["match_count"] == 4
    for sid, usd in ours.items():
        assert usd == pytest.approx(dash[sid], abs=1e-4), sid
    assert dash[SID_GARDEN] == pytest.approx(GARDEN_TOTAL, abs=1e-4)


def test_a_session_stored_twice_counts_once(history):
    conn = sqlite3.connect(str(history.TRENDS_DB))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(session_log)") if r[1] != "id"]
    conn.execute(
        f"INSERT INTO session_log ({', '.join(cols)}) SELECT "
        + ", ".join("jsonl_path || '.copy'" if c == "jsonl_path" else c for c in cols)
        + " FROM session_log WHERE session_uuid = ?", (SID_GARDEN,))
    conn.commit()
    conn.close()
    res = _q(history, "garden")
    assert _ids(res) == [SID_GARDEN]
    assert res["total_cost_usd"] == pytest.approx(GARDEN_TOTAL, abs=1e-6)


# --------------------------------------------------------------------------- #
# Read-only, from a real process, on the real DB layouts
# --------------------------------------------------------------------------- #

def _settled_db(history):
    """The fixture's trends.db, checkpointed so a plain copy carries every row."""
    conn = sqlite3.connect(str(history.TRENDS_DB))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return history.TRENDS_DB


def _decoy_db(path):
    """An empty, schema-only trends.db where an old install would have left one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE session_log (id INTEGER PRIMARY KEY, jsonl_path TEXT, date TEXT)")
    conn.commit()
    conn.close()


def _env(home, **extra):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    # The fixture dated the sessions in this process's zone; the child must
    # resolve "yesterday" in the same one (a user's hook and shell share it).
    if os.environ.get("TZ"):
        env["TZ"] = os.environ["TZ"]
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _run(env, *argv, cwd=None):
    proc = subprocess.run([sys.executable, str(MEASURE), "session-cost", *argv], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=cwd)
    return proc


def _fingerprint(directory):
    out = {}
    for p in sorted(Path(directory).iterdir()):
        st = p.stat()
        out[p.name] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns)
    return out


def _plugin_layout(history, root):
    """Claude Code plugin install: trends.db under plugins/data/<plugin>/data,
    with empty legacy DBs where older installs kept theirs."""
    home = root / "home"
    data = home / ".claude" / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "data"
    data.mkdir(parents=True)
    shutil.copy2(_settled_db(history), data / "trends.db")
    _decoy_db(home / ".claude" / "token-optimizer" / "trends.db")
    _decoy_db(home / ".claude" / "_backups" / "token-optimizer" / "trends.db")
    return home, data / "trends.db"


def test_finds_the_plugin_data_db_not_a_legacy_one(history, tmp_path):
    home, _db = _plugin_layout(history, tmp_path / "m")
    proc = _run(_env(home, TOKEN_OPTIMIZER_RUNTIME="claude"), "garden", "yesterday", "--json")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    res = json.loads(proc.stdout)
    assert "plugins/data/token-optimizer-" in res["db"].replace("\\", "/"), res["db"]
    assert [s["session_id"] for s in res["sessions"]] == [SID_GARDEN]
    assert res["sessions"][0]["cost_usd"]["total"] == pytest.approx(GARDEN_TOTAL, abs=1e-6)


def test_finds_the_db_under_claude_config_dir(history, tmp_path):
    home = tmp_path / "m" / "home"
    cfg = tmp_path / "m" / "cfg"
    data = cfg / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "data"
    data.mkdir(parents=True)
    shutil.copy2(_settled_db(history), data / "trends.db")
    _decoy_db(home / ".claude" / "plugins" / "data" / "token-optimizer-x" / "data" / "trends.db")
    proc = _run(_env(home, TOKEN_OPTIMIZER_RUNTIME="claude", CLAUDE_CONFIG_DIR=cfg), "tr", "sync", "--json")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    res = json.loads(proc.stdout)
    assert Path(res["db"]).resolve() == (data / "trends.db").resolve()
    assert [s["session_id"] for s in res["sessions"]] == [SID_SYNC]


def _db_bytes(db):
    st = db.stat()
    return hashlib.sha256(db.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns


def test_never_writes_the_db_even_while_a_writer_holds_the_lock(history, tmp_path):
    home, db = _plugin_layout(history, tmp_path / "m")
    env = _env(home, TOKEN_OPTIMIZER_RUNTIME="claude")

    # No writer attached: lock-free read, and not one file created or touched.
    before = _fingerprint(db.parent)
    proc = _run(env, "garden", "--json")
    assert proc.returncode == 0, proc.stderr
    modes = [json.loads(proc.stdout)["read_mode"]]
    assert _fingerprint(db.parent) == before

    # A live writer (the collector at session end): one commit sits in the WAL,
    # and the next transaction is open and holding the write lock.
    writer = sqlite3.connect(str(db), timeout=0.2)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE session_log SET topic = 'Garden, committed in the WAL' WHERE session_uuid = ?",
                   (SID_GARDEN,))
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE session_log SET topic = 'uncommitted' WHERE session_uuid = ?", (SID_GARDEN,))
    main_before = _db_bytes(db)
    proc = _run(env, "garden", "--json")
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    modes.append(res["read_mode"])
    # Sees the committed WAL row, not the open transaction.
    assert [s["topic"] for s in res["sessions"]] == ["Garden, committed in the WAL"]
    assert _db_bytes(db) == main_before
    writer.commit()  # the reader never blocked the writer
    writer.close()

    # A writer holding the file exclusively, with a commit in its WAL and no
    # shared index: the lock-free snapshot still answers from the main file.
    writer = sqlite3.connect(str(db), timeout=0.2)
    writer.execute("PRAGMA locking_mode=EXCLUSIVE")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE session_log SET slug = 'held' WHERE session_uuid = ?", (SID_SYNC,))
    writer.commit()
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("UPDATE session_log SET topic = topic || '!' WHERE session_uuid = ?", (SID_SYNC,))
    proc = _run(env, "tr", "sync", "--json")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    res = json.loads(proc.stdout)
    modes.append(res["read_mode"])
    assert [s["session_id"] for s in res["sessions"]] == [SID_SYNC]
    writer.commit()
    writer.close()
    assert modes == ["quiescent", "shared-read", "snapshot"]


def test_read_only_run_leaves_db_bytes_and_mtime_alone(history, tmp_path):
    home, db = _plugin_layout(history, tmp_path / "m")
    st = db.stat()
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    names = sorted(p.name for p in db.parent.iterdir())
    for argv in (("garden", "yesterday"), ("zebra",), ("--id", SID_SYNC), ("--days", "7", "--json")):
        proc = _run(_env(home, TOKEN_OPTIMIZER_RUNTIME="claude"), *argv)
        assert proc.returncode == 0, (argv, proc.stderr)
    assert db.stat().st_mtime_ns == st.st_mtime_ns and db.stat().st_size == st.st_size
    assert hashlib.sha256(db.read_bytes()).hexdigest() == digest
    assert sorted(p.name for p in db.parent.iterdir()) == names


def test_an_older_install_db_is_read_as_is_never_migrated(tmp_path):
    """A trends.db from an older version (few columns, no per-model data) is
    priced on the dominant model and left exactly as it was: no migration.
    Hand-worked: Opus 5.5, 1,000,000 input (no cache) + 100,000 output
    = 1e6 * $4/MTok + 1e5 * $20/MTok = $4.00 + $2.00 = $6.00."""
    home = tmp_path / "home"
    data = home / ".claude" / "plugins" / "data" / "token-optimizer-alexgreensh-token-optimizer" / "data"
    data.mkdir(parents=True)
    db = data / "trends.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE session_log (id INTEGER PRIMARY KEY, jsonl_path TEXT UNIQUE, date TEXT, "
                 "project TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_hit_rate REAL, "
                 "model_usage_json TEXT, topic TEXT)")
    conn.execute("INSERT INTO session_log (jsonl_path, date, project, input_tokens, output_tokens, "
                 "cache_hit_rate, model_usage_json, topic) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (f"/old/-Users-u-garden/{SID_GARDEN}.jsonl", datetime.now().strftime("%Y-%m-%d"),
                  "-Users-u-garden", 1_000_000, 100_000, 0.0,
                  json.dumps({"claude-opus-5-5": 1_100_000}), "Garden old install"))
    conn.commit()
    conn.close()
    before = _fingerprint(data)
    proc = _run(_env(home, TOKEN_OPTIMIZER_RUNTIME="claude"), "garden", "--json")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    (s,) = json.loads(proc.stdout)["sessions"]
    assert s["session_id"] == SID_GARDEN
    assert s["cost_usd"]["total"] == pytest.approx(6.00, abs=1e-6)
    assert s["cost_usd"]["input"] == pytest.approx(4.00, abs=1e-6)
    assert s["cost_usd"]["output"] == pytest.approx(2.00, abs=1e-6)
    assert _fingerprint(data) == before


# --------------------------------------------------------------------------- #
# The real entry point: the skill's own bash, as Claude's Bash tool runs it
# --------------------------------------------------------------------------- #

def _skill_blocks():
    text = (REPO / "skills" / "token-dashboard" / "SKILL.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    blocks, cur = [], None
    for line in lines:
        if line.strip().startswith("```bash"):
            cur = []
        elif line.strip() == "```" and cur is not None:
            blocks.append("\n".join(cur))
            cur = None
        elif cur is not None:
            cur.append(line)
    preamble = next(b for b in blocks if 'RUNTIME="${TOKEN_OPTIMIZER_RUNTIME:-}"' in b)
    ask = next(b for b in blocks if "session-cost" in b)
    return text, preamble, ask


def test_skill_frontmatter_lets_claude_pick_it_for_cost_questions():
    text, _p, _a = _skill_blocks()
    front = text.split("---")[1]
    assert "disable-model-invocation" not in front
    desc = next(l for l in front.splitlines() if l.startswith("description:"))[len("description:"):].strip()
    assert "cost" in desc and len(desc) <= 200
    assert len(text.splitlines()) <= 150


@pytest.mark.skipif(os.name == "nt", reason="the skill preamble is bash")
def test_skill_preamble_plus_command_answers_the_question(history, tmp_path):
    home, _db = _plugin_layout(history, tmp_path / "m")
    skills = home / ".claude" / "skills"
    skills.mkdir(parents=True)
    os.symlink(REPO / "skills" / "token-optimizer", skills / "token-optimizer")
    _text, preamble, ask = _skill_blocks()
    assert "<the user's words>" in ask
    script = preamble + "\n" + ask.replace("<the user's words>", "garden yesterday") + "\n"
    env = _env(home, CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli")
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=120, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    res = json.loads(proc.stdout)
    assert [s["session_id"] for s in res["sessions"]] == [SID_GARDEN]
    assert res["sessions"][0]["cost_usd"]["total"] == pytest.approx(GARDEN_TOTAL, abs=1e-6)


def test_text_output_reads_like_an_answer(history, tmp_path):
    home, _db = _plugin_layout(history, tmp_path / "m")
    proc = _run(_env(home, TOKEN_OPTIMIZER_RUNTIME="claude"), "garden", "yesterday")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "Garden site doodles" in out
    assert "Total:  $0.3444  (main thread $0.3420 + subagents $0.0024)" in out
    assert "not a bill" in out
    bad = _run(_env(home, TOKEN_OPTIMIZER_RUNTIME="claude"), "--date", "someday")
    assert bad.returncode == 1 and "--date" in bad.stdout
