"""aos-delegate: the guards around one worker run; usage read from normalized JSON events or null."""
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
# The normalized stream the runtime adapters produce: one text part, one step_finish.
FIXTURE_TEXT = "\n".join([
    json.dumps({"type": "text", "sessionID": "ses_f3fe2b386ffeTxRC3ZUdXLKOVd", "part": {"type": "text", "text": "OK"}}),
    json.dumps({"type": "step_finish", "sessionID": "ses_f3fe2b386ffeTxRC3ZUdXLKOVd",
                "part": {"reason": "stop", "tokens": {"input": 44691, "output": 34, "reasoning": 0,
                                                       "cache": {"read": 1792, "write": 0}}, "cost": 0}}),
]) + "\n"

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
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git_repo(self.repo)

    @staticmethod
    def runner(repo, model, brief, timeout, max_cost, max_steps):
        """The executor's role in these tests: hand a command line to delegate.invoke."""
        return delegate.invoke(["worker", "--", brief], str(repo), timeout, max_cost, max_steps, None)

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
        usage = delegate.parse_usage(FIXTURE_TEXT)
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
        self.assertIsNone(delegate.parse_error(FIXTURE_TEXT))
        with mock.patch.object(delegate, "invoke", return_value=(1, ev, "")):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False, runner=self.runner)
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
        with mock.patch.object(delegate, "invoke", return_value=(0, FIXTURE_TEXT, "")) as invoke:
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False, runner=self.runner)
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
        """A stand-in for a runtime: prints the given events, optionally sleeps, optionally never exits."""
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

    def test_record_says_when_the_worker_moved_head(self):
        def fake_invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            (self.repo / "a.txt").write_text("moved\n")
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-qam", "x"], check=True)
            return 0, "", ""
        with mock.patch.object(delegate, "invoke", fake_invoke), \
                mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.temp.name) / "xdg")}):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False, runner=self.runner)
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
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False, runner=self.runner)
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
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False, runner=self.runner)
        self.assertTrue(result["git_meta_changed"])
        self.assertFalse(marker.exists())
        # Even with the hook in place and no fingerprint, the script's git ignores it.
        self.assertIn("a.txt", delegate.diff_stat(self.repo))
        self.assertFalse(marker.exists())

    def test_cap_exit_is_named_in_the_record(self):
        with mock.patch.object(delegate, "invoke", return_value=(delegate.EXIT_CAP, "", "")):
            result = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False, runner=self.runner)
        self.assertEqual(result["exit_code"], delegate.EXIT_CAP)
        self.assertIn("tetto", result["error"])

    def test_invoke_never_reads_stdin(self):
        # the CLIs wait on a tty stdin; a delegated run must not.
        code, out, err = delegate.invoke([sys.executable, "-c", "import sys;print(sys.stdin.read()=='')"],
                                         self.temp.name, 5)
        self.assertEqual((code, out.strip()), (0, "True"))

    def test_retry_on_worker_owned_dirty_tree_is_allowed(self):
        # A retry of the same task may run on the dirt the previous attempt left:
        # the baseline HEAD is unchanged and every dirty path is worker-owned.
        state_file = Path(self.temp.name) / "state.json"

        def worker(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            (self.repo / "a.txt").write_text("worker\n")
            return 0, "", ""

        with mock.patch.object(delegate, "invoke", worker):
            first = delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                                 state_file=state_file, runner=self.runner)
        self.assertTrue(first["initial_repo_clean"])
        self.assertFalse(first["dirty_owned_by_current_run"])
        self.assertFalse(first["dirty_conflict_detected"])
        self.assertEqual(first["retry_dirty_policy"], "clean")
        self.assertTrue(state_file.exists())
        with mock.patch.object(delegate, "invoke", worker):
            second = delegate.run(self.repo, "vercel/x/y", "brief2", timeout=5, allow_dirty=False,
                                  state_file=state_file, runner=self.runner)
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
                         state_file=state_file, runner=self.runner)
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
                         state_file=state_file, runner=self.runner)
        (self.repo / "b.txt").write_text("user\n")
        with mock.patch.object(delegate, "invoke") as invoke, self.assertRaises(SystemExit) as ctx:
            delegate.run(self.repo, "vercel/x/y", "brief2", timeout=5, allow_dirty=False,
                         state_file=state_file, runner=self.runner)
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
                         state_file=state_file, runner=self.runner)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qam", "external"], check=True)
        with mock.patch.object(delegate, "invoke") as invoke, self.assertRaises(SystemExit) as ctx:
            delegate.run(self.repo, "vercel/x/y", "brief2", timeout=5, allow_dirty=False,
                         state_file=state_file, runner=self.runner)
        self.assertEqual(ctx.exception.code, delegate.EXIT_CONFLICT)
        invoke.assert_not_called()

    def test_retry_state_survives_a_missing_parent(self):
        # The state file lives wherever the caller points; a missing parent is created.
        state_file = Path(self.temp.name) / "nested" / "dir" / "state.json"

        def worker(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
            return 0, "", ""

        with mock.patch.object(delegate, "invoke", worker):
            delegate.run(self.repo, "vercel/x/y", "brief", timeout=5, allow_dirty=False,
                         state_file=state_file, runner=self.runner)
        self.assertTrue(state_file.exists())
        self.assertEqual(json.loads(state_file.read_text())["schema"], delegate.RETRY_STATE_SCHEMA)


if __name__ == "__main__":
    unittest.main()
