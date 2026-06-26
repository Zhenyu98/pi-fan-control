import tempfile
import unittest
from pathlib import Path

from fan_control_maintenance import cleanup_artifacts


class MaintenanceTests(unittest.TestCase):
    def test_cleanup_artifacts_removes_old_acceptance_runs_but_keeps_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acceptance = root / "acceptance"
            data = root / "data"
            acceptance.mkdir()
            data.mkdir()
            now = 1_000_000.0
            old = acceptance / "random-stress-old"
            newer = acceptance / "random-stress-newer"
            newest = acceptance / "random-stress-newest"
            for index, path in enumerate((old, newer, newest)):
                path.mkdir()
                (path / "summary.json").write_text("{}\n", encoding="utf-8")
                timestamp = now - (30 - index) * 86400
                path.touch()
                (path / "summary.json").touch()
                import os

                os.utime(path, (timestamp, timestamp))
                os.utime(path / "summary.json", (timestamp, timestamp))

            result = cleanup_artifacts(
                acceptance_dir=acceptance,
                data_dir=data,
                retention_days=7,
                keep_latest=2,
                now_epoch=now,
                dry_run=False,
            )

            self.assertFalse(old.exists())
            self.assertTrue(newer.exists())
            self.assertTrue(newest.exists())
            self.assertIn(str(old), result["removed"])

    def test_cleanup_artifacts_removes_old_evaluation_json_but_keeps_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acceptance = root / "acceptance"
            data = root / "data"
            acceptance.mkdir()
            data.mkdir()
            now = 1_000_000.0
            old = data / "evaluation-1.json"
            newer = data / "evaluation-2.json"
            newest = data / "evaluation-3.json"
            for index, path in enumerate((old, newer, newest)):
                path.write_text("{}\n", encoding="utf-8")
                timestamp = now - (30 - index) * 86400
                import os

                os.utime(path, (timestamp, timestamp))

            result = cleanup_artifacts(
                acceptance_dir=acceptance,
                data_dir=data,
                retention_days=7,
                keep_latest=2,
                now_epoch=now,
                dry_run=False,
            )

            self.assertFalse(old.exists())
            self.assertTrue(newer.exists())
            self.assertTrue(newest.exists())
            self.assertIn(str(old), result["removed"])

    def test_keep_latest_applies_separately_to_acceptance_and_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acceptance = root / "acceptance"
            data = root / "data"
            acceptance.mkdir()
            data.mkdir()
            now = 1_000_000.0
            acceptance_old = acceptance / "run-old"
            acceptance_new = acceptance / "run-new"
            evaluation_old = data / "evaluation-1.json"
            evaluation_new = data / "evaluation-2.json"
            acceptance_old.mkdir()
            acceptance_new.mkdir()
            evaluation_old.write_text("{}\n", encoding="utf-8")
            evaluation_new.write_text("{}\n", encoding="utf-8")

            import os

            os.utime(acceptance_old, (1.0, 1.0))
            os.utime(acceptance_new, (2.0, 2.0))
            os.utime(evaluation_old, (1.0, 1.0))
            os.utime(evaluation_new, (2.0, 2.0))

            cleanup_artifacts(
                acceptance_dir=acceptance,
                data_dir=data,
                retention_days=0,
                keep_latest=1,
                now_epoch=now,
                dry_run=False,
            )

            self.assertFalse(acceptance_old.exists())
            self.assertTrue(acceptance_new.exists())
            self.assertFalse(evaluation_old.exists())
            self.assertTrue(evaluation_new.exists())

    def test_cleanup_artifacts_dry_run_reports_without_deleting(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            acceptance = root / "acceptance"
            data = root / "data"
            acceptance.mkdir()
            data.mkdir()
            old = data / "evaluation-1.json"
            old.write_text("{}\n", encoding="utf-8")
            import os

            os.utime(old, (1.0, 1.0))

            result = cleanup_artifacts(
                acceptance_dir=acceptance,
                data_dir=data,
                retention_days=0,
                keep_latest=0,
                now_epoch=1_000_000.0,
                dry_run=True,
            )

            self.assertTrue(old.exists())
            self.assertEqual(result["removed"], [])
            self.assertEqual(result["would_remove"], [str(old)])


if __name__ == "__main__":
    unittest.main()
