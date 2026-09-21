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
                "git_meta_changed": False, "reply": "", "stderr_tail": ""}
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
    def test_a_tampered_git_stops_every_git_of_the_bench(self):
        # The delegate says the worker changed the shared .git: no tester, no
        # reviewer, no `git worktree remove` — the worktree dir is just deleted,
        # and the record says escalated (reviewer round 19).
        wt = Path(tempfile.mkdtemp())
        called = []

        def runner(worktree, model, brief):
            return {"exit_code": 0, "seconds": 1.0, "usage": {"cost_usd": 0.01, "cost_known_usd": 0.01},
                    "diff_stat": None, "git_meta_changed": True, "error": ".git modificato",
                    "reply": "", "stderr_tail": ""}
        r = bench.run_task(TASK, "m", runner=runner,
                           tester=lambda w: called.append("tester") or (0, "ok"),
                           reviewer=lambda w: called.append("reviewer") or NO_FINDINGS,
                           worktree=lambda t: str(wt), cleanup=lambda w: called.append("cleanup"),
                           differ=lambda w: called.append("differ") or 0)
        self.assertEqual(called, [])
        self.assertTrue(r["git_tampered"])
        self.assertTrue(r["escalated"])
        self.assertIsNone(r["findings"])
        self.assertEqual(r["test_tail"], ".git modificato")
        self.assertFalse(wt.exists())
        # On the retry too (round 20): one tester call, then nothing.
        wt = Path(tempfile.mkdtemp())
        called = []
        results = [{"exit_code": 0, "seconds": 1.0, "usage": {"cost_usd": 0.01, "cost_known_usd": 0.01},
                    "diff_stat": "", "git_meta_changed": False, "error": None, "reply": "", "stderr_tail": ""},
                   {"exit_code": 0, "seconds": 2.0, "usage": {"cost_usd": 0.02, "cost_known_usd": 0.02},
                    "diff_stat": None, "git_meta_changed": True, "error": ".git modificato", "reply": "", "stderr_tail": ""}]
        r = bench.run_task(TASK, "m", runner=lambda w, m, b: results.pop(0),
                           tester=lambda w: called.append("tester") or (1, "fail"),
                           reviewer=lambda w: called.append("reviewer") or NO_FINDINGS,
                           worktree=lambda t: str(wt), cleanup=lambda w: called.append("cleanup"),
                           differ=lambda w: called.append("differ") or 0)
        self.assertEqual(called, ["tester"])
        self.assertTrue(r["git_tampered"])
        self.assertEqual(r["attempt_costs"], [0.01, 0.02])
        self.assertEqual(r["seconds"], 3.0)
        self.assertFalse(wt.exists())

    def test_the_tester_window_is_fingerprinted_too(self):
        # The test command runs the worker's code, which can write the shared .git
        # after the delegate's window closed (round 23): checked after every tester.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "a").write_text("1")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one"], check=True)
            (repo / "a").write_text("2")
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "two"], check=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            task = dict(TASK, repo=str(repo), commit=head, test="true", files_hint=[])
            called = []

            def tester(wt):
                with (repo / ".git" / "config").open("a") as f:
                    f.write("[core]\n\tfsmonitor = false\n")
                called.append("tester")
                return 0, "ok"
            r = bench.run_task(task, "m", runner=fake_runner([(0, {"cost_usd": 0.01, "cost_known_usd": 0.01})]),
                               tester=tester, reviewer=lambda w: called.append("reviewer") or NO_FINDINGS,
                               worktree=bench.make_worktree, cleanup=lambda w: called.append("cleanup"))
            self.assertEqual(called, ["tester"])
            self.assertTrue(r["git_tampered"])
            # An exception mid-run (Ctrl-C) still reads the fingerprint before the
            # cleanup: no `git worktree remove` on a .git the worker changed (round 24).
            called = []

            def runner_that_writes_then_dies(wt, model, brief):
                with (repo / ".git" / "config").open("a") as f:
                    f.write("[core]\n\tfsmonitor = false\n")
                raise KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                bench.run_task(task, "m", runner=runner_that_writes_then_dies,
                               tester=lambda w: (0, "ok"), reviewer=lambda w: NO_FINDINGS,
                               worktree=bench.make_worktree, cleanup=lambda w: called.append("cleanup"))
            self.assertEqual(called, [])
            # A refusal on the retry is a refusal, not a tampering.
            results = [{"exit_code": 0, "seconds": 1.0, "usage": {"cost_usd": 0.01, "cost_known_usd": 0.01},
                        "diff_stat": "", "git_meta_changed": False, "error": None, "reply": "", "stderr_tail": ""},
                       {"exit_code": 5, "seconds": 0, "usage": {}, "diff_stat": "", "reply": "", "stderr_tail": ""}]
            called = []
            r = bench.run_task(TASK, "m", runner=lambda w, m, b: results.pop(0),
                               tester=lambda w: called.append("tester") or (1, "fail"),
                               reviewer=lambda w: called.append("reviewer") or NO_FINDINGS,
                               worktree=lambda t: "/wt", cleanup=lambda w: called.append("cleanup"), differ=lambda w: 0)
            self.assertEqual(called, ["tester", "cleanup"])
            self.assertTrue(r["refused"])
            self.assertNotIn("git_tampered", r)

    def test_a_refusal_and_a_mute_record_are_told_apart(self):
        # A refusal payload (exit 5, no run) is escalated and cleaned as usual; a
        # record that cannot say whether .git was left alone is tampered (round 21).
        called = []
        r = bench.run_task(TASK, "m", runner=lambda w, m, b: {"error": "config OpenCode di progetto", "exit_code": 5,
                                                                "model": "m", "seconds": 0, "usage": {}, "diff_stat": ""},
                           tester=lambda w: called.append("tester") or (0, "ok"),
                           reviewer=lambda w: called.append("reviewer") or NO_FINDINGS,
                           worktree=lambda t: "/wt", cleanup=lambda w: called.append("cleanup"), differ=lambda w: 0)
        self.assertEqual(called, ["cleanup"])
        self.assertTrue(r["refused"])
        self.assertTrue(r["escalated"])
        self.assertEqual(r["attempt_costs"], [None])
        wt = Path(tempfile.mkdtemp())
        called = []
        r = bench.run_task(TASK, "m", runner=lambda w, m, b: {"exit_code": 1, "seconds": 0, "usage": {}, "diff_stat": "",
                                                                "reply": "", "stderr_tail": "Traceback"},
                           tester=lambda w: called.append("tester") or (0, "ok"),
                           reviewer=lambda w: called.append("reviewer") or NO_FINDINGS,
                           worktree=lambda t: str(wt), cleanup=lambda w: called.append("cleanup"), differ=lambda w: 0)
        self.assertEqual(called, [])
        self.assertTrue(r["git_tampered"])
        self.assertFalse(wt.exists())
        # opencode_runner fills a refusal payload with the defaults the record needs.
        with mock.patch.object(bench.subprocess, "run", return_value=mock.Mock(
                stdout='{"error": "x", "exit_code": 5, "model": "m"}', stderr="", returncode=5)):
            rec = bench.opencode_runner("/wt", "m", "brief")
        self.assertEqual(rec["seconds"], 0)
        self.assertEqual(rec["usage"], {})
        self.assertEqual(bench.attempt_state(rec), "refused")

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
        with mock.patch.object(bench.subprocess, "run") as run, \
                mock.patch.object(bench.delegate, "git_meta_paths", return_value=[]), \
                mock.patch.object(bench.delegate, "diff_stat", return_value=""):
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            rec = bench.codex_runner("/wt", "codex", "brief")
        self.assertFalse(rec["git_meta_changed"])  # the reference run carries the fingerprint too
        cmd = run.call_args_list[0].args[0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--approve-for-me", cmd)
        self.assertNotIn("--sandbox", cmd)  # refused together with --approve-for-me
        self.assertIn("--json", cmd)
        self.assertEqual(cmd[cmd.index("-C") + 1], "/wt")
        self.assertNotIn("-m", cmd)
        self.assertEqual(run.call_args_list[0].kwargs["stdin"], subprocess.DEVNULL)

    def test_reviewer_reads_the_work_without_the_restored_tests(self):
        # Reviewer round 6: `git diff HEAD` also showed the test files the bench had
        # restored from the commit, attributing them to the worker.
        task = dict(TASK, test_files=["tests/t.py", "scripts/v.mjs"])
        self.assertEqual(bench.review_diff_command(task), "git diff HEAD -- . ':!tests/t.py' ':!scripts/v.mjs'")
        self.assertEqual(bench.review_diff_command(TASK), "git diff HEAD")
        with mock.patch.object(bench.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            bench.make_reviewer(task)("/wt")
        prompt = run.call_args.args[0][-1]
        self.assertIn("':!tests/t.py'", prompt)
        self.assertIn("do it", prompt)

    def test_stage_new_files_handles_quoted_names_and_failures(self):
        # Reviewer round 6: without -z, "caf\303\251.py" was passed quoted to git add
        # and the failure was swallowed.
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            (Path(tmp) / "base").write_text("b\n")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], check=True)
            (Path(tmp) / "café.py").write_text("x\n")
            (Path(tmp) / "plain.py").write_text("y\n")
            staged = bench.stage_new_files(tmp)
            self.assertEqual(sorted(staged), ["café.py", "plain.py"])
            self.assertIn("café.py", bench.diff_text(tmp))
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            if "ls-files" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="plain.py\0", stderr="")
            raise subprocess.CalledProcessError(128, cmd, stderr="fatal")
        with mock.patch.object(bench.subprocess, "run", side_effect=fake):
            with self.assertRaises(subprocess.CalledProcessError):
                bench.stage_new_files("/wt")

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
            (repo / "node_modules" / "pkg.js").write_text("a")
            wt = bench.make_worktree({"repo": str(repo), "commit": head, "test_files": []})
            self.assertEqual((Path(wt) / "a").read_text(), "1")  # parent of the task commit
            # A copy, not a link (round 23): what the worker writes there stays in the worktree.
            self.assertFalse((Path(wt) / "node_modules").is_symlink())
            (Path(wt) / "node_modules" / "pkg.js").write_text("evil")
            self.assertEqual((repo / "node_modules" / "pkg.js").read_text(), "a")
            (Path(wt) / "a").write_text("1\n2\n3\n")
            self.assertEqual(bench.diff_lines(wt), 4)  # "1" without newline → 1 deleted + 3 added
            (Path(wt) / "new.py").write_text("x = 1\n")
            self.assertEqual(bench.diff_lines(wt), 4)  # untracked: invisible to git diff
            bench.stage_new_files(wt)
            self.assertEqual(bench.diff_lines(wt), 5)
            self.assertIn("new.py", bench.diff_text(wt))
            # Reviewer round 5: a deleted tracked file must stay visible after staging.
            (Path(wt) / "a").unlink()
            bench.stage_new_files(wt)
            self.assertIn("a", [l.split("\t")[-1] for l in subprocess.run(
                ["git", "-C", wt, "diff", "HEAD", "--numstat"], capture_output=True, text=True).stdout.splitlines()])
            self.assertIn("deleted file", bench.diff_text(wt))
            self.assertEqual(bench.diff_lines(wt), 2)  # new.py +1, a (one line at HEAD) -1
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
