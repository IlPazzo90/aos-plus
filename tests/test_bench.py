"""aos-bench: replay tasks in worktrees; every external call is injected, none runs here."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-bench.py"
spec = importlib.util.spec_from_file_location("bench", SCRIPT)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

TASK = {"id": "t1", "repo": "/r", "commit": "abc", "tier": "T1", "risk": "LOW",
        "prompt": "do it", "test": "true", "files_hint": ["a.py"]}
NO_FINDINGS = {"high": 0, "medium": 0, "low": 0}


def fake_runner(results):
    """results: list of (exit_code, usage) consumed per call; records the briefs it got."""
    calls = []

    def run(worktree, model, brief):
        code, usage = results.pop(0)
        calls.append(brief)
        return {"exit_code": code, "seconds": 1.0, "usage": usage, "diff_stat": "a.py | 1 +-",
                "reply": "", "stderr_tail": ""}
    run.calls = calls
    return run


def run_task(task=TASK, model="m", runner=None, tester=None, reviewer=None, differ=None, spend=None):
    return bench.run_task(task, model,
                          runner=runner or fake_runner([(0, {"cost_usd": 0.01}), (0, {"cost_usd": 0.01})]),
                          tester=tester or (lambda wt: (0, "ok")),
                          reviewer=reviewer or (lambda wt: NO_FINDINGS),
                          worktree=lambda t: "/wt", cleanup=lambda wt: None,
                          differ=differ or (lambda wt: 4), spend=spend)


class BenchTests(unittest.TestCase):
    def test_first_pass(self):
        r = run_task(reviewer=lambda wt: {"high": 0, "medium": 1, "low": 0})
        self.assertTrue(r["first_pass"])
        self.assertIsNone(r["retry_pass"])
        self.assertFalse(r["escalated"])
        self.assertEqual(r["findings"]["medium"], 1)
        self.assertEqual(r["cost_usd"], 0.01)
        self.assertEqual(r["diff_lines"], 4)
        self.assertIn("diff", r)

    def test_retry_carries_failure_output(self):
        runner = fake_runner([(0, {"cost_usd": 0.01}), (0, {"cost_usd": 0.02})])
        outcomes = iter([(1, "FAILED test_x"), (0, "ok")])
        r = run_task(runner=runner, tester=lambda wt: next(outcomes))
        self.assertFalse(r["first_pass"])
        self.assertTrue(r["retry_pass"])
        self.assertFalse(r["escalated"])
        self.assertIn("FAILED test_x", runner.calls[1])
        self.assertAlmostEqual(r["cost_usd"], 0.03)
        self.assertEqual(r["seconds"], 2.0)

    def test_escalated_after_two_failures(self):
        r = run_task(tester=lambda wt: (1, "boom"), reviewer=lambda wt: self.fail("no review of a failed task"))
        self.assertTrue(r["escalated"])
        self.assertFalse(r["retry_pass"])
        self.assertIsNone(r["findings"])

    def test_null_cost_stays_null_across_retry(self):
        runner = fake_runner([(0, {"cost_usd": None}), (0, {"cost_usd": 0.02})])
        outcomes = iter([(1, "fail"), (0, "ok")])
        r = run_task(runner=runner, tester=lambda wt: next(outcomes))
        self.assertIsNone(r["cost_usd"])
        # Reviewer round 1: the known attempt must still reach the spend cap.
        self.assertEqual(r["attempt_costs"], [None, 0.02])
        spend = bench.Spend(cap=0.01)
        for cost in r["attempt_costs"]:
            spend.add(cost)
        self.assertTrue(spend.exceeded())
        self.assertEqual(spend.unknown, 1)

    def test_retry_does_not_start_past_the_cap(self):
        # Reviewer round 3: the retry started after the first attempt had already
        # crossed --cap-usd; the cap is checked between attempts.
        runner = fake_runner([(0, {"cost_usd": 0.8, "cost_known_usd": 0.8}), (0, {"cost_usd": 0.8, "cost_known_usd": 0.8})])
        outcomes = iter([(1, "FAILED"), (0, "ok")])
        spend = bench.Spend(cap=0.5)
        r = run_task(runner=runner, tester=lambda wt: next(outcomes), spend=spend,
                     reviewer=lambda wt: self.fail("no review of a capped task"))
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(r["capped"])
        self.assertFalse(r["first_pass"])
        self.assertIsNone(r["retry_pass"])
        self.assertFalse(r["escalated"])
        self.assertIsNone(r["findings"])
        self.assertTrue(spend.exceeded())
        self.assertIn("fermati dal tetto", bench.summary([r]))

    def test_capped_record_still_stages_new_files(self):
        # Reviewer round 4: the capped branch returned before stage_new_files, so a
        # module the worker created vanished from the record and with the worktree.
        runner = fake_runner([(0, {"cost_usd": 0.8, "cost_known_usd": 0.8})])
        with mock.patch.object(bench, "stage_new_files") as stage:
            run_task(runner=runner, tester=lambda wt: (1, "FAILED"), spend=bench.Spend(cap=0.5))
        stage.assert_called_once_with("/wt")

    def test_retry_runs_under_the_cap(self):
        runner = fake_runner([(0, {"cost_usd": 0.1, "cost_known_usd": 0.1}), (0, {"cost_usd": 0.1, "cost_known_usd": 0.1})])
        outcomes = iter([(1, "FAILED"), (0, "ok")])
        spend = bench.Spend(cap=0.5)
        r = run_task(runner=runner, tester=lambda wt: next(outcomes), spend=spend)
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue(r["retry_pass"])
        self.assertFalse(r["capped"])
        self.assertAlmostEqual(spend.total, 0.2)

    def test_known_cost_inside_an_attempt_reaches_the_cap(self):
        # Reviewer round 2: 0.8 $ reported then an unreported step → cost_usd None,
        # cost_known_usd 0.8; the cap counts the known part.
        runner = fake_runner([(0, {"cost_usd": None, "cost_known_usd": 0.8})])
        r = run_task(runner=runner)
        self.assertIsNone(r["cost_usd"])
        self.assertEqual(r["known_costs"], [0.8])
        spend = bench.Spend(cap=0.5)
        for cost in r["known_costs"]:
            spend.add(cost)
        self.assertTrue(spend.exceeded())

    def test_worktree_removed_when_preparation_fails(self):
        # Reviewer round 1: a failing restore_tests left the worktree registered.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            env = ["-c", "user.email=t@t", "-c", "user.name=t"]
            for n in ("one", "two"):
                (repo / "a").write_text(n)
                subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
                subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", n], check=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            with self.assertRaises(subprocess.CalledProcessError):
                bench.make_worktree({"repo": str(repo), "commit": head, "test_files": ["missing.py"]})
            listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
            self.assertEqual(len(listed.strip().splitlines()), 1)  # only the main checkout

    def test_cleanup_runs_even_when_runner_raises(self):
        removed = []

        def boom(wt, model, brief):
            raise RuntimeError("provider down")
        with self.assertRaises(RuntimeError):
            bench.run_task(TASK, "m", runner=boom, tester=None, reviewer=None,
                           worktree=lambda t: "/wt", cleanup=removed.append, differ=None)
        self.assertEqual(removed, ["/wt"])

    def test_brief_names_test_and_forbids_commit(self):
        brief = bench.brief_for(TASK)
        self.assertIn("do it", brief)
        self.assertIn("a.py", brief)
        self.assertIn("`true`", brief)
        self.assertIn("Do not commit", brief)
        self.assertNotIn("specification", brief)
        spec = bench.brief_for(dict(TASK, test_files=["tests/t.py"]))
        self.assertIn("tests/t.py: read it first, do not modify it", spec)
        self.assertNotIn("previous attempt", brief)
        self.assertIn("previous attempt", bench.brief_for(TASK, failure="x"))

    def test_spend_cap(self):
        spend = bench.Spend(cap=0.05)
        spend.add(0.03)
        self.assertFalse(spend.exceeded())
        spend.add(0.03)
        self.assertTrue(spend.exceeded())
        spend.add(None)
        self.assertEqual(spend.unknown, 1)
        self.assertFalse(bench.Spend(cap=None).exceeded())

    def test_summary_table(self):
        rows = [{"task": "t1", "model": "m1", "first_pass": True, "retry_pass": None, "escalated": False,
                 "seconds": 10, "cost_usd": 0.01, "findings": {"high": 0, "medium": 1, "low": 0}, "diff_lines": 4},
                {"task": "t2", "model": "m1", "first_pass": False, "retry_pass": False, "escalated": True,
                 "seconds": 30, "cost_usd": None, "findings": None, "diff_lines": 40}]
        md = bench.summary(rows)
        self.assertIn("| m1 | 1/2 | 0/2 | 1/2 |", md)
        self.assertIn("0/1/0", md)
        self.assertIn("costo n/d: 1", md)

    def test_parse_findings_counts_by_severity(self):
        report = json.dumps({"findings": [{"severity": "high", "title": "a"}, {"severity": "low", "title": "b"},
                                          {"severity": "weird", "title": "c"}]})
        parsed = bench.parse_findings(report)
        self.assertEqual({k: parsed[k] for k in ("high", "medium", "low")}, {"high": 1, "medium": 0, "low": 1})
        self.assertEqual([i["title"] for i in parsed["items"]], ["a", "b", "c"])
        self.assertIsNone(bench.parse_findings("not json"))
        self.assertIsNone(bench.parse_findings(""))

    def test_codex_runner_shape(self):
        with mock.patch.object(bench.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            bench.codex_runner("/wt", "codex", "brief")
        cmd = run.call_args_list[0].args[0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--approve-for-me", cmd)
        self.assertNotIn("--sandbox", cmd)  # refused together with --approve-for-me
        self.assertIn("--json", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], "/wt")
        self.assertNotIn("-m", cmd)
        self.assertEqual(run.call_args_list[0].kwargs["stdin"], subprocess.DEVNULL)

    def test_codex_usage_from_json_events(self):
        ev = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 5}})
        self.assertEqual(bench.codex_usage("noise\n" + ev + "\n" + ev + "\n")["input_tokens"], 200)
        self.assertIsNone(bench.codex_usage("nothing")["input_tokens"])
        self.assertIsNone(bench.codex_usage(ev)["cost_usd"])

    def test_worktree_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            env = ["-c", "user.email=t@t", "-c", "user.name=t"]
            (repo / "a").write_text("1")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "one"], check=True)
            (repo / "a").write_text("2")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "two"], check=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            (repo / "node_modules").mkdir()
            wt = bench.make_worktree({"repo": str(repo), "commit": head, "test_files": []})
            self.assertEqual((Path(wt) / "a").read_text(), "1")  # parent of the task commit
            self.assertTrue((Path(wt) / "node_modules").is_symlink())
            (Path(wt) / "a").write_text("1\n2\n3\n")
            self.assertEqual(bench.diff_lines(wt), 4)  # "1" without newline → 1 deleted + 3 added
            (Path(wt) / "new.py").write_text("x = 1\n")
            self.assertEqual(bench.diff_lines(wt), 4)  # untracked: invisible to git diff
            bench.stage_new_files(wt)
            self.assertEqual(bench.diff_lines(wt), 5)
            self.assertIn("new.py", bench.diff_text(wt))
            # The commit's test file is the judge: restored from the commit, tampering undone.
            task = {"repo": str(repo), "commit": head, "test": "true", "test_files": ["a"]}
            bench.restore_tests(wt, task)
            self.assertEqual((Path(wt) / "a").read_text(), "2")
            (Path(wt) / "a").write_text("tampered")
            bench.make_tester(task)(wt)
            self.assertEqual((Path(wt) / "a").read_text(), "2")
            bench.remove_worktree(wt)
            self.assertFalse(Path(wt).exists())
            listed = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
            self.assertNotIn(wt, listed)


if __name__ == "__main__":
    unittest.main()
