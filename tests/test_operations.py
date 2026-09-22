"""aos-operations: operational helpers for later pipeline integration.

Covers prepare_context (safe compaction/handoff), budget_check (fail-closed
cost enforcement), aggregate (workload metrics) and summarize_runtime (read-only
runtime summary).
"""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-operations.py"

spec = importlib.util.spec_from_file_location("operations", SCRIPT)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def write_config(directory, model, target=10, soft=20, hard=30):
    data = {"schema": 1, "context_policy": {"models": {
        model: {"target_context": target, "soft_limit": soft, "hard_limit": hard}}}}
    path = Path(directory) / "config.json"
    path.write_text(json.dumps(data))
    return str(path)


class PrepareContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.model = "acme/test-model"
        self.config = write_config(self.temp.name, self.model)

    # GREEN: small context -> no compaction, prompt preserved, handoff built.
    def test_green_no_compaction(self):
        items = [{"id": "task", "role": "task", "content": "small work"},
                 {"id": "acc", "role": "acceptance", "content": "tests pass"}]
        state = {"task": "small work", "stage": "execute", "role": "executor"}
        result = ops.prepare_context(self.model, items, state, config_path=self.config)
        self.assertEqual(result["context"]["context_state_before"], "GREEN")
        self.assertFalse(result["context"]["compaction_triggered"])
        self.assertFalse(result["context"]["handoff_created"] is False)
        self.assertIn("small work", result["prompt"])
        self.assertEqual(result["handoff"]["task"], "small work")

    # ORANGE compacts structurally: droppable dropped, protected kept.
    def test_orange_compacts_and_preserves_protected(self):
        items = [
            {"id": "task", "role": "task", "content": "t" * 22},
            {"id": "acc", "role": "acceptance", "content": "a" * 22},
            {"id": "sec", "role": "security_finding", "content": "s" * 22},
            {"id": "log", "role": "resolved_log", "content": "l" * 40},
        ]
        state = {"task": "do", "stage": "verify", "security_constraints": ["no secrets"]}
        result = ops.prepare_context(self.model, items, state, config_path=self.config)
        self.assertTrue(result["context"]["compaction_triggered"])
        self.assertIn("t" * 22, result["prompt"])
        self.assertIn("s" * 22, result["prompt"])
        self.assertNotIn("l" * 40, result["prompt"])
        self.assertEqual(result["handoff"]["security_constraints"], ["no secrets"])

    # Still RED after compaction -> fail closed, never truncate protected content.
    def test_red_after_compaction_fails_closed(self):
        items = [{"id": "task", "role": "task", "content": "t" * 160}]
        with self.assertRaises(ValueError):
            ops.prepare_context(self.model, items, {"task": "x"}, config_path=self.config)

    # No context-policy enforcement (missing config) -> nothing compacted.
    def test_missing_config_no_compaction(self):
        items = [{"id": "task", "role": "task", "content": "z" * 200}]
        result = ops.prepare_context(self.model, items, None,
                                     config_path=str(Path(self.temp.name) / "nope.json"))
        self.assertFalse(result["context"]["compaction_triggered"])

    # Handoff keeps run_dir/directory for host resume but never the full repo or
    # any credential; repository contents (history) and secrets stay out.
    def test_handoff_keeps_host_resume_but_excludes_repo_and_credentials(self):
        state = {"task": "t", "plan": {"objective": "o"}, "checks": [{"id": "diff"}],
                 "directory": "/repo", "run_dir": "/tmp/x", "history": ["h"],
                 "stage": "verify", "model": self.model,
                 "api_key": "secret", "token": "tok", "secret": "s"}
        result = ops.prepare_context(self.model, [{"id": "task", "role": "task", "content": "x"}],
                                      state, config_path=self.config)
        handoff = result["handoff"]
        self.assertIn("task", handoff)
        self.assertIn("plan", handoff)
        self.assertIn("checks", handoff)
        self.assertIn("directory", handoff)
        self.assertIn("run_dir", handoff)
        self.assertNotIn("history", handoff)
        self.assertNotIn("api_key", handoff)
        self.assertNotIn("token", handoff)
        self.assertNotIn("secret", handoff)

    # Handoff must retain every safety/state field the next executor needs.
    def test_handoff_retains_full_safety_state(self):
        state = {
            "task": "do", "acceptance_criteria": ["pass"], "do_not_modify": ["x"],
            "unresolved_failures": [{"id": "u"}], "security_findings": ["find"],
            "constraints": ["c1"], "retries": 3, "reviewer_findings": ["r1"],
            "escalation": "pending", "premium_execution_reason": "big",
            "premium_execution_used": True, "current_execution_premium": True,
            "primary": "m-a", "fallback": "m-b", "mid": "m-c", "attempts": 2,
            "open_retry_count": 1, "review_rounds": 2, "role_models": {"e": "m"},
            "verification": {"v": 1}, "fix_findings": ["f1"], "main_host": "h",
            "executor_runtime": "opencode", "classification": "CHEAP",
            "escalation_events": [{
                "previous_failure": "unit failed", "previous_model": "cheap-a",
                "selected_next_model": "cheap-b", "target": "open_executor",
                "transition": True, "expected_capability_gain": "not guaranteed",
                "estimated_cost_increase": None,
            }],
        }
        result = ops.prepare_context(self.model, [{"id": "task", "role": "task", "content": "x"}],
                                     state, config_path=self.config)
        handoff = result["handoff"]
        for field in state:
            self.assertEqual(handoff.get(field), state[field], field)

    # Telemetry carries the complete eval_after picture plus prompt-only scope.
    def test_telemetry_includes_full_eval_after(self):
        result = ops.prepare_context(self.model, [{"id": "task", "role": "task", "content": "small work"}],
                                     {"task": "x"}, config_path=self.config)
        telemetry = result["context"]
        self.assertEqual(telemetry["target_context"], 10)
        self.assertEqual(telemetry["soft_limit"], 20)
        self.assertEqual(telemetry["hard_limit"], 30)
        self.assertIsNone(telemetry["technical_context_limit"])
        self.assertEqual(telemetry["context_tokens"], 2)
        self.assertEqual(telemetry["context_tokens_source"], "estimated")
        self.assertIsInstance(telemetry["context_utilization_ratio"], dict)
        self.assertIn("target_context", telemetry["context_utilization_ratio"])
        self.assertEqual(telemetry["model_context_policy_source"], "model")
        self.assertEqual(telemetry["context_state"], "GREEN")
        self.assertEqual(telemetry["measurement_scope"], "prompt_only")

    # Conflicting duplicate ids silently discard protected data -> reject.
    def test_conflicting_duplicate_id_rejected(self):
        items = [{"id": "task", "role": "task", "content": "original"},
                 {"id": "task", "role": "task", "content": "different"}]
        with self.assertRaises(ValueError):
            ops.prepare_context(self.model, items, {"task": "x"}, config_path=self.config)

    # Identical duplicate ids are a harmless dedup, not a conflict.
    def test_identical_duplicate_id_allowed(self):
        items = [{"id": "task", "role": "task", "content": "same"},
                 {"id": "task", "role": "task", "content": "same"}]
        result = ops.prepare_context(self.model, items, {"task": "x"}, config_path=self.config)
        self.assertIn("same", result["prompt"])


