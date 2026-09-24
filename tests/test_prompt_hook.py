"""aos-prompt-hook: advisory routing hint, and the silence rules that keep it out of the way."""
import importlib.util
import json
import os
import shutil
import time
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
        self.assertLessEqual(len(line), hook.LINE_CAP)
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

    def test_accepted_session_models_is_the_session_list_alone(self):
        with mock.patch.object(hook, "_premium", return_value={
                "claude_model": "fable", "codex_model": "gpt-6-astra",
                "session_models": ["opus", "gpt-6-astra"]}):
            self.assertEqual(hook.accepted_session_models(), ("opus", "gpt-6-astra"))
            self.assertEqual(hook.switch_targets(), ("opus", "gpt-6-astra"))

    def test_switch_targets_fall_back_to_the_premium_pair(self):
        with mock.patch.object(hook, "_premium", return_value={
                "claude_model": "fable", "codex_model": "gpt-6-astra",
                "session_models": ["opus"]}):
            self.assertEqual(hook.switch_targets(), ("opus", "gpt-6-astra"))

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
        self.assertIn("/model opus", body)
        self.assertIn("codex -m gpt-6-astra", body)

    def test_fable_session_is_told_it_burns_premium_tokens(self):
        # The owner keeps Fable as planner/reviewer, not as the session model.
        output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                              "model": "claude-fable-5-1"}, self.registry)
        self.assertIsNotNone(output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sessione su claude-fable-5-1: consuma token premium", body)
        self.assertIn("/model opus", body)

    def test_codex_premium_session_model_produces_no_warning(self):
        output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                              "model": "gpt-6-astra"}, self.registry)
        self.assertNotIn("sessione su", output["hookSpecificOutput"]["additionalContext"])

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


def _usage_line(tokens, **extra):
    line = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": tokens - 110,
        "cache_creation_input_tokens": 100, "output_tokens": 5}}}
    line.update(extra)
    return json.dumps(line)


