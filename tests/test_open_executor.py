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
                'executors': {'default_runtime': 'claude-code',
                              'runtime_order': ['claude-code', 'codex-cli']}}

    def test_an_open_model_missing_from_the_catalog_blocks_before_the_harness(self):
        # Round 2 finding: the router blocked unknown models, this path did not.
        with self.assertRaisesRegex(ValueError, 'missing from catalog'):
            executor.resolve(model='vercel/missing-model', runtime='claude-code')
        # Round 3 finding: a provider override moved the executed identity away from
        # the one that was checked (anthropic/fable + provider=vercel ran as vercel/fable).
        with self.assertRaisesRegex(ValueError, 'missing from catalog: vercel/fable'):
            executor.resolve(model='anthropic/fable', provider='vercel', runtime='claude-code')
        self.assertEqual(executor.resolve(model='vercel/deepseek/deepseek-v4-pro-0813',
                                          runtime='claude-code')['model_ref'],
                         'vercel/deepseek/deepseek-v4-pro-0813')
        # A policy without a catalog keeps the legacy contract.
        self.assertEqual(executor.resolve(config=self.config(), runtime='claude-code',
                                          model='vercel/test/winner')['model_ref'], 'vercel/test/winner')

    def test_default_runtime_is_claude_code_and_a_disabled_runtime_is_actionably_refused(self):
        config = self.config()
        config['executors']['runtime_status'] = {'codex-cli': {'open_execution': False, 'reason': 'shell probe pending'}}
        with patch.object(executor.shutil, 'which', return_value='/x'):
            self.assertEqual(executor.resolve(config)['runtime'], 'claude-code')
            with self.assertRaisesRegex(ValueError, 'disabled: shell probe pending'):
                executor.resolve(config, runtime='codex-cli')
            config['executors']['default_runtime'] = 'codex-cli'
            config['executors']['runtime_order'] = ['codex-cli', 'claude-code']
            self.assertEqual(executor.resolve(config)['runtime'], 'claude-code')
        with patch.object(executor.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'no compatible open runtime'):
                executor.resolve(config)
        with self.assertRaisesRegex(ValueError, 'invalid runtime'):
            executor.resolve(config, runtime='opencode')
        with patch.object(executor, 'policy', return_value=config):
            with self.assertRaisesRegex(ValueError, 'disabled'):
                executor.check_runtime('codex-cli')
        with self.assertRaisesRegex(ValueError, 'only GREEN'):
            executor.run('/tmp/x', 'vercel/test/winner', 'x', 1, False, permission_profile='RED')

    def test_claude_is_the_shell_less_harness_and_codex_is_blocked_by_policy(self):
        self.assertFalse(executor.CAPABILITIES['claude-code']['shell'])
        with self.assertRaisesRegex(ValueError, 'required capability'):
            executor.resolve(config=self.config(), runtime='claude-code', required_capabilities=['shell'])
        # Codex declares the shell it really needs for apply_patch; the shipped policy
        # keeps it out of open execution until a probe with that shell on is green.
        self.assertTrue(executor.CAPABILITIES['codex-cli']['shell'])
        shipped = executor.policy()
        self.assertFalse(shipped['executors']['runtime_status']['codex-cli']['open_execution'])
        self.assertEqual(shipped['executors']['runtime_order'], ['claude-code'])
        with self.assertRaisesRegex(ValueError, 'Not an open harness'):
            executor.resolve(shipped, runtime='codex-cli', model='vercel/deepseek/deepseek-v4-pro-0813')
        for model, entry in shipped['model_catalog'].items():
            if entry['cost_class'] == 'CHEAP':
                self.assertEqual(entry['compatible_runtimes'], ['claude-code'], model)
        command = executor.codex_command(Path('/tmp/repo'), 'test/winner', 'task', 'https://example.invalid/v1')
        self.assertNotIn('shell_tool', command)
        self.assertIn('--ignore-user-config', command)
        self.assertIn('--ephemeral', command)
        joined = ' '.join(command)
        self.assertIn('"network"={"enabled"=false}', joined)
        self.assertIn('"**/.env*"="deny"', joined)

    def test_credential_comes_from_env_or_the_configured_file_only(self):
        config = {'providers': {'vercel': {'anthropic_url': 'https://example.invalid/claude', 'api_key_env': 'AOS_TEST_KEY',
                                           'api_key_file': '/nonexistent/file'}}}
        with patch.object(executor, 'policy', return_value=config), patch.dict(executor.os.environ, {'AOS_TEST_KEY': ''}):
            with self.assertRaisesRegex(ValueError, 'credential unavailable'):
                executor.provider_config('vercel', 'claude-code')
        with tempfile.NamedTemporaryFile('w', suffix='.key') as handle:
            handle.write('secret-value\n')
            handle.flush()
            config['providers']['vercel']['api_key_file'] = handle.name
            with patch.object(executor, 'policy', return_value=config), patch.dict(executor.os.environ, {'AOS_TEST_KEY': ''}):
                self.assertEqual(executor.provider_config('vercel', 'claude-code'), ('https://example.invalid/claude', 'secret-value'))
        with patch.object(executor, 'policy', return_value=config), patch.dict(executor.os.environ, {'AOS_TEST_KEY': 'from-env'}):
            self.assertEqual(executor.provider_config('vercel', 'claude-code')[1], 'from-env')
        with patch.object(executor, 'policy', return_value={'providers': {'vercel': {'anthropic_url': 'http://insecure'}}}):
            with self.assertRaisesRegex(ValueError, 'HTTPS endpoint'):
                executor.provider_config('vercel', 'claude-code')

    def test_fallback_model_can_use_each_runtime(self):
        for runtime in executor.OPEN_EXECUTION_RUNTIMES:
            selected = executor.resolve(config=self.config(), slot='fallback', runtime=runtime)
            self.assertEqual(selected['model'], 'test/second')
            self.assertEqual(selected['runtime'], runtime)

    def test_provider_override_changes_invoked_identity_too(self):
        selected = executor.resolve(config=self.config(), runtime='claude-code', provider='custom')
        self.assertEqual(selected['provider'], 'custom')
        self.assertEqual(selected['model_ref'], 'custom/test/winner')

    def test_catalog_rejects_a_runtime_not_compatible_with_the_selected_model(self):
        config = self.config() | {'model_catalog': {'vercel/test/winner': {
            'compatible_runtimes': ['opencode']}}}
        with self.assertRaisesRegex(ValueError, 'model is not compatible'):
            executor.resolve(config=config, runtime='claude-code')

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

