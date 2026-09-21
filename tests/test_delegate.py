"""aos-delegate: one opencode run, evidence out; usage read from JSON events or null."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-delegate.py"
FIXTURE = Path(__file__).resolve().parents[1] / "evals/opencode-run-sample.jsonl"

spec = importlib.util.spec_from_file_location("delegate", SCRIPT)
delegate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delegate)


def git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)


class DelegateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Hermetic: never the machine's config directory or OpenCode variables
        # (reviewer round 11).
        env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.temp.name) / "xdg"),
                                           "HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        for name in delegate.OPENCODE_ENV:
            os.environ.pop(name, None)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git_repo(self.repo)

    def test_command_shape(self):
        cmd = delegate.command("vercel/x/y", "do the thing")
        self.assertEqual(cmd[:2], ["opencode", "run"])
        self.assertEqual(cmd[cmd.index("-m") + 1], "vercel/x/y")
        self.assertIn("--auto", cmd)
        self.assertIn("--pure", cmd)
        self.assertEqual(cmd[cmd.index("--format") + 1], "json")
        self.assertEqual(cmd[-2:], ["--", "do the thing"])
        self.assertNotIn("--dir", cmd)
        cmd = delegate.command("vercel/x/y", "do the thing", repo="/r")
        self.assertEqual(cmd[cmd.index("--dir") + 1], "/r")
        # A brief that starts with "-" is still the message, not an option.
        self.assertEqual(delegate.command("vercel/x/y", "-x")[-2:], ["--", "-x"])

    def test_refuses_dirty_repo(self):
        (self.repo / "a.txt").write_text("changed\n")
        with self.assertRaises(SystemExit) as ctx:
            delegate.ensure_clean(self.repo, allow_dirty=False)
        self.assertEqual(ctx.exception.code, delegate.EXIT_DIRTY)
        delegate.ensure_clean(self.repo, allow_dirty=True)  # no raise
        # A user's status.showUntrackedFiles=no does not hide an untracked file (round 17).
        (self.repo / "a.txt").write_text("a\n")
        subprocess.run(["git", "-C", str(self.repo), "config", "status.showUntrackedFiles", "no"], check=True)
        (self.repo / "stray.txt").write_text("s\n")
        with self.assertRaises(SystemExit):
            delegate.ensure_clean(self.repo, allow_dirty=False)
        (self.repo / "stray.txt").unlink()

    def test_diff_stat_lists_new_files(self):
        self.assertEqual(delegate.diff_stat(self.repo), "")
        (self.repo / "new.py").write_text("x = 1\n")
        (self.repo / "caf\u00e9.txt").write_text("y\n")
        self.assertEqual(delegate.diff_stat(self.repo), "?? caf\u00e9.txt\n?? new.py")
        (self.repo / "a.txt").write_text("changed\n")
        stat = delegate.diff_stat(self.repo)
        self.assertIn("a.txt", stat.splitlines()[0])
        self.assertTrue(stat.endswith("?? caf\u00e9.txt\n?? new.py"))
        # A file the worker staged is neither unstaged nor untracked (reviewers, round 6).
        subprocess.run(["git", "-C", str(self.repo), "add", "new.py"], check=True)
        stat = delegate.diff_stat(self.repo)
        self.assertIn("new.py", stat)
        self.assertNotIn("?? new.py", stat)
        # A repo without a commit yet: index and tree, no HEAD to diff against.
        bare = Path(self.temp.name) / "fresh"
        bare.mkdir()
        subprocess.run(["git", "init", "-q", str(bare)], check=True)
        (bare / "s.py").write_text("s\n")
        subprocess.run(["git", "-C", str(bare), "add", "s.py"], check=True)
        (bare / "u.py").write_text("u\n")
        stat = delegate.diff_stat(bare)
        self.assertIn("s.py", stat)
        self.assertTrue(stat.endswith("?? u.py"))
        # A --repo below the toplevel still reports a file created above it (round 15).
        sub = self.repo / "sub"
        sub.mkdir()
        self.assertIn("?? caf\u00e9.txt", delegate.diff_stat(sub))
        subprocess.run(["git", "-C", str(self.repo), "config", "diff.relative", "true"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "color.ui", "always"], check=True)
        stat = delegate.diff_stat(sub)
        self.assertIn("a.txt", stat)  # a user's diff.relative does not hide it (round 16)
        self.assertNotIn("\x1b[", stat)
        long = self.repo / "src/components/settings/notifications/NotificationPreferencesPanel.test.tsx"
        long.parent.mkdir(parents=True)
        long.write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", str(long)], check=True)
        self.assertIn("src/components/settings/notifications/NotificationPreferencesPanel.test.tsx", delegate.diff_stat(self.repo))

    def test_usage_null_when_absent(self):
        usage = delegate.parse_usage("plain text, no events\n{not json\n")
        self.assertEqual(usage, {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
                                 "cache_read_tokens": None, "cost_usd": None, "cost_known_usd": None,
                                 "session_id": None, "steps": 0})

    def test_usage_from_fixture(self):
        usage = delegate.parse_usage(FIXTURE.read_text())
        self.assertEqual(usage["input_tokens"], 44691)
        self.assertEqual(usage["output_tokens"], 34)
        self.assertEqual(usage["cache_read_tokens"], 1792)
        self.assertEqual(usage["cost_usd"], 0)
        self.assertEqual(usage["session_id"], "ses_f3fe2b386ffeTxRC3ZUdXLKOVd")
        self.assertEqual(usage["steps"], 1)

    def test_error_event_is_surfaced(self):
        ev = json.dumps({"type": "error", "sessionID": "ses_1",
                         "error": {"name": "UnknownError", "data": {"message": "Free tier users do not have access"}}})
        self.assertEqual(delegate.parse_error(ev), "Free tier users do not have access")
        self.assertIsNone(delegate.parse_error(FIXTURE.read_text()))
        with mock.patch.object(delegate, "invoke", return_value=(1, ev, "")):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False)
        self.assertEqual(result["error"], "Free tier users do not have access")

    def test_usage_field_missing_from_a_step_stays_null(self):
        # Reviewer round 1: a step without `cost` produced cost_usd 0.0, an estimate.
        no_cost = json.dumps({"type": "step_finish", "sessionID": "ses_1",
                              "part": {"tokens": {"input": 10, "output": 2}}})
        usage = delegate.parse_usage(no_cost)
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), (10, 2))
        self.assertIsNone(usage["cost_usd"])
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertIsNone(usage["cache_read_tokens"])
        full = json.dumps(self.step(0.5))
        usage = delegate.parse_usage(full + "\n" + no_cost + "\n")
        self.assertEqual(usage["input_tokens"], 20)
        self.assertIsNone(usage["cost_usd"])  # one step unknown → total unknown
        self.assertEqual(usage["steps"], 2)

    def test_known_cost_survives_an_unreported_step(self):
        # Reviewer round 2: 0.80 $ reported, then a step without cost → total null, but
        # the 0.80 $ must still reach a spend cap.
        events = json.dumps(self.step(0.8)) + "\n" + json.dumps(
            {"type": "step_finish", "sessionID": "ses_1", "part": {"tokens": {"input": 10}}})
        usage = delegate.parse_usage(events)
        self.assertIsNone(usage["cost_usd"])
        self.assertAlmostEqual(usage["cost_known_usd"], 0.8)
        self.assertIsNone(delegate.parse_usage("")["cost_known_usd"])
        self.assertAlmostEqual(delegate.parse_usage(json.dumps(self.step(0.3)))["cost_known_usd"], 0.3)

    def test_timeout_kills_the_grandchild_when_the_parent_already_exited(self):
        # Reviewer round 2: parent exits at once, grandchild keeps the pipes; the
        # timeout fired but the group was not killed.
        worker = ("import subprocess, sys\n"
                  "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "print(p.pid, flush=True)\n")
        code, out, err = delegate.invoke([sys.executable, "-c", worker], self.temp.name, timeout=0.5)
        self.assertEqual(code, delegate.EXIT_TIMEOUT)
        pid = int(out.strip())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_normal_exit_still_reaps_a_descendant_holding_stderr(self):
        # Reviewer round 3: parent exits 0, grandchild keeps only stderr open; the run
        # returned 0 after 2 s and left the grandchild alive.
        worker = ("import subprocess, sys\n"
                  "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stdout=subprocess.DEVNULL)\n"
                  "print(p.pid, flush=True)\n")
        started = time.monotonic()
        code, out, err = delegate.invoke([sys.executable, "-c", worker], self.temp.name, timeout=10)
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 8)
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(out.strip()), 0)

    def test_unparsable_field_does_not_leave_a_partial_contribution(self):
        bad = json.dumps({"type": "step_finish", "sessionID": "ses_1",
                          "part": {"tokens": {"input": 10, "output": "many"}, "cost": 0.1}})
        usage = delegate.parse_usage(bad)
        self.assertEqual(usage["input_tokens"], 10)
        self.assertIsNone(usage["output_tokens"])
        self.assertEqual(usage["cost_usd"], 0.1)

    def test_timeout_kills_the_grandchild_too(self):
        # Reviewer round 1: a child holding stderr kept invoke() waiting 3 s on a 0.2 s timeout.
        worker = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "time.sleep(30)\n")
        started = time.monotonic()
        code, out, err = delegate.invoke([sys.executable, "-c", worker], self.temp.name, timeout=0.5)
        self.assertEqual(code, delegate.EXIT_TIMEOUT)
        self.assertLess(time.monotonic() - started, 5)

    def test_usage_sums_steps(self):
        ev = json.dumps({"type": "step_finish", "sessionID": "ses_1",
                         "part": {"tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 3, "write": 0}}, "cost": 0.5}})
        usage = delegate.parse_usage(ev + "\n" + ev + "\n")
        self.assertEqual(usage["input_tokens"], 20)
        self.assertEqual(usage["cost_usd"], 1.0)
        self.assertEqual(usage["steps"], 2)

    def test_run_collects_evidence(self):
        with mock.patch.object(delegate, "invoke", return_value=(0, FIXTURE.read_text(), "")) as invoke:
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False)
        cmd, cwd, timeout = invoke.call_args.args[:3]
        self.assertEqual(cwd, str(self.repo.resolve()))
        self.assertEqual(timeout, 5)
        self.assertEqual(cmd[-1], "brief")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["model"], "vercel/x/y")
        self.assertEqual(result["diff_stat"], "")
        self.assertGreaterEqual(result["seconds"], 0)
        self.assertEqual(result["usage"]["input_tokens"], 44691)
        self.assertEqual(result["reply"], "OK")

    def fake_worker(self, events, sleep_after=0.0, then_hang=False):
        """A stand-in for opencode: prints the given events, optionally sleeps, optionally never exits."""
        script = Path(self.temp.name) / "worker.py"
        script.write_text(
            "import sys, time, json\n"
            f"for e in {events!r}:\n"
            "    print(json.dumps(e)); sys.stdout.flush()\n"
            f"    time.sleep({sleep_after})\n"
            + ("time.sleep(60)\n" if then_hang else "")
            + "print('partial'); sys.stdout.flush()\n")
        return [sys.executable, str(script)]

    @staticmethod
    def step(cost):
        return {"type": "step_finish", "sessionID": "ses_1",
                "part": {"tokens": {"input": 10, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}}, "cost": cost}}

    def test_timeout_is_reported_not_raised(self):
        cmd = self.fake_worker([self.step(0.1)], then_hang=True)
        started = time.monotonic()
        code, out, err = delegate.invoke(cmd, self.temp.name, timeout=1)
        self.assertEqual(code, delegate.EXIT_TIMEOUT)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("step_finish", out)  # what streamed before the cut is kept

    def test_cost_cap_kills_the_worker(self):
        cmd = self.fake_worker([self.step(0.4), self.step(0.4), self.step(0.4)], sleep_after=0.2, then_hang=True)
        code, out, err = delegate.invoke(cmd, self.temp.name, timeout=30, max_cost=0.5)
        self.assertEqual(code, delegate.EXIT_CAP)
        self.assertEqual(delegate.parse_usage(out)["steps"], 2)  # stopped right after crossing the cap

    def test_step_cap_kills_the_worker(self):
        cmd = self.fake_worker([self.step(0.0)] * 5, sleep_after=0.1, then_hang=True)
        code, out, err = delegate.invoke(cmd, self.temp.name, timeout=30, max_steps=3)
        self.assertEqual(code, delegate.EXIT_CAP)
        self.assertEqual(delegate.parse_usage(out)["steps"], 3)

    def test_worker_that_finishes_returns_its_exit_code(self):
        cmd = self.fake_worker([self.step(0.1)])
        code, out, err = delegate.invoke(cmd, self.temp.name, timeout=30, max_cost=1.0, max_steps=60)
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("partial\n"))

    def test_run_config_denies_every_skill_and_keeps_providers(self):
        home = Path(self.temp.name) / "xdg" / "opencode"
        home.mkdir(parents=True, exist_ok=True)
        user = home / "opencode.json"
        user.write_text(json.dumps({"provider": {"vercel": {"models": {"x": {}}}}, "permission": {"bash": "allow"}}))
        path = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path).parents[1], ignore_errors=True))
        cfg = json.loads(Path(path).read_text())
        self.assertEqual(cfg["permission"]["skill"], {"*": "deny"})
        self.assertEqual(cfg["provider"]["vercel"]["models"], {"x": {}})
        # A string bash rule becomes the map's "*" entry, then our denies.
        self.assertEqual(list(cfg["permission"]["bash"].items())[0], ("*", "allow"))
        self.assertEqual(cfg["permission"]["bash"]["curl"], "deny")
        self.assertEqual(cfg["permission"]["bash"]["git push *"], "deny")
        # A user who denied every command keeps that denial (reviewer round 1).
        user.write_text(json.dumps({"permission": {"bash": "deny"}}))
        path3 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path3).parents[1], ignore_errors=True))
        self.assertEqual(json.loads(Path(path3).read_text())["permission"]["bash"]["*"], "deny")
        # No user config at all still yields a valid run config.
        path2 = delegate.run_config(config_dir=Path(self.temp.name) / "missing", inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path2).parents[1], ignore_errors=True))
        cfg2 = json.loads(Path(path2).read_text())
        self.assertEqual(cfg2["permission"]["skill"], {"*": "deny"})
        for key in ("external_directory", "webfetch", "websearch", "task"):
            self.assertEqual(cfg2["permission"][key], "deny")

    def test_run_config_guards_win_over_the_user_map_and_come_last(self):
        # The later matching rule wins in OpenCode and a trailing "*": "allow" cancels
        # every deny before it (probed 2026-09-21): the user's entries stay, ours close.
        home = Path(self.temp.name) / "xdg" / "opencode"
        home.mkdir(parents=True, exist_ok=True)
        user = home / "opencode.json"
        user.write_text(json.dumps({"permission": {"bash": {"curl *": "allow", "curl https://*": "allow",
                                                              "*": "allow", "make *": "ask"}}}))
        path = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path).parents[1], ignore_errors=True))
        bash = json.loads(Path(path).read_text())["permission"]["bash"]
        self.assertEqual(bash["curl *"], "deny")
        self.assertNotIn("curl https://*", bash)  # a longer pattern for a denied command goes too (round 12)
        self.assertEqual(bash["npx vercel@*"], "deny")
        self.assertEqual(bash["git fetch *"], "deny")
        self.assertEqual(bash["make *"], "ask")
        keys = list(bash)
        self.assertEqual(keys[:2], ["*", "make *"])
        self.assertTrue(all(bash[k] == "deny" for k in keys[2:]))
        expected = sum(1 if n.endswith("*") else 2 for n in delegate.DENIED_BASH)
        self.assertEqual(len(keys), 2 + expected)
        # A "*" the user wrote keeps its place (reviewer round 7: moving it first
        # turned `{"rm *": "allow", "*": "deny"}` into an allow); ours still close.
        user.write_text(json.dumps({"permission": {"bash": {"rm *": "allow", "*": "deny"}}}))
        path2 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path2).parents[1], ignore_errors=True))
        bash = json.loads(Path(path2).read_text())["permission"]["bash"]
        self.assertEqual(list(bash.items())[:2], [("rm *", "allow"), ("*", "deny")])
        self.assertEqual(list(bash)[-1], "sudo *")
        # A string "*" written after `bash` overrode the whole map in the user's
        # order (round 8): only that "*" survives, then our denies.
        user.write_text(json.dumps({"permission": {"bash": {"rm *": "allow"}, "*": "deny"}}))
        path4 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path4).parents[1], ignore_errors=True))
        bash = json.loads(Path(path4).read_text())["permission"]["bash"]
        self.assertEqual(list(bash.items())[0], ("*", "deny"))
        self.assertNotIn("rm *", bash)
        # A "*" written as a map counts by its own "*", or as deny (round 9), and its
        # specific rules seed the bash map (round 10: `rm *` stays denied).
        user.write_text(json.dumps({"permission": {"bash": {"rm *": "allow"}, "*": {"*": "deny"}}}))
        path5 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path5).parents[1], ignore_errors=True))
        bash = json.loads(Path(path5).read_text())["permission"]["bash"]
        keys = list(bash)
        self.assertGreater(keys.index("*"), keys.index("rm *"))  # the later "*" deny still wins
        self.assertEqual(bash["*"], "deny")
        user.write_text(json.dumps({"permission": {"*": {"*": "allow", "rm *": "deny"}}}))
        path6 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path6).parents[1], ignore_errors=True))
        bash = json.loads(Path(path6).read_text())["permission"]["bash"]
        self.assertEqual(list(bash.items())[:2], [("*", "allow"), ("rm *", "deny")])
        # The same pattern in the "*" map and in a bash map written after it: the
        # later value wins (round 11); a "*" map without its own "*" allows (--auto).
        user.write_text(json.dumps({"permission": {"*": {"*": "allow", "rm *": "allow"}, "bash": {"rm *": "deny"}}}))
        path7 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path7).parents[1], ignore_errors=True))
        bash = json.loads(Path(path7).read_text())["permission"]["bash"]
        self.assertEqual(bash["rm *"], "deny")
        user.write_text(json.dumps({"permission": {"*": {"*": "allow", "rm *": "allow"}, "bash": "deny"}}))
        path8 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path8).parents[1], ignore_errors=True))
        bash = json.loads(Path(path8).read_text())["permission"]["bash"]
        self.assertEqual(list(bash.items())[0], ("*", "deny"))
        self.assertNotIn("rm *", bash)
        # A pattern the later map repeats moves to the later position (round 12).
        for perm in ({"*": {"*": "allow", "rm *": "allow"}, "bash": {"*": "deny"}},
                     {"bash": {"*": "allow", "rm *": "allow"}, "*": {"*": "deny"}}):
            user.write_text(json.dumps({"permission": perm}))
            path = delegate.run_config(config_dir=home, inherited="")
            bash = json.loads(Path(path).read_text())["permission"]["bash"]
            shutil.rmtree(Path(path).parents[1], ignore_errors=True)
            self.assertGreater(list(bash).index("*"), list(bash).index("rm *"), perm)
            self.assertEqual(bash["*"], "deny", perm)
        # An empty "*" map after bash adds no rule and replaces nothing (round 13);
        # the copy writes "*" as a string, its rules already in bash.
        user.write_text(json.dumps({"permission": {"bash": {"rm *": "deny"}, "*": {}}}))
        path10 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path10).parents[1], ignore_errors=True))
        perm = json.loads(Path(path10).read_text())["permission"]
        self.assertEqual(perm["bash"]["rm *"], "deny")
        self.assertEqual(perm["*"], {})
        # A permission key that names bash by glob counts too (round 15), and a
        # user pattern with no space or extra spaces for a denied command goes.
        user.write_text(json.dumps({"permission": {"ba*": {"rm *": "deny"}, "bash": {"curl*": "allow", "git  commit": "allow"}}}))
        path11 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path11).parents[1], ignore_errors=True))
        perm = json.loads(Path(path11).read_text())["permission"]
        self.assertEqual(perm["bash"]["rm *"], "deny")
        self.assertNotIn("curl*", perm["bash"])
        self.assertNotIn("git  commit", perm["bash"])
        self.assertEqual(perm["ba*"], {"rm *": "deny"})
        user.write_text(json.dumps({"permission": {"bash": {"rm *": "deny"}, "[b]ash": "allow"}}))
        path13 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path13).parents[1], ignore_errors=True))
        self.assertEqual(json.loads(Path(path13).read_text())["permission"]["bash"]["rm *"], "deny")  # "[" literal (round 16)
        # A key that expands to "bash" through {env:…} is a bash rule (round 17).
        user.write_text(json.dumps({"permission": {"{env:AOS_TEST_TOOL}": "deny"}}))
        with mock.patch.dict(os.environ, {"AOS_TEST_TOOL": "bash"}):
            path14 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path14).parents[1], ignore_errors=True))
        self.assertEqual(list(json.loads(Path(path14).read_text())["permission"]["bash"].items())[0], ("*", "deny"))
        user.write_text(json.dumps({"permission": {"*": None, "b*": "deny"}}))
        path12 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path12).parents[1], ignore_errors=True))
        perm = json.loads(Path(path12).read_text())["permission"]
        self.assertEqual(perm["*"], "allow")
        self.assertEqual(list(perm["bash"].items())[0], ("*", "deny"))
        user.write_text(json.dumps({"permission": {"*": {"rm -rf *": "deny"}}}))
        path9 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path9).parents[1], ignore_errors=True))
        perm = json.loads(Path(path9).read_text())["permission"]
        self.assertEqual(list(perm["bash"].items())[:2], [("*", "allow"), ("rm -rf *", "deny")])
        # The map stays as written: it rules read and edit too (round 14).
        self.assertEqual(perm["*"], {"rm -rf *": "deny"})
        # A permission-level string is the user's rule for commands too.
        user.write_text(json.dumps({"permission": "deny"}))
        path3 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path3).parents[1], ignore_errors=True))
        perm = json.loads(Path(path3).read_text())["permission"]
        self.assertEqual(perm["*"], "deny")
        self.assertEqual(perm["bash"]["*"], "deny")

    def test_user_config_merges_every_global_layer(self):
        # config.json, opencode.json, opencode.jsonc and the inherited OPENCODE_CONFIG,
        # in OpenCode's order, key by key (reviewer round 7: a provider defined only
        # there vanished from the run).
        home = Path(self.temp.name) / "xdg" / "opencode"
        home.mkdir(parents=True)
        (home / "config.json").write_text(json.dumps({"provider": {"a": {"models": {"x": {}}}}, "theme": "old"}))
        (home / "opencode.json").write_text(json.dumps({"provider": {"b": {"models": {"y": {}}}}, "theme": "new"}))
        (home / "opencode.jsonc").write_text('{"provider": {"a": {"options": {"apiKey": "k"}}}}')
        inherited = Path(self.temp.name) / "inherited.json"
        inherited.write_text(json.dumps({"provider": {"c": {}}, "permission": {"edit": "allow"}}))
        merged = delegate.user_config(home, str(inherited))
        # OPENCODE_PERMISSION in its string form is the rule for everything (round 14);
        # {env:} keys expand in the inherited variables too (round 18).
        self.assertEqual(delegate.user_config(home, "", "", '"deny"')["permission"], "deny")
        with mock.patch.dict(os.environ, {"AOS_TEST_TOOL": "bash"}):
            self.assertEqual(delegate.user_config(home, "", '{"permission": {"{env:AOS_TEST_TOOL}": "ask"}}', "")["permission"], {"bash": "ask"})
            self.assertEqual(delegate.user_config(home, "", "", '{"{env:AOS_TEST_TOOL}": "ask"}')["permission"], {"bash": "ask"})
        self.assertEqual(merged["theme"], "new")
        self.assertEqual(merged["provider"]["a"], {"models": {"x": {}}, "options": {"apiKey": "k"}})
        self.assertEqual(set(merged["provider"]), {"a", "b", "c"})
        self.assertEqual(merged["permission"], {"edit": "allow"})
        # A jsonc with comments and a trailing comma is still read (round 8).
        (home / "opencode.jsonc").write_text('{// c\n"theme": "x", /* "theme": "y" */ "s": "a//b", "t": ", }",}')
        self.assertEqual(delegate.user_config(home, "")["theme"], "x")
        self.assertEqual(delegate.user_config(home, "")["s"], "a//b")
        self.assertEqual(delegate.user_config(home, "")["t"], ", }")  # a comma inside a string stays (round 9)
        # A relative config path and an {env:…} reference (round 9).
        rel = Path(os.path.relpath(home / "opencode.jsonc"))
        self.assertTrue(delegate.load_json(rel)["theme"])
        self.assertEqual(delegate.absolute_files({"k": "{file:{env:HOME}/x}"}, Path("/tmp")), {"k": "{file:{env:HOME}/x}"})
        self.assertEqual(delegate.absolute_files({"k": "Bearer {file:./t} {file:./u}"}, Path("/b")),
                         {"k": "Bearer {file:/b/t} {file:/b/u}"})
        self.assertEqual(delegate.absolute_files({"k": "{file:./keys/{env:P}.txt}"}, Path("/b")),
                         {"k": "{file:/b/keys/{env:P}.txt}"})
        # A relative {file:…} is resolved against the file it came from (round 8).
        (home / "opencode.jsonc").write_text('{"provider": {"a": {"options": {"apiKey": "{file:./k}"}}}}')
        self.assertEqual(delegate.user_config(home, "")["provider"]["a"]["options"]["apiKey"],
                         "{file:" + str((home / "k").resolve()) + "}")
        path = delegate.run_config(config_dir=home, inherited=str(inherited))
        self.addCleanup(lambda: shutil.rmtree(Path(path).parents[1], ignore_errors=True))
        cfg = json.loads(Path(path).read_text())
        self.assertEqual(set(cfg["provider"]), {"a", "b", "c"})
        self.assertEqual(cfg["permission"]["edit"], "allow")

    def test_run_refuses_a_repo_with_its_own_opencode_config(self):
        # A project config merges after ours and a pattern it adds lands after our
        # denies (reviewer round 7); there is no order that closes it, so no run.
        (self.repo / "opencode.json").write_text("{}")
        self.assertEqual(delegate.project_config(self.repo), (self.repo / "opencode.json").resolve())
        with mock.patch.object(delegate, "invoke") as invoke, self.assertRaises(SystemExit) as ctx:
            delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=True)
        self.assertEqual(ctx.exception.code, delegate.EXIT_PROJECT_CONFIG)
        invoke.assert_not_called()
        # With --json the refusal is a payload too (round 17).
        brief = self.repo / "brief.txt"
        brief.write_text("b")
        with mock.patch.object(sys, "argv", ["aos-delegate", "--repo", str(self.repo), "--model", "x/y",
                                             "--brief", str(brief), "--json", "--allow-dirty"]), \
                mock.patch.object(shutil, "which", return_value="/usr/bin/opencode"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = delegate.main()
        self.assertEqual(code, delegate.EXIT_PROJECT_CONFIG)
        self.assertEqual(json.loads(out.getvalue())["exit_code"], delegate.EXIT_PROJECT_CONFIG)
        brief.unlink()
        (self.repo / "opencode.json").unlink()
        (self.repo / ".opencode").mkdir()
        self.assertIsNotNone(delegate.project_config(self.repo))
        (self.repo / ".opencode").rmdir()
        self.assertIsNone(delegate.project_config(self.repo))
        # The search stops at the git toplevel, where OpenCode stops (round 8).
        (self.repo.parent / "opencode.json").write_text("{}")
        self.assertIsNone(delegate.project_config(self.repo))
        sub = self.repo / "sub"
        sub.mkdir()
        (self.repo / "opencode.jsonc").write_text("{}")
        self.assertEqual(delegate.project_config(sub), (self.repo / "opencode.jsonc").resolve())

    def test_run_config_survives_a_config_that_is_not_an_object(self):
        # Valid JSON that is not a config (reviewers, round 6): guards, no traceback.
        home = Path(self.temp.name) / "xdg" / "opencode"
        home.mkdir(parents=True, exist_ok=True)
        user = home / "opencode.json"
        for text in ("null", "[]", '"x"', '{"permission": null}', '{"permission": [1]}', '{"permission": {"bash": null}}'):
            user.write_text(text)
            path = delegate.run_config(config_dir=home, inherited="")
            perm = json.loads(Path(path).read_text())["permission"]
            shutil.rmtree(Path(path).parents[1], ignore_errors=True)
            self.assertEqual(perm["webfetch"], "deny", text)
            self.assertEqual(list(perm["bash"])[0], "*", text)
            self.assertEqual(perm["*"], "allow", text)

    def test_run_config_guards_close_the_permission_map_too(self):
        # A wildcard at the permission level after our keys would reopen them
        # (reviewer round 4); a string permission is the user's rule for everything.
        home = Path(self.temp.name) / "xdg" / "opencode"
        home.mkdir(parents=True, exist_ok=True)
        user = home / "opencode.json"
        user.write_text(json.dumps({"permission": {"bash": "allow", "webfetch": "allow", "*": "allow"}}))
        path = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path).parents[1], ignore_errors=True))
        perm = json.loads(Path(path).read_text())["permission"]
        self.assertEqual(list(perm)[0], "*")
        self.assertEqual(list(perm)[-1], "bash")
        self.assertEqual(perm["webfetch"], "deny")
        self.assertEqual(perm["bash"]["*"], "allow")
        # No wildcard from the user: "*" is still the first key of both maps.
        user.write_text(json.dumps({"permission": {"edit": "allow"}}))
        path3 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path3).parents[1], ignore_errors=True))
        perm = json.loads(Path(path3).read_text())["permission"]
        self.assertEqual(list(perm)[:2], ["*", "edit"])
        self.assertEqual(list(perm["bash"])[0], "*")
        user.write_text(json.dumps({"permission": "allow"}))
        path2 = delegate.run_config(config_dir=home, inherited="")
        self.addCleanup(lambda: shutil.rmtree(Path(path2).parents[1], ignore_errors=True))
        perm = json.loads(Path(path2).read_text())["permission"]
        self.assertEqual(list(perm)[0], "*")
        self.assertEqual(perm["task"], "deny")

    def test_run_passes_the_run_config_in_env_and_removes_it(self):
        seen = {}

        def fake_invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            # The copy is the only layer, the global one of a temporary XDG root; every
            # OpenCode config variable inherited from the caller is dropped (rounds 6-8).
            seen["config"] = Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json"
            seen["content"] = json.loads(seen["config"].read_text())
            seen["inherited"] = any(name in env for name in delegate.OPENCODE_ENV)
            seen["linked"] = (Path(env["XDG_CONFIG_HOME"]) / "git").is_symlink()
            seen["agents_linked"] = (Path(env["XDG_CONFIG_HOME"]) / "opencode" / "agent").exists()
            seen["modules_linked"] = (Path(env["XDG_CONFIG_HOME"]) / "opencode" / "node_modules").is_symlink()
            seen["pwd"] = env["PWD"]
            seen["dir"] = cmd[cmd.index("--dir") + 1]
            seen["exists"] = seen["config"].exists()
            seen["caps"] = (max_cost, max_steps)
            return 0, FIXTURE.read_text(), ""
        inherited = Path(self.temp.name) / "inherited.json"
        inherited.write_text(json.dumps({"provider": {"only-here": {}}}))
        # The user's XDG root (never the machine's: reviewer round 8), with a
        # sibling entry the worker's own tools must still find.
        xdg = Path(self.temp.name) / "xdg"
        (xdg / "git").mkdir(parents=True)
        (xdg / "opencode").mkdir()
        (xdg / "opencode" / "opencode.json").write_text(json.dumps(
            {"mcp": {"remote": {}}, "theme": "t", "share": "auto", "tools": {"webfetch": True},
             "agent": {"build": {"permission": {"bash": "allow"}, "tools": {"bash": True}}}}))
        (xdg / "opencode" / "agent").mkdir()
        (xdg / "opencode" / "agent" / "build.md").write_text("---\npermission:\n  bash: allow\n---\n")
        (xdg / "opencode" / "node_modules").mkdir()
        with mock.patch.object(delegate, "invoke", fake_invoke), \
                mock.patch.dict(os.environ, {"OPENCODE_CONFIG": str(inherited), "XDG_CONFIG_HOME": str(xdg),
                                             "OPENCODE_PERMISSION": '{"bash": {"rm -rf *": "deny"}}', "OPENCODE_AUTO_SHARE": "1",
                                             "OPENCODE_CONFIG_CONTENT": '{"provider": {"only-env": {}}}'}):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                                  max_cost=2.0, max_steps=10)
        self.assertTrue(seen["exists"])
        self.assertFalse(seen["config"].parents[1].exists())
        self.assertFalse(seen["inherited"])
        self.assertTrue(seen["linked"])
        self.assertIn("only-here", seen["content"]["provider"])
        self.assertIn("only-env", seen["content"]["provider"])
        # Only the providers and the reworked permission survive (rounds 8-10: mcp,
        # agent.*.permission — measured, the worker ran curl — tools, share); the
        # agent definitions and skills of the config directory are not linked in.
        self.assertEqual(set(seen["content"]), {"provider", "permission", "share", "small_model"})
        self.assertEqual(seen["content"]["share"], "disabled")
        self.assertEqual(seen["content"]["small_model"], "vercel/x/y")  # the title agent too (round 14)
        self.assertFalse(seen["agents_linked"])
        self.assertTrue(seen["modules_linked"])
        self.assertEqual(seen["content"]["permission"]["webfetch"], "deny")
        self.assertEqual(seen["content"]["permission"]["codesearch"], "deny")
        self.assertEqual(seen["content"]["permission"]["bash"]["rm -rf *"], "deny")  # OPENCODE_PERMISSION merged
        # The temporary root is gone; the targets of its links are not.
        self.assertTrue((xdg / "git").is_dir())
        self.assertTrue((xdg / "opencode" / "node_modules").is_dir())
        self.assertEqual(list(seen["content"]["permission"]["bash"])[0], "*")
        self.assertEqual(seen["caps"], (2.0, 10))
        self.assertEqual(seen["pwd"], str(self.repo.resolve()))
        self.assertEqual(seen["dir"], str(self.repo.resolve()))
        self.assertIsNone(result["error"])
        self.assertEqual(result["head_before"], result["head_after"])

    def test_record_says_when_the_worker_moved_head(self):
        def fake_invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            (self.repo / "a.txt").write_text("moved\n")
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-qam", "x"], check=True)
            return 0, "", ""
        with mock.patch.object(delegate, "invoke", fake_invoke), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.temp.name) / "xdg")}):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False)
        self.assertEqual(result["diff_stat"], "")
        self.assertNotEqual(result["head_before"], result["head_after"])

    def test_no_git_runs_on_a_repo_whose_git_config_the_worker_changed(self):
        # Reproduced (round 18): `core.fsmonitor` written by the worker was run by
        # this script's own `git diff`. Any change to .git config/info/hooks: no git.
        def fake_invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            subprocess.run(["git", "-C", str(self.repo), "config", "core.fsmonitor", "false"], check=True)
            (self.repo / "b.txt").write_text("b\n")
            return 0, "", ""
        with mock.patch.object(delegate, "invoke", fake_invoke):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False)
        self.assertTrue(result["git_meta_changed"])
        self.assertIsNone(result["diff_stat"])
        self.assertIsNone(result["head_after"])
        self.assertIn(".git", result["error"])
        # Hooks and info count too; an untouched .git does not.
        paths = delegate.git_meta_paths(self.repo)
        before = delegate.git_meta(paths)
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "attributes").write_text("* filter=x\n")
        self.assertNotEqual(before, delegate.git_meta(paths))
        self.assertEqual(delegate.git_meta(paths), delegate.git_meta(paths))

    def test_git_meta_covers_a_linked_worktree_and_included_configs(self):
        # A linked worktree's own dir has no config/hooks: they are the common dir's,
        # shared with the user's real repo; and a config may include a file the
        # worker can edit without touching the watched ones (reviewers, round 19).
        wt = Path(self.temp.name) / "wt"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "--detach", str(wt), "HEAD"], check=True)
        included = self.repo / "extra.gitconfig"
        included.write_text("[user]\n\tname = t\n")
        subprocess.run(["git", "-C", str(self.repo), "config", "include.path", "../extra.gitconfig"], check=True)
        paths = delegate.git_meta_paths(wt)
        self.assertIn((self.repo / ".git" / "config").resolve(), paths)
        self.assertIn((self.repo / ".git" / "hooks").resolve(), paths)
        self.assertIn(included.resolve(), paths)
        before = delegate.git_meta(paths)
        self.assertTrue(before)
        with included.open("a") as f:
            f.write("[core]\n\tfsmonitor = false\n")
        self.assertNotEqual(before, delegate.git_meta(paths))
        # Nested and quoted includes are followed (round 20); the pointers git
        # follows — the worktree's `.git` file, `<gitdir>/commondir` — are watched.
        nested = self.repo / "nested.gitconfig"
        nested.write_text("[user]\n\temail = t@t\n")
        with included.open("a") as f:
            f.write('[include]\n\tpath = "./nested.gitconfig"\n')
        paths = delegate.git_meta_paths(wt)
        self.assertIn(nested.resolve(), paths)
        self.assertIn((wt / ".git").resolve(), paths)
        before = delegate.git_meta(paths)
        # As git reads includes (round 21): `Path =`, a comment after the value,
        # the key on the header's line.
        (self.repo / "c1.gitconfig").write_text("[user]\n\tname = a\n")
        (self.repo / "c2.gitconfig").write_text("[user]\n\tname = b\n")
        with (self.repo / ".git" / "config").open("a") as f:
            f.write("[include]\n\tPath = ../c1.gitconfig # local rules\n[include] path = ../c2.gitconfig ; note\n")
        paths = delegate.git_meta_paths(wt)
        self.assertIn((self.repo / "c1.gitconfig").resolve(), paths)
        self.assertIn((self.repo / "c2.gitconfig").resolve(), paths)
        # core.hooksPath, the global files' includes and an inherited GIT_CONFIG_GLOBAL
        # are watched too; quotes inside a value and continuation lines are read
        # as git reads them (round 23).
        (self.repo / "myhooks").mkdir()
        subprocess.run(["git", "-C", str(self.repo), "config", "core.hooksPath", "myhooks"], check=True)
        home_cfg = Path(self.temp.name) / ".gitconfig"
        home_cfg.write_text("[include]\n\tpath = ~/.gitconfig.local\n")
        (Path(self.temp.name) / ".gitconfig.local").write_text("")
        inherited_global = Path(self.temp.name) / "inherited.gitconfig"
        inherited_global.write_text("")
        with (self.repo / ".git" / "config").open("a") as f:
            f.write('[include]\n\tpath = "../c3".gitconfig\n\tpath = ../c4\\\n.gitconfig\n\tpath = "../c5\\"q.gitconfig"\n')
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(inherited_global)}):
            paths = delegate.git_meta_paths(wt)
        # A relative core.hooksPath counts from the worktree git runs in, as git does.
        resolved = [p.resolve() for p in paths]
        for expected in (wt / "myhooks", Path(self.temp.name) / ".gitconfig.local", inherited_global,
                         self.repo / "c3.gitconfig", self.repo / "c4.gitconfig", self.repo / 'c5"q.gitconfig'):
            self.assertIn(expected.resolve(), resolved)
        subprocess.run(["git", "-C", str(self.repo), "config", "--unset", "core.hooksPath"], check=True)
        # With --repo below the toplevel the toplevel's `.git` is the pointer watched.
        sub = wt / "sub"
        sub.mkdir()
        self.assertIn((wt / ".git").resolve(), delegate.git_meta_paths(sub))
        self.assertNotIn((sub / ".git").resolve(), delegate.git_meta_paths(sub))
        # An unreadable file is a stable marker: unreadable before and after is not a
        # change, readable before and unreadable after is (round 22).
        before = delegate.git_meta(paths)
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError):
            self.assertEqual(delegate.git_meta(paths), delegate.git_meta(paths))
            self.assertNotEqual(before, delegate.git_meta(paths))
        # A link re-pointed to another file, and a mode change, are changes (round 22).
        (self.repo / "cfg-a").write_text(""); (self.repo / "cfg-b").write_text("")
        real = (self.repo / ".git" / "config").read_text()
        (self.repo / ".git" / "config").unlink()
        (self.repo / ".git" / "config").symlink_to(self.repo / "cfg-a")
        (self.repo / "cfg-a").write_text(real)
        (self.repo / "cfg-b").write_text(real)
        paths = delegate.git_meta_paths(wt)
        before = delegate.git_meta(paths)
        (self.repo / ".git" / "config").unlink()
        (self.repo / ".git" / "config").symlink_to(self.repo / "cfg-b")
        self.assertNotEqual(before, delegate.git_meta(paths))
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\n")
        before = delegate.git_meta(paths)
        hook.chmod(0o755)
        self.assertNotEqual(before, delegate.git_meta(paths))
        pointer = wt / ".git"
        pointer.write_text(pointer.read_text() + "\n")
        self.assertNotEqual(before, delegate.git_meta(paths))
        pointer.write_text(pointer.read_text().rstrip("\n") + "\n")
        commondir = self.repo / ".git" / "worktrees" / "wt" / "commondir"
        self.assertIn(commondir.resolve(), paths)
        # The main repo's `.git` directory is a marker: its index may change freely.
        main_paths = delegate.git_meta_paths(self.repo)
        self.assertEqual(delegate.git_meta(main_paths)[str((self.repo / ".git").resolve())], b"<dir>")
        # After the run, the snapshot is plain reads: no git on the tampered repo.
        with mock.patch.object(delegate.subprocess, "run", side_effect=AssertionError("git ran")):
            delegate.git_meta(paths)
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(wt)], check=True)

    def test_global_git_config_is_neither_read_nor_left_unnoticed(self):
        # A worker can append to ~/.gitconfig with a shell redirect (round 22): the
        # script's own git reads no global or system config, and the record says
        # the file changed.
        hook = Path(self.temp.name) / "hook.sh"
        marker = Path(self.temp.name) / "ran"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)

        def fake_invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            cfg = Path(os.environ["XDG_CONFIG_HOME"]) / "git" / "config"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(f"[core]\n\tfsmonitor = {hook}\n")
            (self.repo / "a.txt").write_text("changed\n")
            return 0, "", ""
        with mock.patch.object(delegate, "invoke", fake_invoke):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False)
        self.assertTrue(result["git_meta_changed"])
        self.assertFalse(marker.exists())
        # Even with the hook in place and no fingerprint, the script's git ignores it.
        self.assertIn("a.txt", delegate.diff_stat(self.repo))
        self.assertFalse(marker.exists())

    def test_cap_exit_is_named_in_the_record(self):
        with mock.patch.object(delegate, "invoke", return_value=(delegate.EXIT_CAP, "", "")):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False)
        self.assertEqual(result["exit_code"], delegate.EXIT_CAP)
        self.assertIn("tetto", result["error"])

    def test_invoke_never_reads_stdin(self):
        # codex/opencode wait on a tty stdin; a delegated run must not.
        code, out, err = delegate.invoke([sys.executable, "-c", "import sys;print(sys.stdin.read()=='')"],
                                         self.temp.name, 5)
        self.assertEqual((code, out.strip()), (0, "True"))

    def test_cli_json_without_opencode(self):
        brief = Path(self.temp.name) / "brief.md"
        brief.write_text("brief")
        env = {"PATH": self.temp.name}  # nothing on PATH: opencode absent
        result = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo),
                                 "--model", "vercel/x/y", "--brief", str(brief), "--json"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, delegate.EXIT_NO_OPENCODE, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"], "opencode non trovato")

    def test_retry_on_worker_owned_dirty_tree_is_allowed(self):
        # A retry of the same task may run on the dirt the previous attempt left:
        # the baseline HEAD is unchanged and every dirty path is worker-owned.
        state_file = Path(self.temp.name) / "state.json"

        def worker(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            (self.repo / "a.txt").write_text("worker\n")
            return 0, "", ""

        with mock.patch.object(delegate, "invoke", worker):
            first = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                                 state_file=state_file)
        self.assertTrue(first["initial_repo_clean"])
        self.assertFalse(first["dirty_owned_by_current_run"])
        self.assertFalse(first["dirty_conflict_detected"])
        self.assertEqual(first["retry_dirty_policy"], "clean")
        self.assertTrue(state_file.exists())
        with mock.patch.object(delegate, "invoke", worker):
            second = delegate.run(self.repo, "vercel/x/y", "brief2", timeout=5, allow_dirty=False,
                                  state_file=state_file)
        self.assertTrue(second["dirty_owned_by_current_run"])
        self.assertEqual(second["retry_dirty_policy"], "owned")
        self.assertTrue(second["initial_repo_clean"])

    def test_dirty_repo_before_first_attempt_is_refused_not_owned(self):
        # Pre-existing dirt is the user's: the first attempt refuses it and writes
        # no state, so a retry can never claim ownership of it.
        (self.repo / "a.txt").write_text("user change\n")
        state_file = Path(self.temp.name) / "state.json"
        with self.assertRaises(SystemExit) as ctx:
            delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                         state_file=state_file)
        self.assertEqual(ctx.exception.code, delegate.EXIT_DIRTY)
        self.assertFalse(state_file.exists())

    def test_retry_refuses_unexpected_external_change(self):
        # A file the worker never owned appeared between attempts: refuse, preserve it.
        state_file = Path(self.temp.name) / "state.json"

        def worker(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            (self.repo / "a.txt").write_text("worker\n")
            return 0, "", ""

        with mock.patch.object(delegate, "invoke", worker):
            delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                         state_file=state_file)
        (self.repo / "b.txt").write_text("user\n")
        with mock.patch.object(delegate, "invoke") as invoke, self.assertRaises(SystemExit) as ctx:
            delegate.run(self.repo, "vercel/x/y", "brief2", timeout=5, allow_dirty=False,
                         state_file=state_file)
        self.assertEqual(ctx.exception.code, delegate.EXIT_CONFLICT)
        invoke.assert_not_called()
        self.assertEqual((self.repo / "b.txt").read_text(), "user\n")

    def test_retry_refuses_when_head_moved(self):
        # An external commit between attempts changes the baseline: refuse, never
        # diff against a HEAD the task did not start from.
        state_file = Path(self.temp.name) / "state.json"

        def worker(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            (self.repo / "a.txt").write_text("worker\n")
            return 0, "", ""

        with mock.patch.object(delegate, "invoke", worker):
            delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                         state_file=state_file)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qam", "external"], check=True)
        with mock.patch.object(delegate, "invoke") as invoke, self.assertRaises(SystemExit) as ctx:
            delegate.run(self.repo, "vercel/x/y", "brief2", timeout=5, allow_dirty=False,
                         state_file=state_file)
        self.assertEqual(ctx.exception.code, delegate.EXIT_CONFLICT)
        invoke.assert_not_called()

    def test_retry_state_survives_a_missing_parent(self):
        # The state file lives wherever the caller points; a missing parent is created.
        state_file = Path(self.temp.name) / "nested" / "dir" / "state.json"

        def worker(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            return 0, "", ""

        with mock.patch.object(delegate, "invoke", worker):
            delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                         state_file=state_file)
        self.assertTrue(state_file.exists())
        self.assertEqual(json.loads(state_file.read_text())["schema"], delegate.RETRY_STATE_SCHEMA)


if __name__ == "__main__":
    unittest.main()
