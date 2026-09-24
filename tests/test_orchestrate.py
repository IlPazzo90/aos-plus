"""aos-orchestrate: capability-driven, performance-aware routing as pure logic.

The orchestrator never names a model, tier or risk threshold in code: those live
in config/adaptive.json and config/open-models.json. These tests exercise the
classification, decomposition, eligibility, scoring, routing and escalation
functions against the shipped config, and a self-check that the module's own
source text contains none of the catalog keys.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-orchestrate.py"
MODELS = Path(__file__).resolve().parents[1] / "config/open-models.json"
ADAPTIVE = Path(__file__).resolve().parents[1] / "config/adaptive.json"

spec = importlib.util.spec_from_file_location("orchestrate", SCRIPT)
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()

    def test_registry_loads_models_and_adaptive_together(self):
        self.assertIn("models", self.registry)
        self.assertIn("adaptive", self.registry)
        self.assertIn("models_config", self.registry)
        self.assertTrue(self.registry["models"])
        self.assertTrue(self.registry["adaptive"].get("domains"))

    def test_every_catalog_entry_carries_the_adaptive_fields(self):
        for model_id, entry in self.registry["models"].items():
            for field in ("class", "family", "invocation", "tools", "capabilities",
                          "capabilities_source", "roles"):
                self.assertIn(field, entry, (model_id, field))

    def test_qwen_is_out_of_the_pool_but_kept_for_history(self):
        qwen = self.registry["models"]["vercel/alibaba/qwen3-coder-next"]
        self.assertFalse(qwen["availability"])
        self.assertIn("removed", qwen)
        self.assertEqual(qwen["roles"], ["executor", "fixer"])

    def test_open_fallback_is_disabled(self):
        self.assertIsNone(self.registry["models_config"]["open"]["fallback"])

    def test_missing_registry_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                orch.load_registry(models_path=str(Path(directory) / "nope.json"))

    def test_no_model_name_is_hardcoded_in_the_module(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for model_id in json.loads(MODELS.read_text())["model_catalog"]:
            self.assertNotIn(model_id, source, model_id)

    def test_module_is_stdlib_only(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for banned in ("import subprocess", "import socket", "import urllib",
                       "import requests", "import aiohttp"):
            self.assertNotIn(banned, source, banned)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.adaptive = json.loads(ADAPTIVE.read_text(encoding="utf-8"))

    def test_classifies_an_italian_bug_report(self):
        result = orch.classify("correggi questo bug nel file app.py", self.adaptive)
        self.assertIn("ENGINEERING", result["domains"])
        self.assertEqual(result["task_type"], "bug_fix")
        self.assertGreater(result["capabilities"].get("debugging", 0), 0)

    def test_accent_normalisation_matches_accent_free_keywords(self):
        result = orch.classify("costruisci questa funzionalità nuova", self.adaptive)
        self.assertIn("ENGINEERING", result["domains"])

    def test_english_engineering_prompt(self):
        result = orch.classify("fix this crash and refactor the module", self.adaptive)
        self.assertIn("ENGINEERING", result["domains"])

    def test_legal_prompt_reaches_legal_compliance(self):
        result = orch.classify("scrivi questo contratto di manutenzione", self.adaptive)
        self.assertIn("LEGAL_COMPLIANCE", result["domains"])

    def test_unclassified_text_falls_back_to_general(self):
        result = orch.classify("zzz quux fnord blarg", self.adaptive)
        self.assertEqual(result["domains"], ["GENERAL"])
        self.assertIsNone(result["task_type"])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.adaptive = json.loads(ADAPTIVE.read_text(encoding="utf-8"))

    def test_default_profile_derives_t1_medium(self):
        profile = orch.build_profile(text="correggi questo bug nel file app.py",
                                     adaptive=self.adaptive)
        self.assertEqual(profile["tier"], "T1")
        self.assertEqual(profile["risk"], "MEDIUM")
        self.assertEqual(profile["reversibility"], 0.8)

    def test_explicit_tier_and_risk_win(self):
        profile = orch.build_profile(profile={"complexity": 0.9, "volume": 0.9,
                                              "architecture_impact": 0.9},
                                     tier="T3", risk="HIGH", adaptive=self.adaptive)
        self.assertEqual((profile["tier"], profile["risk"]), ("T3", "HIGH"))

    def test_invalid_score_raises(self):
        with self.assertRaises(ValueError):
            orch.build_profile(profile={"complexity": 2.0}, adaptive=self.adaptive)

    def test_invalid_tier_raises(self):
        with self.assertRaises(ValueError):
            orch.build_profile(profile={}, tier="T9", adaptive=self.adaptive)


class DecomposeTests(unittest.TestCase):
    def setUp(self):
        self.adaptive = json.loads(ADAPTIVE.read_text(encoding="utf-8"))

    def test_requirement_and_production_decompose_into_three_bundles(self):
        profile = {"domains": ["LEGAL_COMPLIANCE", "ENGINEERING"], "task_type": "feature"}
        bundles = orch.decompose(profile, self.adaptive)
        self.assertEqual([b["id"] for b in bundles],
                         ["requirements", "implementation", "joint_verification"])
        self.assertEqual(bundles[0]["domains"], ["LEGAL_COMPLIANCE"])
        self.assertEqual(bundles[1]["depends_on"], ["requirements"])
        self.assertEqual(bundles[2]["depends_on"], ["implementation"])

    def test_single_domain_stays_one_bundle(self):
        profile = {"domains": ["ENGINEERING"], "task_type": None}
        bundles = orch.decompose(profile, self.adaptive)
        self.assertEqual([b["id"] for b in bundles], ["main"])


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()
        self.adaptive = self.registry["adaptive"]
        self.profile = orch.build_profile(profile={"domain_specialization": 0.0},
                                          tier="T1", risk="LOW", adaptive=self.adaptive)
        self.bundle = {"domains": ["GENERAL"], "capabilities": {"reasoning": 0.5, "writing": 0.5}}

    def test_qwen_is_rejected_as_unavailable(self):
        entry = self.registry["models"]["vercel/alibaba/qwen3-coder-next"]
        ok, reason = orch.eligible("vercel/alibaba/qwen3-coder-next", entry, self.bundle,
                                   self.profile, self.adaptive, "claude")
        self.assertFalse(ok)
        self.assertEqual(reason, "unavailable")

    def test_an_executor_less_entry_is_rejected(self):
        entry = dict(self.registry["models"]["anthropic/fable"], roles=["planner"])
        model_id = "anthropic/fable"
        ok, reason = orch.eligible(model_id, entry, self.bundle, self.profile,
                                   self.adaptive, "claude")
        self.assertFalse(ok)
        self.assertEqual(reason, "missing executor role")

    def test_risk_above_class_ceiling_is_rejected(self):
        entry = self.registry["models"]["anthropic/haiku"]  # class LOW
        high_profile = dict(self.profile, risk="HIGH")
        ok, reason = orch.eligible("anthropic/haiku", entry, self.bundle, high_profile,
                                   self.adaptive, "claude")
        self.assertFalse(ok)
        self.assertIn("risk", reason)

    def test_missing_tools_is_rejected(self):
        entry = self.registry["models"]["anthropic/fable"]
        profile = dict(self.profile, tools_required=["browser"])
        ok, reason = orch.eligible("anthropic/fable", entry, self.bundle, profile,
                                   self.adaptive, "claude")
        self.assertTrue(ok)  # fable has browser

        entry_no_browser = dict(entry, tools=["files", "shell"])
        ok, reason = orch.eligible("anthropic/fable", entry_no_browser, self.bundle,
                                   profile, self.adaptive, "claude")
        self.assertFalse(ok)
        self.assertIn("browser", reason)

    def test_demoted_model_is_rejected(self):
        entry = self.registry["models"]["anthropic/fable"]
        matrix = {"models": {"anthropic/fable": {
            "status": "DEMOTE", "overall": {"score": 0.5, "confidence": 0.9}}}}
        ok, reason = orch.eligible("anthropic/fable", entry, self.bundle, self.profile,
                                   self.adaptive, "claude", matrix=matrix)
        self.assertFalse(ok)
        self.assertEqual(reason, "demoted")


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()
        self.adaptive = self.registry["adaptive"]
        self.profile = orch.build_profile(profile={}, tier="T1", risk="LOW",
                                          adaptive=self.adaptive)
        self.bundle = {"domains": ["ENGINEERING"],
                       "capabilities": {"coding": 0.9, "debugging": 0.6}}

    def test_score_is_a_number_between_zero_and_one(self):
        entry = self.registry["models"]["vercel/deepseek/deepseek-v4-pro-0813"]
        result = orch.score("vercel/deepseek/deepseek-v4-pro-0813", entry, self.bundle,
                            self.profile, self.adaptive, available_classes={"OPEN", "LOW", "MID", "HIGH", "PREMIUM"})
        self.assertTrue(0.0 <= result["p_success"] <= 1.0)
        self.assertGreaterEqual(result["expected_cost"], result["initial_cost"])
        self.assertIn("capability_match", result)

    def test_matrix_confidence_blends_toward_observed(self):
        entry = self.registry["models"]["vercel/deepseek/deepseek-v4-pro-0813"]
        matrix = {"models": {"vercel/deepseek/deepseek-v4-pro-0813": {
            "overall": {"score": 1.0, "confidence": 1.0}, "status": "KEEP"}}}
        result = orch.score("vercel/deepseek/deepseek-v4-pro-0813", entry, self.bundle,
                            self.profile, self.adaptive, matrix=matrix,
                            available_classes={"OPEN", "LOW", "MID", "HIGH", "PREMIUM"})
        self.assertAlmostEqual(result["p_success"], 1.0, places=4)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()

    def test_route_produces_a_complete_decision_for_a_bug_report(self):
        profile = orch.build_profile(text="correggi questo bug nel file app.py",
                                     adaptive=self.registry["adaptive"])
        result = orch.route(profile, self.registry, "claude")
        self.assertIn("bundles", result)
        self.assertFalse(result["needs_approval"])
        bundle = result["bundles"][0]
        self.assertIsNotNone(bundle["executor"])
        self.assertIn("model", bundle["executor"])
        self.assertIsInstance(bundle["rejected"], dict)
        self.assertTrue(bundle["candidates"])

    def test_critical_risk_requires_approval(self):
        profile = orch.build_profile(profile={}, tier="T3", risk="CRITICAL",
                                     adaptive=self.registry["adaptive"])
        result = orch.route(profile, self.registry, "claude")
        self.assertTrue(result["needs_approval"])

    def test_codex_host_excludes_claude_only_subagents(self):
        profile = orch.build_profile(profile={}, tier="T1", risk="LOW",
                                     adaptive=self.registry["adaptive"])
        result = orch.route(profile, self.registry, "codex")
        rejected = result["bundles"][0]["rejected"]
        self.assertIn("anthropic/sonnet", rejected)


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.adaptive = json.loads(ADAPTIVE.read_text(encoding="utf-8"))

    def test_bug_fix_routes_to_systematic_debugging(self):
        profile = {"task_type": "bug_fix"}
        bundle = {"domains": ["ENGINEERING"], "capabilities": {"debugging": 0.9}}
        skills = orch.select_skills(bundle, profile, self.adaptive)
        self.assertIn("superpowers:systematic-debugging", skills)

    def test_writing_above_threshold_adds_humanizer_skills(self):
        profile = {"task_type": "drafting"}
        bundle = {"domains": ["LEGAL_COMPLIANCE"], "capabilities": {"writing": 0.8}}
        skills = orch.select_skills(bundle, profile, self.adaptive)
        self.assertIn("testo-umano", skills)
        self.assertIn("humanizer", skills)


class EscalateTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()

    def test_implementation_failure_retries_then_escalates(self):
        first = orch.escalate("implementation_failure",
                              "vercel/deepseek/deepseek-v4-pro-0813",
                              self.registry, attempts=1)
        self.assertEqual(first["action"], "retry_same")
        second = orch.escalate("implementation_failure",
                               "vercel/deepseek/deepseek-v4-pro-0813",
                               self.registry, attempts=3)
        self.assertEqual(second["action"], "escalate")
        self.assertIsNotNone(second["target_class"])

    def test_security_violation_stops_and_asks_a_human(self):
        result = orch.escalate("security_violation",
                               "vercel/deepseek/deepseek-v4-pro-0813",
                               self.registry)
        self.assertEqual(result["action"], "stop_and_ask_human")
        self.assertIsNone(result["target_model"])

    def test_unknown_failure_type_raises(self):
        with self.assertRaises(ValueError):
            orch.escalate("not_a_failure", "vercel/deepseek/deepseek-v4-pro-0813",
                          self.registry)


# --- Host-written coverage: the behaviours the spec is about. ---------------------

LEARNING = Path(__file__).resolve().parents[1] / "bin/aos-learning.py"
_learning_spec = importlib.util.spec_from_file_location("learning_for_orchestrate", LEARNING)
learning = importlib.util.module_from_spec(_learning_spec)
_learning_spec.loader.exec_module(learning)

DEEPSEEK = "vercel/deepseek/deepseek-v4-pro-0813"


def _entry(klass, family, capability, *, invocation="host_session", runtimes=("claude-code",)):
    caps = {name: capability for name in ("coding", "debugging", "architecture", "reasoning",
                                          "review", "security", "tool_use", "legal", "research",
                                          "writing", "data", "business", "risk_reasoning")}
    return {"class": klass, "family": family, "invocation": invocation, "roles": ["executor"],
            "tools": ["files", "shell"], "capabilities": caps,
            "compatible_runtimes": list(runtimes), "target_context": 80000}


class SpecPromptTests(unittest.TestCase):
    """The requests of spec section 51, routed with no model, skill or workflow named."""

    def setUp(self):
        self.registry = orch.load_registry()
        self.adaptive = self.registry["adaptive"]

    def domains(self, text):
        return orch.classify(text, self.adaptive)["domains"]

    def test_the_spec_prompts_reach_their_domains(self):
        cases = {
            "Correggi questo bug": "ENGINEERING",
            "Costruisci questa funzione": "ENGINEERING",
            "Analizza questi dati": "DATA_ANALYTICS",
            "Analizza questo Excel": "DATA_ANALYTICS",
            "Scrivi questo contratto": "LEGAL_COMPLIANCE",
            "Scrivimi un contratto di manutenzione": "LEGAL_COMPLIANCE",
            "Fammi una ricerca sui nuovi incentivi e proponimi una strategia": "RESEARCH",
            "Analizza i KPI di questa azienda": "BUSINESS_OPERATIONS",
        }
        for text, domain in cases.items():
            with self.subTest(text=text):
                self.assertIn(domain, self.domains(text))

    def test_the_gdpr_module_is_legal_and_engineering_in_three_bundles(self):
        text = "Costruisci un modulo GDPR nella mia applicazione"
        self.assertEqual(set(self.domains(text)), {"LEGAL_COMPLIANCE", "ENGINEERING"})
        profile = orch.build_profile(text=text, adaptive=self.adaptive, tier="T2", risk="MEDIUM")
        result = orch.route(profile, self.registry, "claude")
        self.assertEqual([b["id"] for b in result["bundles"]],
                         ["requirements", "implementation", "joint_verification"])

    def test_an_analysis_over_two_domains_stays_one_bundle(self):
        profile = orch.build_profile(text="Analizza i KPI di questa azienda", adaptive=self.adaptive)
        self.assertEqual(len(orch.decompose(profile, self.adaptive)), 1)

    def test_correcting_a_text_is_not_engineering(self):
        self.assertNotIn("ENGINEERING", self.domains("correggi questo testo per favore"))

    def test_every_spec_prompt_gets_a_complete_decision(self):
        for text in ("Correggi questo bug", "Costruisci questa funzione", "Analizza questi dati",
                     "Scrivi questo contratto", "Fammi una ricerca e proponimi una strategia"):
            with self.subTest(text=text):
                profile = orch.build_profile(text=text, adaptive=self.adaptive, tier="T2", risk="MEDIUM")
                for bundle in orch.route(profile, self.registry, "claude")["bundles"]:
                    self.assertIsNotNone(bundle["executor"])
                    self.assertIsNotNone(bundle["reviewer"])
                    self.assertTrue(bundle["verification"])

    def test_a_contract_does_not_go_to_the_open_worker(self):
        profile = orch.build_profile(text="Scrivi questo contratto di manutenzione",
                                     adaptive=self.adaptive, tier="T1", risk="LOW")
        bundle = orch.route(profile, self.registry, "claude")["bundles"][0]
        self.assertNotEqual(bundle["executor"]["model"], DEEPSEEK)
        self.assertIn(DEEPSEEK, bundle["rejected"])
        self.assertIn("anthropic-skills:consulente-legale-fiscale", bundle["skills"])
        self.assertIn("testo-umano", bundle["skills"])

    def test_a_small_bug_fix_does_not_spend_a_premium(self):
        profile = orch.build_profile(text="correggi questo bug nel parser", adaptive=self.adaptive,
                                     tier="T1", risk="LOW")
        executor = orch.route(profile, self.registry, "claude")["bundles"][0]["executor"]
        self.assertNotEqual(executor["class"], "PREMIUM")
        self.assertEqual(orch.select_skills({"domains": ["ENGINEERING"], "capabilities": {}},
                                            profile, self.adaptive),
                         ["superpowers:systematic-debugging"])


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()
        self.adaptive = self.registry["adaptive"]
        self.bundle = {"domains": ["ENGINEERING"], "capabilities": {"coding": 0.9}}

    def check(self, model_id, profile, host="claude", entry=None):
        entry = entry or self.registry["models"][model_id]
        return orch.eligible(model_id, entry, self.bundle, profile, self.adaptive, host)

    def profile(self, **values):
        tier = values.pop("tier", "T1")
        risk = values.pop("risk", "LOW")
        return orch.build_profile(profile=values, tier=tier, risk=risk, adaptive=self.adaptive)

    def test_critical_admits_only_premium(self):
        self.assertFalse(self.check("anthropic/sonnet", self.profile(risk="CRITICAL"))[0])
        self.assertTrue(self.check("anthropic/fable", self.profile(risk="CRITICAL"))[0])

    def test_high_risk_never_reaches_the_open_worker(self):
        ok, reason = self.check(DEEPSEEK, self.profile(risk="HIGH"))
        self.assertFalse(ok)
        self.assertIn("risk", reason)

    def test_the_open_worker_has_no_browser(self):
        ok, reason = self.check(DEEPSEEK, self.profile(tools_required=["browser"]))
        self.assertFalse(ok)
        self.assertIn("browser", reason)

    def test_t0_never_goes_to_the_open_worker(self):
        self.assertEqual(self.check(DEEPSEEK, self.profile(tier="T0")), (False, "T0 cannot use open_worker"))

    def test_context_over_the_hard_limit_is_refused(self):
        ok, reason = self.check("anthropic/haiku", self.profile(context_requirement=1.0),
                                entry=dict(self.registry["models"]["anthropic/haiku"]))
        # 200k tokens against the claude-class hard limit of 320k: admitted.
        self.assertTrue(ok, reason)
        huge = dict(self.adaptive, context_tokens_at_full_requirement=10_000_000)
        ok, reason = orch.eligible("anthropic/haiku", self.registry["models"]["anthropic/haiku"],
                                   self.bundle, self.profile(context_requirement=1.0), huge, "claude")
        self.assertEqual((ok, reason), (False, "context over hard limit"))

    def test_a_host_subagent_runs_only_on_its_own_host(self):
        self.assertTrue(self.check("anthropic/haiku", self.profile())[0])
        self.assertEqual(self.check("anthropic/haiku", self.profile(), host="codex"),
                         (False, "host_subagent runtime incompatible"))
        # A premium of the other family still runs through that family's CLI.
        self.assertTrue(self.check("openai/gpt-6-astra", self.profile())[0])


class ScoringTests(unittest.TestCase):
    """Expected cost and confidence on a synthetic registry, so the shipped priors do not matter."""

    def setUp(self):
        self.adaptive = orch.load_registry()["adaptive"]
        self.profile = orch.build_profile(profile={"volume": 0.5}, tier="T1", risk="LOW",
                                          adaptive=self.adaptive)
        self.bundle = {"id": "main", "domains": ["ENGINEERING"], "capabilities": {"coding": 0.8}}

    def registry(self, models):
        return {"models": models, "adaptive": self.adaptive, "models_path": str(MODELS)}

    def test_a_reliable_pricier_model_beats_a_cheap_unreliable_one(self):
        models = {"x/cheap": _entry("LOW", "anthropic", 0.8, invocation="host_subagent"),
                  "x/mid": _entry("MID", "anthropic", 0.8, invocation="host_subagent")}
        matrix = {"models": {
            "x/cheap": {"status": "KEEP", "overall": {"score": 0.3, "confidence": 0.9}},
            "x/mid": {"status": "KEEP", "overall": {"score": 0.97, "confidence": 0.9}}}}
        result = orch.route(self.profile, self.registry(models), "claude", matrix=matrix)
        executor = result["bundles"][0]["executor"]
        self.assertEqual(executor["model"], "x/mid")
        costs = {c["model"]: c["expected_cost"] for c in result["bundles"][0]["candidates"]}
        self.assertGreater(costs["x/cheap"], 0)

    def test_without_history_the_cheaper_sufficient_model_wins(self):
        models = {"x/cheap": _entry("LOW", "anthropic", 0.95, invocation="host_subagent"),
                  "x/premium": _entry("PREMIUM", "anthropic", 0.95)}
        executor = orch.route(self.profile, self.registry(models), "claude")["bundles"][0]["executor"]
        self.assertEqual(executor["model"], "x/cheap")

    def test_one_perfect_sample_does_not_beat_many_good_ones(self):
        models = {"x/lucky": _entry("MID", "anthropic", 0.7, invocation="host_subagent"),
                  "x/steady": _entry("MID", "anthropic", 0.7, invocation="host_subagent")}
        matrix = {"models": {
            "x/lucky": {"status": "NEW", "overall": {"score": 1.0, "sample_size": 1, "confidence": 0.1667}},
            "x/steady": {"status": "KEEP", "overall": {"score": 0.9, "sample_size": 30, "confidence": 0.9}}}}
        # The need (0.9) exceeds the declared capability (0.7): the prior is well under 1.
        bundle = {"id": "main", "domains": ["ENGINEERING"], "capabilities": {"coding": 0.9}}
        scored = {model: orch.score(model, models[model], bundle, self.profile, self.adaptive,
                                    matrix=matrix) for model in models}
        self.assertGreater(scored["x/steady"]["p_success"], scored["x/lucky"]["p_success"])

    def test_a_watched_model_pays_a_penalty(self):
        models = {"x/m": _entry("MID", "anthropic", 0.8, invocation="host_subagent")}
        watch = {"models": {"x/m": {"status": "WATCH", "overall": {"score": 0.8, "confidence": 0.5}}}}
        keep = {"models": {"x/m": {"status": "KEEP", "overall": {"score": 0.8, "confidence": 0.5}}}}
        entry = models["x/m"]
        watched = orch.score("x/m", entry, self.bundle, self.profile, self.adaptive, matrix=watch)
        kept = orch.score("x/m", entry, self.bundle, self.profile, self.adaptive, matrix=keep)
        self.assertLess(watched["p_success"], kept["p_success"])
        self.assertIn("watch", watched["penalties"])


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()
        self.adaptive = self.registry["adaptive"]

    def executors(self, risk, verifiable, task_ids):
        profile = orch.build_profile(profile={"verifiable": verifiable, "reversibility": 0.9},
                                     text="correggi questo bug nel parser",
                                     tier="T1", risk=risk, adaptive=self.adaptive)
        return [orch.route(profile, self.registry, "claude", task_id=task_id)["bundles"][0]["executor"]
                for task_id in task_ids]

    def test_exploration_is_rare_deterministic_and_low_risk_only(self):
        ids = ["task-%d" % n for n in range(400)]
        low = self.executors("LOW", True, ids)
        explored = [e for e in low if e.get("exploration")]
        self.assertLess(len(explored), 400 * 0.12)
        self.assertEqual(low, self.executors("LOW", True, ids))
        self.assertFalse(any(e.get("exploration") for e in self.executors("MEDIUM", True, ids)))
        self.assertFalse(any(e.get("exploration") for e in self.executors("LOW", False, ids)))
        self.assertFalse(any(e.get("exploration") for e in self.executors("LOW", True, [None])))

    def test_a_keyword_profile_is_not_verifiable_by_default(self):
        self.assertFalse(orch.build_profile(text="correggi questo bug", adaptive=self.adaptive)["verifiable"])


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()
        self.adaptive = self.registry["adaptive"]

    def reviewer(self, risk, host="claude", text="correggi questo bug nel parser"):
        profile = orch.build_profile(text=text, tier="T2", risk=risk, adaptive=self.adaptive)
        bundle = orch.route(profile, self.registry, host)["bundles"][0]
        return bundle["executor"], bundle["reviewer"]

    def test_the_reviewer_is_the_other_family_and_never_open_or_low(self):
        for host in ("claude", "codex"):
            for risk in ("LOW", "MEDIUM", "HIGH"):
                with self.subTest(host=host, risk=risk):
                    executor, reviewer = self.reviewer(risk, host)
                    entry = self.registry["models"][reviewer]
                    self.assertIn(entry["class"], ("MID", "PREMIUM"))
                    self.assertNotEqual(entry["family"], self.registry["models"][executor["model"]]["family"])
                    self.assertNotEqual(entry["family"], orch.FAMILY_OF_HOST[host]
                                        if executor["class"] == "OPEN" else None)

    def test_high_risk_gets_a_premium_reviewer(self):
        _, reviewer = self.reviewer("HIGH")
        self.assertEqual(self.registry["models"][reviewer]["class"], "PREMIUM")

    def test_t1_has_no_external_reviewer(self):
        profile = orch.build_profile(text="correggi questo bug", tier="T1", risk="LOW", adaptive=self.adaptive)
        self.assertIsNone(orch.route(profile, self.registry, "claude")["bundles"][0]["reviewer"])


class EscalationChainTests(unittest.TestCase):
    def setUp(self):
        self.registry = orch.load_registry()

    def test_infrastructure_and_aos_failures_do_not_count_against_the_model(self):
        tool = orch.escalate("tool_failure", DEEPSEEK, self.registry)
        self.assertEqual((tool["action"], tool["attribution"], tool["counts_against_model"]),
                         ("retry_same", "infra", False))
        context = orch.escalate("context_failure", DEEPSEEK, self.registry)
        self.assertEqual(context["action"], "retry_same_with_more_context")
        self.assertFalse(context["counts_against_model"])

    def test_reasoning_failure_climbs_one_class_and_skips_the_empty_high(self):
        from_open = orch.escalate("reasoning_failure", DEEPSEEK, self.registry, host="claude")
        self.assertEqual(from_open["target_class"], "MID")
        self.assertEqual(from_open["target_model"], "anthropic/sonnet")
        from_mid = orch.escalate("reasoning_failure", "anthropic/sonnet", self.registry, host="claude")
        self.assertEqual(from_mid["target_class"], "PREMIUM")

    def test_an_escalation_target_is_runnable_on_the_host(self):
        target = orch.escalate("reasoning_failure", DEEPSEEK, self.registry, host="codex")
        self.assertEqual(target["target_model"], "openai/gpt-5.6-sol")

    def test_architecture_ambiguity_goes_to_a_premium_planner(self):
        result = orch.escalate("architecture_ambiguity", "anthropic/sonnet", self.registry)
        self.assertEqual((result["action"], result["target_class"]), ("premium_planner", "PREMIUM"))


class LearningLoopTests(unittest.TestCase):
    """The matrix aos-learning.py writes is the matrix the router reads."""

    def test_a_demoted_model_from_the_real_ledger_is_not_routed(self):
        registry = orch.load_registry()
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "ledger.sqlite3")
            for n in range(25):
                learning.record_outcome(database, {
                    "model": DEEPSEEK, "provider": "vercel", "role": "executor", "tier": "T1",
                    "risk": "LOW", "worker_exit": 1, "test_pass": False, "project": "p", "task_id": "t%d" % n,
                    "domain": "ENGINEERING", "capabilities": {"coding": 0.9},
                    "failure_type": "implementation_failure", "verification_status": "verified"})
            matrix = learning.matrix(database)
        self.assertEqual(matrix["models"][DEEPSEEK]["status"], "DEMOTE")
        profile = orch.build_profile(text="correggi questo bug nel parser", tier="T1", risk="LOW",
                                     adaptive=registry["adaptive"])
        bundle = orch.route(profile, registry, "claude", matrix=matrix)["bundles"][0]
        self.assertEqual(bundle["rejected"][DEEPSEEK], "demoted")
        self.assertNotEqual(bundle["executor"]["model"], DEEPSEEK)

    def test_aos_attributed_failures_leave_the_model_routable(self):
        registry = orch.load_registry()
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "ledger.sqlite3")
            for n in range(25):
                learning.record_outcome(database, {
                    "model": DEEPSEEK, "role": "executor", "worker_exit": 1, "test_pass": False,
                    "project": "p", "task_id": "t%d" % n, "failure_type": "context_failure",
                    "verification_status": "verified"})
            matrix = learning.matrix(database)
        self.assertNotIn(DEEPSEEK, {m for m, e in matrix["models"].items() if e["status"] == "DEMOTE"})


class ReviewRoundOneTests(unittest.TestCase):
    """Regressions for the findings confirmed in round 1 of the 3.0 cross-model review."""

    def setUp(self):
        self.registry = orch.load_registry()
        self.adaptive = self.registry["adaptive"]

    def test_explicit_profile_needs_survive_decomposition(self):
        profile = orch.build_profile(profile={"capabilities": {"legal": 1.0}}, adaptive=self.adaptive,
                                     tier="T1", risk="LOW")
        bundle = orch.route(profile, self.registry, "claude")["bundles"][0]
        # legal 1.0 with a 0.25 shortfall allowed: sonnet (0.75) qualifies, haiku (0.5) does not.
        self.assertEqual(bundle["rejected"]["anthropic/haiku"], "capability shortfall legal")
        self.assertNotIn(bundle["executor"]["class"], ("LOW", "OPEN"))

    def test_domain_performance_moves_the_score(self):
        profile = orch.build_profile(text="Scrivi questo contratto", adaptive=self.adaptive)
        bundle = orch.decompose(profile, self.adaptive)[0]
        model = "anthropic/sonnet"

        def p(domain_score):
            matrix = {"models": {model: {"overall": {"score": 0.9, "confidence": 0.8},
                                         "domains": {"LEGAL_COMPLIANCE": {"score": domain_score, "confidence": 0.9}}}}}
            return orch.score(model, self.registry["models"][model], bundle, profile, self.adaptive,
                              matrix=matrix)["p_success"]

        self.assertLess(p(0.0), p(1.0))

    def test_a_failing_reviewer_escalates_inside_its_family(self):
        result = orch.escalate("review_failure", "openai/gpt-5.6-sol", self.registry, host="claude")
        self.assertEqual(result["target_model"], "openai/gpt-6-astra")
        result = orch.escalate("review_failure", "anthropic/sonnet", self.registry, host="claude")
        self.assertEqual(result["target_model"], "anthropic/fable")

    def test_the_classify_command_runs(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(orch.main(["classify", "--text", "Correggi questo bug"]), 0)
        self.assertIn("ENGINEERING", json.loads(out.getvalue())["domains"])


class ReviewRoundTwoTests(unittest.TestCase):
    """Regressions for round 2: two of them were introduced by the round 1 fixes."""

    def setUp(self):
        self.registry = orch.load_registry()
        self.adaptive = self.registry["adaptive"]

    def test_domain_order_does_not_change_the_score_and_the_weakest_decides(self):
        profile = orch.build_profile(adaptive=self.adaptive)
        model = "anthropic/sonnet"
        history = {"models": {model: {"domains": {
            "LEGAL_COMPLIANCE": {"score": 0, "confidence": 0.9},
            "BUSINESS_OPERATIONS": {"score": 1, "confidence": 0.9}}}}}
        values = [orch.score(model, self.registry["models"][model],
                             {"domains": domains, "capabilities": {"reasoning": 0.6}},
                             profile, self.adaptive, matrix=history)["p_success"]
                  for domains in (["LEGAL_COMPLIANCE", "BUSINESS_OPERATIONS"],
                                  ["BUSINESS_OPERATIONS", "LEGAL_COMPLIANCE"])]
        self.assertAlmostEqual(values[0], values[1])
        self.assertLess(values[0], self.adaptive["min_success_probability"]["MEDIUM"])

    def test_inferred_needs_stay_per_bundle(self):
        profile = orch.build_profile(text="Costruisci un modulo GDPR nella mia applicazione",
                                     adaptive=self.adaptive)
        bundles = {b["id"]: b["capabilities"] for b in orch.decompose(profile, self.adaptive)}
        self.assertLess(bundles["implementation"].get("legal", 0), 0.7)
        self.assertGreaterEqual(bundles["requirements"]["legal"], 0.9)


if __name__ == "__main__":
    unittest.main()
