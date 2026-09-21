import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('open_executor', ROOT / 'bin/aos-open-executor.py')
executor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(executor)


class OpenExecutorTests(unittest.TestCase):
    def config(self):
        return {'open': {'primary': 'vercel/test/winner', 'fallback': 'vercel/test/second'},
                'executors': {'default_runtime': 'opencode',
                              'runtime_order': ['opencode', 'codex-cli', 'claude-code']}}

    def test_no_opencode_selects_codex_without_changing_model(self):
        with patch.object(executor.shutil, 'which', side_effect=lambda s: '/bin/codex' if s == 'codex' else None):
            selected = executor.resolve(config=self.config())
        self.assertEqual((selected['runtime'], selected['provider'], selected['model']),
                         ('codex-cli', 'vercel', 'test/winner'))

    def test_fallback_model_can_use_each_runtime(self):
        for runtime in executor.RUNTIMES:
            selected = executor.resolve(config=self.config(), slot='fallback', runtime=runtime)
            self.assertEqual(selected['model'], 'test/second')
            self.assertEqual(selected['runtime'], runtime)

    def test_provider_override_changes_invoked_identity_too(self):
        selected = executor.resolve(config=self.config(), runtime='opencode', provider='custom')
        self.assertEqual(selected['provider'], 'custom')
        self.assertEqual(selected['model_ref'], 'custom/test/winner')

    def test_global_allow_rules_are_shadowed_without_modifying_user_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = 'prefix_rule(pattern=["python3", "private-script.py"], decision="allow")\n'
            (root / 'default.rules').write_text(original)
            rules = executor.codex_rules([root])
            self.assertIn('pattern=["python3", "private-script.py"], decision="forbidden"', rules)
            self.assertNotIn('decision="allow"', rules)
            self.assertEqual((root / 'default.rules').read_text(), original)
            (root / 'default.rules').write_text('prefix_rule(pattern=dynamic())\n')
            with self.assertRaises(ValueError):
                executor.codex_rules([root])

    def test_invalid_runtime_and_red_profile_fail_closed(self):
        with self.assertRaises(ValueError):
            executor.resolve(config=self.config(), runtime='fake')
        with self.assertRaises(ValueError):
            executor.run('.', 'vercel/test/winner', 'task', 30, False, permission_profile='RED')
        for role in ('planner', 'reviewer'):
            with self.assertRaises(ValueError):
                executor.run('.', 'vercel/test/winner', 'task', 30, False, role=role)

    def test_every_runtime_preserves_dirty_and_retry_ownership(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as state_dir:
            repo = Path(directory)
            for args in [('init', '-q'), ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                         'commit', '--allow-empty', '-qm', 'fixture')]:
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            (repo / 'unowned.py').write_text('value = 1\n')
            state = Path(state_dir) / 'retry.json'
            executor.delegate.save_retry_state(state, dict(baseline_head=executor.delegate.git_head(repo),
                                                           initial_repo_clean=True, owned=set()))
            for runtime in executor.RUNTIMES:
                with self.subTest(runtime=runtime), \
                        patch.object(executor.shutil, 'which', return_value='/fixture/runtime'), \
                        patch.object(executor, 'check_runtime'), \
                        patch.object(executor, 'provider_config', return_value=('https://example.invalid/v1', 'dummy')), \
                        patch.object(executor.delegate, 'user_config', return_value={}), \
                        patch.object(executor.delegate, 'invoke') as invoke:
                    with self.assertRaises(SystemExit) as caught:
                        executor.run(repo, 'provider/vendor/model', 'task', 30, False, runtime=runtime)
                    self.assertEqual(caught.exception.code, executor.delegate.EXIT_DIRTY)
                    with self.assertRaises(SystemExit) as caught:
                        executor.run(repo, 'provider/vendor/model', 'task', 30, False,
                                     runtime=runtime, state_file=state)
                    self.assertEqual(caught.exception.code, executor.delegate.EXIT_CONFLICT)
                    invoke.assert_not_called()

    def test_codex_uses_open_provider_and_native_restrictions(self):
        with tempfile.TemporaryDirectory() as directory:
            command = executor.codex_command(Path(directory), 'test/winner', 'task', 'https://gateway.example/v1')
        text = ' '.join(command)
        self.assertIn('model_provider="aos_open"', text)
        self.assertIn('default_permissions="aos-open"', text)
        self.assertIn('approval_policy="never"', text)
        self.assertIn('shell_environment_policy.inherit="none"', text)
        self.assertNotIn('--sandbox', command)
        self.assertNotIn('--ignore-rules', command)
        self.assertNotIn('gpt-', text)
        # Dotted CLI keys containing a quoted path did not activate project rules
        # on the installed CLI. Supply the complete trust map as a TOML table.
        self.assertTrue(any(arg.startswith('projects={') for arg in command))

    def test_claude_never_uses_subscription_or_unsandboxed_tools(self):
        command = executor.claude_command(Path('/tmp/repo'), 'test/winner', 'task')
        settings = json.loads(command[command.index('--settings') + 1])
        self.assertIn('--restricted', command)
        self.assertIn('--safe-mode', command)
        self.assertTrue(settings['sandbox']['failIfUnavailable'])
        self.assertFalse(settings['sandbox']['allowUnsandboxedCommands'])
        self.assertEqual(settings['sandbox']['network']['allowedDomains'], [])
        self.assertIn('Bash(git push *)', settings['permissions']['deny'])
        self.assertTrue(settings['permissions']['blockReadsOutsideWorkingDirectories'])
        self.assertIn({'name': 'ANTHROPIC_AUTH_TOKEN', 'mode': 'deny'},
                      settings['sandbox']['credentials']['envVars'])

    def test_claude_file_executor_does_not_claim_shell_capability(self):
        self.assertFalse(executor.CAPABILITIES['claude-code']['shell'])
        command = executor.claude_command(Path('/tmp/repo'), 'test/winner', 'task')
        self.assertNotIn('Bash', command[command.index('--tools') + 1].split(','))
        with self.assertRaises(ValueError):
            executor.resolve(config=self.config(), runtime='claude-code', required_capabilities=['shell'])

    def test_unknown_claude_cost_stays_null(self):
        stream = executor.Stream('claude-code')
        stream(json.dumps({'type': 'result', 'result': 'ok', 'is_error': False,
                           'total_cost_usd': 1.23, 'usage': {'input_tokens': 10, 'output_tokens': 2},
                           'modelUsage': {'custom': {'costBasis': 'unknown'}}}))
        self.assertIsNone(stream.usage['cost_usd'])
        self.assertEqual(stream.usage['input_tokens'], 10)

    def test_codex_usage_is_open_usage_and_tool_events_are_capped(self):
        stream = executor.Stream('codex-cli')
        line = stream(json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution'}}))
        self.assertEqual(json.loads(line)['type'], 'step_finish')
        stream(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 12, 'output_tokens': 4}}))
        self.assertEqual(stream.usage['input_tokens'], 12)
        self.assertIsNone(stream.usage['cost_usd'])


if __name__ == '__main__':
    unittest.main()
