"""aos-prompt-hook: advisory routing hint, and the silence rules that keep it out of the way."""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "bin/aos-prompt-hook.py"

spec = importlib.util.spec_from_file_location("prompt_hook", SCRIPT)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def payload(prompt):
    return {"prompt": prompt}


class SilentRuleTests(unittest.TestCase):
    def test_env_off_is_silent(self):
        with mock.patch.dict(os.environ, {"AOS_PROMPT_HOOK": "off"}):
            self.assertEqual(hook.silent_reason("correggi questo bug nel file app.py"),
                             "AOS_PROMPT_HOOK=off")

    def test_slash_command_is_silent(self):
        self.assertEqual(hook.silent_reason("/model opus"), "host control prefix")

    def test_bang_prefix_is_silent(self):
        self.assertEqual(hook.silent_reason("!compact"), "host control prefix")

    def test_short_prompt_is_silent(self):
        self.assertEqual(hook.silent_reason("ciao"), "too short to classify")

    def test_empty_prompt_is_silent(self):
        self.assertEqual(hook.silent_reason(""), "empty prompt")

    def test_a_real_task_is_not_silent(self):
        self.assertIsNone(hook.silent_reason("correggi questo bug nel file app.py"))


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.registry = hook._load_orchestrate().load_registry()

    def test_a_bug_report_yields_a_hook_specific_output(self):
        output = hook.decide(payload("correggi questo bug nel file app.py"), self.registry)
        self.assertIsNotNone(output)
        self.assertIn("hookSpecificOutput", output)
        body = output["hookSpecificOutput"]
        self.assertEqual(body["hookEventName"], "UserPromptSubmit")
        self.assertIn("AOS", body["additionalContext"])

    def test_only_general_with_no_task_type_stays_silent(self):
        output = hook.decide(payload("zzz quux fnord blarg"), self.registry)
        self.assertIsNone(output)

    def test_a_missing_prompt_stays_silent(self):
        output = hook.decide({}, self.registry)
        self.assertIsNone(output)

    def test_a_non_object_payload_stays_silent(self):
        output = hook.decide(["not", "an", "object"], self.registry)
        self.assertIsNone(output)


class MainTests(unittest.TestCase):
    def _run(self, stdin_bytes, env=None):
        import contextlib
        import io
        fake_stdin = mock.Mock()
        fake_stdin.buffer.read = mock.Mock(return_value=stdin_bytes)
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False), \
                mock.patch("sys.stdin", fake_stdin), \
                contextlib.redirect_stdout(out):
            code = hook.main()
        return code, out.getvalue()

    def test_main_prints_one_json_object_for_a_task(self):
        hook_input = json.dumps({"prompt": "correggi questo bug nel file app.py"})
        code, stdout = self._run(hook_input.encode("utf-8"))
        self.assertEqual(code, 0)
        value = json.loads(stdout)
        self.assertIn("hookSpecificOutput", value)

    def test_main_returns_zero_with_no_output_on_malformed_stdin(self):
        code, stdout = self._run(b"{not json")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")

    def test_main_returns_zero_with_env_off(self):
        code, stdout = self._run(b"{}", {"AOS_PROMPT_HOOK": "off"})
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")

    def test_main_returns_zero_on_empty_stdin(self):
        code, stdout = self._run(b"")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")

    def test_main_returns_zero_on_oversized_stdin(self):
        code, stdout = self._run(b"x" * (hook.MAX_STDIN_BYTES + 1))
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")


class ReviewFollowUpTests(unittest.TestCase):
    """3.0.1, from Codex's review of the installed hook."""

    _run = MainTests._run

    def test_host_notifications_stay_silent(self):
        for text in ("<task-notification>\n<task-id>x</task-id> completed</task-notification>",
                     "[SYSTEM NOTIFICATION - NOT USER INPUT] Background command completed"):
            with self.subTest(text=text[:20]):
                code, stdout = self._run(json.dumps({"prompt": text}).encode("utf-8"))
                self.assertEqual((code, stdout), (0, ""))

    def test_a_silent_prompt_loads_no_module_or_configuration(self):
        with mock.patch.object(hook, "_load_orchestrate") as load:
            code, stdout = self._run(json.dumps({"prompt": "ciao"}).encode("utf-8"))
        self.assertEqual((code, stdout), (0, ""))
        load.assert_not_called()

    def test_the_hook_writes_no_bytecode(self):
        import shutil
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(SCRIPT.parents[1] / "bin", root / "bin",
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(SCRIPT.parents[1] / "config", root / "config")
            result = subprocess.run([sys.executable, str(root / "bin/aos-prompt-hook.py")],
                                    input=json.dumps({"prompt": "correggi il bug nel parser del login"}),
                                    capture_output=True, text=True, timeout=30)
            self.assertIn("hookSpecificOutput", result.stdout)
            self.assertEqual(list(root.rglob("__pycache__")), [])


class StdlibOnlyTests(unittest.TestCase):
    def test_hook_is_stdlib_only(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for banned in ("import subprocess", "import socket", "import urllib", "import requests"):
            self.assertNotIn(banned, source, banned)


if __name__ == "__main__":
    unittest.main()


class StatusProfileTests(unittest.TestCase):
    """The hook feeds domains and task type to the Claude Code status bar."""

    def setUp(self):
        import tempfile
        self.registry = hook._load_orchestrate().load_registry()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.record = Path(self.directory.name) / "sess-hook.json"

    def run_hook(self, env, body):
        with mock.patch.dict(os.environ, env, clear=False):
            return hook.decide(body, self.registry)

    def test_claude_session_gets_domains_and_task_type(self):
        self.run_hook({"AOS_STATUS_DIR": self.directory.name, "CLAUDE_PROJECT_DIR": "/x"},
                      {"prompt": "correggi questo bug nel file app.py", "session_id": "sess-hook"})
        record = json.loads(self.record.read_text())
        self.assertIn("ENGINEERING", record["domains"])
        self.assertIn("ENG", record["segment"])

    def test_without_claude_code_nothing_is_written(self):
        env = {"AOS_STATUS_DIR": self.directory.name}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            hook.decide({"prompt": "correggi questo bug nel file app.py", "session_id": "sess-hook"},
                        self.registry)
        self.assertFalse(self.record.exists())

    def test_unsafe_session_id_is_not_a_file_name(self):
        self.run_hook({"AOS_STATUS_DIR": self.directory.name, "CLAUDE_PROJECT_DIR": "/x"},
                      {"prompt": "correggi questo bug nel file app.py", "session_id": "../evil"})
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])