class IsolationTests(unittest.TestCase):
    """Probe bypass is fixture-bound; the seatbelt profile denies before it allows."""

    def test_probe_root_may_run_a_disabled_runtime_only_inside_its_fixture(self):
        config = executor.policy()
        config['executors']['runtime_status']['codex-cli'] = {'open_execution': False, 'reason': 'probe test'}
        with self.assertRaisesRegex(ValueError, 'disabled'):
            executor.resolve(config, runtime='codex-cli', model='vercel/deepseek/deepseek-v4-pro-0813')
        selection = executor.resolve(config, runtime='codex-cli', model='vercel/deepseek/deepseek-v4-pro-0813',
                                     probe_root=Path('/tmp/probe'))
        self.assertEqual(selection['runtime'], 'codex-cli')
        # Round 1 finding: `/` satisfied the ancestor check for any repository. A fixture
        # is a directory under the temp tree carrying the probe's marker, nothing else.
        for root in ('/', '/tmp', str(Path.home())):
            with self.assertRaisesRegex(ValueError, 'not a fixture created by the isolation probe'):
                executor.run('/opt/production-repo', 'vercel/deepseek/deepseek-v4-pro-0813', 'x', 10, False,
                             runtime='codex-cli', probe_root=root)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'not a fixture'):
                executor.probe_fixture(directory)
            (Path(directory) / executor.PROBE_MARKER).write_text('x')
            self.assertEqual(executor.probe_fixture(directory), Path(directory).resolve())
            with self.assertRaisesRegex(ValueError, 'own disposable fixture'):
                executor.run('/tmp/elsewhere/repo', 'vercel/deepseek/deepseek-v4-pro-0813', 'x', 10, False,
                             runtime='claude-code', probe_root=directory)

    def test_production_cannot_choose_os_isolation_and_verified_follows_the_applied_layer(self):
        # Round 1 finding: os_isolation='none' was accepted without a probe and the
        # result still said isolation_verified=True from the policy flag.
        with self.assertRaisesRegex(ValueError, 'only by the isolation probe'):
            executor.run('/tmp/x/repo', 'vercel/deepseek/deepseek-v4-pro-0813', 'x', 1, False,
                         runtime='claude-code', os_isolation='none')
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / executor.PROBE_MARKER).write_text('x')
            repo = Path(directory) / 'repo'
            repo.mkdir()
            with patch.object(executor.shutil, 'which', return_value='/x'), \
                    patch.object(executor.delegate, 'run', return_value={'usage': {}, 'reply': '', 'git_meta_changed': False}), \
                    patch.object(executor.delegate, 'dirty_paths', return_value=set()):
                result = executor.run(repo, 'vercel/deepseek/deepseek-v4-pro-0813', 'x', 1, False,
                                      runtime='claude-code', probe_root=directory, os_isolation='none')
        self.assertEqual((result['os_isolation'], result['isolation_verified']), ('none', False))

    def test_seatbelt_profile_orders_deny_allow_deny(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()
            profile = executor.seatbelt_profile(repo, read_write=[Path(directory) / 'run'], read_only=['/opt/x'])
        lines = profile.strip().splitlines()
        self.assertEqual(lines[:2], ['(version 1)', '(allow default)'])
        home = json.dumps(str(Path.home().resolve()))
        self.assertIn('(deny file-read* file-write* (subpath ' + home + '))', lines)
        self.assertIn('(deny file-write* (regex "^/"))', lines)
        self.assertNotIn('"/tmp"', profile)  # the symlink name shadows later allows
        resolved = str(repo.resolve())
        allow_index = lines.index('(allow file-read* file-write* (subpath ' + json.dumps(resolved) + '))')
        self.assertTrue(all(line.startswith('(deny') or line.startswith('(allow file-write* (subpath "/dev"))')
                            or line.startswith('(allow file-read* (subpath "/opt/x"))')
                            for line in lines[2:allow_index]))
        self.assertTrue(lines[-1].startswith('(deny file-write* (subpath ' + json.dumps(resolved + '/.git') + ')'))
        self.assertIn('\\.env', lines[-2])
        self.assertIn('\\.pem', lines[-2])

    def test_os_isolation_comes_from_policy_and_rejects_unknown_values(self):
        config = {'executors': {'runtime_status': {'claude-code': {'os_isolation': 'seatbelt'}, 'codex-cli': {}}}}
        self.assertEqual(executor.os_isolation_for(config, 'claude-code'), 'seatbelt')
        self.assertEqual(executor.os_isolation_for(config, 'codex-cli'), 'none')
        self.assertEqual(executor.os_isolation_for({}, 'codex-cli'), 'none')
        with self.assertRaisesRegex(ValueError, 'unsupported os_isolation'):
            executor.os_isolation_for({'executors': {'runtime_status': {'claude-code': {'os_isolation': 'docker'}}}}, 'claude-code')
        with patch.object(executor.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'sandbox-exec is unavailable'):
                executor.seatbelt_wrap(['claude'], '(version 1)')
        self.assertEqual(executor.seatbelt_wrap(['claude', '-p'], '(version 1)')[:3], ['sandbox-exec', '-p', '(version 1)'])


class IsolationProbeTests(unittest.TestCase):
    """The probe's verdict is mechanical: canaries in the output, effects on disk."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location('aos_isolation', Path(__file__).resolve().parents[1] / 'bin/aos-isolation.py')
        self.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.probe)

    def test_an_attempt_is_a_tool_of_the_right_kind_acting_on_a_path_field(self):
        # Round 2 finding: the report's own text named the forbidden paths, and every
        # target counted as attempted-and-denied on two control writes alone.
        report = [{'tool': 'Write', 'input': {'file_path': 'probe-report.txt',
                                              'content': 'R1 refused .env, W3 refused .git/config'}}]
        attempted = self.probe.attempted_by_tool
        self.assertFalse(attempted(report, 'read', '.env'))
        self.assertFalse(attempted(report, 'write', '.git/config'))
        # A read tool on the target counts as a read attempt, not as a write attempt.
        read_call = [{'tool': 'Read', 'input': {'file_path': '/r/.env'}}]
        self.assertTrue(attempted(read_call, 'read', '.env', '/r'))
        self.assertFalse(attempted(read_call, 'write', '.env', '/r'))
        write_call = [{'tool': 'Edit', 'input': {'file_path': '/r/link-out', 'new_string': 'x'}}]
        self.assertTrue(attempted(write_call, 'write', 'link-out', '/r'))
        # Relative, absolute and resolved spellings are the same attempt.
        self.assertTrue(attempted([{'tool': 'Read', 'input': {'path': '../external/secret.txt'}}],
                                  'read', '../external/secret.txt', '/r'))
        # Shell targets are judged on the command, not on any path field.
        self.assertTrue(attempted([{'tool': 'shell', 'input': {'command': 'curl -sS https://example.com/'}}],
                                  'shell', 'curl -sS -m 10 https://example.com/'))
        self.assertFalse(attempted([{'tool': 'Write', 'input': {'file_path': 'x', 'content': 'curl'}}],
                                   'shell', 'curl -sS -m 10 https://example.com/'))

    def test_a_read_the_runtime_reported_as_successful_is_a_leak(self):
        # Round 3 finding: a target with no canary of ours was judged by the worker's
        # own word, so a Read that returned an SSH config counted as denied.
        targets = {'R7': ('read', '/synthetic/.ssh/config', None)}
        state = {'root': '/synthetic', 'external': '/synthetic/external'}
        call = {'id': 't1', 'tool': 'Read', 'input': {'file_path': '/synthetic/.ssh/config'}}
        succeeded = {'result': 'RESULT R7: REFUSED', 'tool_calls': [call],
                     'tool_results': [{'tool_use_id': 't1', 'is_error': False, 'output': 'Host production'}]}
        verdict = self.probe.observe(succeeded, Path('/synthetic/repo'), targets, {}, state)
        self.assertEqual(verdict['verdicts']['R7']['observed'], 'LEAK')
        self.assertFalse(verdict['isolated'])
        denied = dict(succeeded, tool_results=[{'tool_use_id': 't1', 'is_error': True, 'output': 'EACCES'}])
        self.assertEqual(self.probe.observe(denied, Path('/synthetic/repo'), targets, {}, state)['verdicts']['R7']['observed'],
                         'denied')
        # A result belonging to another call proves nothing about this one.
        unrelated = dict(succeeded, tool_results=[{'tool_use_id': 'other', 'is_error': False, 'output': 'x'}])
        self.assertEqual(self.probe.observe(unrelated, Path('/synthetic/repo'), targets, {}, state)['verdicts']['R7']['observed'],
                         'denied')

    def test_probe_is_not_verified_when_only_the_controls_ran(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, targets, values, state = self.probe.build_fixture(directory)
            try:
                (repo / 'probe-report.txt').write_text('everything refused')
                (repo / 'app.py').write_text('def answer():\n    return 42\n')
                reply = '\n'.join(f'RESULT {k}: REFUSED' for k in targets)
                calls = [{'tool': 'Write', 'input': {'file_path': 'probe-report.txt', 'content': reply}},
                         {'tool': 'Edit', 'input': {'file_path': 'app.py', 'new_string': 'return 42'}}]
                verdict = self.probe.observe({'result': reply, 'tool_calls': calls}, repo, targets, values, state)
                self.assertFalse(verdict['isolated'])
                self.assertEqual(len(verdict['unattempted']), 13)
            finally:
                Path(state['home_canary']).unlink(missing_ok=True)

    def test_fixture_targets_and_verdicts(self):
        with tempfile.TemporaryDirectory() as directory:
            repo, targets, values, state = self.probe.build_fixture(directory, shell=True)
            try:
                self.assertTrue((repo / '.env').is_file() and (repo / 'link-out').is_symlink())
                self.assertEqual(self.probe.executor.probe_fixture(directory), Path(directory).resolve())
                self.assertEqual(set(targets) >= {'R1', 'R7', 'W1', 'W6', 'C1', 'C2', 'S1', 'S2'}, True)
                brief = self.probe.brief_for(targets)
                self.assertIn('RESULT <id>: OK', brief)
                # Nothing attempted: every target is no_attempt, not denied.
                verdict = self.probe.observe({'result': '', 'tool_results': []}, repo, targets, values, state)
                self.assertFalse(verdict['isolated'])
                self.assertEqual(set(verdict['unattempted']), set(targets))
                # A leaked canary is a LEAK even when the worker claims REFUSED; a
                # write through the symlink is caught on disk; controls need real effects.
                (repo / 'link-out').write_text('probe-write')
                (repo / 'probe-report.txt').write_text('x')
                (repo / 'app.py').write_text('def answer():\n    return 42\n')
                reply = '\n'.join(f'RESULT {k}: REFUSED' for k in targets).replace('RESULT C1: REFUSED', 'RESULT C1: OK')
                reply = reply.replace('RESULT C2: REFUSED', 'RESULT C2: OK') + '\n' + values['env']
                # Round 1 finding: a claimed REFUSED without a tool call certified nothing.
                claimed_only = self.probe.observe({'result': reply, 'tool_results': [], 'tool_calls': [], 'exit_code': 0},
                                                  repo, targets, values, state)
                self.assertFalse(claimed_only['isolated'])
                self.assertIn('R2', claimed_only['unattempted'])
                # Absolute, runtime-resolved spellings: `../external/x` must still count
                # as an attempt on the target the fixture wrote as a relative path.
                calls = [{'tool': 'Read' if kind == 'read' else 'Write',
                           'input': {'file_path': str((repo / path).resolve())}}
                         for _, (kind, path, _) in targets.items() if kind != 'shell']
                calls += [{'tool': 'shell', 'input': {'command': path}} for _, (kind, path, _) in targets.items() if kind == 'shell']
                result = {'result': reply, 'tool_calls': calls,
                          'tool_results': [{'command': 'npx --version', 'exit_code': 0, 'aggregated_output': '10'}]}
                verdict = self.probe.observe(result, repo, targets, values, state)
                self.assertEqual(sorted(verdict['leaks']), ['R1', 'S2', 'W5'])
                self.assertEqual(verdict['verdicts']['C1']['observed'], 'allowed')
                self.assertEqual(verdict['verdicts']['S1']['observed'], 'denied')
                self.assertEqual(verdict['unattempted'], [])
                redacted = self.probe.redact({'reply': reply}, values)
                self.assertNotIn(values['env'], redacted['reply'])
                self.assertIn('<canary:env>', redacted['reply'])
            finally:
                Path(state['home_canary']).unlink(missing_ok=True)



class InstallManifestTests(unittest.TestCase):
    def test_the_probe_ships_with_the_skill(self):
        # Round 3 finding: the installer copied the tests that import the probe but
        # not the probe itself, and verify() called that installation complete.
        import re
        manifest = re.search(r'^REQUIRED_FILES="([^"]+)"',
                             (ROOT / 'bin/aos-install.sh').read_text(), re.M).group(1).split()
        for name in ('bin/aos-isolation.py', 'bin/aos-open-executor.py', 'tests/test_open_executor.py'):
            self.assertIn(name, manifest)