class BudgetCheckTests(unittest.TestCase):
    def now(self):
        return datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    def ts(self, m, d, h=0):
        return f"2026-{m:02d}-{d:02d}T{h:02d}:00:00Z"

    def policy(self, **kw):
        base = {"max_task_cost": 10, "daily_budget": 5, "monthly_budget": 50,
                "premium_budget": 20}
        base.update(kw)
        return base

    # Under every cap -> allowed with per-cap remaining.
    def test_under_budget_allowed(self):
        records = [{"cost": 1.0, "timestamp": self.ts(9, 22, 8)},
                   {"cost": 2.0, "timestamp": self.ts(9, 21)},
                   {"cost": 5.0, "timestamp": self.ts(8, 15)}]
        report = ops.budget_check(self.policy(), records, 1.0, now=self.now())
        self.assertTrue(report["allowed"])
        self.assertEqual(report["budgets"]["daily_budget"]["total"], 2.0)
        self.assertEqual(report["budgets"]["monthly_budget"]["total"], 4.0)
        self.assertEqual(report["budgets"]["max_task_cost"]["total"], 1.0)

    # Monthly totals include yesterday, daily totals do not.
    def test_scopes_budgets_by_window(self):
        records = [{"cost": 3.0, "timestamp": self.ts(9, 21)},
                   {"cost": 1.0, "timestamp": self.ts(9, 22, 8)}]
        report = ops.budget_check(self.policy(), records, 0.0, now=self.now())
        self.assertEqual(report["budgets"]["daily_budget"]["measured"], 1.0)
        self.assertEqual(report["budgets"]["monthly_budget"]["measured"], 4.0)

    # Over monthly cap -> fail closed.
    def test_over_monthly_fails_closed(self):
        records = [{"cost": 10.0, "timestamp": self.ts(9, 21)}]
        with self.assertRaises(ValueError):
            ops.budget_check(self.policy(), records, 45.0, now=self.now())

    # Missing measured cost with a configured cap -> fail closed.
    def test_missing_cost_fails_closed(self):
        records = [{"cost": None, "timestamp": self.ts(9, 22, 8)}]
        with self.assertRaises(ValueError):
            ops.budget_check(self.policy(), records, 1.0, now=self.now())

    # Premium budget enforces separately; no bypass.
    def test_premium_budget_enforced_no_bypass(self):
        records = [{"cost": 15.0, "timestamp": self.ts(9, 20), "premium": True}]
        with self.assertRaises(ValueError):
            ops.budget_check(self.policy(), records, 10.0, premium=True, now=self.now())

    def test_premium_cost_class_record_counts_against_premium_budget(self):
        records = [{"cost": 15.0, "timestamp": self.ts(9, 20), "cost_class": "PREMIUM"}]
        report = ops.budget_check(self.policy(), records, 4.0, premium=True, now=self.now())
        self.assertEqual(report["budgets"]["premium_budget"]["measured"], 15.0)

    # Premium with missing reserved estimate cannot bypass a configured premium cap.
    def test_premium_missing_estimate_fails_closed(self):
        records = [{"cost": 15.0, "timestamp": self.ts(9, 20), "premium": True}]
        with self.assertRaises(ValueError):
            ops.budget_check(self.policy(), records, None, premium=True, now=self.now())

    # Zero costs in and out are allowed.
    def test_zero_costs_allowed(self):
        records = [{"cost": 0.0, "timestamp": self.ts(9, 22, 8)},
                   {"cost": 0.0, "timestamp": self.ts(9, 21)}]
        report = ops.budget_check(self.policy(), records, 0.0, now=self.now())
        self.assertTrue(report["allowed"])
        self.assertEqual(report["budgets"]["monthly_budget"]["total"], 0.0)

    # An unconfigured cap is not enforced even when costs are unknown.
    def test_unconfigured_cap_not_enforced(self):
        policy = self.policy()
        del policy["premium_budget"]
        records = [{"cost": None, "timestamp": self.ts(9, 22, 8)}]
        # daily_budget still configured -> missing daily cost must fail.
        with self.assertRaises(ValueError):
            ops.budget_check(policy, records, 0.0, now=self.now())

    # max_task_cost scopes by task_id.
    def test_max_task_cost_scopes_by_task_id(self):
        records = [{"cost": 3.0, "timestamp": self.ts(9, 21), "task_id": "t1"},
                   {"cost": 100.0, "timestamp": self.ts(8, 15), "task_id": "t2"}]
        report = ops.budget_check(self.policy(), records, 1.0, task_id="t1", now=self.now())
        self.assertEqual(report["budgets"]["max_task_cost"]["total"], 4.0)
        self.assertTrue(report["allowed"])

    # The learning ledger's created_at is accepted when timestamp is absent.
    def test_record_uses_created_at_fallback(self):
        records = [{"cost": 3.0, "created_at": self.ts(9, 22, 8)}]
        report = ops.budget_check(self.policy(), records, 0.0, now=self.now())
        self.assertEqual(report["budgets"]["daily_budget"]["measured"], 3.0)

    # NaN / Infinity must never slip through as a cost, cap or estimate.
    def test_nan_estimate_rejected(self):
        with self.assertRaises(ValueError):
            ops.budget_check(self.policy(), [], float("nan"), now=self.now())

    def test_infinity_cost_rejected(self):
        records = [{"cost": float("inf"), "timestamp": self.ts(9, 22, 8)}]
        with self.assertRaises(ValueError):
            ops.budget_check(self.policy(), records, 0.0, now=self.now())

    def test_nan_cap_rejected(self):
        policy = self.policy(max_task_cost=float("nan"))
        with self.assertRaises(ValueError):
            ops.budget_check(policy, [], 0.0, now=self.now())


