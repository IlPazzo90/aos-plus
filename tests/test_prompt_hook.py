"""aos-prompt-hook: advisory routing hint, and the silence rules that keep it out of the way."""
import importlib.util
import json
import os
import shutil
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

    def test_malformed_transcript_path_still_yields_the_hint(self):
        # A NUL byte in transcript_path raises ValueError on open: the session-model
        # probe must swallow it and leave the plain hint (no session suffix).
        output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                              "transcript_path": "bad\x00path"}, self.registry)
        self.assertIsNotNone(output)
        self.assertIn("hookSpecificOutput", output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("AOS", body)
        self.assertNotIn("sessione su", body)


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


class SessionModelTests(unittest.TestCase):
    """The session-model probe drives the non-premium hint suffix."""

    def setUp(self):
        self.registry = hook._load_orchestrate().load_registry()

    def _transcript(self, lines):
        import tempfile
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "transcript.jsonl"
        path.write_text("".join(line + "\n" for line in lines))
        return str(path)

    def test_a_fifo_transcript_is_not_opened(self):
        import tempfile
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        fifo = Path(directory) / "transcript.fifo"
        os.mkfifo(str(fifo))
        # open() on a FIFO without a writer would block forever.
        with mock.patch("builtins.open", side_effect=AssertionError("opened a FIFO")):
            self.assertIsNone(hook._model_from_transcript(str(fifo)))

    def test_a_long_skill_list_never_drops_the_model_warning(self):
        class Module:
            def classify(self, prompt, adaptive):
                return {"domains": ["ENGINEERING"], "task_type": "feature"}
            def build_profile(self, **kwargs):
                return {}
            def decompose(self, profile, adaptive):
                return range(20)
            def select_skills(self, bundle, profile, adaptive):
                return ["skill_%d_%s" % (bundle, "x" * 20)]
        with mock.patch.object(hook, "premium_names", return_value=("fable", "gpt-6-astra")), \
                mock.patch.object(hook, "accepted_session_models", return_value=("fable", "gpt-6-astra")), \
                mock.patch.object(hook, "_publish_profile"):
            line = hook._routing_hint(Module(), "valid prompt with enough words", {"adaptive": {}},
                                      {"model": "claude-sonnet-5"})
        self.assertLessEqual(len(line), 420)
        self.assertTrue(line.endswith("codex -m gpt-6-astra)"))

    def test_model_from_payload(self):
        self.assertEqual(
            hook.session_model({"model": "claude-opus-5-5", "prompt": "correggi questo bug nel file app.py"}),
            "claude-opus-5-5")

    def test_empty_payload_model_falls_back_to_transcript(self):
        path = self._transcript([
            '{"type":"user"}',
            '{"type":"assistant","message":{"model":"claude-opus-5-5"}}',
        ])
        self.assertEqual(hook.session_model({"transcript_path": path}), "claude-opus-5-5")

    def test_transcript_scans_last_line_first(self):
        # A fable line appears before the opus line; the opus line (last) wins.
        path = self._transcript([
            '{"type":"assistant","message":{"model":"claude-fable-5-1"}}',
            '{"type":"assistant","message":{"model":"claude-opus-5-5"}}',
        ])
        self.assertEqual(hook.session_model({"transcript_path": path}), "claude-opus-5-5")

    def test_synthetic_model_is_skipped(self):
        # `<synthetic>` is the last assistant line: it must be skipped, leaving
        # the earlier fable line as the answer.
        path = self._transcript([
            '{"type":"assistant","message":{"model":"claude-fable-5-1"}}',
            '{"type":"assistant","message":{"model":"<synthetic>"}}',
        ])
        self.assertEqual(hook.session_model({"transcript_path": path}), "claude-fable-5-1")

    def test_codex_turn_context_model(self):
        path = self._transcript([
            '{"type":"turn_context","payload":{"model":"gpt-5.6-sol"}}',
        ])
        self.assertEqual(hook.session_model({"transcript_path": path}), "gpt-5.6-sol")

    def test_transcript_bigger_than_tail_still_finds_the_last_model(self):
        # Total file exceeds 256 KiB, so the head is unreachable; the model in the
        # final line must still be found.
        pad = "x" * (hook.TRANSCRIPT_TAIL_BYTES + 1024)
        path = self._transcript([
            '{"type":"assistant","message":{"model":"claude-fable-5-1"},"pad":"' + pad + '"}',
            '{"type":"assistant","message":{"model":"claude-opus-5-5"}}',
        ])
        self.assertEqual(hook.session_model({"transcript_path": path}), "claude-opus-5-5")

    def test_missing_transcript_returns_none(self):
        self.assertIsNone(hook.session_model({"transcript_path": "/does/not/exist.jsonl"}))

    def test_unreadable_short_transcript_returns_none(self):
        self.assertIsNone(hook.session_model({"transcript_path": None}))

    def test_premium_names_from_config(self):
        names = hook.premium_names()
        self.assertEqual(names, ("fable", "gpt-6-astra"))

    def test_accepted_session_models_includes_the_session_list(self):
        with mock.patch.object(hook, "_premium", return_value={
                "claude_model": "fable", "codex_model": "gpt-6-astra",
                "session_models": ["fable", "opus", "gpt-6-astra"]}):
            self.assertIn("opus", hook.accepted_session_models())
            self.assertIn("fable", hook.accepted_session_models())

    def test_accepted_session_models_ignores_an_invalid_list(self):
        for bad in ("not-a-list", ["", 5], []):
            with mock.patch.object(hook, "_premium", return_value={
                    "claude_model": "fable", "codex_model": "gpt-6-astra",
                    "session_models": bad}):
                self.assertEqual(hook.accepted_session_models(), ("fable", "gpt-6-astra"))

    def test_accepted_session_models_without_the_key_is_the_premium_pair(self):
        with mock.patch.object(hook, "_premium", return_value={
                "claude_model": "fable", "codex_model": "gpt-6-astra"}):
            self.assertEqual(hook.accepted_session_models(), ("fable", "gpt-6-astra"))

    def test_is_premium_substring_match(self):
        names = ("fable", "gpt-6-astra")
        self.assertTrue(hook.is_premium("claude-fable-5-1", names))
        self.assertTrue(hook.is_premium("gpt-6-astra", names))
        self.assertFalse(hook.is_premium("claude-opus-5-5", names))
        self.assertFalse(hook.is_premium("claude-sonnet-5", names))
        self.assertFalse(hook.is_premium("gpt-5.6-sol", names))


class NonPremiumHintTests(unittest.TestCase):
    """A known, non-premium session model appends the change-model advisory."""

    def setUp(self):
        self.registry = hook._load_orchestrate().load_registry()

    def test_non_premium_model_appends_warning_to_a_real_hint(self):
        output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                              "model": "claude-sonnet-5"}, self.registry)
        self.assertIsNotNone(output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sessione su claude-sonnet-5", body)
        self.assertIn("/model fable", body)
        self.assertIn("codex -m gpt-6-astra", body)

    def test_premium_model_produces_no_warning(self):
        output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                              "model": "claude-fable-5-1"}, self.registry)
        self.assertIsNotNone(output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("sessione su", body)

    def test_silent_prompt_stays_silent_even_with_non_premium_model(self):
        # GENERAL with no task type -> no hint at all, so no warning alone.
        output = hook.decide({"prompt": "zzz quux fnord blarg", "model": "claude-opus-5-5"},
                             self.registry)
        self.assertIsNone(output)

    def test_opus_session_model_is_accepted_with_the_repo_config(self):
        # The owner runs the main session on Opus by choice: the hook must stop
        # asking for a switch then, but still warn on weaker models.
        output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                              "model": "claude-opus-5-5"}, self.registry)
        self.assertIsNotNone(output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("sessione su", body)

    def test_sonnet_session_model_still_warns(self):
        output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                              "model": "claude-sonnet-5"}, self.registry)
        self.assertIsNotNone(output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sessione su claude-sonnet-5", body)

    def test_config_without_session_models_warns_on_opus_again(self):
        with mock.patch.object(hook, "_premium", return_value={
                "claude_model": "fable", "codex_model": "gpt-6-astra"}):
            output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                                  "model": "claude-opus-5-5"}, self.registry)
        self.assertIsNotNone(output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sessione su claude-opus-5-5", body)

    def test_an_invalid_session_models_value_is_ignored(self):
        with mock.patch.object(hook, "_premium", return_value={
                "claude_model": "fable", "codex_model": "gpt-6-astra",
                "session_models": "opus"}):
            output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                                  "model": "claude-opus-5-5"}, self.registry)
        self.assertIsNotNone(output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sessione su claude-opus-5-5", body)


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
