"""aos-router: AOS is the router; the manual main model is a fallback, not the default.

The 10 acceptance cases from the routing change: open executor for LOW/MEDIUM,
AOS precedence over the manual session model, explicit override wins, fallbacks
when open providers are unavailable, HIGH policy review, T3 planning, CRITICAL
approval and telemetry-exposed executor identity.
"""
import contextlib
import importlib.util
import io
import json
import os
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
    def test_t0_stays_on_the_host_with_a_cheaper_subagent_by_risk(self):
        policy = json.loads(CONFIG.read_text())['host_subagents']
        for risk, klass in (('LOW', 'cheap'), ('MEDIUM', 'mid'), ('HIGH', None)):
            d = router.decide("T0", risk, config=self.config)
            self.assertEqual((d.executor, d.host_model_class, d.model), ('main', klass, None), risk)
            self.assertTrue(d.routed_by_aos)
            self.assertIsNone(d.reviewer)
            for host in ('claude', 'codex'):
                named = router.decide("T0", risk, config=self.config, host=host)
                self.assertEqual(named.model, policy[host][klass] if klass else None, (risk, host))
        self.assertEqual(router.decide("T0", "CRITICAL", config=self.config).verify, 'needs_approval')

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

    def test_catalog_selects_the_cheapest_model_that_meets_role_capabilities(self):
        catalog = {
            'vendor/cheap': {'provider': 'vendor', 'compatible_runtimes': ['opencode'],
                             'cost_class': 'CHEAP', 'capability_scores': {'coding': 2},
                             'context': 32000, 'input_cost_per_million': 0.1,
                             'output_cost_per_million': 0.2, 'roles': ['executor'],
                             'benchmark': {'available': True}, 'historical': {'available': True}},
            'vendor/mid': {'provider': 'vendor', 'compatible_runtimes': ['opencode'],
                           'cost_class': 'MID', 'capability_scores': {'coding': 4},
                           'context': 128000, 'input_cost_per_million': 1.0,
                           'output_cost_per_million': 2.0, 'roles': ['executor'],
                           'benchmark': {'available': True}, 'historical': {'available': True}},
            'vendor/premium': {'provider': 'vendor', 'compatible_runtimes': ['opencode'],
                               'cost_class': 'PREMIUM', 'capability_scores': {'coding': 5},
                               'context': 128000, 'input_cost_per_million': 10.0,
                               'output_cost_per_million': 20.0, 'roles': ['executor'],
                               'benchmark': {'available': True}, 'historical': {'available': True}},
        }
        config = router.RoutingConfig(catalog=catalog)
        self.assertEqual(router.choose_model(config, 'executor',
                                              capability_requirements={'coding': 4}, runtime='opencode'),
                         'vendor/mid')

    def test_catalog_budget_can_forbid_a_model_without_silently_using_premium(self):
        catalog = {'vendor/mid': {'provider': 'vendor', 'compatible_runtimes': ['opencode'],
                                  'cost_class': 'MID', 'capability_scores': {'coding': 4},
                                  'context': 128000, 'input_cost_per_million': 1.0,
                                  'output_cost_per_million': 2.0, 'roles': ['executor'],
                                  'benchmark': {'available': True}, 'historical': {'available': True}}}
        self.assertIsNone(router.choose_model(router.RoutingConfig(catalog=catalog), 'executor',
                                               capability_requirements={'coding': 4}, runtime='opencode',
                                               budget={'max_cost_class': 'CHEAP'}))

    def test_open_ladder_uses_configured_mid_only_after_primary_and_cheap_fallback(self):
        config = router.RoutingConfig(open_primary='vendor/cheap-primary', open_fallback='vendor/cheap-fallback',
                                      open_mid='vendor/mid', retries_before_escalation=2)
        d = router.decide('T1', 'LOW', config=config, failed_open_attempts=4)
        self.assertEqual((d.executor, d.model), ('open', 'vendor/mid'))
        self.assertIn('mid open', d.rationale)

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

    def test_t1_high_gets_inline_checks_not_an_external_reviewer(self):
        # External cross-model review is for T2/T3 (HIGH included); T0/T1 HIGH
        # run the extended HIGH checks inline.
        for families in (('codex', 'claude'), ('codex',), ('claude',)):
            with self.subTest(families=families):
                d = router.decide('T1', 'HIGH', config=self.config, observable_check=True,
                                  premium_families=families)
                self.assertIsNone(d.planner)
                self.assertIsNone(d.reviewer)
                self.assertFalse(d.cross_model_review)
                self.assertEqual(d.verify, 'deterministic')
        d = router.decide('T2', 'HIGH', config=self.config)
        self.assertTrue(d.cross_model_review)
        self.assertEqual(d.verify, 'premium_review')

    def test_t3_routes_to_open_with_premium_plan_and_review(self):
        d = router.decide("T3", "MEDIUM", config=self.config)
        self.assertEqual(d.executor, "open")
        self.assertEqual(d.verify, "premium_review")
        self.assertEqual((d.planner, d.planner_model), ('claude', 'anthropic/fable'))
        self.assertEqual((d.reviewer, d.reviewer_model), ('codex', 'openai/gpt-6-astra'))
        self.assertEqual(d.fixer_model, self.config.open_primary)


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

    def _entry(self, provider, runtime, cost_class, scores, input_cost, output_cost):
        return {'provider': provider, 'compatible_runtimes': [runtime],
                'cost_class': cost_class, 'capability_scores': scores,
                'roles': ['planner', 'reviewer', 'executor'],
                'benchmark': {'available': True}, 'historical': {'available': True},
                'input_cost_per_million': input_cost, 'output_cost_per_million': output_cost}

    def test_catalog_mid_planner_for_simple_t2(self):
        catalog = {
            'openai/mid': self._entry('openai', 'codex-cli', 'MID',
                                       {'planning': 3, 'reasoning': 3, 'review': 3, 'coding': 4}, 1.0, 2.0),
            'openai/premium': self._entry('openai', 'codex-cli', 'PREMIUM',
                                           {'planning': 5, 'reasoning': 5, 'review': 5, 'coding': 5}, 10.0, 20.0),
            'anthropic/mid': self._entry('anthropic', 'claude-code', 'MID',
                                         {'planning': 3, 'reasoning': 3, 'review': 3, 'coding': 4}, 1.5, 2.5),
            'anthropic/premium': self._entry('anthropic', 'claude-code', 'PREMIUM',
                                             {'planning': 5, 'reasoning': 5, 'review': 5, 'coding': 5}, 15.0, 25.0),
        }
        config = router.RoutingConfig(role_pipeline=True, catalog=catalog)
        d = router.decide('T2', 'MEDIUM', config=config)
        self.assertEqual(d.planner_model, 'openai/mid')
        self.assertEqual(d.planner, 'codex')
        self.assertEqual(d.reviewer_model, 'anthropic/mid')
        self.assertEqual(d.reviewer, 'claude')

    def test_catalog_premium_planner_for_high_uncertainty_t2(self):
        catalog = {
            'openai/mid': self._entry('openai', 'codex-cli', 'MID',
                                       {'planning': 3, 'reasoning': 3, 'review': 3, 'coding': 4}, 1.0, 2.0),
            'openai/premium': self._entry('openai', 'codex-cli', 'PREMIUM',
                                           {'planning': 5, 'reasoning': 5, 'review': 5, 'coding': 5}, 10.0, 20.0),
            'anthropic/mid': self._entry('anthropic', 'claude-code', 'MID',
                                         {'planning': 3, 'reasoning': 3, 'review': 3, 'coding': 4}, 1.5, 2.5),
            'anthropic/premium': self._entry('anthropic', 'claude-code', 'PREMIUM',
                                             {'planning': 5, 'reasoning': 5, 'review': 5, 'coding': 5}, 15.0, 25.0),
        }
        config = router.RoutingConfig(role_pipeline=True, catalog=catalog)
        d = router.decide('T2', 'HIGH', config=config, uncertainty='HIGH')
        self.assertEqual(d.planner_model, 'openai/premium')
        self.assertEqual(d.planner, 'codex')
        self.assertEqual(d.reviewer_model, 'anthropic/premium')
        self.assertEqual(d.reviewer, 'claude')

    def test_catalog_opposite_provider_reviewer_when_available(self):
        catalog = {
            'openai/mid': self._entry('openai', 'codex-cli', 'MID',
                                       {'planning': 3, 'reasoning': 3, 'review': 3, 'coding': 4}, 1.0, 2.0),
            'anthropic/mid': self._entry('anthropic', 'claude-code', 'MID',
                                         {'planning': 3, 'reasoning': 3, 'review': 3, 'coding': 4}, 1.5, 2.5),
        }
        config = router.RoutingConfig(role_pipeline=True, catalog=catalog, escalation_executor='claude')
        d = router.decide('T2', 'MEDIUM', config=config, planner='claude')
        self.assertEqual(d.planner, 'claude')
        self.assertEqual(d.planner_model, 'anthropic/mid')
        self.assertEqual(d.reviewer, 'codex')
        self.assertEqual(d.reviewer_model, 'openai/mid')

    def test_catalog_host_preference_only_explicit(self):
        catalog = {
            'openai/mid': self._entry('openai', 'codex-cli', 'MID',
                                       {'planning': 3, 'reasoning': 3, 'review': 3, 'coding': 4}, 1.0, 2.0),
            'anthropic/mid': self._entry('anthropic', 'claude-code', 'MID',
                                         {'planning': 3, 'reasoning': 3, 'review': 3, 'coding': 4}, 1.5, 2.5),
        }
        config = router.RoutingConfig(role_pipeline=True, catalog=catalog)
        d = router.decide('T2', 'MEDIUM', config=config, planner='codex')
        self.assertEqual(d.planner, 'codex')
        self.assertEqual(d.planner_model, 'openai/mid')
        self.assertEqual(d.reviewer, 'claude')
        self.assertEqual(d.reviewer_model, 'anthropic/mid')

    def test_open_decision_premium_when_exhausted_with_mid_available(self):
        config = router.RoutingConfig(open_primary='vendor/cheap', open_mid='vendor/mid',
                                       retries_before_escalation=2)
        d = router.decide('T1', 'LOW', config=config, failed_open_attempts=5)
        self.assertEqual(d.executor, 'premium')
        self.assertNotEqual(d.model, 'vendor/mid')
        self.assertIn('exhausted', d.rationale[-1])

    def test_real_catalog_keeps_configured_premium_models_without_benchmark(self):
        d = router.decide('T3', 'MEDIUM', config=self.config, executor_runtime='opencode')
        self.assertEqual((d.planner_model, d.reviewer_model), ('anthropic/fable', 'openai/gpt-6-astra'))

    def test_legacy_role_pipeline_without_catalog_has_no_name_error(self):
        config = router.RoutingConfig(role_pipeline=True, open_primary='vendor/open')
        d = router.decide('T2', 'MEDIUM', config=config)
        self.assertEqual((d.executor, d.planner, d.reviewer), ('open', 'codex', 'claude'))
        self.assertEqual((d.planner_model, d.reviewer_model), (None, None))

    def test_unavailable_family_never_claims_cross_model_review(self):
        catalog = {'openai/mid': self._entry('openai', 'codex-cli', 'MID',
                                               {'planning': 3, 'reasoning': 3, 'review': 3}, 1, 1)}
        d = router.decide('T2', 'MEDIUM', config=router.RoutingConfig(role_pipeline=True, catalog=catalog))
        self.assertEqual((d.planner, d.reviewer, d.cross_model_review), ('codex', None, False))

    def test_zero_price_wins_and_nonfinite_prices_are_rejected(self):
        catalog = {
            'vendor/free': self._entry('vendor', 'opencode', 'CHEAP', {'coding': 3}, 0, 0),
            'vendor/paid': self._entry('vendor', 'opencode', 'CHEAP', {'coding': 3}, 1, 1),
            'vendor/nan': self._entry('vendor', 'opencode', 'CHEAP', {'coding': 3}, float('nan'), 1),
        }
        self.assertEqual(router.choose_model(router.RoutingConfig(catalog=catalog), 'executor',
                                              capability_requirements={'coding': 3}, runtime='opencode'), 'vendor/free')

    def test_benchmark_absence_is_not_unavailability_but_explicit_false_is(self):
        entry = self._entry('vendor', 'opencode', 'CHEAP', {'coding': 3}, 1, 1)
        entry['benchmark']['available'] = False
        config = router.RoutingConfig(catalog={'vendor/configured': entry})
        self.assertEqual(router.choose_model(config, 'executor', runtime='opencode'), 'vendor/configured')
        entry['availability'] = False
        self.assertIsNone(router.choose_model(config, 'executor', runtime='opencode'))