class AggregateTests(unittest.TestCase):
    def records(self):
        return [
            {"cost_class": "CHEAP", "events": 50, "tokens": 100, "cost": 1.0, "success": True},
            {"cost_class": "CHEAP", "events": 30, "tokens": 50, "cost": 2.0, "success": False},
            {"cost_class": "MID", "events": 20, "tokens": 80, "cost": 4.0, "success": True},
            {"cost_class": "PREMIUM", "events": 10, "tokens": 20, "cost": 10.0, "success": True},
        ]

    def test_workload_by_events_and_tokens(self):
        agg = ops.aggregate(self.records())
        self.assertAlmostEqual(agg["by_events"]["CHEAP"], 80 / 110, places=4)
        self.assertAlmostEqual(agg["by_events"]["PREMIUM"], 10 / 110, places=4)
        self.assertAlmostEqual(agg["by_tokens"]["CHEAP"], 150 / 250, places=4)
        self.assertAlmostEqual(agg["by_tokens"]["MID"], 80 / 250, places=4)

    def test_premium_execution_ratio(self):
        agg = ops.aggregate(self.records())
        self.assertAlmostEqual(agg["premium_execution_ratio"], 10 / 110, places=4)

    def test_average_cost_by_tier(self):
        agg = ops.aggregate(self.records())
        self.assertAlmostEqual(agg["average_cost_by_tier"]["CHEAP"], 3 / 80, places=4)
        self.assertAlmostEqual(agg["average_cost_by_tier"]["PREMIUM"], 1.0, places=4)

    def test_cost_per_successful_task(self):
        agg = ops.aggregate(self.records())
        # Failed-attempt cost is part of the spend incurred per success.
        self.assertAlmostEqual(agg["cost_per_successful_task"], 17 / 3, places=4)

    def test_unknown_cost_class_reported(self):
        records = self.records() + [{"cost_class": "UNKNOWN", "events": 5, "tokens": 10, "cost": 0.5}]
        agg = ops.aggregate(records)
        self.assertEqual(agg["unknown"]["records"], 1)
        self.assertEqual(agg["unknown"]["events"], 5)
        self.assertEqual(agg["unknown"]["tokens"], 10)
        self.assertEqual(agg["unknown"]["cost"], 0.5)
        # Known-tier ratios still sum to 1; the unknown bucket is explicit.
        self.assertAlmostEqual(sum(v for v in agg["by_events"].values()), 1.0)

    def test_missing_cost_stays_null(self):
        records = self.records()
        records[0]["cost"] = None
        agg = ops.aggregate(records)
        self.assertIsNone(agg["average_cost_by_tier"]["CHEAP"])

    def test_ledger_input_output_tokens_supply_missing_role_event_total(self):
        records = [
            {"cost_class": "CHEAP", "events": 1, "input_tokens": 7, "output_tokens": 3},
            {"cost_class": "MID", "events": 1, "tokens": 5, "input_tokens": 90, "output_tokens": 90},
            {"cost_class": "PREMIUM", "events": 1, "input_tokens": 2, "output_tokens": 4},
        ]
        agg = ops.aggregate(records)
        self.assertEqual(agg["totals"]["tokens"], 21)
        self.assertAlmostEqual(agg["by_tokens"]["CHEAP"], 10 / 21, places=4)
        self.assertAlmostEqual(agg["by_tokens"]["MID"], 5 / 21, places=4)

    def test_missing_ledger_token_component_stays_unknown(self):
        agg = ops.aggregate([{"cost_class": "CHEAP", "events": 1,
                              "input_tokens": 7, "output_tokens": None}])
        self.assertIsNone(agg["totals"]["tokens"])
        self.assertIsNone(agg["by_tokens"]["CHEAP"])
        self.assertIsNone(agg["cost_per_successful_task"])

    def test_empty_records_null(self):
        agg = ops.aggregate([])
        self.assertIsNone(agg["premium_execution_ratio"])
        self.assertIsNone(agg["cost_per_successful_task"])


class SummarizeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "schema": 1,
            "open": {"primary": "vercel/deepseek/deepseek-v4-pro-0813",
                     "fallback": "vercel/alibaba/qwen3-coder-next",
                     "source": "benchmark 2026-09-20 winner"},
            "model_catalog": {
                "vercel/deepseek/deepseek-v4-pro-0813": {
                    "cost_class": "CHEAP",
                    "benchmark": {"available": True, "status": "winner"},
                },
                "anthropic/fable": {"cost_class": "PREMIUM"},
            },
            "executors": {
                "default_runtime": "opencode",
                "runtime_status": {
                    "codex-cli": {"open_execution": False,
                                  "reason": "Native restrictive rule loading is unverified"}
                },
            },
            "context_policy": {"defaults": {"target_context": 80000}},
        }

    def test_reports_disabled_reason(self):
        result = ops.summarize_runtime(self.config, ["opencode"])
        self.assertFalse(result["runtimes"]["codex-cli"]["open_execution"])
        self.assertEqual(result["runtimes"]["codex-cli"]["disabled_reason"],
                         "Native restrictive rule loading is unverified")
        self.assertEqual(result["runtimes"]["opencode"]["available"], True)

    def test_reports_winner_and_classes(self):
        result = ops.summarize_runtime(self.config, ["opencode"])
        self.assertEqual(result["winner"]["model"],
                         "vercel/deepseek/deepseek-v4-pro-0813")
        self.assertEqual(result["winner"]["cost_class"], "CHEAP")
        self.assertIn("vercel/deepseek/deepseek-v4-pro-0813", result["classes"]["CHEAP"])
        self.assertIn("anthropic/fable", result["classes"]["PREMIUM"])

    def test_context_and_learning_status(self):
        result = ops.summarize_runtime(self.config, ["opencode"])
        self.assertTrue(result["context"]["configured"])
        self.assertTrue(result["learning"]["benchmark_winner"])

    # No credential keys leak into the summary.
    def test_no_credential_reads(self):
        result = ops.summarize_runtime(self.config, ["opencode"])
        text = json.dumps(result)
        self.assertNotIn("key", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("token", text)


if __name__ == "__main__":
    unittest.main()
