"""aos-context: budget states, model policy resolution, structural compaction and handoff."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-context.py"
CONFIG = Path(__file__).resolve().parents[1] / "config" / "open-models.json"

spec = importlib.util.spec_from_file_location("context", SCRIPT)
ctx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctx)


def policy(**kw):
    return ctx.ContextPolicy(target_context=100, soft_limit=200, hard_limit=300, **kw)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    # 1. Under target -> GREEN, no action.
    def test_context_under_target_is_green_no_action(self):
        result = ctx.evaluate("vercel/deepseek/deepseek-v4-pro-0813", tokens=50000, config_path=CONFIG)
        self.assertEqual(result["context_state"], "GREEN")
        self.assertEqual(result["action"], "none")
        self.assertFalse(result["action_mandatory"])

    # 2. Over target, under soft -> YELLOW.
    def test_context_over_target_under_soft_is_yellow(self):
        result = ctx.evaluate("vercel/deepseek/deepseek-v4-pro-0813", tokens=120000, config_path=CONFIG)
        self.assertEqual(result["context_state"], "YELLOW")
        self.assertEqual(result["action"], "note")
        self.assertFalse(result["action_mandatory"])

    # 3. Over soft -> compact proposed.
    def test_context_over_soft_is_orange(self):
        result = ctx.evaluate("vercel/deepseek/deepseek-v4-pro-0813", tokens=200000, config_path=CONFIG)
        self.assertEqual(result["context_state"], "ORANGE")
        self.assertEqual(result["action"], "compact")
        self.assertFalse(result["action_mandatory"])

    # 4. Over hard -> compaction/new session mandatory.
    def test_context_over_hard_is_red_and_mandatory(self):
        result = ctx.evaluate("vercel/deepseek/deepseek-v4-pro-0813", tokens=300000, config_path=CONFIG)
        self.assertEqual(result["context_state"], "RED")
        self.assertEqual(result["action"], "compact")
        self.assertTrue(result["action_mandatory"])
        self.assertEqual(result["action_alternative"], "new_session")

    # 5. DeepSeek applies the DeepSeek policy.
    def test_deepseek_applies_deepseek_policy(self):
        result = ctx.evaluate("vercel/deepseek/deepseek-v4-pro-0813", tokens=1, config_path=CONFIG)
        self.assertEqual(result["target_context"], 100000)
        self.assertEqual(result["soft_limit"], 160000)
        self.assertEqual(result["hard_limit"], 250000)
        self.assertEqual(result["model_context_policy_source"], "model")

    # 6. Qwen applies the Qwen policy.
    def test_qwen_applies_qwen_policy(self):
        result = ctx.evaluate("vercel/alibaba/qwen3-coder-next", tokens=1, config_path=CONFIG)
        self.assertEqual(result["target_context"], 60000)
        self.assertEqual(result["soft_limit"], 90000)
        self.assertEqual(result["hard_limit"], 130000)

    # 7. Unknown model falls back to defaults.
    def test_unknown_model_uses_default_policy(self):
        result = ctx.evaluate("acme/unknown-model", tokens=85000, config_path=CONFIG)
        self.assertEqual(result["model_context_policy_source"], "default")
        self.assertEqual(result["target_context"], 80000)
        self.assertEqual(result["context_state"], "YELLOW")  # 85000 > 80000 target, < 120000 soft

    # 7b. A model class without an exact entry resolves by class.
    def test_model_class_fallback_for_unlisted_variant(self):
        result = ctx.evaluate("vercel/deepseek/deepseek-v4-pro-9999", tokens=1, config_path=CONFIG)
        self.assertEqual(result["model_context_policy_source"], "model_class")
        self.assertEqual(result["target_context"], 100000)

    # 7c. The premium families have their own class policy, not the defaults.
    def test_claude_codex_and_gpt_have_class_policies(self):
        claude = ctx.evaluate("claude/claude-fable-5-1", tokens=1, config_path=CONFIG)
        self.assertEqual(claude["model_context_policy_source"], "model_class")
        self.assertEqual(claude["target_context"], 120000)
        self.assertEqual(claude["technical_context_limit"], 1000000)
        codex = ctx.evaluate("codex/gpt-5.6-sol", tokens=1, config_path=CONFIG)
        self.assertEqual(codex["model_context_policy_source"], "model_class")
        self.assertEqual(codex["target_context"], 100000)
        gpt = ctx.evaluate("openai/gpt-5", tokens=1, config_path=CONFIG)
        self.assertEqual(gpt["model_context_policy_source"], "model_class")
        self.assertEqual(gpt["hard_limit"], 250000)

    # 8. Compaction keeps acceptance criteria and unresolved findings.
    def test_compaction_keeps_acceptance_and_findings(self):
        items = [
            {"id": "task", "role": "task", "content": "implement X"},
            {"id": "acc", "role": "acceptance", "content": "X must pass tests"},
            {"id": "f1", "role": "review_finding", "content": "BLOCKER: unvalidated input"},
        ]
        result = ctx.compact_context(items)
        self.assertEqual(len(result["kept"]), 3)
        self.assertIn("review_finding", [i["role"] for i in result["kept"]])

    # 9. Compaction removes redundant/resolved logs.
    def test_compaction_removes_redundant_logs(self):
        items = [
            {"id": "keep", "role": "task", "content": "implement X"},
            {"id": "log1", "role": "resolved_log", "content": "x" * 400},
            {"id": "log2", "role": "verbose_output", "content": "y" * 400},
        ]
        result = ctx.compact_context(items)
        self.assertEqual([i["id"] for i in result["kept"]], ["keep"])
        self.assertEqual(len(result["removed"]), 2)
        self.assertLess(result["tokens_after_compaction"], result["tokens_before_compaction"])

    # 10. An open security finding is never lost.
    def test_open_security_finding_survives_compaction(self):
        items = [
            {"id": "s1", "role": "security_finding", "content": "open injection risk"},
            {"id": "log", "role": "resolved_log", "content": "z" * 200},
        ]
        result = ctx.compact_context(items)
        self.assertIn("security_finding", [i["role"] for i in result["kept"]])

    # 10b. Unknown roles default to kept (compaction drops only what it names).
    def test_unknown_role_is_kept_not_dropped(self):
        items = [{"id": "u", "role": "mysterious_role", "content": "do not drop"}]
        result = ctx.compact_context(items)
        self.assertEqual(len(result["kept"]), 1)

    # 11. Handoff carries enough state to continue.
    def test_handoff_contains_enough_state(self):
        handoff = ctx.build_handoff(
            task="implement X", acceptance_criteria=["tests pass"],
            modified_files=["app.py"], tests_failed=["test_y"], open_issues=["flaky test"],
            review_findings=["MAJOR: missing guard"], executor="vercel/deepseek/deepseek-v4-pro-0813")
        self.assertEqual(handoff["task"], "implement X")
        self.assertIn("tests pass", handoff["acceptance_criteria"])
        self.assertIn("app.py", handoff["modified_files"])
        self.assertIn("test_y", handoff["tests"]["failed"])
        self.assertIn("flaky test", handoff["open_issues"])
        self.assertIn("MAJOR: missing guard", handoff["review_findings"])
        self.assertEqual(handoff["executor"], "vercel/deepseek/deepseek-v4-pro-0813")

    # 12. Telemetry records measured vs estimated and before/after tokens.
    def test_telemetry_records_measured_vs_estimated(self):
        self.assertEqual(ctx.measure(tokens=5000), (5000, "measured"))
        count, source = ctx.measure(text="a" * 8000)
        self.assertEqual((count, source), (2000, "estimated"))
        items = [{"id": "k", "role": "task", "content": "x" * 400},
                 {"id": "d", "role": "resolved_log", "content": "y" * 800}]
        result = ctx.compact_context(items)
        self.assertEqual(result["tokens_before_compaction"], (400 + 800) // 4)
        self.assertEqual(result["tokens_after_compaction"], 400 // 4)

    # 12b. utilization ratio is present in a normal evaluation.
    def test_utilization_ratio_reported(self):
        result = ctx.evaluate("vercel/deepseek/deepseek-v4-pro-0813", tokens=100000, config_path=CONFIG)
        self.assertEqual(result["context_utilization_ratio"]["target_context"], 1.0)
        self.assertEqual(result["context_utilization_ratio"]["technical_context_limit"], 0.1)

    # Missing config -> no limits, no enforcement.
    def test_missing_config_yields_no_enforcement(self):
        result = ctx.evaluate("vercel/deepseek/deepseek-v4-pro-0813", tokens=500000,
                              config_path=str(Path(self.temp.name) / "missing.json"))
        self.assertIsNone(result["context_state"])
        self.assertEqual(result["action"], "none")
        self.assertEqual(result["model_context_policy_source"], "default")

    def test_claude_aliases_resolve_to_claude_class(self):
        for model in ("anthropic/fable", "fable", "claude-opus-5-5", "anthropic/sonnet", "haiku"):
            self.assertEqual(ctx.model_class(model), "claude", model)
        result = ctx.evaluate("anthropic/fable", tokens=150000, config_path=CONFIG)
        self.assertEqual(result["model_context_policy_source"], "model_class")

    def test_conflicting_duplicate_id_is_refused(self):
        items = [{"id": 1, "role": "acceptance_criteria", "content": "old"},
                 {"id": 1, "role": "acceptance_criteria", "content": "NEW"}]
        with self.assertRaises(ValueError):
            ctx.compact_context(items)
        same = [{"id": 1, "role": "task", "content": "x"}, {"id": 1, "role": "task", "content": "x"}]
        self.assertEqual(len(ctx.compact_context(same)["kept"]), 1)

    def test_cli_reports_bad_input_without_traceback(self):
        import contextlib, io, sys
        for stdin in ('{"foo": 1}', '[{"id":1,"role":"a","content":"x"},{"id":1,"role":"a","content":"y"}]'):
            old = sys.stdin
            sys.stdin = io.StringIO(stdin)
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    self.assertEqual(ctx.main(["compact"]), 1)
            finally:
                sys.stdin = old
            self.assertIn("ERRORE", err.getvalue())

    def test_missing_policy_message(self):
        result = ctx.evaluate("x", tokens=10, config_path="/nonexistent")
        self.assertIn("nessuna context policy", ctx._state_text(result))


if __name__ == "__main__":
    unittest.main()
