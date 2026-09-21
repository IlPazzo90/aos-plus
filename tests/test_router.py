"""aos-router: AOS is the router; the manual main model is a fallback, not the default.

The 10 acceptance cases from the routing change: open executor for LOW/MEDIUM,
AOS precedence over the manual session model, explicit override wins, fallbacks
when open providers are unavailable, HIGH policy review, T3 planning, CRITICAL
approval and telemetry-exposed executor identity.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-router.py"

spec = importlib.util.spec_from_file_location("router", SCRIPT)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "open-models.json"


class RouterTests(unittest.TestCase):
    def test_role_wrapper_preserves_case_insensitive_critical_approval(self):
        for tier, risk in [('T2', 'critical'), ('t2', 'critical'), ('t3', 'CrItIcAl')]:
            decision = router.decide(tier, risk, config=router.load_config(CONFIG))
            self.assertEqual(decision.verify, 'needs_approval')
            self.assertFalse(decision.pipeline)

    def test_malformed_optional_policy_falls_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for patch in ({'premium': []}, {'premium': 'bad'}, {'policy': 'bad'},
                          {'policy': {'retries_before_escalation': 'bad'}},
                          {'policy': {'retries_before_escalation': -1}}):
                path.write_text(json.dumps({'schema': 1, 'open': {'primary': 'x/y'}} | patch))
                self.assertIsNone(router.load_config(path).open_primary)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = router.load_config(str(CONFIG))
        self.assertTrue(self.config.open_primary, "config/open-models.json must set open.primary")

    # 1. T0/LOW -> open primary executor.
    def test_t0_low_routes_to_open_primary(self):
        d = router.decide("T0", "LOW", config=self.config)
        self.assertEqual(d.executor, "open")
        self.assertEqual(d.model, "vercel/deepseek/deepseek-v4-pro-0813")
        self.assertEqual(d.provider, "vercel")
        self.assertTrue(d.routed_by_aos)
        self.assertFalse(d.manual_model_override)

    # 2. T1/MEDIUM -> DeepSeek/open winner via config, not hardcoded in the router.
    def test_t1_medium_routes_to_config_open_winner(self):
        d = router.decide("T1", "MEDIUM", config=self.config)
        self.assertEqual(d.executor, "open")
        self.assertEqual(d.model, self.config.open_primary)

    # 3. Manual OpenCode main model present, no explicit override -> AOS routing wins.
    def test_aos_routing_wins_over_present_manual_model(self):
        d = router.decide("T1", "LOW", config=self.config,
                          manual_override=False)
        self.assertEqual(d.executor, "open")
        self.assertFalse(d.manual_model_override)
        self.assertTrue(d.routed_by_aos)
        self.assertEqual(d.model, self.config.open_primary)

    # 4. Explicit user override -> the main session model executes.
    def test_explicit_override_sends_work_to_main(self):
        d = router.decide("T1", "LOW", config=self.config, manual_override=True)
        self.assertEqual(d.executor, "main")
        self.assertTrue(d.manual_model_override)
        self.assertFalse(d.routed_by_aos)

    # 5. DeepSeek unavailable -> Qwen (fallback).
    def test_primary_unavailable_falls_back_to_qwen(self):
        d = router.decide("T1", "LOW", config=self.config,
                          open_primary_available=False, open_fallback_available=True)
        self.assertEqual(d.executor, "open")
        self.assertEqual(d.model, self.config.open_fallback)
        self.assertIn("fallback open", d.rationale)

    # 6. Both open models unavailable -> premium fallback, never a security regression.
    def test_no_open_provider_falls_back_to_premium(self):
        d = router.decide("T1", "LOW", config=self.config,
                          open_primary_available=False, open_fallback_available=False)
        self.assertEqual(d.executor, "premium")
        self.assertIsNone(d.model)
        self.assertEqual(d.escalation_target, self.config.escalation_executor)

    # 6b. Open exhausted on retries -> premium escalation, with the reason recorded.
    def test_open_exhausted_on_retries_escalates(self):
        d = router.decide("T1", "LOW", config=self.config,
                          open_primary_available=True, open_fallback_available=True,
                          failed_open_attempts=self.config.retries_before_escalation * 2)
        self.assertEqual(d.executor, "premium")
        self.assertIn("exhausted", d.rationale[-1])

    # 7. T2/HIGH -> open only when policy and an observable check allow it, then
    #    premium review is mandatory. Without the observable check, premium direct.
    def test_t2_high_without_observable_check_stays_premium(self):
        d = router.decide("T2", "HIGH", config=self.config, observable_check=False)
        self.assertEqual(d.executor, "premium")
        self.assertEqual(d.verify, "premium_review")
        self.assertEqual(d.escalation_target, self.config.premium_reviewer)

    def test_t2_high_with_observable_check_can_route_open_with_premium_review(self):
        d = router.decide("T2", "HIGH", config=self.config, observable_check=True)
        self.assertEqual(d.executor, "open")
        self.assertEqual(d.verify, "premium_review")
        self.assertEqual(d.model, self.config.open_primary)

    # 7b. HIGH with a disabled open policy never routes open.
    def test_high_never_routes_open_when_policy_disallows(self):
        strict = router.RoutingConfig(open_primary="x/y", open_fallback="z/w",
                                      open_t2_high_requires_observable_check=False)
        d = router.decide("T2", "HIGH", config=strict, observable_check=True)
        self.assertEqual(d.executor, "premium")

    # 8. T3 -> premium planning/final review; open is reserved for bounded subtasks.
    def test_t3_routes_to_open_with_premium_plan_and_review(self):
        d = router.decide("T3", "MEDIUM", config=self.config)
        self.assertEqual(d.executor, "open")
        self.assertEqual(d.verify, "premium_review")
        self.assertEqual(d.planner, self.config.escalation_executor)
        self.assertEqual(d.reviewer, 'claude')


    # 9. CRITICAL -> no regression: stays on main with approval, nothing routed open.
    def test_critical_stays_on_main_requiring_approval(self):
        d = router.decide("T3", "CRITICAL", config=self.config)
        self.assertEqual(d.executor, "main")
        self.assertEqual(d.verify, "needs_approval")
        self.assertFalse(d.routed_by_aos)

    # 10. Telemetry: the decision exposes the real executor and open/premium split.
    def test_router_decision_exposes_telemetry_fields(self):
        d = router.decide("T1", "MEDIUM", config=self.config)
        payload = {"main_executor_runtime": "opencode",
                   "main_executor_model": d.model,
                   "main_executor_provider": d.provider,
                   "routed_by_aos": d.routed_by_aos,
                   "manual_model_override": d.manual_model_override,
                   "open_executor_tokens": 8000, "premium_executor_tokens": 0,
                   "premium_review_tokens": 0, "escalation_count": 0,
                   "escalation_reason": None, "outcome": "delivered"}
        payload_entry = json.dumps(payload)
        self.assertIn('"routed_by_aos": true', payload_entry)
        self.assertIn('"main_executor_model": "vercel/deepseek/deepseek-v4-pro-0813"', payload_entry)
        self.assertIn('open_executor_tokens', payload_entry)

    # Legacy/absent config -> empty config means premium/legacy fallback, never error.
    def test_absent_config_falls_back_to_legacy_premium(self):
        d = router.decide("T1", "MEDIUM", config=router.RoutingConfig())
        self.assertEqual(d.executor, "main")

    def test_load_config_absent_file_returns_empty(self):
        self.assertEqual(router.load_config(Path(self.temp.name) / "missing.json"),
                         router.RoutingConfig())

    def test_load_config_invalid_schema_returns_empty(self):
        bad = Path(self.temp.name) / "bad.json"
        bad.write_text(json.dumps({"schema": 99}))
        self.assertEqual(router.load_config(bad), router.RoutingConfig())

    def test_invalid_classification_is_rejected_even_with_override(self):
        for tier, risk in [('T9', 'LOW'), ('T0', 'UNKNOWN'), (None, 'LOW')]:
            with self.assertRaises(ValueError):
                router.decide(tier, risk, config=self.config, manual_override=True)

    def test_policy_ceiling_applies_to_t1_too(self):
        strict = router.RoutingConfig(open_primary='x/y', open_max_risk='LOW')
        self.assertNotEqual(router.decide('T1', 'MEDIUM', config=strict).executor, 'open')

    def test_required_premium_runtime_routes_low_risk_task(self):
        d = router.decide('T1', 'LOW', config=self.config,
                          capabilities=['codex_app'])
        self.assertEqual(d.executor, 'premium')

    def test_critical_cannot_be_overridden_into_execution(self):
        d = router.decide('T1', 'CRITICAL', config=self.config, manual_override=True)
        self.assertEqual(d.verify, 'needs_approval')


if __name__ == "__main__":
    unittest.main()
