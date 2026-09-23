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


def _checkpoint_zone(optimal=100, degradation=160, hard=250, enforced=True):
    return {"model": "m", "factor": 1.0, "optimal_until": optimal,
            "degradation_from": degradation, "hard_limit": hard,
            "technical_context_limit": 1000000, "policy_source": "model", "enforced": enforced}


def _complete_checkpoint():
    doc = ctx.checkpoint_template()
    doc.update({
        "objective": "ship bundle C",
        "completed_work": ["implemented zones and checkpoints"],
        "known_facts": ["model policy resolved"],
        "architecture_decisions": ["zones scale by task profile"],
        "pending_work": ["write docs"],
        "test_evidence": ["unit tests pass"],
        "review_findings": ["no blockers"],
        "next_action": "continue with documentation",
    })
    return doc


class ContextAdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _write(self, name, obj):
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(obj))
        return str(path)

    # --- zone: factor math, floor, deep shrink, hard limit, ordering ---

    def test_factor_math(self):
        self.assertEqual(ctx._profile_factor({}), 1.0)
        # 1 - 0.3*0.5 - 0.2*0.5 - 0.1*0.5 = 0.7
        self.assertEqual(ctx._profile_factor(
            {"complexity": 0.5, "uncertainty": 0.5, "evidence_volume": 0.5}), 0.7)

    def test_factor_floor(self):
        # extreme profiles floor at 0.4 rather than collapsing to nothing
        self.assertEqual(ctx._profile_factor(
            {"complexity": 1.0, "uncertainty": 1.0, "evidence_volume": 1.0}), 0.4)
        self.assertEqual(ctx._profile_factor(
            {"complexity": 1.0, "uncertainty": 1.0, "evidence_volume": 1.0, "reasoning": "deep"}), 0.4)

    def test_deep_reasoning_shrinks_zones(self):
        shallow = ctx._profile_factor({"complexity": 0.2})
        deep = ctx._profile_factor({"complexity": 0.2, "reasoning": "deep"})
        self.assertLess(deep, shallow)
        self.assertEqual(deep, round(shallow * 0.85, 4))

    def test_zone_hard_limit_unchanged_and_ordering(self):
        config = {"context_policy": {"models": {"m": {
            "target_context": 100, "soft_limit": 200, "hard_limit": 300}}}}
        result = ctx.zone("m", profile={"complexity": 0.5, "uncertainty": 1.0,
                                        "evidence_volume": 1.0},
                          config_path=self._write("c.json", config))
        self.assertEqual(result["factor"], 0.55)  # 1 - 0.15 - 0.2 - 0.1
        self.assertEqual(result["hard_limit"], 300)  # never scaled
        self.assertLessEqual(result["optimal_until"], result["degradation_from"])
        self.assertLessEqual(result["degradation_from"], result["hard_limit"])

    def test_zone_invalid_profile_values_rejected(self):
        for bad in ({"complexity": 1.5}, {"complexity": -0.1}, {"uncertainty": "high"},
                    {"reasoning": "medium"}, {"evidence_volume": True},
                    {"complexity": "0.5"}):
            with self.assertRaises(ValueError):
                ctx._profile_factor(bad)

    def test_zone_unenforced_policy(self):
        result = ctx.zone("x", profile={"complexity": 0.5}, config_path="/nonexistent")
        self.assertFalse(result["enforced"])
        self.assertEqual(result["optimal_until"], 0)
        self.assertEqual(result["degradation_from"], 0)
        self.assertEqual(result["hard_limit"], 0)

    # --- natural_checkpoint: every branch ---

    def test_natural_checkpoint_branches(self):
        z = _checkpoint_zone()
        self.assertEqual(ctx.natural_checkpoint(50, z, "none")["action"], "continue")
        self.assertEqual(ctx.natural_checkpoint(100, z, "none")["action"], "continue")
        self.assertEqual(ctx.natural_checkpoint(150, z, "none")["action"], "prepare_checkpoint")
        self.assertEqual(ctx.natural_checkpoint(200, z, "discovery_done")["action"], "compact_at_checkpoint")
        self.assertEqual(ctx.natural_checkpoint(200, z, "none")["action"], "wait_for_checkpoint")
        # over hard -> compact_now even with event none
        self.assertEqual(ctx.natural_checkpoint(260, z, "none")["action"], "compact_now")
        self.assertEqual(ctx.natural_checkpoint(260, z, "bundle_done")["action"], "compact_now")

    def test_natural_checkpoint_positions(self):
        z = _checkpoint_zone()
        self.assertEqual(ctx.natural_checkpoint(50, z, "none")["position"], "optimal")
        self.assertEqual(ctx.natural_checkpoint(150, z, "none")["position"], "approaching")
        self.assertEqual(ctx.natural_checkpoint(200, z, "none")["position"], "degradation")
        self.assertEqual(ctx.natural_checkpoint(260, z, "none")["position"], "over_hard")

    def test_natural_checkpoint_unenforced(self):
        z = _checkpoint_zone(enforced=False)
        result = ctx.natural_checkpoint(999999, z, "none")
        self.assertEqual(result["action"], "continue")

    # --- checkpoint document and gate ---

    def test_checkpoint_template_fields_and_failing_gate(self):
        tpl = ctx.checkpoint_template()
        self.assertEqual(tpl["schema"], 1)
        for field in ctx.CHECKPOINT_FIELDS:
            self.assertIn(field, tpl)
        result = ctx.validate_checkpoint(tpl)
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing"], [])
        for item in ("goal", "current_state", "decisions", "unresolved_work",
                     "verification_state", "next_action"):
            self.assertIn(item, result["empty_gate"])

    def test_complete_checkpoint_passes(self):
        result = ctx.validate_checkpoint(_complete_checkpoint())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["empty_gate"], [])
        self.assertEqual(result["missing"], [])

    def test_each_gate_item_missing_reports_its_name(self):
        def gate_of(**overrides):
            doc = _complete_checkpoint()
            doc.update(overrides)
            return ctx.validate_checkpoint(doc)["empty_gate"]

        self.assertIn("goal", gate_of(objective=""))
        self.assertIn("current_state", gate_of(completed_work=[], known_facts=[]))
        self.assertIn("decisions", gate_of(architecture_decisions=[]))
        self.assertIn("unresolved_work", gate_of(pending_work=[], next_action="keep going"))
        self.assertIn("verification_state", gate_of(test_evidence=[], review_findings=[]))
        self.assertIn("next_action", gate_of(next_action=""))

    def test_missing_key_reported_in_missing(self):
        doc = _complete_checkpoint()
        del doc["objective"]
        result = ctx.validate_checkpoint(doc)
        self.assertIn("objective", result["missing"])
        self.assertFalse(result["ok"])

    def test_empty_pending_work_requires_done_next_action(self):
        def gate_of(**overrides):
            doc = _complete_checkpoint()
            doc.update(overrides)
            return ctx.validate_checkpoint(doc)["empty_gate"]
        self.assertIn("unresolved_work", gate_of(pending_work=[], next_action="keep going"))
        self.assertNotIn("unresolved_work", gate_of(pending_work=[], next_action="done, ship it"))
        self.assertNotIn("unresolved_work", gate_of(pending_work=[], next_action="completo"))

    def test_warnings_for_huge_strings_and_lists(self):
        doc = _complete_checkpoint()
        doc["objective"] = "x" * 5000
        doc["completed_work"] = ["y"] * 250
        result = ctx.validate_checkpoint(doc)
        self.assertTrue(result["ok"])  # oversized fields are warnings, not errors
        self.assertTrue(any("objective" in w for w in result["warnings"]))
        self.assertTrue(any("completed_work" in w for w in result["warnings"]))

    # --- compaction effectiveness ---

    def test_effectiveness_no_data(self):
        result = ctx.compaction_effectiveness([])
        self.assertEqual(result["verdict"], "no_data")
        self.assertEqual(result["context_tokens_saved"], 0)
        self.assertEqual(set(result["rates"].values()), {None})

    def test_effectiveness_verdicts(self):
        good = [{
            "resume_failure": 0, "duplicated_discovery": 0, "information_recovery_requests": 1,
            "post_compaction_errors": 0, "tokens_before": 1000, "tokens_after": 400}]
        self.assertEqual(ctx.compaction_effectiveness(good)["verdict"], "effective")
        bad = [
            {"resume_failure": 1, "duplicated_discovery": 0, "information_recovery_requests": 0,
             "post_compaction_errors": 0, "tokens_before": 1000, "tokens_after": 400},
            {"resume_failure": 0, "duplicated_discovery": 0, "information_recovery_requests": 0,
             "post_compaction_errors": 0, "tokens_before": 1000, "tokens_after": 400},
        ]
        self.assertEqual(ctx.compaction_effectiveness(bad)["verdict"], "ineffective")

    def test_effectiveness_clamps_tokens_saved(self):
        events = [
            {"resume_failure": 0, "duplicated_discovery": 0, "information_recovery_requests": 0,
             "post_compaction_errors": 0, "tokens_before": 1000, "tokens_after": 400},
            {"resume_failure": 0, "duplicated_discovery": 0, "information_recovery_requests": 0,
             "post_compaction_errors": 0, "tokens_before": 100, "tokens_after": 500},
        ]
        self.assertEqual(ctx.compaction_effectiveness(events)["context_tokens_saved"], 600)

    def test_effectiveness_rates(self):
        events = [
            {"resume_failure": 1, "duplicated_discovery": 0, "information_recovery_requests": 4,
             "post_compaction_errors": 2, "tokens_before": 1000, "tokens_after": 400},
            {"resume_failure": 0, "duplicated_discovery": 1, "information_recovery_requests": 2,
             "post_compaction_errors": 1, "tokens_before": 1000, "tokens_after": 400},
        ]
        result = ctx.compaction_effectiveness(events)
        self.assertEqual(result["rates"]["resume_failure_rate"], 0.5)
        self.assertEqual(result["rates"]["duplicated_discovery_rate"], 0.5)
        self.assertEqual(result["rates"]["mean_information_recovery_requests"], 3.0)
        self.assertEqual(result["rates"]["post_compaction_error_rate"], 1.5)

    def test_effectiveness_invalid_values_rejected(self):
        base = {"resume_failure": 0, "duplicated_discovery": 0, "information_recovery_requests": 0,
                "post_compaction_errors": 0, "tokens_before": 100, "tokens_after": 50}
        with self.assertRaises(ValueError):
            ctx.compaction_effectiveness("not a list")
        with self.assertRaises(ValueError):
            ctx.compaction_effectiveness([{**base, "resume_failure": 2}])
        with self.assertRaises(ValueError):
            ctx.compaction_effectiveness([{**base, "information_recovery_requests": -1}])
        with self.assertRaises(ValueError):
            ctx.compaction_effectiveness([{**base, "tokens_after": True}])

    # --- CLI ---

    def test_cli_checkpoint_validate_exit_codes(self):
        import contextlib, io
        bad = self._write("bad.json", ctx.checkpoint_template())
        self.assertEqual(ctx.main(["checkpoint", "validate", bad]), 2)
        good = self._write("good.json", _complete_checkpoint())
        self.assertEqual(ctx.main(["checkpoint", "validate", good]), 0)

    def test_cli_zone_template_when_effectiveness(self):
        profile = self._write("profile.json", {"complexity": 0.2, "reasoning": "deep"})
        events = self._write("events.json", [
            {"resume_failure": 0, "duplicated_discovery": 0, "information_recovery_requests": 0,
             "post_compaction_errors": 0, "tokens_before": 100, "tokens_after": 50}])
        self.assertEqual(ctx.main(["zone", "--model", "acme/x", "--profile", profile]), 0)
        self.assertEqual(ctx.main(["checkpoint", "template"]), 0)
        self.assertEqual(ctx.main(["checkpoint", "when", "--model", "acme/x",
                                   "--tokens", "50000", "--event", "none", "--profile", profile]), 0)
        self.assertEqual(ctx.main(["effectiveness", events]), 0)


class CheckpointBlankItemsTests(unittest.TestCase):
    def test_blank_items_do_not_pass_the_gate(self):
        doc = ctx.checkpoint_template()
        doc.update(objective="Fix bug", known_facts=[""], architecture_decisions=[" "],
                   pending_work=[""], test_evidence=[{}], next_action="continue")
        result = ctx.validate_checkpoint(doc)
        self.assertFalse(result["ok"])
        self.assertEqual(set(result["empty_gate"]),
                         {"current_state", "decisions", "unresolved_work", "verification_state"})


if __name__ == "__main__":
    unittest.main()