class ContextSignalTests(unittest.TestCase):
    """The context budget reaches the model and, under Claude Code, the user."""

    def setUp(self):
        import tempfile
        self.registry = hook._load_orchestrate().load_registry()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        patcher = mock.patch.dict(os.environ, {"AOS_STATUS_DIR": self.directory})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def transcript(self, *lines):
        path = Path(self.directory) / "t.jsonl"
        path.write_text("".join(line + "\n" for line in lines))
        return str(path)

    def decide(self, lines, prompt="correggi questo bug nel file app.py", claude=True):
        body = {"prompt": prompt, "transcript_path": self.transcript(*lines), "session_id": "sess-ctx"}
        env = {"CLAUDE_PROJECT_DIR": "/x"} if claude else {}
        with mock.patch.dict(os.environ, env):
            return hook.decide(body, self.registry)

    def test_red_context_reaches_the_model_and_the_user(self):
        output = self.decide([_usage_line(350000)])
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("contesto 350k token, RED (soft 200k, hard 320k)", body)
        self.assertIn("/compact", output["systemMessage"])
        self.assertLessEqual(len(body), hook.LINE_CAP)

    def test_orange_context(self):
        body = self.decide([_usage_line(250000)])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ORANGE", body)

    def test_green_context_adds_nothing(self):
        output = self.decide([_usage_line(100000)])
        self.assertNotIn("contesto", output["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("systemMessage", output)

    def test_sidechain_and_synthetic_lines_are_skipped(self):
        self.assertEqual(hook.context_tokens({"transcript_path": self.transcript(
            _usage_line(300000),
            _usage_line(20000, isSidechain=True),
            json.dumps({"type": "assistant", "message": {"model": "<synthetic>",
                                                         "usage": {"input_tokens": 0}}}),
        )}), 300000)

    def test_codex_token_count_without_system_message(self):
        lines = [json.dumps({"type": "turn_context", "payload": {"model": "gpt-6-astra"}}),
                 json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                     "last_token_usage": {"input_tokens": 300000, "cached_input_tokens": 290000}}}})]
        output = self.decide(lines, claude=False)
        self.assertIn("RED", output["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("systemMessage", output)

    def test_red_context_speaks_even_when_the_classifier_is_silent(self):
        output = self.decide([_usage_line(400000)], prompt="zzz quux fnord blarg")
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(body.startswith("AOS · contesto 400k"))

    def test_compact_command_stays_silent(self):
        self.assertIsNone(self.decide([_usage_line(400000)], prompt="/compact keep the plan"))

    def test_broken_transcript_leaves_the_plain_hint(self):
        output = self.decide(["{not json", "[]"])
        self.assertNotIn("contesto", output["hookSpecificOutput"]["additionalContext"])

    def test_only_the_tail_is_read(self):
        pad = "x" * (hook.TRANSCRIPT_TAIL_BYTES + 1024)
        path = self.transcript(_usage_line(50000, pad=pad), _usage_line(330000))
        self.assertEqual(hook.context_tokens({"transcript_path": path}), 330000)


    def test_long_hint_keeps_warning_reminder_and_context_whole(self):
        class Module:
            def classify(self, prompt, adaptive):
                return {"domains": ["ENGINEERING"], "task_type": "feature"}
            def build_profile(self, **kwargs):
                return {}
            def decompose(self, profile, adaptive):
                return range(30)
            def select_skills(self, bundle, profile, adaptive):
                return ["skill_%d_%s" % (bundle, "x" * 20)]
        with mock.patch.object(hook, "reroute_reminder", return_value=" · REMINDER_WHOLE"), \
                mock.patch.object(hook, "_publish_profile"), \
                mock.patch.object(hook, "context_signal", return_value=(" · CONTEXT_WHOLE", "m")), \
                mock.patch.object(hook, "session_model", return_value="claude-sonnet-5"):
            output = hook.decide({"prompt": "valid prompt with enough words"}, {"adaptive": {}},
                                 module=Module())
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(body), hook.LINE_CAP)
        self.assertIn("sessione su claude-sonnet-5", body)
        self.assertIn("REMINDER_WHOLE", body)
        self.assertTrue(body.endswith("CONTEXT_WHOLE"))
        self.assertIn("per T1+ censimento", body)

    def test_overflowing_domains_never_cut_the_context_segment(self):
        class Module:
            def classify(self, prompt, adaptive):
                return {"domains": ["D" * 60] * 6, "task_type": "feature"}
            def build_profile(self, **kwargs):
                return {}
            def decompose(self, profile, adaptive):
                return []
        segment = " · contesto 400k token, RED (soft 200k, hard 320k): chiudi … /compact al punto di pausa"
        with mock.patch.object(hook, "reroute_reminder", return_value=" · REMINDER_WHOLE"), \
                mock.patch.object(hook, "_publish_profile"), \
                mock.patch.object(hook, "session_model", return_value="claude-" + "x" * 53):
            line = hook._routing_hint(Module(), "valid prompt with enough words", {"adaptive": {}},
                                      {}, segment)
        self.assertEqual(len(line), hook.LINE_CAP)
        self.assertTrue(line.endswith(segment))
        self.assertIn("REMINDER_WHOLE", line)
        self.assertIn(" · per T1+ censimento e route con /aos ($aos in Codex)", line)

    def test_invalid_counters_count_zero_instead_of_older_lines(self):
        line = json.dumps({"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
            "input_tokens": True, "cache_read_input_tokens": "bad",
            "cache_creation_input_tokens": None}}})
        path = self.transcript(_usage_line(350000), line)
        self.assertEqual(hook.context_tokens({"transcript_path": path}), 0)

    def test_unsafe_session_id_gets_no_system_message(self):
        body = {"prompt": "correggi questo bug nel file app.py", "session_id": "../evil",
                "transcript_path": self.transcript(_usage_line(400000))}
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": "/x"}):
            output = hook.decide(body, self.registry)
        self.assertIn("RED", output["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("systemMessage", output)


class RerouteReminderTests(unittest.TestCase):
    """A session that routed once is reminded to route a new task again."""

    def setUp(self):
        import tempfile
        self.registry = hook._load_orchestrate().load_registry()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        patcher = mock.patch.dict(os.environ, {"AOS_STATUS_DIR": self.directory,
                                               "CLAUDE_PROJECT_DIR": "/x"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, **record):
        record.setdefault("expires_at", time.time() + 3600)
        (Path(self.directory) / "sess-rr.json").write_text(json.dumps(record))

    def body(self, prompt="correggi questo bug nel file app.py"):
        output = hook.decide({"prompt": prompt, "session_id": "sess-rr"}, self.registry)
        return output["hookSpecificOutput"]["additionalContext"] if output else None

    def test_routed_session_is_reminded_with_minutes(self):
        self.write(tier="T2", risk="high", routed_at=int(time.time()) - 30 * 60 - 5)
        self.assertIn("tier attivo T2/high da 30 min: se è un task nuovo", self.body())

    def test_missing_routed_at_omits_minutes(self):
        self.write(tier="T1", risk="LOW")
        body = self.body()
        self.assertIn("tier attivo T1/LOW: se è un task nuovo", body)

    def test_no_tier_no_reminder(self):
        self.write(domains=["GENERAL"])
        self.assertNotIn("tier attivo", self.body())

    def test_expired_record_no_reminder(self):
        self.write(tier="T2", risk="LOW", expires_at=time.time() - 1)
        self.assertNotIn("tier attivo", self.body())
        # Publishing the new profile must not revive the expired routing.
        self.assertNotIn("tier attivo", self.body())

    def test_corrupt_expiry_no_reminder(self):
        self.write(tier="T2", risk="HIGH", expires_at="corrupt", routed_at=1)
        self.assertNotIn("tier attivo", self.body())

    def test_non_finite_or_negative_epochs_are_junk(self):
        self.write(tier="T2", risk="HIGH", expires_at=float("nan"), routed_at=1)
        self.assertNotIn("tier attivo", self.body())
        self.write(tier="T2", risk="HIGH", routed_at=-1e300)
        body = self.body()
        self.assertIn("tier attivo T2/HIGH: se è un task nuovo", body)
        self.assertLessEqual(len(body), hook.LINE_CAP)

    def test_corrupt_tier_is_not_a_routing(self):
        for tier in ("BANANA", " "):
            self.write(tier=tier, risk="HIGH")
            body = self.body("aggiungi una nuova feature al login")
            self.assertNotIn("tier attivo", body)
            self.assertIn("nessun routing registrato", body)

    def test_prompt_without_task_type_no_reminder(self):
        self.write(tier="T2", risk="LOW")
        self.assertEqual(hook.reroute_reminder({"session_id": "sess-rr"}, None), "")

    def test_outside_claude_code_no_reminder(self):
        self.write(tier="T2", risk="LOW")
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.assertNotIn("tier attivo", self.body())


class SessionModelFallbackTests(unittest.TestCase):
    """The status record's session_model reaches the first prompt's model probe."""

    def setUp(self):
        import tempfile
        self.registry = hook._load_orchestrate().load_registry()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        patcher = mock.patch.dict(os.environ, {"AOS_STATUS_DIR": self.directory,
                                               "CLAUDE_PROJECT_DIR": "/x"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, session="sess-sm", **record):
        record.setdefault("expires_at", time.time() + 3600)
        (Path(self.directory) / (session + ".json")).write_text(json.dumps(record))

    def test_record_session_model_warns_on_the_first_prompt(self):
        self.write(session_model="claude-fable-5-1")
        output = hook.decide({"prompt": "correggi questo bug nel file app.py",
                              "session_id": "sess-sm"}, self.registry)
        self.assertIsNotNone(output)
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("sessione su claude-fable-5-1: consuma token premium", body)

    def test_transcript_model_wins_over_the_record(self):
        self.write(session_model="claude-fable-5-1")
        path = Path(self.directory) / "t.jsonl"
        path.write_text('{"type":"assistant","message":{"model":"claude-opus-5-5"}}\n')
        self.assertEqual(hook.session_model({"session_id": "sess-sm", "transcript_path": str(path)}),
                         "claude-opus-5-5")

    def test_outside_claude_code_the_record_is_not_read(self):
        self.write(session_model="claude-fable-5-1")
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.assertIsNone(hook.session_model({"session_id": "sess-sm"}))

    def test_record_without_session_model_returns_none(self):
        self.write(domains=["ENGINEERING"])
        self.assertIsNone(hook.session_model({"session_id": "sess-sm"}))


class UnroutedNoticeTests(unittest.TestCase):
    """A work prompt in a session that never routed gets the unrouted notice."""

    def setUp(self):
        import tempfile
        self.registry = hook._load_orchestrate().load_registry()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        patcher = mock.patch.dict(os.environ, {"AOS_STATUS_DIR": self.directory,
                                               "CLAUDE_PROJECT_DIR": "/x"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, session="sess-un", **record):
        record.setdefault("expires_at", time.time() + 3600)
        (Path(self.directory) / (session + ".json")).write_text(json.dumps(record))

    def body(self, prompt="correggi questo bug nel file app.py", session="sess-un"):
        output = hook.decide({"prompt": prompt, "session_id": session}, self.registry)
        return output["hookSpecificOutput"]["additionalContext"] if output else None

    def test_feature_prompt_with_no_record_gets_the_notice(self):
        body = self.body("aggiungi una nuova feature al modulo di login")
        self.assertIn("nessun routing registrato", body)

    def test_bug_fix_prompt_with_a_record_without_tier_gets_the_notice(self):
        self.write(domains=["ENGINEERING"])
        self.assertIn("nessun routing registrato", self.body())

    def test_review_prompt_with_no_tier_gets_nothing(self):
        self.write(domains=["ENGINEERING"])
        self.assertNotIn("nessun routing registrato",
                         self.body("controlla e verifica questo codice per la review finale"))

    def test_research_prompt_with_no_tier_gets_nothing(self):
        self.assertNotIn("nessun routing registrato",
                         self.body("fammi una ricerca sulle fonti primarie del tema"))

    def test_record_with_tier_keeps_the_existing_reminder(self):
        self.write(tier="T2", risk="LOW")
        body = self.body()
        self.assertIn("tier attivo T2/LOW", body)
        self.assertNotIn("nessun routing registrato", body)

    def test_codex_gets_nothing(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.write(tier="T2", risk="LOW")
        body = self.body()
        self.assertNotIn("nessun routing registrato", body)
        self.assertNotIn("tier attivo", body)

    def test_expired_record_gets_the_notice(self):
        self.write(tier="T2", risk="LOW", expires_at=time.time() - 1)
        self.assertIn("nessun routing registrato", self.body())

    def test_corrupt_expiry_gets_the_notice(self):
        self.write(tier="T2", risk="LOW", expires_at="corrupt")
        self.assertIn("nessun routing registrato", self.body())

    def test_prompt_without_task_type_gets_nothing(self):
        self.assertEqual(hook.reroute_reminder({"session_id": "sess-un"}, None), "")


if __name__ == "__main__":
    unittest.main()
