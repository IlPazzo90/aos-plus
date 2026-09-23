"""Persistent verified-learning ledger regressions; sqlite3 stdlib, no command execution."""
import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import time as time_module
import unittest
from unittest import mock

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "bin/aos-learning.py"


def load_module():
    spec = importlib.util.spec_from_file_location("aos_learning", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


L = load_module()


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "learn.db"

    def make_lesson(self, **overrides):
        lesson = {
            "lesson_type": "execution",
            "source_task": "task-1",
            "evidence": ["c1", "c2"],
            "confidence": "high",
            "scope": "project",
            "project": "proj-a",
            "affected_component": "router",
            "recommended_action": "add a guardrail",
            "mechanically_verified": True,
        }
        lesson.update(overrides)
        return lesson

    def make_checks(self, extra=None):
        checks = [
            {"id": "c1", "argv": ["check", "one"], "exit_code": 0, "output": "ok", "revision": "rev-abc"},
            {"id": "c2", "argv": ["check", "two"], "exit_code": 0, "output": "ok", "revision": "rev-def"},
        ]
        if extra:
            checks.extend(extra)
        return checks

    def row_counts(self):
        conn = sqlite3.connect(str(self.db))
        try:
            tables = {}
            for name in ("lessons", "outcomes", "applications"):
                tables[name] = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
            return tables
        finally:
            conn.close()

    # --- record_lesson ---

    def test_persistence_survives_reopen(self):
        result = L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        self.assertEqual(result["status"], "approved")
        report = L.report(self.db)
        self.assertEqual(report["lessons"]["approved"], 1)
        # A brand-new file handle must observe the committed row.
        self.assertEqual(self.row_counts()["lessons"], 1)
        conn = sqlite3.connect(str(self.db))
        try:
            row = conn.execute("SELECT scope, project, status FROM lessons").fetchone()
        finally:
            conn.close()
        self.assertEqual((row[0], row[1], row[2]), ("project", "proj-a", "approved"))

    def test_valid_lesson_is_approved(self):
        result = L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        self.assertEqual(result["status"], "approved")
        self.assertIn("lesson_id", result)

    def test_false_mechanically_verified_is_refused(self):
        lesson = self.make_lesson(mechanically_verified=False)
        result = L.record_lesson(self.db, lesson, self.make_checks())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "not_mechanically_verified")

    def test_invalid_lesson_type_is_refused(self):
        result = L.record_lesson(self.db, self.make_lesson(lesson_type="magic"), self.make_checks())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "invalid_lesson_type")

    def test_invalid_confidence_is_refused(self):
        result = L.record_lesson(self.db, self.make_lesson(confidence="sure"), self.make_checks())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "invalid_confidence")

    def test_invalid_scope_is_refused(self):
        result = L.record_lesson(self.db, self.make_lesson(scope="universe"), self.make_checks())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "invalid_scope")

    def test_project_scope_requires_project(self):
        lesson = self.make_lesson(scope="project")
        lesson.pop("project")
        result = L.record_lesson(self.db, lesson, self.make_checks())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "missing_project")

    def test_global_scope_refuses_project_field(self):
        result = L.record_lesson(self.db, self.make_lesson(scope="global"), self.make_checks())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "global_with_project")

    def test_rejected_contradictory_actions_are_distinct(self):
        first = L.record_lesson(self.db, self.make_lesson(scope="global", recommended_action="pin A"), self.make_checks())
        second = L.record_lesson(self.db, self.make_lesson(scope="global", recommended_action="drop A"), self.make_checks())
        self.assertEqual((first["count"], second["count"]), (1, 1))
        self.assertEqual(self.row_counts()["lessons"], 2)

    def test_unverified_model_only_claim_is_rejected_and_persisted(self):
        # Evidence references a check id that does not exist: a model-only claim.
        lesson = self.make_lesson(evidence=["ghost"])
        first = L.record_lesson(self.db, lesson, self.make_checks())
        self.assertEqual(first["status"], "rejected")
        self.assertEqual(first["reason"], "invalid_evidence")
        # The rejected candidate is persisted with a reason and a running count.
        second = L.record_lesson(self.db, lesson, self.make_checks())
        self.assertEqual(second["status"], "rejected")
        self.assertGreater(second["count"], first["count"])
        self.assertEqual(self.row_counts()["lessons"], 1)
        report = L.report(self.db)
        self.assertEqual(report["lessons"]["rejected"], 1)
        self.assertGreaterEqual(report["candidates"], 1)

    def test_empty_evidence_is_unverified(self):
        lesson = self.make_lesson(evidence=[])
        result = L.record_lesson(self.db, lesson, self.make_checks())
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "unverified_model_only")

    def test_evidence_needs_nonempty_revision(self):
        lesson = self.make_lesson(evidence=["c_blank"])
        checks = [{"id": "c_blank", "argv": ["x"], "exit_code": 0, "output": "ok", "revision": ""}]
        result = L.record_lesson(self.db, lesson, checks)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "invalid_evidence")

    def test_nonzero_exit_check_is_valid_evidence_not_rejected(self):
        # A failing check proves a failure; it must not be discarded as non-passing.
        lesson = self.make_lesson(evidence=["c_fail"])
        checks = [{"id": "c_fail", "argv": ["x"], "exit_code": 1, "output": "boom", "revision": "rev-fail"}]
        result = L.record_lesson(self.db, lesson, checks)
        self.assertEqual(result["status"], "approved")

    def test_duplicate_lesson_deduplicates_and_counts(self):
        first = L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        self.assertEqual(first["status"], "approved")
        second = L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["lesson_id"], first["lesson_id"])
        self.assertEqual(second["count"], 2)
        self.assertEqual(self.row_counts()["lessons"], 1)
        report = L.report(self.db)
        self.assertEqual(report["lessons"]["approved"], 1)
        self.assertTrue(any(p["count"] >= 2 for p in report["repeated_patterns"]))

    def test_contradictory_lesson_is_not_deduplicated(self):
        first = L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        second = L.record_lesson(self.db, self.make_lesson(recommended_action="remove the guardrail"), self.make_checks())
        self.assertEqual(first["status"], "approved")
        self.assertEqual(second["status"], "approved")
        self.assertEqual(self.row_counts()["lessons"], 2)

    # --- scope isolation / apply ---

    def test_project_lessons_do_not_leak_to_global(self):
        # A single global lesson, plus two project lessons on the same component
        # and action, must still refuse application: project sources do not count.
        g = self.make_lesson(scope="global", source_task="g1", evidence=["c1"], recommended_action="act")
        g.pop("project")
        gid = L.record_lesson(self.db, g, self.make_checks())
        self.assertEqual(gid["status"], "approved")
        for task in ("p1", "p2"):
            p = self.make_lesson(source_task=task, evidence=["c1"], recommended_action="act", project="proj-a")
            self.assertEqual(L.record_lesson(self.db, p, self.make_checks())["status"], "approved")
        refused = L.apply_lesson(self.db, gid["lesson_id"], "routing", "host evidence rollback:r1")
        self.assertEqual(refused["status"], "refused")

    def test_global_one_observation_is_refused_without_approval(self):
        g = self.make_lesson(scope="global", source_task="g1", evidence=["c1"])
        g.pop("project")
        gid = L.record_lesson(self.db, g, self.make_checks())
        self.assertEqual(gid["status"], "approved")
        refused = L.apply_lesson(self.db, gid["lesson_id"], "test", "host evidence rollback:r1")
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["reason"], "insufficient_evidence")

    def test_global_three_independent_source_tasks_allow_application(self):
        ids = []
        for i, task in enumerate(("g1", "g2", "g3")):
            g = self.make_lesson(scope="global", source_task=task, evidence=["c1"],
                                 recommended_action="shared-action")
            g.pop("project")
            result = L.record_lesson(self.db, g, [{"id": "c1", "argv": ["check", task],
                                                   "exit_code": 0, "output": "ok", "revision": task}])
            self.assertEqual(result["status"], "approved")
            ids.append(result["lesson_id"])
        applied = L.apply_lesson(self.db, ids[0], "routing", "host evidence rollback:r1")
        self.assertEqual(applied["status"], "applied")

    def test_global_renamed_check_ids_are_not_independent_evidence(self):
        ids = []
        for task, check_id in (("g1", "one"), ("g2", "two"), ("g3", "three")):
            lesson = self.make_lesson(scope="global", source_task=task, evidence=[check_id],
                                       recommended_action="shared")
            lesson.pop("project")
            ids.append(L.record_lesson(self.db, lesson, [{"id": check_id, "argv": ["same"],
                                                           "exit_code": 1, "output": "same", "revision": "same"}])["lesson_id"])
        self.assertEqual(L.apply_lesson(self.db, ids[0], "routing", "host rollback:r")["reason"], "insufficient_evidence")

    def test_global_approval_bypasses_evidence_threshold(self):
        g = self.make_lesson(scope="global", source_task="g1", evidence=["c1"])
        g.pop("project")
        gid = L.record_lesson(self.db, g, self.make_checks())
        applied = L.apply_lesson(self.db, gid["lesson_id"], "policy", "host evidence rollback:r1", approved=True)
        self.assertEqual(applied["status"], "applied")

    def test_apply_requires_valid_action_and_host_evidence(self):
        result = L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        lid = result["lesson_id"]
        self.assertEqual(L.apply_lesson(self.db, lid, "teleport", "e")["status"], "refused")
        self.assertEqual(L.apply_lesson(self.db, lid, "test", "")["status"], "refused")
        self.assertEqual(L.apply_lesson(self.db, lid, "test", "   ")["status"], "refused")

    def test_applied_lesson_records_rollback_and_evidence_audit(self):
        result = L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        lid = result["lesson_id"]
        applied = L.apply_lesson(self.db, lid, "guardrail", "host-evidence-body rollback:rb1")
        self.assertEqual(applied["status"], "applied")
        self.assertIn("application_id", applied)
        self.assertEqual(applied["rollback_identifier"], "rb1")
        conn = sqlite3.connect(str(self.db))
        try:
            row = conn.execute(
                "SELECT lesson_id, action, evidence, approved, rollback_identifier "
                "FROM applications").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], lid)
        self.assertEqual(row[1], "guardrail")
        self.assertEqual(row[2], "host-evidence-body rollback:rb1")
        self.assertEqual(row[3], 0)
        self.assertEqual(row[4], "rb1")
        self.assertEqual(L.report(self.db)["applied"], 1)

    # --- record_outcome / history ---

    def outcome(self, **overrides):
        event = {
            "runtime": "codex",
            "provider": "openai",
            "model": "model-x",
            "role": "executor",
            "tier": "T2",
            "risk": "low",
            "success": True,
            "worker_exit": 0,
            "test_pass": True,
            "cost": 1.5,
            "input_tokens": 100,
            "output_tokens": 50,
            "task_id": "t-1",
            "project": "proj-a",
        }
        event.update(overrides)
        return event

    def test_worker_timeout_with_green_test_cannot_succeed(self):
        L.record_outcome(self.db, self.outcome(success=True, worker_exit=137, test_pass=True))
        groups = L.history(self.db)["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["success"], 0)
        self.assertEqual(groups[0]["first_pass"], 0)

    def test_success_requires_worker_exit_zero_and_test_pass(self):
        # Nonzero exit alone, or failing tests alone, both deny success.
        L.record_outcome(self.db, self.outcome(success=True, worker_exit=1, test_pass=True))
        L.record_outcome(self.db, self.outcome(success=True, worker_exit=0, test_pass=False, task_id="t-2"))
        groups = L.history(self.db)["groups"]
        self.assertEqual(sum(g["success"] for g in groups), 0)

    def test_unknown_costs_are_null_not_zero(self):
        L.record_outcome(self.db, self.outcome(cost=None, task_id="t-1"))
        L.record_outcome(self.db, self.outcome(cost=3.0, task_id="t-2"))
        group = L.history(self.db)["groups"][0]
        self.assertEqual(group["success"], 2)
        # One success has an unknown cost, so the ratio must be null, never 0.
        self.assertIsNone(group["cost_per_success"])

    def test_cost_per_success_when_all_costs_known(self):
        L.record_outcome(self.db, self.outcome(cost=1.0, task_id="t-1"))
        L.record_outcome(self.db, self.outcome(cost=4.0, task_id="t-2"))
        group = L.history(self.db)["groups"][0]
        self.assertEqual(group["success"], 2)
        self.assertAlmostEqual(group["cost_per_success"], 2.5)

    def test_history_splits_first_pass_and_retry(self):
        L.record_outcome(self.db, self.outcome(success=False, worker_exit=1, test_pass=True, task_id="t-1"))
        L.record_outcome(self.db, self.outcome(success=True, worker_exit=0, test_pass=True, task_id="t-1", retry=1))
        group = L.history(self.db)["groups"][0]
        self.assertEqual(group["first_pass"], 0)
        self.assertEqual(group["retry"], 1)
        self.assertEqual(group["success"], 1)

    def test_history_filters_by_project(self):
        L.record_outcome(self.db, self.outcome(project="p-a", task_id="t-1"))
        L.record_outcome(self.db, self.outcome(project="p-b", task_id="t-2"))
        self.assertEqual(L.history(self.db, project="p-a")["groups"][0]["observations"], 1)
        self.assertEqual(L.history(self.db)["groups"][0]["observations"], 2)

    # --- confirmed-defect regressions ---

    def test_check_missing_host_execution_is_not_approved(self):
        # id + revision alone is a model-only claim, not executed host evidence.
        lesson = self.make_lesson(evidence=["x"])
        checks = [{"id": "x", "revision": "arbitrary"}]
        result = L.record_lesson(self.db, lesson, checks)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "invalid_check")

    def test_check_requires_argv_exit_and_output(self):
        bads = (
            {"id": "c1", "argv": [], "exit_code": 0, "output": "ok", "revision": "r"},        # empty argv
            {"id": "c1", "argv": ["x"], "output": "ok", "revision": "r"},                     # exit missing
            {"id": "c1", "argv": [1], "exit_code": 0, "output": "ok", "revision": "r"},       # argv not strings
            {"id": "c1", "argv": ["x"], "exit_code": 0, "revision": "r"},                     # output missing
        )
        for bad in bads:
            result = L.record_lesson(self.db, self.make_lesson(evidence=["c1"]), [bad])
            self.assertEqual(result["status"], "rejected", bad)

    def test_bool_exit_code_is_rejected(self):
        checks = [{"id": "c1", "argv": ["x"], "exit_code": True, "output": "ok", "revision": "r"}]
        result = L.record_lesson(self.db, self.make_lesson(evidence=["c1"]), checks)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "invalid_check")

    def test_duplicate_check_ids_are_rejected(self):
        checks = self.make_checks(extra=[{"id": "c1", "argv": ["x"], "exit_code": 0, "output": "ok", "revision": "r2"}])
        result = L.record_lesson(self.db, self.make_lesson(evidence=["c1"]), checks)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "duplicate_check")

    def test_full_checks_snapshot_is_persisted(self):
        L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        conn = sqlite3.connect(str(self.db))
        try:
            raw = conn.execute("SELECT evidence FROM lessons").fetchone()[0]
        finally:
            conn.close()
        snapshot = json.loads(raw)
        self.assertIn("checks", snapshot)
        by_id = {c["id"]: c for c in snapshot["checks"]}
        self.assertEqual(by_id["c1"]["argv"], ["check", "one"])
        self.assertEqual(by_id["c1"]["exit_code"], 0)
        self.assertEqual(by_id["c1"]["output"], "ok")
        self.assertEqual(by_id["c1"]["revision"], "rev-abc")

    def test_dedup_is_scoped_by_project(self):
        a = self.make_lesson(project="proj-a", source_task="t", evidence=["c1"])
        b = self.make_lesson(project="proj-b", source_task="t", evidence=["c1"])
        ra = L.record_lesson(self.db, a, self.make_checks())
        rb = L.record_lesson(self.db, b, self.make_checks())
        self.assertEqual(ra["status"], "approved")
        self.assertEqual(rb["status"], "approved")
        self.assertNotEqual(ra["lesson_id"], rb["lesson_id"])
        self.assertEqual(self.row_counts()["lessons"], 2)

    def test_rejected_then_verified_becomes_separate_approved(self):
        bad = self.make_lesson(evidence=["ghost"])
        first = L.record_lesson(self.db, bad, self.make_checks())
        self.assertEqual(first["status"], "rejected")
        good = self.make_lesson(evidence=["c1"])
        second = L.record_lesson(self.db, good, self.make_checks())
        self.assertEqual(second["status"], "approved")
        report = L.report(self.db)
        self.assertEqual(report["lessons"]["rejected"], 1)
        self.assertEqual(report["lessons"]["approved"], 1)
        self.assertEqual(self.row_counts()["lessons"], 2)

    def test_global_approval_must_be_exact_true_not_truthy_string(self):
        g = self.make_lesson(scope="global", source_task="g1", evidence=["c1"])
        g.pop("project")
        gid = L.record_lesson(self.db, g, self.make_checks())
        for bad in ("false", "no", "0", 1):
            refused = L.apply_lesson(self.db, gid["lesson_id"], "policy", "e rollback:r", approved=bad)
            self.assertEqual(refused["status"], "refused", bad)
        applied = L.apply_lesson(self.db, gid["lesson_id"], "policy", "e rollback:r", approved=True)
        self.assertEqual(applied["status"], "applied")

    def test_low_confidence_global_does_not_auto_apply(self):
        ids = []
        for i, task in enumerate(("g1", "g2", "g3")):
            g = self.make_lesson(scope="global", source_task=task, evidence=["c1"],
                                 recommended_action="shared", confidence="low")
            g.pop("project")
            result = L.record_lesson(self.db, g, self.make_checks())
            self.assertEqual(result["status"], "approved")
            ids.append(result["lesson_id"])
        refused = L.apply_lesson(self.db, ids[0], "routing", "host evidence rollback:r1")
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["reason"], "insufficient_confidence")

    def test_apply_requires_rollback_reference(self):
        lid = L.record_lesson(self.db, self.make_lesson(), self.make_checks())["lesson_id"]
        refused = L.apply_lesson(self.db, lid, "test", "host evidence without a reference")
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["reason"], "missing_rollback")

    def test_worker_exit_must_be_integer_not_bool(self):
        with self.assertRaises(ValueError):
            L.record_outcome(self.db, self.outcome(worker_exit=True))

    def test_negative_or_nonfinite_cost_raises(self):
        with self.assertRaises(ValueError):
            L.record_outcome(self.db, self.outcome(cost=-1))
        with self.assertRaises(ValueError):
            L.record_outcome(self.db, self.outcome(cost=float("inf")))

    def test_negative_tokens_raise(self):
        with self.assertRaises(ValueError):
            L.record_outcome(self.db, self.outcome(input_tokens=-5))

    def test_worker_error_field_forces_failure(self):
        result = L.record_outcome(self.db, self.outcome(worker_exit=0, test_pass=True, error="crashed"))
        self.assertFalse(result["success"])
        group = L.history(self.db)["groups"][0]
        self.assertEqual(group["success"], 0)

    def test_cost_per_success_includes_failed_attempts(self):
        L.record_outcome(self.db, self.outcome(cost=1.0, worker_exit=0, task_id="t-1"))
        L.record_outcome(self.db, self.outcome(cost=3.0, worker_exit=1, test_pass=True, task_id="t-2"))
        group = L.history(self.db)["groups"][0]
        self.assertEqual(group["success"], 1)
        self.assertAlmostEqual(group["cost_per_success"], 4.0)

    def test_unknown_failed_cost_makes_cost_per_success_null(self):
        L.record_outcome(self.db, self.outcome(cost=1.0, worker_exit=0, task_id="t-1"))
        L.record_outcome(self.db, self.outcome(cost=None, worker_exit=1, test_pass=True, task_id="t-2"))
        group = L.history(self.db)["groups"][0]
        self.assertIsNone(group["cost_per_success"])

    def test_attempts_count_within_pair_not_across_roles(self):
        L.record_outcome(self.db, self.outcome(role="executor", task_id="t-1"))
        L.record_outcome(self.db, self.outcome(role="reviewer", task_id="t-1"))
        groups = {g["role"]: g for g in L.history(self.db)["groups"]}
        self.assertEqual(groups["executor"]["first_pass"], 1)
        self.assertEqual(groups["executor"]["retry"], 0)
        self.assertEqual(groups["reviewer"]["first_pass"], 1)

    def test_historical_success_rate_is_published(self):
        L.record_outcome(self.db, self.outcome(worker_exit=0, task_id="t-1"))
        L.record_outcome(self.db, self.outcome(worker_exit=1, test_pass=True, task_id="t-2"))
        group = L.history(self.db)["groups"][0]
        self.assertEqual(group["observations"], 2)
        self.assertEqual(group["success"], 1)
        self.assertAlmostEqual(group["historical_success_rate"], 0.5)

    def test_list_outcomes_preserves_timestamps_and_cost_class(self):
        L.record_outcome(self.db, self.outcome(cost_class="CHEAP", task_id="t-1"))
        rows = L.list_outcomes(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_class"], "CHEAP")
        self.assertIsNotNone(rows[0]["created_at"])
        self.assertEqual(rows[0]["success"], True)
        self.assertEqual(rows[0]["cost"], 1.5)
        self.assertEqual(rows[0]["input_tokens"], 100)

    def test_outcome_validates_cost_class_and_verification_status(self):
        for field, value in (("cost_class", "FREE"), ("verification_status", "claimed")):
            with self.assertRaises(ValueError):
                L.record_outcome(self.db, self.outcome(**{field: value}))
        L.record_outcome(self.db, self.outcome(verification_status="pending"))
        self.assertEqual(L.list_outcomes(self.db)[0]["verification_status"], "pending")

    def test_outcome_records_retry_host_and_uncertainty(self):
        L.record_outcome(self.db, self.outcome(retry=2, main_host="codex-cli", uncertainty="HIGH"))
        row = L.list_outcomes(self.db)[0]
        self.assertEqual((row["retry"], row["main_host"], row["uncertainty"]), (2, "codex-cli", "HIGH"))

    def test_outcome_retry_validation_and_legacy_database_migration(self):
        for retry in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                L.record_outcome(self.db, self.outcome(retry=retry))
        legacy = pathlib.Path(self.temp.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.execute("CREATE TABLE outcomes (id INTEGER PRIMARY KEY, runtime TEXT, provider TEXT, model TEXT, role TEXT, tier TEXT, risk TEXT, success INTEGER NOT NULL, worker_exit INTEGER, test_pass INTEGER, error TEXT, cost_class TEXT, cost REAL, input_tokens INTEGER, output_tokens INTEGER, task_id TEXT, project TEXT, created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO outcomes VALUES (1, 'r', 'p', 'm', 'executor', 'T1', 'LOW', 1, 0, 1, NULL, NULL, 1, 1, 1, 't', 'proj', '2026-01-01T00:00:00Z')")
        conn.commit()
        conn.close()
        row = L.list_outcomes(legacy)[0]
        self.assertEqual(row["retry"], 0)
        self.assertIsNone(row["main_host"])

    # --- routing advice ---

    def test_routing_advice_excludes_only_after_independent_verified_tasks(self):
        for task_id, exit_code in (("t1", 1), ("t2", 1), ("t3", 1), ("t4", 0), ("t5", 0)):
            L.record_outcome(self.db, self.outcome(task_id=task_id, worker_exit=exit_code,
                                                    test_pass=exit_code == 0, verification_status="verified"))
        advice = L.routing_advice(self.db, "proj-a", "codex", "executor", "T2", "low")
        self.assertEqual(advice["excluded_models"], ["model-x"])
        row = advice["recommendations"][0]
        self.assertEqual(row["observations"], 5)
        self.assertEqual(row["source_tasks"], ["t1", "t2", "t3", "t4", "t5"])
        self.assertAlmostEqual(row["success_rate"], .4)
        self.assertEqual(row["recommendation"], "exclude")

    def test_routing_advice_does_not_count_retries_or_pending_review(self):
        for _ in range(8):
            L.record_outcome(self.db, self.outcome(task_id="one", worker_exit=1, test_pass=False,
                                                    verification_status="verified"))
        L.record_outcome(self.db, self.outcome(task_id="pending", worker_exit=0, test_pass=False,
                                                error="pending mandatory review", verification_status="pending"))
        advice = L.routing_advice(self.db, "proj-a", "codex", "executor", "T2", "low")
        self.assertEqual(advice["excluded_models"], [])
        self.assertEqual(advice["recommendations"][0]["observations"], 1)
        self.assertEqual(advice["recommendations"][0]["success_rate"], 0)

    def test_routing_advice_ignores_untyped_and_not_scored_outcomes(self):
        for task_id, status in (("u", None), ("n", "not_scored"), ("p", "pending")):
            L.record_outcome(self.db, self.outcome(task_id=task_id, worker_exit=1, test_pass=False,
                                                    verification_status=status))
        advice = L.routing_advice(self.db, "proj-a", "codex", "executor", "T2", "low")
        self.assertEqual(advice["recommendations"], [])

    def test_routing_advice_is_exactly_project_and_runtime_scoped(self):
        for task_id in ("a1", "a2", "a3", "a4", "a5"):
            L.record_outcome(self.db, self.outcome(task_id=task_id, worker_exit=1, test_pass=False,
                                                    verification_status="verified"))
        for task_id in ("b1", "b2", "b3", "b4", "b5"):
            L.record_outcome(self.db, self.outcome(project="proj-b", task_id=task_id,
                                                    worker_exit=1, test_pass=False, verification_status="verified"))
        L.record_outcome(self.db, self.outcome(runtime="claude", task_id="other",
                                                worker_exit=0, test_pass=True, verification_status="verified"))
        advice = L.routing_advice(self.db, "proj-a", "codex", "executor", "T2", "low")
        self.assertEqual(advice["excluded_models"], ["model-x"])
        self.assertEqual(advice["recommendations"][0]["source_tasks"], ["a1", "a2", "a3", "a4", "a5"])

    def test_routing_advice_validates_thresholds(self):
        for observations, rate in ((0, .6), (True, .6), (5, float("nan")), (5, 1.1)):
            with self.assertRaises(ValueError):
                L.routing_advice(self.db, "proj-a", "codex", "executor", "T2", "low", observations, rate)

    # --- report tests ---

    def test_report_returns_lessons_detail_with_all_fields(self):
        lesson = self.make_lesson(project="proj-a")
        result = L.record_lesson(self.db, lesson, self.make_checks())
        lesson_id = result["lesson_id"]

        report = L.report(self.db)

        self.assertIn("lessons_detail", report)
        self.assertEqual(len(report["lessons_detail"]), 1)
        detail = report["lessons_detail"][0]
        self.assertEqual(detail["scope"], "project")
        self.assertEqual(detail["project"], "proj-a")
        self.assertEqual(detail["source_task"], "task-1")
        self.assertEqual(detail["component"], "router")
        self.assertEqual(detail["recommended_action"], "add a guardrail")
        self.assertEqual(detail["confidence"], "high")
        self.assertEqual(detail["status"], "approved")
        self.assertEqual(detail["evidence"], ["c1", "c2"])
        self.assertIn("created_at", detail)

    def test_report_returns_applications_detail(self):
        lesson = self.make_lesson()
        result = L.record_lesson(self.db, lesson, self.make_checks())
        lid = result["lesson_id"]

        applied = L.apply_lesson(self.db, lid, "routing", "host evidence rollback:rb1")
        self.assertEqual(applied["status"], "applied")

        report = L.report(self.db)

        self.assertIn("applications_detail", report)
        self.assertEqual(len(report["applications_detail"]), 1)
        detail = report["applications_detail"][0]
        self.assertEqual(detail["id"], 1)
        self.assertEqual(detail["lesson_id"], lid)
        self.assertEqual(detail["action"], "routing")
        self.assertEqual(detail["evidence"], "host evidence rollback:rb1")
        self.assertEqual(detail["approved"], False)
        self.assertEqual(detail["rollback_identifier"], "rb1")
        self.assertIn("created_at", detail)

    def test_report_filters_by_project(self):
        # Lesson in proj-a
        lesson_a = self.make_lesson(project="proj-a", source_task="task-a")
        L.record_lesson(self.db, lesson_a, self.make_checks())

        # Lesson in proj-b
        lesson_b = self.make_lesson(project="proj-b", source_task="task-b")
        L.record_lesson(self.db, lesson_b, self.make_checks())

        # Report without filter
        all_report = L.report(self.db)
        self.assertEqual(len(all_report["lessons_detail"]), 2)

        # Report filtered by proj-a
        report_a = L.report(self.db, project="proj-a")
        self.assertEqual(len(report_a["lessons_detail"]), 1)
        self.assertEqual(report_a["lessons_detail"][0]["project"], "proj-a")
        self.assertEqual(report_a["lessons_detail"][0]["source_task"], "task-a")

        # Report filtered by proj-b
        report_b = L.report(self.db, project="proj-b")
        self.assertEqual(len(report_b["lessons_detail"]), 1)
        self.assertEqual(report_b["lessons_detail"][0]["project"], "proj-b")
        self.assertEqual(report_b["lessons_detail"][0]["source_task"], "task-b")

    def test_report_filters_by_since_timestamp(self):
        lesson = self.make_lesson(project="proj-a", source_task="task-old")
        result = L.record_lesson(self.db, lesson, self.make_checks())
        old_lesson_id = result["lesson_id"]

        # Sleep briefly to ensure timestamp difference
        time_module.sleep(0.1)

        lesson2 = self.make_lesson(project="proj-a", source_task="task-new", confidence="low")
        result2 = L.record_lesson(self.db, lesson2, self.make_checks())
        new_lesson_id = result2["lesson_id"]

        # Get report with since set to after the first lesson
        # Use the created_at from the second lesson
        report_new = L.report(self.db, since=result2["created_at"])
        self.assertEqual(len(report_new["lessons_detail"]), 1)
        self.assertEqual(report_new["lessons_detail"][0]["source_task"], "task-new")

        # Get report with since set earlier (before both lessons)
        # Parse the created_at from the first lesson and subtract a bit
        from datetime import datetime, timedelta
        first_timestamp = result["created_at"].replace("Z", "+00:00")
        dt_first = datetime.fromisoformat(first_timestamp)
        earlier = dt_first - timedelta(seconds=1)
        since_earlier = earlier.isoformat().replace("+00:00", "Z")

        report_earlier = L.report(self.db, since=since_earlier)
        self.assertEqual(len(report_earlier["lessons_detail"]), 2)

    def test_report_rejects_invalid_since_timestamp(self):
        with self.assertRaises(ValueError):
            L.report(self.db, since="not-a-timestamp")

        with self.assertRaises(ValueError):
            L.report(self.db, since="2024-13-01")  # invalid month

        with self.assertRaises(ValueError):
            L.report(self.db, since="")  # empty string

    def test_report_normalizes_offset_since_to_utc(self):
        L.record_lesson(self.db, self.make_lesson(), self.make_checks())
        report = L.report(self.db, since="2000-01-01T02:00:00+02:00")
        self.assertEqual(len(report["lessons_detail"]), 1)

    def test_report_project_and_since_filters_together(self):
        # Create lessons in different projects and times
        from datetime import datetime, timedelta
        import time as time_module

        # Early lesson in proj-a
        lesson_a1 = self.make_lesson(project="proj-a", source_task="task-a1")
        result_a1 = L.record_lesson(self.db, lesson_a1, self.make_checks())

        time_module.sleep(0.1)

        # Later lesson in proj-a
        lesson_a2 = self.make_lesson(project="proj-a", source_task="task-a2")
        result_a2 = L.record_lesson(self.db, lesson_a2, self.make_checks())

        time_module.sleep(0.1)

        # Later lesson in proj-b
        lesson_b = self.make_lesson(project="proj-b", source_task="task-b")
        result_b = L.record_lesson(self.db, lesson_b, self.make_checks())

        # Filter by proj-a and since result_a1
        report = L.report(self.db, project="proj-a", since=result_a1["created_at"])
        self.assertEqual(len(report["lessons_detail"]), 2)
        source_tasks = {d["source_task"] for d in report["lessons_detail"]}
        self.assertEqual(source_tasks, {"task-a1", "task-a2"})

        # Filter by proj-a and since result_a2 (only task-a2 in proj-a after that time)
        report2 = L.report(self.db, project="proj-a", since=result_a2["created_at"])
        self.assertEqual(len(report2["lessons_detail"]), 1)
        self.assertEqual(report2["lessons_detail"][0]["source_task"], "task-a2")

        # Filter by proj-b and since result_a1 (should only include proj-b)
        report3 = L.report(self.db, project="proj-b", since=result_a1["created_at"])
        self.assertEqual(len(report3["lessons_detail"]), 1)
        self.assertEqual(report3["lessons_detail"][0]["project"], "proj-b")
        self.assertEqual(report3["lessons_detail"][0]["source_task"], "task-b")

    def test_report_evidence_parsed_correctly(self):
        lesson = self.make_lesson(evidence=["check1", "check2", "check1"])
        result = L.record_lesson(self.db, lesson, self.make_checks(extra=[
            {"id": "check1", "argv": ["x"], "exit_code": 0, "output": "ok", "revision": "r1"},
            {"id": "check2", "argv": ["x"], "exit_code": 0, "output": "ok", "revision": "r2"},
        ]))
        _ = result["lesson_id"]

        report = L.report(self.db)
        detail = report["lessons_detail"][0]

        # Evidence should be parsed from JSON into a list
        self.assertIsInstance(detail["evidence"], list)
        # Duplicates are removed in evidence
        self.assertEqual(len(detail["evidence"]), 2)
        self.assertIn("check1", detail["evidence"])
        self.assertIn("check2", detail["evidence"])

    def test_report_counts_still_work_with_filters(self):
        lesson_a = self.make_lesson(project="proj-a")
        L.record_lesson(self.db, lesson_a, self.make_checks())

        lesson_b = self.make_lesson(project="proj-b")
        L.record_lesson(self.db, lesson_b, self.make_checks())

        report = L.report(self.db, project="proj-a")

        # Counts should still reflect the filtered data
        self.assertEqual(report["lessons"]["total"], 1)
        self.assertEqual(report["lessons"]["approved"], 1)
        self.assertEqual(report["status_counts"]["approved"], 1)
        self.assertEqual(report["candidates"], 0)


class ReservationTests(unittest.TestCase):
    """Budget holds are atomic across sessions: one BEGIN IMMEDIATE per admission."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "learn.db"

    @staticmethod
    def check(policy, records, estimate, premium=False, task_id=None, now=None):
        total = sum(r["cost"] for r in records) + estimate
        if total > policy["daily_budget"]:
            raise ValueError("daily_budget exceeded")
        return {"allowed": True, "total": total}

    def test_reservation_counts_for_other_sessions_until_settled(self):
        policy = {"daily_budget": 1.0}
        first = L.reserve_budget(self.db, policy, 0.6, "task-a", self.check)
        self.assertEqual(first["open_reservations"], 0)
        with self.assertRaisesRegex(ValueError, "daily_budget exceeded"):
            L.reserve_budget(self.db, policy, 0.6, "task-b", self.check)
        # An outcome without a reservation id settles nothing (round 1 finding: one
        # outcome used to release every hold of the task).
        L.record_outcome(self.db, {"task_id": "task-a", "worker_exit": 0, "test_pass": True, "cost": 0.1})
        with self.assertRaisesRegex(ValueError, "daily_budget exceeded"):
            L.reserve_budget(self.db, policy, 0.6, "task-b", self.check)
        # The outcome that names the hold releases exactly that one; its measured cost replaces it.
        L.record_outcome(self.db, {"task_id": "task-a", "worker_exit": 0, "test_pass": True, "cost": 0.1,
                                   "reservation_id": first["reservation_id"]})
        second = L.reserve_budget(self.db, policy, 0.6, "task-b", self.check)
        self.assertEqual(second["open_reservations"], 0)
        self.assertAlmostEqual(second["total"], 0.8)

    def test_sibling_holds_of_the_same_task_survive_one_outcome(self):
        policy = {"daily_budget": 1.0}
        a1 = L.reserve_budget(self.db, policy, 0.4, "task-a", self.check)
        L.reserve_budget(self.db, policy, 0.4, "task-a", self.check)
        L.record_outcome(self.db, {"task_id": "task-a", "worker_exit": 0, "test_pass": True, "cost": 0.4,
                                   "reservation_id": a1["reservation_id"]})
        # Committed 0.4 + the sibling hold 0.4 + 0.6 requested = 1.4 > 1.0
        with self.assertRaisesRegex(ValueError, "daily_budget exceeded"):
            L.reserve_budget(self.db, policy, 0.6, "task-b", self.check)
        with self.assertRaisesRegex(ValueError, "reservation_id must be an integer"):
            L.record_outcome(self.db, {"task_id": "t", "worker_exit": 0, "reservation_id": "7"})

    def test_unknown_estimate_admitted_by_check_is_held_as_unknown(self):
        first = L.reserve_budget(self.db, {"daily_budget": 1.0}, None,
                                 "task-a", lambda *a, **k: {"allowed": True})
        self.assertIsNotNone(first["reservation_id"])
        seen = {}

        def check(policy, records, estimate, **kwargs):
            seen["records"] = records
            return {"allowed": True}
        L.reserve_budget(self.db, {}, 0.0, "task-b", check)
        self.assertEqual([r["cost"] for r in seen["records"] if "reservation_id" in r], [None])

    def test_expired_reservation_stops_counting(self):
        old = "2000-01-01T00:00:00Z"
        L.reserve_budget(self.db, {"daily_budget": 1.0}, 0.9, "task-old", self.check, now=old)
        report = L.reserve_budget(self.db, {"daily_budget": 1.0}, 0.9, "task-new", self.check)
        self.assertEqual(report["open_reservations"], 0)

    def test_reservation_accepts_a_datetime_or_string_now_with_the_real_check(self):
        import importlib.util as iu
        from datetime import datetime, timezone
        spec = iu.spec_from_file_location("ops_for_now", pathlib.Path(L.__file__).with_name("aos-operations.py"))
        ops = iu.module_from_spec(spec)
        spec.loader.exec_module(ops)
        for now in ("2026-09-22T10:00:00Z", datetime(2026, 9, 22, 11, tzinfo=timezone.utc)):
            report = L.reserve_budget(self.db, {"daily_budget": 10.0}, 0.1, "task-now", ops.budget_check, now=now)
            self.assertIn("reservation_id", report)

    def test_timestamps_keep_microseconds_so_text_order_is_time_order(self):
        from datetime import datetime, timezone
        self.assertEqual(L._iso(datetime(2026, 9, 22, 18, 52, 5, tzinfo=timezone.utc)),
                         "2026-09-22T18:52:05.000000Z")
        self.assertLess(L._iso(datetime(2026, 9, 22, 18, 52, 5, tzinfo=timezone.utc)),
                        L._iso(datetime(2026, 9, 22, 18, 52, 5, 625163, tzinfo=timezone.utc)))

    def test_concurrent_processes_admit_exactly_one(self):
        import subprocess
        import sys
        script = (
            "import importlib.util, sys, time\n"
            "spec = importlib.util.spec_from_file_location('l', sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "def check(policy, records, estimate, premium=False, task_id=None, now=None):\n"
            "    time.sleep(0.5)\n"  # widen the race window inside the transaction
            "    total = sum(r['cost'] for r in records) + estimate\n"
            "    if total > 1.0: raise ValueError('exceeded')\n"
            "    return {'allowed': True}\n"
            "try:\n"
            "    m.reserve_budget(sys.argv[2], {}, 0.7, sys.argv[3], check); print('ADMITTED')\n"
            "except ValueError as e: print('REFUSED', e)\n")
        procs = [subprocess.Popen([sys.executable, "-c", script, str(SCRIPT), str(self.db), name],
                                  stdout=subprocess.PIPE, text=True) for name in ("s1", "s2")]
        outputs = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(sorted(o.split()[0] for o in outputs), ["ADMITTED", "REFUSED"], outputs)


class ContinuousLearningTests(unittest.TestCase):
    """Outcome fields, performance matrix, KPI and recommend stages (Bundle B)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "learn.db"

    def outcome(self, **overrides):
        event = {
            "runtime": "codex",
            "provider": "openai",
            "model": "model-x",
            "role": "executor",
            "tier": "T2",
            "risk": "low",
            "worker_exit": 0,
            "test_pass": True,
            "task_id": "t-1",
            "project": "proj-a",
            # The matrix and recommend count verified outcomes only.
            "verification_status": "verified",
        }
        event.update(overrides)
        return event

    def add_outcome(self, created_at=None, **overrides):
        """Record an outcome and, if given, pin its created_at for determinism."""
        result = L.record_outcome(self.db, self.outcome(**overrides))
        if created_at is not None:
            conn = sqlite3.connect(str(self.db))
            try:
                conn.execute("UPDATE outcomes SET created_at = ? WHERE id = ?",
                             (created_at, result["outcome_id"]))
                conn.commit()
            finally:
                conn.close()
        return result["outcome_id"]

    # --- schema / record_outcome ---

    def test_backward_compatible_migration_adds_new_columns(self):
        legacy = pathlib.Path(self.temp.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.execute("CREATE TABLE outcomes (id INTEGER PRIMARY KEY, runtime TEXT, provider TEXT,"
                     " model TEXT, role TEXT, tier TEXT, risk TEXT, success INTEGER NOT NULL,"
                     " worker_exit INTEGER, test_pass INTEGER, error TEXT, cost_class TEXT,"
                     " cost REAL, input_tokens INTEGER, output_tokens INTEGER, task_id TEXT,"
                     " project TEXT, created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO outcomes VALUES (1, 'r', 'p', 'm', 'executor', 'T1', 'LOW',"
                     " 1, 0, 1, NULL, NULL, 1, 1, 1, 't', 'proj', '2026-01-01T00:00:00Z')")
        conn.commit()
        conn.close()
        # No error means migration ran; new columns are present and nullable.
        row = L.list_outcomes(legacy)[0]
        self.assertEqual(row["model"], "m")
        self.assertIsNone(row["domain"])
        self.assertIsNone(row["failure_type"])
        for col in ("escalated", "review_findings", "compactions",
                    "compaction_recovered", "routing_appropriate"):
            self.assertIsNone(row[col], col)

    def test_record_outcome_stores_all_new_fields(self):
        oid = L.record_outcome(self.db, self.outcome(
            domain="ENGINEERING+RESEARCH",
            task_type="bugfix",
            capabilities={"coding": 0.9, "testing": 0.4},
            failure_type="test_failure",
            escalated=True,
            review_findings=3,
            user_acceptance="rejected",
            duration_s=12.5,
            exploration=True,
            premium_planning_tokens=10,
            premium_execution_tokens=20,
            premium_review_tokens=30,
            open_tokens=40,
            compactions=2,
            compaction_recovered=1,
            routing_appropriate=False,
        ))
        self.assertIsInstance(oid["outcome_id"], int)
        row = L.list_outcomes(self.db)[0]
        self.assertEqual(row["domain"], "ENGINEERING+RESEARCH")
        self.assertEqual(row["task_type"], "bugfix")
        self.assertEqual(json.loads(row["capabilities"]), {"coding": 0.9, "testing": 0.4})
        self.assertEqual(row["failure_type"], "test_failure")
        self.assertEqual(row["escalated"], 1)
        self.assertEqual(row["review_findings"], 3)
        self.assertEqual(row["user_acceptance"], "rejected")
        self.assertEqual(row["duration_s"], 12.5)
        self.assertEqual(row["exploration"], 1)
        self.assertEqual(row["premium_planning_tokens"], 10)
        self.assertEqual(row["premium_execution_tokens"], 20)
        self.assertEqual(row["premium_review_tokens"], 30)
        self.assertEqual(row["open_tokens"], 40)
        self.assertEqual(row["compactions"], 2)
        self.assertEqual(row["compaction_recovered"], 1)
        self.assertEqual(row["routing_appropriate"], 0)
        # failure_type forces success off.
        self.assertFalse(row["success"])

    def test_capabilities_stored_sorted(self):
        L.record_outcome(self.db, self.outcome(capabilities={"z": 0.5, "a": 0.9}))
        raw = L.list_outcomes(self.db)[0]["capabilities"]
        self.assertEqual(raw, json.dumps({"a": 0.9, "z": 0.5}, sort_keys=True))

    def test_new_field_validation_errors(self):
        cases = [
            {"domain": "MAGIC"},                       # not a known domain
            {"domain": "ENGINEERING+MAGIC"},           # one bad among good
            {"task_type": "x" * 41},                   # > 40 chars
            {"capabilities": "not-a-dict"},
            {"capabilities": {"coding": 1.5}},         # out of [0,1]
            {"capabilities": {"coding": float("nan")}},
            {"capabilities": {"coding": True}},        # bool is not a float score
            {"failure_type": "not_a_failure_type"},
            {"escalated": "yes"},
            {"exploration": "yes"},
            {"routing_appropriate": "yes"},
            {"review_findings": -1},
            {"review_findings": True},
            {"user_acceptance": "maybe"},
            {"duration_s": -1},
            {"duration_s": float("inf")},
            {"duration_s": True},
            {"premium_planning_tokens": -2},
            {"premium_planning_tokens": True},
            {"compactions": -1},
            {"compaction_recovered": True},
        ]
        for override in cases:
            with self.assertRaises(ValueError, msg=override):
                L.record_outcome(self.db, self.outcome(**override))

    # --- attribution / score ---

    def test_context_failure_does_not_lower_model_score(self):
        self.add_outcome(created_at="2026-09-23T00:00:00Z",
                         model="m", task_id="t1", worker_exit=0, test_pass=True)
        self.add_outcome(created_at="2026-09-23T00:00:00Z",
                         model="m", task_id="t2", worker_exit=1, test_pass=False,
                         failure_type="context_failure")
        result = L.matrix(self.db, now="2026-09-23T00:00:00Z")
        entry = result["models"]["m"]
        # The aos-attributed failure never counts for/against the score.
        self.assertEqual(entry["overall"]["sample_size"], 1)
        self.assertAlmostEqual(entry["overall"]["score"], 1.0)
        # ...but it is still tallied in failure_types.
        self.assertEqual(entry["failure_types"], {"context_failure": 1})

    def test_rejected_user_acceptance_counts_as_failure(self):
        self.add_outcome(created_at="2026-09-23T00:00:00Z",
                         worker_exit=0, test_pass=True, user_acceptance="rejected")
        result = L.matrix(self.db, now="2026-09-23T00:00:00Z")
        entry = result["models"]["model-x"]
        self.assertEqual(entry["overall"]["sample_size"], 1)
        self.assertAlmostEqual(entry["overall"]["score"], 0.0)

    def test_decay_old_failure_weighs_less_than_recent_success(self):
        self.add_outcome(created_at="2026-01-01T00:00:00Z", worker_exit=1, test_pass=False)
        self.add_outcome(created_at="2026-09-23T00:00:00Z", worker_exit=0, test_pass=True, task_id="t2")
        result = L.matrix(self.db, now="2026-09-23T00:00:00Z")
        entry = result["models"]["model-x"]
        self.assertEqual(entry["overall"]["sample_size"], 2)
        self.assertGreater(entry["overall"]["score"], 0.9)

    def test_confidence_grows_with_samples(self):
        self.add_outcome(created_at="2026-09-23T00:00:00Z", model="one", worker_exit=0, test_pass=True)
        small = L.matrix(self.db, now="2026-09-23T00:00:00Z")["models"]["one"]["overall"]["confidence"]
        for i in range(30):
            self.add_outcome(created_at="2026-09-23T00:00:00Z", model="many",
                             task_id="t-%d" % i, worker_exit=0, test_pass=True)
        large = L.matrix(self.db, now="2026-09-23T00:00:00Z")["models"]["many"]["overall"]["confidence"]
        self.assertGreater(large, small)

    def test_one_high_score_has_lower_confidence_than_thirty_lower_scores(self):
        self.add_outcome(created_at="2026-09-23T00:00:00Z", model="perfect", worker_exit=0, test_pass=True)
        # 30 samples, 28 success + 2 failures -> score 0.9333, confidence 30/35.
        for i in range(28):
            self.add_outcome(created_at="2026-09-23T00:00:00Z", model="bulk",
                             task_id="s%d" % i, worker_exit=0, test_pass=True)
        for i in range(2):
            self.add_outcome(created_at="2026-09-23T00:00:00Z", model="bulk",
                             task_id="f%d" % i, worker_exit=1, test_pass=False)
        result = L.matrix(self.db, now="2026-09-23T00:00:00Z")
        perfect = result["models"]["perfect"]["overall"]
        bulk = result["models"]["bulk"]["overall"]
        self.assertGreater(perfect["score"], bulk["score"])
        self.assertLess(perfect["confidence"], bulk["confidence"])

    def test_drift_to_watch_not_demote(self):
        # 10 historical mostly-failing tries (rate 0.2), then 10 recent all-fail
        # (rate 0.0). With recent_window=10 the last 10 are "recent" and the
        # first 10 "historical". A 0.15 delta marks drift; a single bad streak
        # must yield WATCH, never DEMOTE (even though the unweighted score is
        # below demote_below).
        for i in range(2):
            self.add_outcome(created_at="2026-09-01T00:00:00Z", worker_exit=0, test_pass=True, task_id="h%d" % i)
        for i in range(8):
            self.add_outcome(created_at="2026-09-01T00:00:00Z", worker_exit=1, test_pass=False, task_id="hf%d" % i)
        for i in range(10):
            self.add_outcome(created_at="2026-09-23T00:00:00Z", worker_exit=1, test_pass=False, task_id="r%d" % i)
        result = L.matrix(self.db, now="2026-09-23T00:00:00Z",
                          min_observations=10, drift_delta=0.15, recent_window=10)
        entry = result["models"]["model-x"]
        self.assertTrue(entry["drift"])
        self.assertEqual(entry["status"], "WATCH")

    def test_promote_and_demote_thresholds(self):
        for i in range(30):
            self.add_outcome(created_at="2026-09-23T00:00:00Z", model="good",
                             task_id="g%d" % i, worker_exit=0, test_pass=True)
        for i in range(30):
            self.add_outcome(created_at="2026-09-23T00:00:00Z", model="bad",
                             task_id="b%d" % i, worker_exit=1, test_pass=False)
        result = L.matrix(self.db, now="2026-09-23T00:00:00Z")
        self.assertEqual(result["models"]["good"]["status"], "PROMOTE")
        self.assertEqual(result["models"]["bad"]["status"], "DEMOTE")

    def test_new_model_status_below_three_samples(self):
        self.add_outcome(created_at="2026-09-23T00:00:00Z", worker_exit=0, test_pass=True)
        result = L.matrix(self.db, now="2026-09-23T00:00:00Z")
        self.assertEqual(result["models"]["model-x"]["status"], "NEW")

    # --- kpi ---

    def test_kpi_nulls_when_no_data(self):
        result = L.kpi(self.db)
        self.assertEqual(result["tasks"], 0)
        self.assertEqual(result["attempts"], 0)
        for key in ("first_pass_success_rate", "verified_success_rate", "retry_rate",
                    "escalation_rate", "review_findings_rate", "cost_per_verified_task",
                    "tokens_per_verified_task", "premium_dependency_ratio",
                    "worker_success_rate", "compaction_rate", "compaction_recovery_rate",
                    "routing_accuracy", "user_acceptance"):
            self.assertIsNone(result[key], key)

    def test_kpi_exact_values_on_crafted_dataset(self):
        # Task t1: single verified success.
        self.add_outcome(task_id="t1", worker_exit=0, test_pass=True,
                         verification_status="verified", cost=10.0, cost_class="CHEAP",
                         premium_planning_tokens=60, open_tokens=40,
                         routing_appropriate=1, user_acceptance="accepted")
        # Task t2: failed first attempt then a verified retry.
        self.add_outcome(task_id="t2", worker_exit=1, test_pass=False, cost=5.0,
                         cost_class="CHEAP")
        self.add_outcome(task_id="t2", worker_exit=0, test_pass=True, retry=1,
                         verification_status="verified", cost=15.0, cost_class="CHEAP",
                         premium_execution_tokens=20, open_tokens=80,
                         routing_appropriate=0, user_acceptance="rejected")
        result = L.kpi(self.db)
        self.assertEqual(result["tasks"], 2)
        self.assertEqual(result["attempts"], 3)
        self.assertAlmostEqual(result["first_pass_success_rate"], 0.5)
        self.assertAlmostEqual(result["verified_success_rate"], 1.0)
        self.assertAlmostEqual(result["retry_rate"], 0.5)
        # The failed t2 attempt (5.0) is part of what the verified work cost: (10 + 5 + 15) / 2.
        self.assertAlmostEqual(result["cost_per_verified_task"], 15.0)
        self.assertAlmostEqual(result["tokens_per_verified_task"], 100.0)
        self.assertAlmostEqual(result["premium_dependency_ratio"], 0.4)
        self.assertAlmostEqual(result["premium_leverage"]["planning"], 30.0)
        self.assertAlmostEqual(result["premium_leverage"]["execution"], 10.0)
        self.assertAlmostEqual(result["premium_leverage"]["review"], 0.0)
        self.assertAlmostEqual(result["worker_success_rate"], 2 / 3)
        self.assertAlmostEqual(result["routing_accuracy"], 0.5)
        self.assertAlmostEqual(result["user_acceptance"], 0.5)

    def test_premium_dependency_ratio(self):
        # No open tokens at all: ratio = premium / premium -> 1.0.
        self.add_outcome(task_id="t1", worker_exit=0, test_pass=True,
                         verification_status="verified", premium_planning_tokens=50)
        result = L.kpi(self.db)
        self.assertAlmostEqual(result["premium_dependency_ratio"], 1.0)
        # No tokens at all -> null, never division by zero.
        empty = L.kpi(pathlib.Path(self.temp.name) / "empty.db")
        self.assertIsNone(empty["premium_dependency_ratio"])

    # --- recommend ---

    def _rec_items(self, **kwargs):
        return L.recommend(self.db, **kwargs)["items"]

    def test_recommend_stages(self):
        # 1 failure -> observation.
        self.add_outcome(model="obs", domain="ENGINEERING", task_id="o1",
                         worker_exit=1, test_pass=False)
        # 3 of 3 -> hypothesis.
        for i in range(3):
            self.add_outcome(model="hyp", domain="ENGINEERING", task_id="h%d" % i,
                             worker_exit=1, test_pass=False)
        # 5 of 6 -> routing_recommendation.
        for i in range(5):
            self.add_outcome(model="route", domain="LEGAL_COMPLIANCE", task_id="r%d" % i,
                             worker_exit=1, test_pass=False)
        self.add_outcome(model="route", domain="LEGAL_COMPLIANCE", task_id="r-ok",
                         worker_exit=0, test_pass=True)
        # 2 of 6 -> omitted (under threshold with >= 2 attempts).
        for i in range(2):
            self.add_outcome(model="omit", domain="RESEARCH", task_id="m%d" % i,
                             worker_exit=1, test_pass=False)
        for i in range(4):
            self.add_outcome(model="omit", domain="RESEARCH", task_id="m-ok%d" % i,
                             worker_exit=0, test_pass=True)

        result = L.recommend(self.db)
        self.assertFalse(result["applied"])

        by_stage = {}
        for item in result["items"]:
            by_stage.setdefault(item["stage"], []).append(item)

        obs = [i for i in by_stage.get("observation", []) if i["model"] == "obs"]
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["attempts"], 1)
        self.assertEqual(obs[0]["failures"], 1)

        hyp = [i for i in by_stage.get("hypothesis", []) if i["model"] == "hyp"]
        self.assertEqual(len(hyp), 1)
        self.assertEqual(hyp[0]["stage"], "hypothesis")
        self.assertEqual((hyp[0]["attempts"], hyp[0]["failures"]), (3, 3))

        route = [i for i in by_stage.get("routing_recommendation", []) if i["model"] == "route"]
        self.assertEqual(len(route), 1)
        self.assertEqual((route[0]["attempts"], route[0]["failures"]), (6, 5))
        self.assertEqual(route[0]["recommendation"], "deprioritize route for LEGAL_COMPLIANCE")

        # A single observation must never be a routing_recommendation.
        self.assertTrue(all(i["stage"] != "routing_recommendation" or i["attempts"] >= 5
                            for i in result["items"]))
        # The omitted group produced nothing.
        self.assertFalse(any(i["model"] == "omit" for i in result["items"]))


class TaskOutcomeTests(unittest.TestCase):
    """The pipeline's stored task verdicts override attempt-order inference."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = pathlib.Path(self.temp.name) / "learn.db"

    def task(self, **overrides):
        event = {
            "project": "proj-a",
            "task_id": "t-1",
            "outcome": "verified",
            "first_pass": True,
            "failed_attempts": 0,
            "escalated": False,
            "review_rounds": 1,
            "findings_confirmed": 0,
            "findings_refuted": 0,
            "bundles": 1,
            "domain": "ENGINEERING",
            "task_type": "bugfix",
        }
        event.update(overrides)
        return event

    def outcome(self, **overrides):
        event = {
            "runtime": "codex",
            "provider": "openai",
            "model": "model-x",
            "role": "executor",
            "tier": "T2",
            "risk": "low",
            "worker_exit": 0,
            "test_pass": True,
            "task_id": "t-1",
            "project": "proj-a",
            "verification_status": "verified",
        }
        event.update(overrides)
        return event

    def test_record_task_rejects_invalid_fields(self):
        bad_values = [
            {"outcome": "pass"},                    # not a terminal verdict
            {"failed_attempts": True},              # bool is not a counter
            {"failed_attempts": -1},                # negative counter
            {"bundles": "1"},                       # string counter
            {"first_pass": 1},                      # bool required, not int
            {"escalated": 1},
            {"domain": "MAGIC"},                    # same rule as record_outcome
            {"project": ""},
            {"task_id": "   "},
        ]
        for override in bad_values:
            with self.assertRaises(ValueError, msg=override):
                L.record_task(self.db, self.task(**override))

    def test_first_pass_true_is_refused_with_failures(self):
        for override in ({"failed_attempts": 1},
                         {"escalated": True},
                         {"findings_confirmed": 1},
                         {"outcome": "blocked"}):
            with self.assertRaises(ValueError, msg=override):
                L.record_task(self.db, self.task(first_pass=True, **override))
        # And a clean verified verdict accepts first_pass True.
        stored = L.record_task(self.db, self.task())
        self.assertEqual(stored["outcome"], "verified")
        self.assertEqual(stored["created_at"], L.list_tasks(self.db)[0]["created_at"])

    def test_upsert_blocked_then_verified_keeps_one_row(self):
        blocked = L.record_task(self.db, self.task(outcome="blocked", first_pass=False))
        verified = L.record_task(self.db, self.task(outcome="verified", first_pass=True))
        rows = L.list_tasks(self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "verified")
        self.assertEqual(rows[0]["first_pass"], 1)
        # The latest terminal state wins on the same row id.
        self.assertEqual(verified["id"], blocked["id"])

    def test_kpi_uses_task_row_over_contradictory_attempt_rows(self):
        # Outcome rows that would infer a retry (fail, then succeed).
        L.record_outcome(self.db, self.outcome(worker_exit=1, test_pass=False))
        L.record_outcome(self.db, self.outcome(retry=1))
        result = L.kpi(self.db)
        self.assertEqual(result["task_records"], 0)
        self.assertEqual(result["first_pass_success_rate"], 0.0)
        self.assertEqual(result["retry_rate"], 1.0)
        # The pipeline's stored verdict says it was a clean first pass.
        L.record_task(self.db, self.task())
        result = L.kpi(self.db)
        self.assertEqual(result["task_records"], 1)
        self.assertEqual(result["first_pass_success_rate"], 1.0)
        self.assertEqual(result["retry_rate"], 0.0)
        self.assertEqual(result["verified_success_rate"], 1.0)

    def test_task_without_task_row_keeps_inference(self):
        L.record_outcome(self.db, self.outcome(worker_exit=1, test_pass=False))
        L.record_outcome(self.db, self.outcome(retry=1))
        result = L.kpi(self.db)
        self.assertEqual(result["task_records"], 0)
        self.assertEqual(result["first_pass_success_rate"], 0.0)
        self.assertEqual(result["retry_rate"], 1.0)

    def test_task_row_without_outcome_rows_still_counts_as_task(self):
        L.record_task(self.db, self.task())
        result = L.kpi(self.db)
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(result["task_records"], 1)
        self.assertEqual(result["first_pass_success_rate"], 1.0)
        self.assertEqual(result["verified_success_rate"], 1.0)

    def test_task_rows_respect_project_filter(self):
        L.record_task(self.db, self.task(project="p-a", task_id="a"))
        L.record_task(self.db, self.task(project="p-b", task_id="b"))
        self.assertEqual([r["task_id"] for r in L.list_tasks(self.db, project="p-a")], ["a"])
        result = L.kpi(self.db, project="p-a")
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["task_records"], 1)
        self.assertEqual(result["first_pass_success_rate"], 1.0)

    def test_task_rows_respect_since_filter(self):
        L.record_task(self.db, self.task(task_id="old"))
        time_module.sleep(0.02)
        new = L.record_task(self.db, self.task(task_id="new"))
        result = L.kpi(self.db, since=new["created_at"])
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["task_records"], 1)
        rows = L.list_tasks(self.db)
        self.assertEqual({r["task_id"] for r in rows}, {"old", "new"})

    def test_list_tasks_preserves_stored_fields(self):
        L.record_task(self.db, self.task(first_pass=False, domain="ENGINEERING+RESEARCH",
                                         review_rounds=3, findings_confirmed=2,
                                         findings_refuted=1, bundles=4))
        row = L.list_tasks(self.db)[0]
        self.assertEqual(row["domain"], "ENGINEERING+RESEARCH")
        self.assertEqual((row["review_rounds"], row["findings_confirmed"],
                          row["findings_refuted"], row["bundles"]), (3, 2, 1, 4))


class LearningCliTests(unittest.TestCase):
    """CLI subcommands print JSON and exit 0."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(pathlib.Path(self.temp.name) / "learn.db")
        self.event_file = pathlib.Path(self.temp.name) / "event.json"

    def run_cli(self, *argv):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = L.main(list(argv))
        return code, buf.getvalue()

    def test_record_outcome_cli_from_file_and_stdin(self):
        import contextlib
        import io
        event = {"runtime": "codex", "provider": "openai", "model": "m",
                 "role": "executor", "tier": "T2", "risk": "low",
                 "worker_exit": 0, "test_pass": True, "task_id": "t", "project": "p",
                 "domain": "ENGINEERING", "task_type": "fix"}
        self.event_file.write_text(json.dumps(event))
        code, out = self.run_cli("record-outcome", "--database", self.db, "--event", str(self.event_file))
        self.assertEqual(code, 0)
        self.assertIn("outcome_id", json.loads(out))
        # stdin path (feed a controllable stdin stream).
        with mock.patch("sys.stdin", io.StringIO(json.dumps(event))):
            code, out = self.run_cli("record-outcome", "--database", self.db, "--event", "-")
        self.assertEqual(code, 0)
        self.assertIn("outcome_id", json.loads(out))

    def test_matrix_model_status_kpi_recommend_cli_exit_zero(self):
        self.event_file.write_text(json.dumps({
            "runtime": "codex", "provider": "openai", "model": "m", "role": "executor",
            "tier": "T2", "risk": "low", "worker_exit": 0, "test_pass": True,
            "task_id": "t", "project": "p"}))
        self.run_cli("record-outcome", "--database", self.db, "--event", str(self.event_file))
        for argv in (("matrix",), ("model-status",), ("kpi",), ("recommend",)):
            code, out = self.run_cli(argv[0], "--database", self.db)
            self.assertEqual(code, 0, argv)
            self.assertIsInstance(json.loads(out), dict, argv)

    def test_matrix_cli_writes_output_file(self):
        self.run_cli("matrix", "--database", self.db, "--now", "2026-09-23T00:00:00Z",
                     "--output", str(pathlib.Path(self.temp.name) / "matrix.json"))
        data = json.loads((pathlib.Path(self.temp.name) / "matrix.json").read_text())
        self.assertIn("models", data)

    def test_record_task_and_tasks_cli(self):
        import contextlib
        import io
        task = {"project": "p", "task_id": "t", "outcome": "verified", "first_pass": True,
                "failed_attempts": 0, "escalated": False, "review_rounds": 1,
                "findings_confirmed": 0, "findings_refuted": 0, "bundles": 1,
                "domain": "ENGINEERING", "task_type": "fix"}
        self.event_file.write_text(json.dumps(task))
        code, out = self.run_cli("record-task", "--database", self.db, "--event", str(self.event_file))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["outcome"], "verified")
        # stdin path when no --event is given.
        with mock.patch("sys.stdin", io.StringIO(json.dumps(task))):
            code, out = self.run_cli("record-task", "--database", self.db)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["outcome"], "verified")
        code, out = self.run_cli("tasks", "--database", self.db, "--project", "p")
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], "t")