class CatalogIntegrityTests(unittest.TestCase):
    def test_catalog_without_the_configured_open_models_blocks(self):
        import copy
        config = copy.deepcopy(router.load_config(CONFIG))
        config.catalog.pop(config.open_primary, None)
        config.catalog.pop(config.open_fallback, None)
        with self.assertRaisesRegex(ValueError, 'missing from the model catalog'):
            router.decide('T1', 'LOW', config=config)


class CommandLineDefaultTests(unittest.TestCase):
    """The CLI answers about the shipped policy, not about an empty config.

    Without a config every tier and risk answers "main", which reads like a
    routing decision rather than a missing file, so the default has to be the
    policy the skill ships with, resolved from the script and not from cwd.
    """

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(router.main(list(argv)), 0)
        return json.loads(buffer.getvalue())

    def test_the_cli_routes_to_the_open_model_without_being_told_where_the_policy_is(self):
        decision = self.run_cli('--tier', 'T1', '--risk', 'LOW', '--json')
        self.assertEqual(decision['executor'], 'open')
        self.assertEqual(decision['model'], json.loads(CONFIG.read_text())['open']['primary'])
        self.assertTrue(decision['routed_by_aos'])

    def test_the_default_policy_is_found_from_an_unrelated_working_directory(self):
        origin = os.getcwd()
        with tempfile.TemporaryDirectory() as elsewhere:
            os.chdir(elsewhere)
            try:
                decision = self.run_cli('--tier', 'T2', '--risk', 'MEDIUM', '--json')
            finally:
                os.chdir(origin)
        self.assertEqual(decision['executor'], 'open')

    def test_an_explicit_config_still_wins_over_the_default(self):
        # The explicit file is read (not the default), and an unusable one is an
        # error rather than a silent route to main.
        with tempfile.TemporaryDirectory() as directory:
            for payload in ({'schema': 2}, None):
                unusable = Path(directory) / 'policy.json'
                if payload is None:
                    unusable.unlink()
                else:
                    unusable.write_text(json.dumps(payload))
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    code = router.main(['--tier', 'T1', '--risk', 'LOW', '--config', str(unusable), '--json'])
                self.assertEqual(code, 2)
                self.assertIn('non utilizzabile', err.getvalue())

    def test_open_ladder_never_goes_backwards_from_premium(self):
        config = router.load_config(CONFIG)
        # An eligible MID (the fallback's model, reused) with the fallback unavailable:
        # primary twice, then the MID stands in; once the caller reports it ran, premium.
        config = router.replace(config, open_mid=config.open_fallback)
        seen = [router.decide('T1', 'LOW', config=config, open_fallback_available=False,
                              failed_open_attempts=n) for n in range(3)]
        self.assertEqual([d.model for d in seen[:2]], [config.open_primary] * 2)
        self.assertIn('mid open', seen[2].rationale)
        after = [router.decide('T1', 'LOW', config=config, open_fallback_available=False,
                               mid_available=False, failed_open_attempts=n) for n in range(3, 6)]
        self.assertEqual([d.executor for d in after], ['premium'] * 3)

    def test_manual_override_keeps_the_t2_review(self):
        config = router.load_config(CONFIG)
        for tier in ('T2', 'T3'):
            for host, opposite in (('claude', 'codex'), ('codex', 'claude')):
                d = router.decide(tier, 'HIGH', config=config, manual_override=True, host=host)
                self.assertEqual((d.executor, d.reviewer, d.verify, d.cross_model_review),
                                 ('main', opposite, 'premium_review', True), (tier, host))
                self.assertIsNotNone(d.reviewer_model)
        for risk in ('LOW', 'MEDIUM'):
            d = router.decide('T2', risk, config=config, manual_override=True, host='codex')
            self.assertEqual(d.reviewer, 'claude', risk)
            self.assertIsNotNone(d.reviewer_model, risk)
        for kwargs in (dict(premium_reviewer_available=False), dict(premium_families=('claude',))):
            d = router.decide('T2', 'HIGH', config=config, manual_override=True, host='claude', **kwargs)
            self.assertEqual((d.reviewer, d.reviewer_model, d.verify, d.cross_model_review),
                             (None, None, 'reported', False), kwargs)
        d = router.decide('T1', 'HIGH', config=config, manual_override=True, host='claude')
        self.assertIsNone(d.reviewer)

    def test_a_fallback_lost_after_its_turns_still_leaves_the_mid(self):
        config = router.RoutingConfig(open_primary='vendor/primary', open_fallback='vendor/fallback',
                                      open_mid='vendor/mid', retries_before_escalation=2)
        d = router.decide('T1', 'LOW', config=config, failed_open_attempts=4,
                          open_fallback_available=False)
        self.assertEqual((d.executor, d.model), ('open', 'vendor/mid'))

    def test_router_and_pipeline_give_the_mid_the_same_budget(self):
        import importlib.util as iu
        spec = iu.spec_from_file_location('pipe_for_mid', CONFIG.parents[1] / 'bin/aos-pipeline.py')
        pipe = iu.module_from_spec(spec)
        spec.loader.exec_module(pipe)
        for fallback in (None, 'vendor/fallback'):
            config = router.RoutingConfig(open_primary='vendor/primary', open_fallback=fallback,
                                          open_mid='vendor/mid', retries_before_escalation=2)
            state = pipe.start('T1', 'LOW', None, None, 'vendor/primary', fallback, 2, mid='vendor/mid')
            for n in range(7):
                d = router.decide('T1', 'LOW', config=config, failed_open_attempts=n)
                routed = d.model if d.executor == 'open' else 'premium'
                piped = state['model'] if state['stage'] == 'execute' else 'premium'
                self.assertEqual(routed, piped, (fallback, n))
                if state['stage'] == 'execute':
                    pipe.fail(state, 'failed')

    def test_a_primary_lost_mid_task_does_not_consume_the_fallback(self):
        config = router.load_config(CONFIG)
        d = router.decide('T1', 'LOW', config=config, open_primary_available=False,
                          failed_open_attempts=2)
        self.assertEqual((d.executor, d.model), ('open', config.open_fallback))

    def test_no_premium_review_is_promised_without_a_reviewer(self):
        config = router.load_config(CONFIG)
        for risk in ('MEDIUM', 'HIGH'):
            decision = router.decide('T2', risk, config=config, premium_reviewer_available=False)
            if decision.reviewer is None:
                self.assertNotEqual(decision.verify, 'premium_review', risk)

    def test_catalog_with_a_string_roles_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            data = json.loads(CONFIG.read_text())
            first = next(iter(data['model_catalog']))
            data['model_catalog'][first]['roles'] = 'executor-only'
            path = Path(directory) / 'policy.json'
            path.write_text(json.dumps(data))
            self.assertEqual(router.load_config(path), router.RoutingConfig())

if __name__ == "__main__":
    unittest.main()
