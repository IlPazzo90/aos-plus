"""aos-status: the routing decision and the open worker's tokens, in the status bar.

The bar is cosmetic, so the two properties that matter are that it never fails a
delegation (no session id, unwritable directory, malformed record) and that the
number it shows is an estimate from the catalog, peak multiplier included, never
presented as billing.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-status.py"

spec = importlib.util.spec_from_file_location("status", SCRIPT)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

DEEPSEEK = "vercel/deepseek/deepseek-v4-pro-0813"
OFF_PEAK = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)   # 1200 min, outside 0-840
PEAK = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)        # 360 min, inside 0-840


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"AOS_STATUS_DIR": self.directory.name,
                                               "CLAUDE_CODE_SESSION_ID": "sess-1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.directory.cleanup)

    def record(self, session="sess-1"):
        return json.loads((Path(self.directory.name) / (session + ".json")).read_text())

    def test_set_publishes_tier_risk_chain_without_cost_before_any_run(self):
        status.main(["set", "--tier", "T2", "--risk", "LOW", "--executor", "open",
                     "--model", DEEPSEEK, "--planner", "codex", "--reviewer", "claude"])
        segment = self.record()["segment"]
        self.assertIn("T2/LOW", segment)
        self.assertIn("codex → deepseek-v4-pro → claude", segment)
        self.assertNotIn("$", segment)

    def test_premium_executor_shows_subscription_not_money(self):
        status.main(["set", "--tier", "T3", "--risk", "HIGH", "--executor", "premium",
                     "--model", "claude-fable-5-1"])
        self.assertIn("sub", self.record()["segment"])
        self.assertNotIn("$", self.record()["segment"])

    def test_set_stores_the_routed_executor(self):
        status.main(["set", "--tier", "T2", "--risk", "LOW", "--executor", "open",
                     "--model", DEEPSEEK])
        self.assertEqual(self.record()["routed_executor"], "open")

    def test_set_warns_when_the_open_route_was_published_as_main(self):
        status.main(["set", "--tier", "T2", "--risk", "LOW", "--executor", "main",
                     "--model", "claude-opus-5"])
        self.assertIn("⚠ open", self.record()["segment"])

    def test_set_does_not_warn_when_the_open_route_was_followed(self):
        status.main(["set", "--tier", "T2", "--risk", "LOW", "--executor", "open",
                     "--model", DEEPSEEK])
        self.assertNotIn("⚠", self.record()["segment"])

    def test_set_does_not_warn_on_premium_route_published_as_main(self):
        status.main(["set", "--tier", "T2", "--risk", "HIGH", "--executor", "main",
                     "--model", "claude-opus-5"])
        self.assertNotIn("⚠", self.record()["segment"])

    def test_routed_executor_matches_the_router_for_t2_low(self):
        self.assertEqual(status.routed_executor("T2", "LOW"), "open")

    def test_routed_executor_returns_none_for_unknown_classification(self):
        self.assertIsNone(status.routed_executor("T9", "LOW"))
        self.assertIsNone(status.routed_executor("T2", "BANANA"))

    def test_set_survives_a_router_that_cannot_answer(self):
        # Point the script at a directory without bin/aos-router.py: the router
        # cannot load, so the bar must still render what the caller published.
        with mock.patch.object(status, "ROOT", Path(self.directory.name)):
            status.main(["set", "--tier", "T2", "--risk", "LOW", "--executor", "open",
                         "--model", DEEPSEEK])
        state = self.record()
        self.assertIsNone(state["routed_executor"])
        self.assertTrue(state["segment"])

    def test_usage_accumulates_tokens_across_runs(self):
        status.main(["set", "--tier", "T1", "--risk", "LOW", "--executor", "open", "--model", DEEPSEEK])
        status.main(["usage", "--model", DEEPSEEK, "--input", "40000", "--output", "10000"])
        status.main(["usage", "--model", DEEPSEEK, "--input", "5000", "--output", "2000"])
        self.assertEqual(self.record()["tokens"], {"input": 45000, "output": 12000, "cache": 0})
        self.assertIn("45k/12k", self.record()["segment"])

    def test_reclassification_keeps_the_tokens_already_spent(self):
        status.main(["set", "--tier", "T1", "--risk", "LOW", "--executor", "open", "--model", DEEPSEEK])
        status.main(["usage", "--model", DEEPSEEK, "--input", "40000", "--output", "10000"])
        status.main(["set", "--tier", "T2", "--risk", "MEDIUM", "--executor", "open", "--model", DEEPSEEK])
        state = self.record()
        self.assertEqual(state["tier"], "T2")
        self.assertEqual(state["tokens"]["input"], 40000)

    def test_cost_is_the_catalog_price_off_peak(self):
        tokens = {"input": 1_000_000, "output": 1_000_000, "cache": 0}
        cost, multiplier = status.estimate_cost(DEEPSEEK, tokens, when=OFF_PEAK)
        self.assertEqual(multiplier, 1.0)
        self.assertAlmostEqual(cost, 0.66 + 1.98, places=6)

    def test_peak_window_doubles_the_estimate(self):
        tokens = {"input": 1_000_000, "output": 0, "cache": 0}
        cost, multiplier = status.estimate_cost(DEEPSEEK, tokens, when=PEAK)
        self.assertEqual(multiplier, 2.0)
        self.assertAlmostEqual(cost, 1.32, places=6)

    def test_cached_input_is_priced_as_cache_read_not_as_input(self):
        cached = status.estimate_cost(DEEPSEEK, {"input": 0, "output": 0, "cache": 1_000_000}, when=OFF_PEAK)[0]
        fresh = status.estimate_cost(DEEPSEEK, {"input": 1_000_000, "output": 0, "cache": 0}, when=OFF_PEAK)[0]
        self.assertLess(cached, fresh)
        self.assertAlmostEqual(cached, 0.066, places=6)

    def test_peak_window_crossing_midnight(self):
        entry = {"pricing_details": {"peak_pricing": {"multiplier": 3,
                                                      "windows": [{"start_minute_utc": 1380,
                                                                   "end_minute_utc": 120}]}}}
        inside = datetime(2026, 9, 22, 23, 30, tzinfo=timezone.utc)
        outside = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(status.peak_multiplier(entry, inside), 3.0)
        self.assertEqual(status.peak_multiplier(entry, outside), 1.0)

    def test_unknown_model_reports_tokens_but_no_price(self):
        status.main(["set", "--tier", "T1", "--risk", "LOW", "--executor", "open", "--model", "acme/unknown"])
        status.main(["usage", "--model", "acme/unknown", "--input", "1000", "--output", "500"])
        state = self.record()
        self.assertIsNone(state["cost_usd"])
        self.assertIn("1k/500", state["segment"])
        self.assertNotIn("$", state["segment"])

    def test_show_is_empty_once_the_record_expires(self):
        status.main(["--ttl", "0", "set", "--tier", "T1", "--risk", "LOW", "--executor", "open",
                     "--model", DEEPSEEK])
        self.assertEqual(status.cmd_show(mock.Mock(session=None)), "")

    def test_show_is_empty_without_a_record(self):
        self.assertEqual(status.cmd_show(mock.Mock(session="never-seen")), "")

    def test_no_session_id_writes_nothing(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            status.main(["set", "--tier", "T2", "--risk", "LOW", "--executor", "open", "--model", DEEPSEEK])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_corrupt_record_is_replaced_not_fatal(self):
        (Path(self.directory.name) / "sess-1.json").write_text("{not json")
        status.main(["set", "--tier", "T1", "--risk", "LOW", "--executor", "open", "--model", DEEPSEEK])
        self.assertEqual(self.record()["tier"], "T1")

    def test_from_result_reads_the_open_executor_payload(self):
        result = {"model_ref": DEEPSEEK,
                  "usage": {"input_tokens": 12000, "output_tokens": 3000, "cache_read_tokens": 900}}
        path = Path(self.directory.name) / "result.json"
        path.write_text(json.dumps(result))
        status.main(["usage", "--from-result", str(path)])
        self.assertEqual(self.record()["tokens"], {"input": 12000, "output": 3000, "cache": 900})

    def test_money_uses_the_italian_comma(self):
        self.assertEqual(status.money(0.1125), "0,11 $")
        self.assertEqual(status.money(0.0043), "0,004 $")

    def test_short_model_strips_only_a_four_digit_release_suffix(self):
        self.assertEqual(status.short_model(DEEPSEEK), "deepseek-v4-pro")
        self.assertEqual(status.short_model("vercel/alibaba/qwen3-coder-next"), "qwen3-coder-next")
        self.assertEqual(status.short_model(None), "")

    def test_clear_removes_the_record(self):
        status.main(["set", "--tier", "T1", "--risk", "LOW", "--executor", "open", "--model", DEEPSEEK])
        with mock.patch.object(status.fcntl, "flock", wraps=status.fcntl.flock) as flock:
            status.main(["clear"])
        self.assertFalse((Path(self.directory.name) / "sess-1.json").exists())
        self.assertTrue(flock.called)
        self.assertTrue((Path(self.directory.name) / "sess-1.lock").exists())


    def test_concurrent_usage_loses_no_tokens(self):
        import subprocess
        import sys
        env = dict(os.environ)
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), "usage", "--input", "1000"],
                                  env=env, stdout=subprocess.DEVNULL) for _ in range(12)]
        for proc in procs:
            proc.wait()
        self.assertEqual(self.record()["tokens"]["input"], 12000)

    def test_session_id_that_leaves_the_directory_is_ignored(self):
        for bad in ("../escaped", "a/b", ".hidden", "x..y"):
            self.assertEqual(status.session_id(bad), "")
        status.main(["--session", "../escaped", "set", "--tier", "T1", "--risk", "LOW",
                     "--executor", "main"])
        self.assertFalse((Path(self.directory.name).parent / "escaped.json").exists())

    def test_negative_token_count_is_rejected(self):
        with self.assertRaises(SystemExit):
            status.main(["usage", "--input", "-5"])

    def test_non_object_json_is_ignored(self):
        (Path(self.directory.name) / "sess-1.json").write_text("[1, 2]")
        self.assertEqual(status.load("sess-1"), {})
        payload = Path(self.directory.name) / "result.json"
        payload.write_text("[1]")
        self.assertEqual(status.main(["usage", "--from-result", str(payload)]), 0)


class ExecutorHookTests(unittest.TestCase):
    """The open executor publishes usage without letting the bar break a delegation."""

    def test_publish_status_swallows_a_broken_status_directory(self):
        executor_spec = importlib.util.spec_from_file_location(
            "open_executor_status", Path(__file__).resolve().parents[1] / "bin/aos-open-executor.py")
        executor = importlib.util.module_from_spec(executor_spec)
        executor_spec.loader.exec_module(executor)
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "file"
            blocked.write_text("not a directory")
            with mock.patch.dict(os.environ, {"AOS_STATUS_DIR": str(blocked),
                                              "CLAUDE_CODE_SESSION_ID": "sess-2"}):
                executor.publish_status(DEEPSEEK, {"usage": {"input_tokens": 10, "output_tokens": 5}})

    def test_publish_status_is_a_no_op_outside_a_session(self):
        executor_spec = importlib.util.spec_from_file_location(
            "open_executor_status", Path(__file__).resolve().parents[1] / "bin/aos-open-executor.py")
        executor = importlib.util.module_from_spec(executor_spec)
        executor_spec.loader.exec_module(executor)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"AOS_STATUS_DIR": directory,
                                              "CLAUDE_CODE_SESSION_ID": ""}):
                executor.publish_status(DEEPSEEK, {"usage": {"input_tokens": 10, "output_tokens": 5}})
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