class EventKeyTests(unittest.TestCase):
    """A pipeline event is stored once, however often a step is retried."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        self.db = str(pathlib.Path(directory) / "ledger.sqlite3")

    def test_same_event_key_is_written_once(self):
        event = {"model": "m", "role": "executor", "worker_exit": 0, "test_pass": True,
                 "task_id": "t", "project": "p", "event_key": "t#1", "cost": 1.0}
        first = L.record_outcome(self.db, event)
        second = L.record_outcome(self.db, dict(event))
        self.assertEqual(first["outcome_id"], second["outcome_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(L.list_outcomes(self.db)), 1)
        L.record_outcome(self.db, dict(event, event_key="t#2"))
        L.record_outcome(self.db, dict(event, event_key=None))
        self.assertEqual(len(L.list_outcomes(self.db)), 3)

    def test_duplicate_write_settles_its_reservation(self):
        conn = sqlite3.connect(self.db)
        L._ensure_schema(conn)
        conn.execute("INSERT INTO reservations (task_id, estimate, premium, created_at) VALUES ('t', 1.0, 0, 'x')")
        conn.commit()
        conn.close()
        event = {"model": "m", "worker_exit": 0, "task_id": "t", "project": "p", "event_key": "k"}
        L.record_outcome(self.db, event)
        L.record_outcome(self.db, dict(event, reservation_id=1))
        conn = sqlite3.connect(self.db)
        self.assertIsNotNone(conn.execute("SELECT settled_at FROM reservations WHERE id = 1").fetchone()[0])
        conn.close()

    def test_invalid_event_key_is_refused(self):
        for key in ("", "x" * 201, 5):
            with self.assertRaises(ValueError):
                L.record_outcome(self.db, {"model": "m", "worker_exit": 0, "event_key": key})


class VerifiedOnlyTests(unittest.TestCase):
    """Round 1 of the 3.0 review: unverified outcomes promoted a model."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        self.db = str(pathlib.Path(directory) / "ledger.sqlite3")

    def record(self, n, status, success=True):
        for i in range(n):
            L.record_outcome(self.db, {"model": "m", "role": "executor", "worker_exit": 0 if success else 1,
                                       "test_pass": success, "task_id": "t%d" % i, "domain": "GENERAL",
                                       "task_type": "feature", "verification_status": status})

    def test_unverified_outcomes_never_move_a_model(self):
        self.record(25, "not_scored")
        self.record(25, "pending", success=False)
        self.assertNotIn("m", L.matrix(self.db)["models"])
        self.assertEqual(L.recommend(self.db)["items"], [])

    def test_the_matrix_reports_task_type_performance(self):
        self.record(4, "verified")
        entry = L.matrix(self.db)["models"]["m"]
        self.assertEqual(entry["task_types"]["feature"]["sample_size"], 4)

    def test_taxonomy_and_thresholds_come_from_the_router_config(self):
        adaptive = json.loads((SCRIPT.parents[1] / "config/adaptive.json").read_text())
        self.assertEqual(set(L.FAILURE_TYPES), set(adaptive["failure_types"]))
        self.assertEqual(set(L.DOMAINS), set(adaptive["domains"]))
        self.assertEqual(L.LEARNING, adaptive["learning"])

    def test_tokens_per_verified_task_counts_every_class_and_null_without_data(self):
        L.record_outcome(self.db, {"model": "m", "role": "executor", "worker_exit": 0, "test_pass": True,
                                   "task_id": "t", "cost_class": "MID", "verification_status": "verified",
                                   "input_tokens": 1000, "output_tokens": 500})
        self.assertEqual(L.kpi(self.db)["tokens_per_verified_task"], 1500)
        other = self.db + ".empty"
        L.record_outcome(other, {"model": "m", "role": "executor", "worker_exit": 0, "test_pass": True,
                                 "task_id": "t", "verification_status": "verified"})
        self.assertIsNone(L.kpi(other)["tokens_per_verified_task"])


class ReviewRoundTwoLearningTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        self.db = str(pathlib.Path(directory) / "ledger.sqlite3")

    def record(self, database, **extra):
        event = {"model": "m", "role": "executor", "worker_exit": 0, "test_pass": True,
                 "task_id": "t", "verification_status": "verified"}
        event.update(extra)
        L.record_outcome(database, event)

    def test_retries_escalation_and_findings_lower_the_credit_of_a_success(self):
        clean, messy = self.db, self.db + ".messy"
        self.record(clean)
        self.record(messy, retry=2, escalated=True, review_findings=3)
        self.assertEqual(L.matrix(clean)["models"]["m"]["overall"]["score"], 1.0)
        self.assertLess(L.matrix(messy)["models"]["m"]["overall"]["score"], 0.5)

    def test_a_review_row_is_not_a_retry(self):
        self.record(self.db)
        self.record(self.db, role="reviewer")
        self.assertEqual(L.kpi(self.db)["retry_rate"], 0.0)
        # An attempt after a failed one is a retry (round 3: a second passing bundle is not).
        self.record(self.db, worker_exit=1, test_pass=False)
        self.record(self.db)
        self.assertEqual(L.kpi(self.db)["retry_rate"], 1.0)


class ReviewRoundThreeLearningTests(unittest.TestCase):
    """Round 3: roles, attempts and the task's final outcome are different things."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        self.db = str(pathlib.Path(directory) / "ledger.sqlite3")

    def record(self, **extra):
        event = {"model": "m", "role": "executor", "worker_exit": 0, "test_pass": True,
                 "task_id": "t", "verification_status": "verified"}
        event.update(extra)
        if extra.get("success") is False:
            event.pop("success")
            event.update(worker_exit=1, test_pass=False)
        L.record_outcome(self.db, event)

    def test_a_successful_plan_does_not_verify_a_failed_execution(self):
        self.record(role="planner")
        self.record(success=False)
        result = L.kpi(self.db)
        self.assertEqual((result["first_pass_success_rate"], result["verified_success_rate"]), (0.0, 0.0))

    def test_a_failed_final_review_fails_the_task(self):
        self.record()
        self.record(role="reviewer", success=False)
        self.assertEqual(L.kpi(self.db)["verified_success_rate"], 0.0)

    def test_mixed_token_reporting_adds_up_per_row(self):
        self.record(input_tokens=100, output_tokens=50)
        self.record(role="reviewer", premium_review_tokens=1000)
        self.assertEqual(L.kpi(self.db)["tokens_per_verified_task"], 1150)

    def test_two_bundles_passing_first_time_are_not_retries(self):
        self.record(domain="LEGAL_COMPLIANCE")
        self.record(domain="ENGINEERING")
        self.assertEqual(L.kpi(self.db)["retry_rate"], 0.0)
        self.record(success=False)
        self.record()
        self.assertEqual(L.kpi(self.db)["retry_rate"], 1.0)


class ReviewRoundFourLearningTests(unittest.TestCase):
    """Round 4: a bundle id separates deliverables from retries; reviews must be verified."""

    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        self.db = str(pathlib.Path(directory) / "ledger.sqlite3")

    def record(self, **extra):
        event = {"model": "m", "role": "executor", "worker_exit": 0, "test_pass": True,
                 "task_id": "t", "verification_status": "verified"}
        event.update(extra)
        L.record_outcome(self.db, event)

    def test_a_passing_bundle_does_not_hide_a_failed_one(self):
        self.record(bundle="requirements", worker_exit=1, test_pass=False)
        self.record(bundle="implementation")
        result = L.kpi(self.db)
        self.assertEqual((result["verified_success_rate"], result["retry_rate"]), (0.0, 0.0))

    def test_a_bundle_fixed_on_retry_verifies_the_task(self):
        self.record(bundle="requirements", worker_exit=1, test_pass=False)
        self.record(bundle="requirements", retry=1)
        self.record(bundle="implementation")
        result = L.kpi(self.db)
        self.assertEqual((result["verified_success_rate"], result["retry_rate"],
                          result["first_pass_success_rate"]), (1.0, 1.0, 0.0))

    def test_an_unverified_review_does_not_verify_the_task(self):
        for status in ("pending", "not_scored"):
            with self.subTest(status=status):
                database = self.db + status
                for event in ({}, {"role": "reviewer", "verification_status": status}):
                    L.record_outcome(database, dict({"model": "m", "role": "executor", "worker_exit": 0,
                                                     "test_pass": True, "task_id": "t",
                                                     "verification_status": "verified"}, **event))
                self.assertEqual(L.kpi(database)["verified_success_rate"], 0.0)

    def test_bundle_is_validated(self):
        for bad in ("", "x" * 41, 7):
            with self.assertRaises(ValueError):
                self.record(bundle=bad)


class ReviewRoundFiveLearningTests(unittest.TestCase):
    """Round 5: a review belongs to its bundle; a later bundle does not erase it."""

    def test_a_later_bundle_does_not_hide_a_failed_or_unverified_review(self):
        for status in ("verified", "pending", "not_scored"):
            with self.subTest(status=status):
                directory = tempfile.mkdtemp()
                self.addCleanup(lambda d=directory: __import__("shutil").rmtree(d, ignore_errors=True))
                database = str(pathlib.Path(directory) / "ledger.sqlite3")
                for role, bundle, passed, verification in (("executor", "A", True, "verified"),
                                                            ("reviewer", "A", False, status),
                                                            ("executor", "B", True, "verified")):
                    L.record_outcome(database, {"task_id": "t", "model": "m", "role": role, "bundle": bundle,
                                                "worker_exit": 0 if passed else 1, "test_pass": passed,
                                                "verification_status": verification})
                self.assertEqual(L.kpi(database)["verified_success_rate"], 0.0)

    def test_a_fix_after_the_failed_review_of_its_bundle_can_verify_the_task(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        database = str(pathlib.Path(directory) / "ledger.sqlite3")
        for role, bundle, passed in (("executor", "A", True), ("reviewer", "A", False),
                                     ("fixer", "A", True), ("reviewer", "A", True), ("executor", "B", True)):
            L.record_outcome(database, {"task_id": "t", "model": "m", "role": role, "bundle": bundle,
                                        "worker_exit": 0 if passed else 1, "test_pass": passed,
                                        "verification_status": "verified"})
        self.assertEqual(L.kpi(database)["verified_success_rate"], 1.0)


class ReviewRoundSixLearningTests(unittest.TestCase):
    def test_the_same_task_id_in_two_projects_is_two_tasks(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        database = str(pathlib.Path(directory) / "ledger.sqlite3")
        for project, ok in (("project-a", False), ("project-b", True)):
            L.record_outcome(database, {"project": project, "task_id": "fix-login", "bundle": "main",
                                        "model": "anthropic/sonnet", "role": "executor",
                                        "worker_exit": 0 if ok else 1, "test_pass": ok,
                                        "verification_status": "verified"})
        k = L.kpi(database)
        self.assertEqual((k["tasks"], k["verified_success_rate"], k["retry_rate"]), (2, 0.5, 0.0))


if __name__ == "__main__":
    unittest.main()
