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

    def test_unverified_codex_runtime_is_refused_when_opencode_is_missing(self):
        with patch.object(executor.shutil, 'which', side_effect=lambda s: '/bin/codex' if s == 'codex' else None):
            with self.assertRaisesRegex(ValueError, 'Codex CLI open execution is disabled'):
                executor.resolve(config=self.config())

    def test_disabled_opencode_falls_back_to_native_claude(self):
        config = self.config()
        config['executors']['runtime_status'] = {'opencode': {
            'open_execution': False, 'reason': 'native read/symlink bypass confirmed'}}
        with patch.object(executor.shutil, 'which', return_value='/fixture/runtime'):
            selected = executor.resolve(config=config)
        self.assertEqual(selected['runtime'], 'claude-code')

    def test_explicitly_disabled_opencode_and_codex_are_actionably_refused(self):
        config = self.config()
        config['executors']['runtime_status'] = {
            'opencode': {'open_execution': False, 'reason': 'native read/symlink bypass confirmed'},
            'codex-cli': {'open_execution': False, 'reason': 'restrictive rule loading unverified'}}
        for runtime in ('opencode', 'codex-cli'):
            with self.subTest(runtime=runtime), self.assertRaisesRegex(ValueError, 'open execution is disabled'):
                executor.resolve(config=config, runtime=runtime)

    def test_check_runtime_honors_disabled_runtime_policy(self):
        config = self.config()
        config['executors']['runtime_status'] = {'opencode': {
            'open_execution': False, 'reason': 'native read/symlink bypass confirmed'}}
        executor.check_runtime.cache_clear()
        with patch.object(executor, 'policy', return_value=config), \
                self.assertRaisesRegex(ValueError, 'native read/symlink bypass confirmed'):
            executor.check_runtime('opencode')
        with patch.object(executor, 'policy', return_value=config), \
                self.assertRaisesRegex(ValueError, 'native read/symlink bypass confirmed'):
            executor.run('.', 'vercel/test/winner', 'task', 30, False, runtime='opencode')

    def test_fallback_model_can_use_each_runtime(self):
        for runtime in executor.OPEN_EXECUTION_RUNTIMES:
            selected = executor.resolve(config=self.config(), slot='fallback', runtime=runtime)
            self.assertEqual(selected['model'], 'test/second')
            self.assertEqual(selected['runtime'], runtime)

    def test_provider_override_changes_invoked_identity_too(self):
        selected = executor.resolve(config=self.config(), runtime='opencode', provider='custom')
        self.assertEqual(selected['provider'], 'custom')
        self.assertEqual(selected['model_ref'], 'custom/test/winner')

    def test_catalog_rejects_a_runtime_not_compatible_with_the_selected_model(self):
        config = self.config() | {'model_catalog': {'vercel/test/winner': {
            'compatible_runtimes': ['opencode']}}}
        with self.assertRaisesRegex(ValueError, 'model is not compatible'):
            executor.resolve(config=config, runtime='claude-code')

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
        with self.assertRaisesRegex(ValueError, 'Codex CLI open execution is disabled'):
            executor.resolve(config=self.config(), runtime='codex-cli')
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
            for runtime in executor.OPEN_EXECUTION_RUNTIMES:
                with self.subTest(runtime=runtime), \
                        patch.object(executor.shutil, 'which', return_value='/fixture/runtime'), \
                        patch.object(executor, 'policy', return_value=self.config()), \
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

    def test_opencode_restrict_denies_bash_and_reads_secrets(self):
        restricted = executor.restrict_opencode({'permission': {}})
        permission = restricted['permission']
        self.assertEqual(permission['bash'], 'deny')
        self.assertEqual(permission['webfetch'], 'deny')
        self.assertEqual(permission['websearch'], 'deny')
        self.assertEqual(permission['task'], 'deny')
        self.assertEqual(permission['external_directory'], 'deny')
        for pattern in ('**/.env*', '**/*.pem', '**/*.key', '.git/**', '.claude/**', '.codex/**'):
            self.assertEqual(permission['read'][pattern], 'deny')
        self.assertEqual(permission['read']['*'], 'allow')

    def test_opencode_restrict_never_weakens_user_read_deny(self):
        restricted = executor.restrict_opencode({'permission': {'read': 'deny'}})
        self.assertEqual(restricted['permission']['read']['*'], 'deny')

    def test_blanket_wildcard_deny_is_not_overridden_by_read_allow(self):
        restricted = executor.restrict_opencode({'permission': {'*': 'deny'}})
        self.assertEqual(restricted['permission']['read']['*'], 'deny')
        self.assertEqual(restricted['permission']['edit']['*'], 'deny')

    def test_ordered_globs_preserve_original_rule_order(self):
        config = {'permission': {'e*': 'deny', 'ed*': 'allow'}}
        restricted = executor.restrict_opencode(json.loads(json.dumps(config)))
        self.assertEqual(restricted['permission']['edit']['*'], 'allow')
        config = {'permission': {'ed*': 'allow', 'e*': 'deny'}}
        restricted = executor.restrict_opencode(json.loads(json.dumps(config)))
        self.assertEqual(restricted['permission']['edit']['*'], 'deny')
        config = {'permission': {'rea*': 'deny', 'read': {'*': 'allow'}}}
        restricted = executor.restrict_opencode(json.loads(json.dumps(config)))
        self.assertEqual(restricted['permission']['read']['*'], 'allow')

    def test_read_wildcard_map_rules_read_and_edit_too(self):
        config = {'permission': {'*': {'**/.env*': 'deny', 'src/**': 'allow'}}}
        restricted = executor.restrict_opencode(json.loads(json.dumps(config)))
        self.assertEqual(restricted['permission']['read']['src/**'], 'allow')
        self.assertEqual(restricted['permission']['edit']['src/**'], 'allow')
        self.assertEqual(restricted['permission']['read']['**/.env*'], 'deny')

    def test_existing_deny_reordered_after_wildcard_allow(self):
        config = {'permission': {'read': {'**/.env*': 'deny', '*': 'allow'}}}
        restricted = executor.restrict_opencode(json.loads(json.dumps(config)))
        read = restricted['permission']['read']
        self.assertEqual(read['**/.env*'], 'deny')
        self.assertGreater(list(read).index('**/.env*'), list(read).index('*'))

    def test_edit_write_patch_deny_metadata_and_secrets(self):
        restricted = executor.restrict_opencode({'permission': {}})
        for tool in ('edit', 'write', 'patch'):
            for pattern in ('**/.env*', '**/*.pem', '**/*.key', '.git/**', '.claude/**', '.codex/**'):
                self.assertEqual(restricted['permission'][tool][pattern], 'deny', msg=f'{tool} {pattern}')
            self.assertEqual(restricted['permission'][tool]['*'], 'allow')

    def test_formatter_and_lsp_disabled(self):
        restricted = executor.restrict_opencode({'permission': {}})
        self.assertFalse(restricted['formatter'])
        self.assertFalse(restricted['lsp'])
        self.assertEqual(restricted['permission']['lsp'], 'deny')

    def test_opencode_restrict_keeps_custom_user_denies(self):
        config = {'permission': {'read': {'src/private.py': 'deny'}, 'edit': {'docs/**': 'deny'}}}
        restricted = executor.restrict_opencode(json.loads(json.dumps(config)))
        self.assertEqual(restricted['permission']['read']['src/private.py'], 'deny')
        self.assertEqual(restricted['permission']['edit']['docs/**'], 'deny')
        self.assertEqual(restricted['permission']['read']['**/.env*'], 'deny')
        self.assertEqual(restricted['permission']['bash'], 'deny')

    def test_opencode_env_drops_inherited_secrets(self):
        env = executor.opencode_env('/tmp/xdg-root', '/tmp/aos-repo')
        self.assertNotIn('AOS_OPEN_API_KEY', env)
        self.assertNotIn('ANTHROPIC_AUTH_TOKEN', env)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', env)
        self.assertEqual(env['XDG_CONFIG_HOME'], '/tmp/xdg-root')
        self.assertEqual(env['PWD'], '/tmp/aos-repo')
        for key in env:
            self.assertIn(key, executor.OPENCODE_ENV_KEYS + ('XDG_CONFIG_HOME', 'PWD'))

    def test_opencode_runner_restricts_config_and_sanitizes_env(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as config_root:
            repo = Path(directory).resolve()
            for args in [('init', '-q'), ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                         'commit', '--allow-empty', '-qm', 'fixture')]:
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            config_path = Path(config_root) / 'opencode' / 'opencode.json'
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({'permission': {'read': {'src/secrets/**': 'deny'}}}))
            captured = {}

            def fake_invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None, event_adapter=None):
                captured['env'] = env
                captured['cwd'] = cwd
                captured['config'] = json.loads(config_path.read_text())
                return 0, '', ''

            with patch.object(executor.shutil, 'which', return_value='/fixture/opencode'), \
                    patch.object(executor, 'policy', return_value=self.config()), \
                    patch.object(executor.delegate, 'run_config', return_value=str(config_path)), \
                    patch.object(executor.delegate, 'invoke', side_effect=fake_invoke):
                result = executor.run(repo, 'vercel/test/winner', 'task', 30, False, runtime='opencode')
            self.assertEqual(result['exit_code'], 0)
            on_disk = captured['config']
            self.assertEqual(on_disk['permission']['bash'], 'deny')
            self.assertEqual(on_disk['permission']['read']['**/.env*'], 'deny')
            self.assertEqual(on_disk['permission']['read']['src/secrets/**'], 'deny')
            self.assertEqual(captured['env']['XDG_CONFIG_HOME'], str(config_path.parents[1]))
            self.assertEqual(captured['env']['PWD'], str(repo))
            for key in captured['env']:
                self.assertIn(key, executor.OPENCODE_ENV_KEYS + ('XDG_CONFIG_HOME', 'PWD'))
            self.assertNotIn('AOS_OPEN_API_KEY', captured['env'])

    def test_codex_cli_open_execution_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Codex CLI open execution is disabled'):
            executor.resolve(config=self.config(), runtime='codex-cli')

    def test_opencode_is_refused_when_shell_required(self):
        self.assertFalse(executor.CAPABILITIES['opencode']['shell'])
        with self.assertRaisesRegex(ValueError, 'required capability'):
            executor.resolve(config=self.config(), runtime='opencode', required_capabilities=['shell'])


if __name__ == '__main__':
    unittest.main()
