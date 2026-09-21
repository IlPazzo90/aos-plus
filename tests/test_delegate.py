"""aos-delegate: one opencode run, evidence out; usage read from JSON events or null."""
import importlib.util
import json
import os
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

    def test_diff_stat_lists_new_files(self):
        self.assertEqual(delegate.diff_stat(self.repo), "")
        (self.repo / "new.py").write_text("x = 1\n")
        (self.repo / "caf\u00e9.txt").write_text("y\n")
        self.assertEqual(delegate.diff_stat(self.repo), "?? caf\u00e9.txt\n?? new.py")
        (self.repo / "a.txt").write_text("changed\n")
        stat = delegate.diff_stat(self.repo)
        self.assertIn("a.txt", stat.splitlines()[0])
        self.assertTrue(stat.endswith("?? caf\u00e9.txt\n?? new.py"))

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
        user = Path(self.temp.name) / "opencode.json"
        user.write_text(json.dumps({"provider": {"vercel": {"models": {"x": {}}}}, "permission": {"bash": "allow"}}))
        path = delegate.run_config(user)
        self.addCleanup(lambda: Path(path).exists() and Path(path).unlink())
        cfg = json.loads(Path(path).read_text())
        self.assertEqual(cfg["permission"]["skill"], {"*": "deny"})
        self.assertEqual(cfg["provider"]["vercel"]["models"], {"x": {}})
        # A string bash rule becomes the map's "*" entry, then our denies.
        self.assertEqual(list(cfg["permission"]["bash"].items())[0], ("*", "allow"))
        self.assertEqual(cfg["permission"]["bash"]["curl"], "deny")
        self.assertEqual(cfg["permission"]["bash"]["git push *"], "deny")
        # A user who denied every command keeps that denial (reviewer round 1).
        user.write_text(json.dumps({"permission": {"bash": "deny"}}))
        path3 = delegate.run_config(user)
        self.addCleanup(lambda: Path(path3).unlink())
        self.assertEqual(json.loads(Path(path3).read_text())["permission"]["bash"]["*"], "deny")
        # No user config at all still yields a valid run config.
        path2 = delegate.run_config(Path(self.temp.name) / "missing.json")
        self.addCleanup(lambda: Path(path2).unlink())
        cfg2 = json.loads(Path(path2).read_text())
        self.assertEqual(cfg2["permission"]["skill"], {"*": "deny"})
        for key in ("external_directory", "webfetch", "websearch", "task"):
            self.assertEqual(cfg2["permission"][key], "deny")

    def test_run_config_guards_win_over_the_user_map_and_come_last(self):
        # The later matching rule wins in OpenCode and a trailing "*": "allow" cancels
        # every deny before it (probed 2026-09-21): the user's entries stay, ours close.
        user = Path(self.temp.name) / "opencode.json"
        user.write_text(json.dumps({"permission": {"bash": {"curl *": "allow", "*": "allow", "make *": "ask"}}}))
        path = delegate.run_config(user)
        self.addCleanup(lambda: Path(path).unlink())
        bash = json.loads(Path(path).read_text())["permission"]["bash"]
        self.assertEqual(bash["curl *"], "deny")
        self.assertEqual(bash["make *"], "ask")
        keys = list(bash)
        self.assertEqual(keys[:2], ["*", "make *"])
        self.assertTrue(all(bash[k] == "deny" for k in keys[2:]))
        self.assertEqual(len(keys), 2 + 2 * len(delegate.DENIED_BASH))

    def test_run_config_guards_close_the_permission_map_too(self):
        # A wildcard at the permission level after our keys would reopen them
        # (reviewer round 4); a string permission is the user's rule for everything.
        user = Path(self.temp.name) / "opencode.json"
        user.write_text(json.dumps({"permission": {"bash": "allow", "webfetch": "allow", "*": "allow"}}))
        path = delegate.run_config(user)
        self.addCleanup(lambda: Path(path).unlink())
        perm = json.loads(Path(path).read_text())["permission"]
        self.assertEqual(list(perm)[0], "*")
        self.assertEqual(list(perm)[-1], "bash")
        self.assertEqual(perm["webfetch"], "deny")
        self.assertEqual(perm["bash"]["*"], "allow")
        user.write_text(json.dumps({"permission": "allow"}))
        path2 = delegate.run_config(user)
        self.addCleanup(lambda: Path(path2).unlink())
        perm = json.loads(Path(path2).read_text())["permission"]
        self.assertEqual(list(perm)[0], "*")
        self.assertEqual(perm["task"], "deny")

    def test_run_passes_the_run_config_in_env_and_removes_it(self):
        seen = {}

        def fake_invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            seen["config"] = env["OPENCODE_CONFIG"]
            seen["pwd"] = env["PWD"]
            seen["dir"] = cmd[cmd.index("--dir") + 1]
            seen["exists"] = Path(env["OPENCODE_CONFIG"]).exists()
            seen["caps"] = (max_cost, max_steps)
            return 0, FIXTURE.read_text(), ""
        with mock.patch.object(delegate, "invoke", fake_invoke):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                                  max_cost=2.0, max_steps=10)
        self.assertTrue(seen["exists"])
        self.assertFalse(Path(seen["config"]).exists())
        self.assertEqual(seen["caps"], (2.0, 10))
        self.assertEqual(seen["pwd"], str(self.repo.resolve()))
        self.assertEqual(seen["dir"], str(self.repo.resolve()))
        self.assertIsNone(result["error"])

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


if __name__ == "__main__":
    unittest.main()
