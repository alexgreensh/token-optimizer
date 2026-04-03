import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


class ClaudeCheckpointPolicyTests(unittest.TestCase):
    def test_list_checkpoints_parses_new_trigger_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checkpoint_dir = Path(tmpdir)
            files = [
                "sess-20260403-120000-progressive-20.md",
                "sess-20260403-120100-quality-70.md",
                "sess-20260403-120200-milestone-edit-batch.md",
                "sess-20260403-120300-end.md",
            ]
            for name in files:
                (checkpoint_dir / name).write_text("# checkpoint\n", encoding="utf-8")

            with mock.patch.object(measure, "CHECKPOINT_DIR", checkpoint_dir):
                checkpoints = measure.list_checkpoints()

            triggers = {cp["trigger"] for cp in checkpoints}
            self.assertIn("progressive-20", triggers)
            self.assertIn("quality-70", triggers)
            self.assertIn("milestone-edit-batch", triggers)
            self.assertIn("end", triggers)

    def test_list_checkpoints_skips_symlinked_checkpoint_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            checkpoint_dir = Path(tmpdir)
            outside = checkpoint_dir.parent / "outside-checkpoint.md"
            outside.write_text("secret", encoding="utf-8")
            (checkpoint_dir / "sess-20260403-120000-progressive-20.md").symlink_to(outside)

            with mock.patch.object(measure, "CHECKPOINT_DIR", checkpoint_dir):
                checkpoints = measure.list_checkpoints()

            self.assertEqual(checkpoints, [])

    def test_checkpoint_trigger_captures_pre_fanout_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_file = Path(tmpdir) / "session.jsonl"
            session_file.write_text("", encoding="utf-8")
            cache_path = Path(tmpdir) / "quality-cache-session.json"

            with (
                mock.patch.object(measure, "_quality_cache_path_for", return_value=cache_path),
                mock.patch.object(measure, "_read_quality_cache", return_value={"score": 82, "fill_pct": 21.0}),
                mock.patch.object(measure, "_checkpoint_cooldown_remaining", return_value=0),
                mock.patch.object(measure, "compact_capture", return_value="/tmp/cp.md") as compact_capture,
                mock.patch.object(measure, "_record_checkpoint_metadata") as record_meta,
            ):
                result = measure.checkpoint_trigger(
                    milestone="pre-fanout",
                    session_id="session",
                    transcript_path=str(session_file),
                    quiet=True,
                )

            self.assertEqual(result, "/tmp/cp.md")
            compact_capture.assert_called_once()
            record_meta.assert_called_once()

            with (
                mock.patch.object(measure, "_quality_cache_path_for", return_value=cache_path),
                mock.patch.object(
                    measure,
                    "_read_quality_cache",
                    return_value={"milestones_captured": ["pre-fanout"]},
                ),
                mock.patch.object(measure, "_checkpoint_cooldown_remaining", return_value=0),
                mock.patch.object(measure, "compact_capture") as compact_capture,
            ):
                result = measure.checkpoint_trigger(
                    milestone="pre-fanout",
                    session_id="session",
                    transcript_path=str(session_file),
                    quiet=True,
                )

            self.assertIsNone(result)
            compact_capture.assert_not_called()

    def test_quality_threshold_checkpoint_fires_on_first_drop(self):
        result = {"score": 69.0, "fill_pct": 24.0}
        quality_data = {"writes": []}

        with (
            mock.patch.object(measure, "_checkpoint_cooldown_remaining", return_value=0),
            mock.patch.object(measure, "compact_capture", return_value="/tmp/quality.md") as compact_capture,
            mock.patch.object(measure, "_record_checkpoint_metadata") as record_meta,
        ):
            measure._maybe_checkpoint_on_quality_or_milestone(
                quality_data=quality_data,
                cache_path=Path("/tmp/cache.json"),
                result=result,
                filepath=Path("/tmp/session.jsonl"),
            )

        compact_capture.assert_called_once()
        self.assertEqual(compact_capture.call_args.kwargs["trigger"], "quality-80")
        self.assertEqual(result["quality_thresholds_captured"], [80])
        record_meta.assert_called_once()

    def test_edit_batch_checkpoint_uses_write_threshold(self):
        result = {
            "score": 92.0,
            "fill_pct": 37.0,
            "edit_batch_marker": {"write_count": 0, "unique_file_count": 0},
        }
        quality_data = {
            "writes": [
                (1, "/tmp/a.py", "t1"),
                (2, "/tmp/b.py", "t2"),
                (3, "/tmp/a.py", "t3"),
                (4, "/tmp/c.py", "t4"),
            ]
        }

        with (
            mock.patch.object(measure, "_checkpoint_cooldown_remaining", return_value=0),
            mock.patch.object(measure, "compact_capture", return_value="/tmp/edit-batch.md") as compact_capture,
            mock.patch.object(measure, "_record_checkpoint_metadata") as record_meta,
        ):
            measure._maybe_checkpoint_on_quality_or_milestone(
                quality_data=quality_data,
                cache_path=Path("/tmp/cache.json"),
                result=result,
                filepath=Path("/tmp/session.jsonl"),
            )

        compact_capture.assert_called_once()
        self.assertEqual(compact_capture.call_args.kwargs["trigger"], "milestone-edit-batch")
        self.assertEqual(result["edit_batch_marker"]["write_count"], 4)
        self.assertEqual(result["edit_batch_marker"]["unique_file_count"], 3)
        record_meta.assert_called_once()


if __name__ == "__main__":
    unittest.main()
