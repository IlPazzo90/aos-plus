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


class StdlibOnlyTests(unittest.TestCase):
    def test_hook_is_stdlib_only(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for banned in ("import subprocess", "import socket", "import urllib", "import requests"):
            self.assertNotIn(banned, source, banned)


if __name__ == "__main__":
    unittest.main()
