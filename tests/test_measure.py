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

    def test_pipeline_roles_are_imported_separately_and_unknowns_stay_null(self):
        self.start()
        packet = Path(self.temp.name) / 'pipeline.json'
        packet.write_text(json.dumps(dict(tier='T2', role_events=[
            dict(role='planner',model='p',provider='codex',tokens=10),
            dict(role='executor',model='o',provider='vercel',tokens=80),
            dict(role='reviewer',model='r',provider='claude',tokens=10)],
            planner='codex', reviewer='claude', premium_execution_used=False,
            premium_execution_reason=None, findings_total=1, findings_confirmed=0,
            findings_refuted=1, open_retry_count=0)))
        result = self.run_cli('finish','--outcome','delivered','--pipeline',str(packet))
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(self.record.read_text())
        self.assertEqual((data['planner_tokens'],data['executor_tokens'],data['reviewer_tokens']), (10,80,10))
        self.assertEqual(data['premium_dependency_ratio'], .2)
        self.assertEqual(data['workload_open_ratio'],1)
        self.assertTrue(data['cross_model_review'])
        self.assertIsNone(data['estimated_premium_tokens_saved'])


    def test_explicit_counters_survive_a_pipeline_with_unknown_tokens(self):
        self.start()
        packet = Path(self.temp.name) / 'pipeline.json'
        packet.write_text(json.dumps(dict(role_events=[dict(role='executor', model='o', tokens=None)])))
        result = self.run_cli('finish', '--outcome', 'delivered', '--open-executor-tokens', '100',
                              '--premium-executor-tokens', '300', '--pipeline', str(packet))
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.record.read_text())
        self.assertEqual((data['open_executor_tokens'], data['premium_executor_tokens']), (100, 300))
        self.assertEqual(data['workload_open_ratio'], .25)

    def test_premium_dependency_counts_premium_review(self):
        self.start()
        result = self.run_cli('finish', '--outcome', 'delivered', '--open-executor-tokens', '100',
                              '--premium-executor-tokens', '300', '--premium-review-tokens', '600',
                              '--planner-tokens', '0')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.record.read_text())
        self.assertEqual(data['workload_open_ratio'], .25)
        self.assertEqual(data['premium_dependency_ratio'], .9)

    def test_unknown_premium_review_keeps_dependency_null(self):
        self.start()
        result = self.run_cli('finish', '--outcome', 'delivered', '--open-executor-tokens', '100',
                              '--premium-executor-tokens', '0')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.record.read_text())
        self.assertEqual(data['workload_open_ratio'], 1)
        self.assertIsNone(data['premium_dependency_ratio'])

    def test_unreadable_pipeline_fails_before_the_lock(self):
        self.start()
        result = self.run_cli('finish', '--outcome', 'delivered', '--pipeline', '/nonexistent.json')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(Path(str(self.record) + '.lock').exists())
        self.assertIsNone(json.loads(self.record.read_text())['finished_at'])

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

    def test_start_names_delivered_records_nobody_judged(self):
        # A finished, unjudged sibling is named; a judged one, a blocked one, an open
        # one and a foreign JSON file are not. The new record is still created.
        folder = Path(self.temp.name)
        delivered = self.start_then_finish(folder / "2026-09-01-old.json", "delivered")
        partial = self.start_then_finish(folder / "2026-09-02-part.json", "partial")
        judged = self.start_then_finish(folder / "2026-09-03-judged.json", "delivered")
        subprocess.run([sys.executable, str(SCRIPT), "judge", "--verdict", "accepted", "--record", str(judged)],
                       capture_output=True, text=True, check=True)
        self.start_then_finish(folder / "2026-09-04-blocked.json", "blocked")
        subprocess.run([sys.executable, str(SCRIPT), "start", "--task", "t", "--runtime", "r", "--model", "m",
                        "--version", "1", "--record", str(folder / "2026-09-05-open.json")], check=True, capture_output=True)
        (folder / "notes.json").write_text('{"schema": 1, "outcome": "delivered"}')
        (folder / "broken.json").write_text("{")
        (folder / "shaped.json").write_text(json.dumps({"schema": 1, "task": "t", "runtime": "r", "model": "m",
                                                        "version": "1", "started_at": "2026-09-01T00:00:00Z",
                                                        "finished_at": True, "outcome": "delivered"}))
        result = self.run_cli("start", "--task", "Explicit task", "--runtime", "codex", "--model", "model-x", "--version", "1.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.record.exists())
        self.assertIn("AVVISO: record consegnati senza verdetto", result.stderr)
        # One runnable command per record: a copy-paste of the line must judge that file.
        for pending in (delivered, partial):
            self.assertIn(f"judge --record {pending} --verdict accepted|rejected", result.stderr)
        for absent in ("2026-09-03-judged.json", "2026-09-04-blocked.json", "2026-09-05-open.json",
                       "notes.json", "broken.json", "shaped.json"):
            self.assertNotIn(absent, result.stderr)

    def test_the_printed_judge_command_is_relative_to_the_cwd_when_it_can_be(self):
        folder = Path(self.temp.name).resolve() / "docs" / "misure"
        folder.mkdir(parents=True)
        self.start_then_finish(folder / "2026-09-01-old.json", "delivered")
        result = subprocess.run([sys.executable, str(SCRIPT), "start", "--task", "t", "--runtime", "r", "--model", "m",
                                 "--version", "1", "--record", "docs/misure/2026-09-02-new.json"],
                                capture_output=True, text=True, cwd=str(folder.parents[1]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("judge --record docs/misure/2026-09-01-old.json --verdict", result.stderr)

    def test_start_is_silent_when_every_sibling_is_judged_or_absent(self):
        folder = Path(self.temp.name)
        result = self.run_cli("start", "--task", "Explicit task", "--runtime", "codex", "--model", "model-x", "--version", "1.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("AVVISO", result.stderr)
        judged = self.start_then_finish(folder / "2026-09-03-judged.json", "delivered")
        subprocess.run([sys.executable, str(SCRIPT), "judge", "--verdict", "rejected", "--record", str(judged)],
                       capture_output=True, text=True, check=True)
        other = Path(self.temp.name) / "second.json"
        result = subprocess.run([sys.executable, str(SCRIPT), "start", "--task", "t", "--runtime", "r", "--model", "m",
                                 "--version", "1", "--record", str(other)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("AVVISO", result.stderr)

    def start_then_finish(self, path, outcome):
        base = [sys.executable, str(SCRIPT)]
        subprocess.run(base + ["start", "--task", "t", "--runtime", "r", "--model", "m", "--version", "1",
                               "--record", str(path)], check=True, capture_output=True)
        subprocess.run(base + ["finish", "--outcome", outcome, "--record", str(path)], check=True, capture_output=True)
        return path

    def test_start_does_not_overwrite(self):
        self.start()
        before = self.record.read_bytes()
        result = self.run_cli("start", "--task", "Other", "--runtime", "claude", "--model", "m", "--version", "v")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.record.read_bytes(), before)

    def test_finish_separates_provider_and_estimate(self):
        self.start()
        result = self.run_cli("finish", "--outcome", "delivered", "--corrections", "2", "--input-tokens", "100", "--cached-input-tokens", "20", "--output-tokens", "50", "--metric-source", "provider usage report", "--rtk-saved-estimate", "600", "--rtk-source", "rtk gain estimate")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.record.read_text())
        self.assertEqual(data["outcome"], "delivered")
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
                result = self.run_cli("finish", "--outcome", "delivered", *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.record.read_bytes(), before)

    def test_cannot_finish_twice(self):
        self.start()
        self.assertEqual(self.run_cli("finish", "--outcome", "partial").returncode, 0)
        before = self.record.read_bytes()
        self.assertNotEqual(self.run_cli("finish", "--outcome", "delivered").returncode, 0)
        self.assertEqual(self.record.read_bytes(), before)

    def test_concurrent_finish_preserves_first_completion(self):
        self.start()
        command = [sys.executable, str(SCRIPT), "finish", "--record", str(self.record), "--outcome"]
        first = subprocess.Popen([*command, "delivered"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen([*command, "partial"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first.communicate(timeout=10)
        second.communicate(timeout=10)
        self.assertEqual(sorted((first.returncode, second.returncode)), [0, 1])
        data = json.loads(self.record.read_text())
        self.assertEqual(data["outcome"], "delivered" if first.returncode == 0 else "partial")
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
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PRIVATE-CONTENT", result.stdout + result.stderr)

    def test_missing_or_invalid_record_fails_cleanly(self):
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.record.write_text("{}")
        before = self.record.read_bytes()
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.record.read_bytes(), before)

    def test_a_stale_lock_names_itself_and_the_remedy(self):
        # A finish killed between creating the lock and releasing it left every later
        # finish failing with a bare "FileExistsError" and no way to know what to remove.
        self.start()
        lock = self.record.with_name(self.record.name + ".lock")
        lock.write_text("")
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(lock), result.stderr)
        self.assertIn("rimuovi", result.stderr)
        self.assertIsNone(json.loads(self.record.read_text())["outcome"])

    def test_a_record_started_after_the_work_says_so(self):
        # The first real record measured 65 seconds because start ran after the work was
        # committed. Inside Git with a clean tree and no commit since start, finish must
        # say the elapsed time covers nothing — and still complete the record.
        repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        import os, time
        earlier = dict(os.environ, GIT_COMMITTER_DATE=f"@{int(time.time()) - 120} +0000")
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "before"], check=True, env=earlier)
        self.start()
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AVVISO", result.stderr)
        self.assertIs(json.loads(self.record.read_text())["work_observed_after_start"], False)

    def test_a_commit_in_the_same_second_as_start_is_unknown_not_absent(self):
        # Git dates commits to the second: a commit made after start within that second
        # compared as "not later" and was filed as no work at all.
        repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        data = self.start()
        import os
        from datetime import datetime
        second = int(datetime.fromisoformat(data["started_at"].replace("Z", "+00:00")).timestamp())
        same = dict(os.environ, GIT_COMMITTER_DATE=f"@{second} +0000")
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "same second"], check=True, env=same)
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(self.record.read_text())["work_observed_after_start"])

    def test_a_file_already_dirty_before_start_is_not_work_after_start(self):
        # git status says what is dirty, not since when: the first version read a file
        # left dirty before start as work done after it and silenced the warning.
        repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        stale = repo / "pre.txt"
        stale.write_text("before start")
        import os, time
        old = time.time() - 120
        os.utime(stale, (old, old))
        self.start()
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AVVISO", result.stderr)
        self.assertIs(json.loads(self.record.read_text())["work_observed_after_start"], False)

    def test_only_the_record_and_its_lock_are_excluded_not_every_path_sharing_the_prefix(self):
        # A glob on the record name also hid record.json.py, real work after start.
        repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.start()
        (repo / (self.record.name + ".py")).write_text("work")
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("AVVISO", result.stderr)
        self.assertIs(json.loads(self.record.read_text())["work_observed_after_start"], True)

    def test_an_untracked_directory_holding_the_record_is_not_work(self):
        # `git status` groups an untracked directory as one entry; judged by the
        # directory's mtime, which the lock itself updates, the record counted as work.
        repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.record = repo / "misure/r.json"
        self.start()
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(self.record.read_text())["work_observed_after_start"], False)

    def test_a_deletion_is_unknown_not_work_and_not_its_absence(self):
        # A deleted path has no mtime, and its directory's is moved by the lock.
        repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "old.txt").write_text("o")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"], check=True)
        (repo / "old.txt").unlink()
        self.start()
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(self.record.read_text())["work_observed_after_start"])

    def test_an_arrow_inside_a_file_name_and_a_real_rename_are_both_read_correctly(self):
        repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "dest.txt").write_text("d")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"], check=True)
        import os, time
        old = time.time() - 120
        os.utime(repo / "dest.txt", (old, old))
        self.start()
        (repo / "source -> dest.txt").write_text("work")
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(self.record.read_text())["work_observed_after_start"], True)
        # A rename keeps the file's mtime, and the index's mtime is moved by git status
        # itself: a rename of an old file is unknown, never asserted either way.
        (repo / "source -> dest.txt").unlink()
        self.record = repo / "r2.json"
        self.start()
        subprocess.run(["git", "-C", str(repo), "mv", "dest.txt", "moved.txt"], check=True)
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(self.record.read_text())["work_observed_after_start"])

    def test_work_after_start_is_seen_and_unknown_outside_git(self):
        repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.start()
        (repo / "work.txt").write_text("changed after start")
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("AVVISO", result.stderr)
        self.assertIs(json.loads(self.record.read_text())["work_observed_after_start"], True)
        # Outside Git nothing can be observed; the field is null, never a guess.
        with tempfile.TemporaryDirectory() as plain:
            record = Path(plain) / "r.json"
            subprocess.run([sys.executable, str(SCRIPT), "start", "--record", str(record), "--task", "t",
                            "--runtime", "claude", "--model", "m", "--version", "v"], check=True, capture_output=True)
            subprocess.run([sys.executable, str(SCRIPT), "finish", "--record", str(record), "--outcome", "delivered"],
                           check=True, capture_output=True)
            self.assertIsNone(json.loads(record.read_text())["work_observed_after_start"])

    def test_a_blocked_task_is_not_filed_as_partial(self):
        # A task stopped by a missing capability produced no deliverable to accept in
        # part. Without its own value the record would have to lie to close.
        self.start()
        result = self.run_cli("finish", "--outcome", "blocked")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.record.read_text())["outcome"], "blocked")

    def test_a_completion_word_is_not_an_outcome(self):
        # The report vocabulary and the acceptance axis answer different questions;
        # accepting "DONE" here would record the author's opinion as the user's verdict.
        self.start()
        before = self.record.read_bytes()
        result = self.run_cli("finish", "--outcome", "DONE")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.record.read_bytes(), before)

    def test_the_author_cannot_write_the_users_verdict_at_finish(self):
        # Both records before this said "accepted", written minutes before the user had
        # read the result. finish records what was delivered; the verdict is a later word.
        self.start()
        before = self.record.read_bytes()
        for verdict in ("accepted", "rejected"):
            self.assertNotEqual(self.run_cli("finish", "--outcome", verdict).returncode, 0)
        self.assertEqual(self.record.read_bytes(), before)
        result = self.run_cli("finish", "--outcome", "delivered")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.record.read_text())
        self.assertEqual(data["outcome"], "delivered")
        self.assertIsNone(data["judged_at"])

    def test_judge_records_the_verdict_once_after_finish(self):
        self.start()
        self.assertNotEqual(self.run_cli("judge", "--verdict", "accepted").returncode, 0, "judged before finish")
        self.assertIsNone(json.loads(self.record.read_text())["outcome"])
        self.run_cli("finish", "--outcome", "partial")
        result = self.run_cli("judge", "--verdict", "rejected")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.record.read_text())
        self.assertEqual(data["outcome"], "rejected")
        self.assertEqual(data["delivered_as"], "partial")
        self.assertTrue(data["judged_at"].endswith("Z"))
        before = self.record.read_bytes()
        self.assertNotEqual(self.run_cli("judge", "--verdict", "accepted").returncode, 0, "judged twice")
        self.assertEqual(self.record.read_bytes(), before)
        self.assertFalse(self.record.with_suffix(".json.lock").exists())

    def test_a_blocked_task_has_nothing_to_judge(self):
        self.start()
        self.run_cli("finish", "--outcome", "blocked")
        before = self.record.read_bytes()
        result = self.run_cli("judge", "--verdict", "accepted")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bloccato", result.stderr)
        self.assertEqual(self.record.read_bytes(), before)

    def test_a_missing_directory_is_not_a_reason_to_close_without_a_record(self):
        # The record is mandatory at T2/T3 and docs/misure/ will not exist the first
        # time. Failing here would make "I could not measure it" the default excuse.
        nested = Path(self.temp.name) / "docs/misure/2026-09-15-task.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "start", "--record", str(nested),
             "--task", "Explicit task", "--runtime", "claude", "--model", "m", "--version", "v"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(nested.read_text())["schema"], 1)

    def test_context_budget_file_is_stored_and_validated(self):
        # A context-budget telemetry file becomes a nested field; a non-object is refused.
        self.start()
        budget = Path(self.temp.name) / "budget.json"
        budget.write_text(json.dumps({"context_state": "ORANGE", "context_tokens": 210000,
                                      "model": "vercel/deepseek/deepseek-v4-pro-0813"}))
        result = self.run_cli("finish", "--outcome", "delivered",
                              "--context-budget", str(budget))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.record.read_text())["context_budget"]["context_state"], "ORANGE")
        # A non-object payload is refused without overwriting the record.
        self.record = Path(self.temp.name) / "record2.json"
        self.start()
        budget.write_text("[1, 2, 3]")
        result = self.run_cli("finish", "--outcome", "delivered", "--context-budget", str(budget))
        self.assertEqual(result.returncode, 1)
        self.assertIn("oggetto JSON", result.stderr)


if __name__ == "__main__":
    unittest.main()
