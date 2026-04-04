import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "token-optimizer"
    / "scripts"
    / "measure.py"
)

spec = importlib.util.spec_from_file_location("token_optimizer_measure", MODULE_PATH)
measure = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(measure)


class CacheTtlMixTests(unittest.TestCase):
    def _with_temp_trends_db(self):
        tmpdir = tempfile.TemporaryDirectory()
        original_snapshot_dir = measure.SNAPSHOT_DIR
        original_trends_db = measure.TRENDS_DB
        measure.SNAPSHOT_DIR = Path(tmpdir.name)
        measure.TRENDS_DB = measure.SNAPSHOT_DIR / "trends.db"
        return tmpdir, original_snapshot_dir, original_trends_db

    def test_parse_session_jsonl_extracts_ttl_split(self):
        records = [
            {
                "type": "user",
                "timestamp": "2026-04-04T09:00:00Z",
                "message": {"content": "Investigate cache behavior"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-04T09:01:00Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 300,
                        "cache_creation_input_tokens": 90,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 60,
                            "ephemeral_5m_input_tokens": 30,
                        },
                    },
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            session_path = Path(tmpdir) / "session.jsonl"
            session_path.write_text(
                "\n".join(json.dumps(r) for r in records) + "\n",
                encoding="utf-8",
            )

            parsed = measure._parse_session_jsonl(session_path)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["total_cache_create"], 90)
        self.assertEqual(parsed["total_cache_create_1h"], 60)
        self.assertEqual(parsed["total_cache_create_5m"], 30)
        self.assertAlmostEqual(parsed["cache_hit_rate"], 300 / 490, places=6)

    def test_parse_session_turns_extracts_per_turn_ttl_split(self):
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-04-04T09:01:00Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "tool_use", "name": "Read", "input": {}}],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 300,
                        "cache_creation_input_tokens": 90,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 60,
                            "ephemeral_5m_input_tokens": 30,
                        },
                    },
                },
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            session_path = Path(tmpdir) / "session.jsonl"
            session_path.write_text(
                "\n".join(json.dumps(r) for r in records) + "\n",
                encoding="utf-8",
            )

            turns = measure.parse_session_turns(session_path)

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["cache_creation"], 90)
        self.assertEqual(turns[0]["cache_creation_1h"], 60)
        self.assertEqual(turns[0]["cache_creation_5m"], 30)
        self.assertEqual(turns[0]["tools_used"], ["Read"])

    def test_parse_session_jsonl_computes_call_gap_stats(self):
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-04-04T09:00:00Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-04T09:00:30Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-04-04T09:02:00Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            session_path = Path(tmpdir) / "session.jsonl"
            session_path.write_text(
                "\n".join(json.dumps(r) for r in records) + "\n",
                encoding="utf-8",
            )

            parsed = measure._parse_session_jsonl(session_path)

        self.assertAlmostEqual(parsed["avg_call_gap_seconds"], 60.0, places=6)
        self.assertEqual(parsed["max_call_gap_seconds"], 90.0)
        self.assertEqual(parsed["p95_call_gap_seconds"], 90.0)

    def test_make_session_key_is_stable_for_same_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_path = Path(tmpdir) / "nested" / "session.jsonl"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_text("", encoding="utf-8")

            first = measure._make_session_key(session_path)
            second = measure._make_session_key(Path(tmpdir) / "nested" / "." / "session.jsonl")

        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_init_trends_db_migrates_ttl_columns(self):
        tmpdir, original_snapshot_dir, original_trends_db = self._with_temp_trends_db()
        try:
            conn = sqlite3.connect(str(measure.TRENDS_DB))
            conn.execute(
                """CREATE TABLE session_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    jsonl_path TEXT UNIQUE,
                    date TEXT NOT NULL
                )"""
            )
            conn.commit()
            conn.close()

            migrated = measure._init_trends_db()
            cols = {
                row[1] for row in migrated.execute("PRAGMA table_info(session_log)").fetchall()
            }
            migrated.close()

            self.assertIn("cache_create_1h_tokens", cols)
            self.assertIn("cache_create_5m_tokens", cols)
            self.assertIn("cache_ttl_scanned", cols)
            self.assertIn("avg_call_gap_seconds", cols)
            self.assertIn("max_call_gap_seconds", cols)
            self.assertIn("p95_call_gap_seconds", cols)
        finally:
            measure.SNAPSHOT_DIR = original_snapshot_dir
            measure.TRENDS_DB = original_trends_db
            tmpdir.cleanup()

    def test_backfill_cache_ttl_mix_updates_existing_rows(self):
        records = [
            {
                "type": "assistant",
                "timestamp": "2026-04-04T09:01:00Z",
                "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_creation_input_tokens": 90,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 60,
                            "ephemeral_5m_input_tokens": 30,
                        },
                    },
                },
            }
        ]

        tmpdir, original_snapshot_dir, original_trends_db = self._with_temp_trends_db()
        try:
            session_path = Path(tmpdir.name) / "session.jsonl"
            session_path.write_text(
                "\n".join(json.dumps(r) for r in records) + "\n",
                encoding="utf-8",
            )

            conn = measure._init_trends_db()
            conn.execute(
                """INSERT INTO session_log
                   (jsonl_path, date, project, cache_hit_rate, cache_create_1h_tokens,
                    cache_create_5m_tokens, cache_ttl_scanned)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(session_path), "2026-04-04", "home", 0.0, 0, 0, 0),
            )
            conn.commit()

            updated = measure._backfill_session_metrics(conn, days=30, limit=10)
            row = conn.execute(
                """SELECT cache_create_1h_tokens, cache_create_5m_tokens, cache_ttl_scanned,
                          avg_call_gap_seconds, max_call_gap_seconds, p95_call_gap_seconds
                   FROM session_log WHERE jsonl_path = ?""",
                (str(session_path),),
            ).fetchone()
            conn.close()

            self.assertEqual(updated, 1)
            self.assertEqual(row[0], 60)
            self.assertEqual(row[1], 30)
            self.assertEqual(row[2], 1)
            self.assertIsNone(row[3])
            self.assertIsNone(row[4])
            self.assertIsNone(row[5])
        finally:
            measure.SNAPSHOT_DIR = original_snapshot_dir
            measure.TRENDS_DB = original_trends_db
            tmpdir.cleanup()

    def test_build_ttl_period_summary_formats_all_1h_only(self):
        original_collect = measure._collect_trends_data
        try:
            measure._collect_trends_data = lambda days=30: {
                "daily": [
                    {
                        "session_details": [
                            {"cache_create_1h_tokens": 10, "cache_create_5m_tokens": 0},
                            {"cache_create_1h_tokens": 20, "cache_create_5m_tokens": 0},
                        ]
                    }
                ]
            }
            summary = measure._build_ttl_period_summary(7)
            self.assertEqual(summary["label"], "7d: all 1h-only")
            self.assertEqual(summary["mixed_sessions"], 0)
            self.assertEqual(summary["five_only_sessions"], 0)
            self.assertEqual(summary["one_hour_only_sessions"], 2)
        finally:
            measure._collect_trends_data = original_collect


if __name__ == "__main__":
    unittest.main()
