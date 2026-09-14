"""Explicit measurement records; no automatic provider/history collection."""
import json
import importlib.util
from unittest import mock
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-measure.py"


class MeasureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.record = Path(self.temp.name) / "record.json"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--record", str(self.record)],
                              capture_output=True, text=True)

    def start(self):
        result = self.run_cli("start", "--task", "Explicit task", "--runtime", "codex", "--model", "model-x", "--version", "1.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.record.read_text())

    def test_start_records_explicit_identity_and_null_metrics(self):
        data = self.start()
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["runtime"], "codex")
        self.assertEqual(data["model"], "model-x")
        self.assertEqual(data["version"], "1.2")
        self.assertTrue(data["started_at"].endswith("Z"))
        self.assertIsNone(data["provider_metrics"]["input_tokens"])
        self.assertIsNone(data["rtk_estimate"]["saved"])
        self.assertIsNone(data["elapsed_seconds"])

    def test_start_does_not_overwrite(self):
        self.start()
        before = self.record.read_bytes()
        result = self.run_cli("start", "--task", "Other", "--runtime", "claude", "--model", "m", "--version", "v")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.record.read_bytes(), before)

    def test_finish_separates_provider_and_estimate(self):
        self.start()
        result = self.run_cli("finish", "--outcome", "accepted", "--corrections", "2", "--input-tokens", "100", "--cached-input-tokens", "20", "--output-tokens", "50", "--metric-source", "provider usage report", "--rtk-saved-estimate", "600", "--rtk-source", "rtk gain estimate")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.record.read_text())
        self.assertEqual(data["outcome"], "accepted")
        self.assertEqual(data["corrections"], 2)
        self.assertGreaterEqual(data["elapsed_seconds"], 0)
        self.assertTrue(data["finished_at"].endswith("Z"))
        self.assertEqual(data["provider_metrics"]["cached_input_tokens"], 20)
        self.assertEqual(data["rtk_estimate"]["saved"], 600)
        self.assertNotIn("cost", data)

    def test_finish_without_counters_keeps_unknown_null(self):
        self.start()
        result = self.run_cli("finish", "--outcome", "partial")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.record.read_text())
        self.assertIsNone(data["corrections"])
        self.assertTrue(all(value is None for value in data["provider_metrics"].values()))

    def test_finish_rejects_invalid_or_unsourced_metrics_without_write(self):
        self.start()
        before = self.record.read_bytes()
        invalid = [("--input-tokens", "-1", "--metric-source", "api"),
                   ("--input-tokens", "10"), ("--rtk-saved-estimate", "10"),
                   ("--input-tokens", "10", "--cached-input-tokens", "11", "--metric-source", "api"),
                   ("--corrections", "-1"), ("--output-tokens", "5", "--metric-source", " ")]
        for args in invalid:
            with self.subTest(args=args):
                result = self.run_cli("finish", "--outcome", "accepted", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.record.read_bytes(), before)

    def test_cannot_finish_twice(self):
        self.start()
        self.assertEqual(self.run_cli("finish", "--outcome", "rejected").returncode, 0)
        before = self.record.read_bytes()
        self.assertNotEqual(self.run_cli("finish", "--outcome", "accepted").returncode, 0)
        self.assertEqual(self.record.read_bytes(), before)

    def test_concurrent_finish_preserves_first_completion(self):
        self.start()
        command = [sys.executable, str(SCRIPT), "finish", "--record", str(self.record), "--outcome"]
        first = subprocess.Popen([*command, "accepted"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen([*command, "rejected"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first.communicate(timeout=10)
        second.communicate(timeout=10)
        self.assertEqual(sorted((first.returncode, second.returncode)), [0, 1])
        data = json.loads(self.record.read_text())
        self.assertEqual(data["outcome"], "accepted" if first.returncode == 0 else "rejected")
        self.assertFalse(self.record.with_suffix(".json.lock").exists())

    def test_atomic_write_failure_keeps_original_and_cleans_temporary(self):
        self.start()
        before = self.record.read_bytes()
        spec = importlib.util.spec_from_file_location("aos_measure_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                module.atomic_write(self.record, {"new": "value"})
        self.assertEqual(self.record.read_bytes(), before)
        self.assertEqual(list(self.record.parent.iterdir()), [self.record])

    def test_invalid_timestamp_does_not_disclose_record_content(self):
        self.start()
        data = json.loads(self.record.read_text())
        data["started_at"] = "PRIVATE-CONTENT"
        self.record.write_text(json.dumps(data))
        result = self.run_cli("finish", "--outcome", "accepted")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PRIVATE-CONTENT", result.stdout + result.stderr)

    def test_missing_or_invalid_record_fails_cleanly(self):
        result = self.run_cli("finish", "--outcome", "accepted")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.record.write_text("{}")
        before = self.record.read_bytes()
        result = self.run_cli("finish", "--outcome", "accepted")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.record.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
