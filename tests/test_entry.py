"""Executable entry contracts: classification is data, never a command."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class EntryTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / 'bin/aos-entry.py'
        self.assertTrue(path.exists(), 'AOS entry bridge must exist')
        spec = importlib.util.spec_from_file_location('entry', path)
        self.entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.entry)

    def test_native_tool_names_are_normalized_to_capability_axes(self):
        raw = dict(tier='T0', risk='LOW', domain='general', capabilities=['bash', 'read', 'skill'], reason='pwd')
        parsed = self.entry.parse_classification(json.dumps(raw))
        self.assertEqual(parsed['capabilities'], ['shell', 'files'])

    def test_classification_requires_valid_axes_and_known_capabilities(self):
        good = dict(tier='T1', risk='LOW', domain='general', capabilities=[], reason='simple')
        self.assertEqual(self.entry.parse_classification(json.dumps(good)), good)
        for extra in [dict(tier='T9'), dict(risk='low'), dict(capabilities=['shell; rm']),
                      dict(capabilities='web'), dict(domain='unknown')]:
            with self.assertRaises(ValueError):
                self.entry.parse_classification(json.dumps(good | extra))
        self.assertEqual(self.entry.parse_classification(json.dumps(good | {'uncertainty': 'UNSETTLED'}))['uncertainty'],
                         'UNSETTLED')

    def test_empty_or_prose_classification_fails_closed(self):
        for value in ['', 'okay', '{}', '[]']:
            with self.assertRaises(ValueError):
                self.entry.parse_classification(value)

    def test_premium_commands_do_not_bypass_permissions_or_interpolate_shell(self):
        for backend in ['codex', 'claude']:
            command = self.entry.premium_command(backend)
            self.assertEqual(command[0], backend)
            self.assertFalse(any('bypass' in s or 'skip-permissions' in s for s in command))
        with self.assertRaises(ValueError):
            self.entry.premium_command('bash')

    def test_codex_inherits_configured_permissions(self):
        command = self.entry.premium_command('codex')
        for override in ('--sandbox', '-s', '--config', '-c', '--approve-for-me',
                         '--ignore-user-config', '--ignore-rules'):
            self.assertNotIn(override, command)
        self.assertEqual(self.entry.premium_command('claude')[-2:],
                         ['--permission-mode', 'dontAsk'])
        claude = self.entry.premium_command('claude')
        self.assertEqual(claude[claude.index('--allowedTools') + 1], 'Read,Glob,Grep,Edit,Write')

    def test_premium_claude_denial_is_not_a_success(self):
        stream = ('{"type":"result","subtype":"success","result":"done","usage":{},'
                  '"permission_denials":[{"tool_name":"Write"}]}')
        with self.assertRaisesRegex(ValueError, 'denied'):
            self.entry.premium_reply('claude', stream)

    def test_usage_and_error_are_not_confused_with_successful_text(self):
        with self.assertRaises(ValueError):
            self.entry.premium_reply('codex', '{"type":"turn.failed","error":{"message":"quota"}}')
        with self.assertRaises(ValueError):
            self.entry.premium_reply('claude', '{"type":"result","is_error":true,"result":"failed"}')
        with self.assertRaises(ValueError):
            self.entry.premium_reply('codex', '')
        stream = '\n'.join([json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}),
                           json.dumps({'type':'turn.completed','usage':{'input_tokens':2}})])
        self.assertEqual(self.entry.premium_reply('codex', stream)['reply'], 'done')

    def test_critical_requires_approval_not_premium_execution(self):
        info = dict(tier='T3', risk='CRITICAL', domain='software', capabilities=[], reason='deletion')
        self.assertEqual(self.entry.route(info)['verify'], 'needs_approval')

    def test_classifier_fallback_is_also_used_for_the_executor(self):
        info = dict(tier='T1', risk='LOW', domain='general', capabilities=[], reason='simple')
        result = self.entry.route(info, primary_available=False)
        config = self.entry.router.load_config(self.entry.POLICY)
        self.assertEqual(result['model'], config.open_fallback)
        self.assertEqual(result['coordinator_model'], config.open_fallback)

    def test_t0_stays_on_the_host_with_its_configured_subagent(self):
        info = dict(tier='T0', risk='LOW', domain='general', capabilities=[], reason='typo')
        policy = json.loads(self.entry.POLICY.read_text())
        for host, family in (('claude', 'claude'), ('claude-code', 'claude'),
                             ('codex', 'codex'), ('codex-cli', 'codex')):
            result = self.entry.route(info, main_host=host)
            self.assertEqual((result['executor'], result['model']),
                             ('main', policy['host_subagents'][family]['cheap']), host)

    def test_premium_model_names_come_from_policy(self):
        policy = json.loads(self.entry.POLICY.read_text())
        for backend in ('codex', 'claude'):
            command = self.entry.premium_command(backend)
            self.assertIn(policy['premium'][backend + '_model'], command)

    def test_catalog_role_model_is_the_actual_cli_argument(self):
        command = self.entry.premium_command('codex', 'openai/gpt-6-astra')
        self.assertEqual(command[command.index('-m') + 1], 'gpt-6-astra')
        command = self.entry.premium_command('claude', 'anthropic/fable')
        self.assertEqual(command[command.index('--model') + 1], 'fable')

    def test_codex_cli_requirement_routes_to_premium_without_claiming_app_tools(self):
        info = dict(tier='T0', risk='LOW', domain='general', capabilities=['codex_runtime'], reason='CLI required')
        parsed = self.entry.parse_classification(json.dumps(info))
        result = self.entry.route(parsed)
        self.assertEqual((result['executor'], result['backend']), ('premium', 'codex'))

    def test_selected_open_runtime_consumes_its_own_capability(self):
        info = dict(tier='T1', risk='LOW', domain='general', capabilities=['claude_runtime'], reason='runtime')
        result = self.entry.route(info, executor_runtime='claude-code')
        self.assertEqual(result['executor'], 'open')

    def test_classification_uses_the_resolved_harness(self):
        reply = json.dumps(dict(tier='T1', risk='LOW', domain='general', capabilities=[], reason='bounded'))
        with patch.object(self.entry.open_executor, 'resolve', return_value={'runtime': 'claude-code'}), \
             patch.object(self.entry.open_executor, 'infer', return_value={
                 'exit_code': 0, 'reply': reply, 'usage': {}, 'runtime': 'claude-code'}) as infer:
            result = self.entry.classify('write one local file')
        self.assertEqual(result['classifier'][0]['runtime'], 'claude-code')
        self.assertEqual(infer.call_args.kwargs['runtime'], 'claude-code')

    def test_role_usage_keeps_actual_cost_separate_from_catalog_estimate(self):
        state = dict(main_host='codex-cli', tier='T2', risk='MEDIUM', failures=1,
                     classification={'uncertainty': 'HIGH'})
        self.entry.role_usage(state, 'executor', 'open', dict(
            model='deepseek-v4-pro-0813', requested_model='vercel/deepseek/deepseek-v4-pro-0813',
            runtime='claude-code', usage={'input_tokens': 4, 'output_tokens': 2}, cost=0.07, exit_code=0))
        event = state['role_events'][0]
        self.assertEqual(event['cost'], 0.07)
        catalog = json.loads(self.entry.POLICY.read_text())['model_catalog']['vercel/deepseek/deepseek-v4-pro-0813']
        self.assertEqual(event['estimated_cost'], self.entry._estimated_cost(catalog, event['usage']))
        self.assertEqual((event['main_host'], event['tier'], event['risk'], event['uncertainty'], event['retry']),
                         ('codex-cli', 'T2', 'MEDIUM', 'HIGH', 1))

    def test_learning_configuration_honors_disabled_flag_and_database_path(self):
        policy = json.loads(self.entry.POLICY.read_text())
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / 'ledger.sqlite3')
            policy['learning'] = {'enabled': False, 'database': database}
            configured = Path(directory) / 'policy.json'
            configured.write_text(json.dumps(policy))
            with patch.object(self.entry, 'POLICY', configured):
                enabled, path = self.entry._learning_configuration()
        self.assertFalse(enabled)
        self.assertEqual(path, Path(database).resolve())

    def test_learning_configuration_expands_tilde_and_refuses_worktree_storage(self):
        with patch.object(self.entry, 'POLICY', self.entry.POLICY):
            enabled, path = self.entry._learning_configuration(
                {'learning_enabled': False, 'learning_database': '~/.local/state/aos/test-ledger.sqlite3'})
        self.assertFalse(enabled)
        self.assertEqual(path, (Path.home() / '.local/state/aos/test-ledger.sqlite3').resolve())
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / 'repo'
            worktree.mkdir()
            database = worktree / 'learning.sqlite3'
            with self.assertRaisesRegex(ValueError, 'outside the worktree'):
                self.entry._learning_configuration({'learning_database': str(database)}, worktree)
            self.assertFalse(database.exists())

    def test_budget_uses_global_outcomes_and_requires_explicit_allow(self):
        state = {'budget': {'daily_budget': 1}, 'learning_database': '/tmp/aos-ledger.sqlite3',
                 'project': '/project', 'task_id': 'task'}
        with patch.object(self.entry.learning, 'reserve_budget', return_value={'allowed': True, 'reservation_id': 7}) as reserve:
            self.entry._budget(state, 'vercel/deepseek/deepseek-v4-pro-0813')
        self.assertEqual(reserve.call_args.args[0], '/tmp/aos-ledger.sqlite3')
        self.assertEqual(reserve.call_args.args[3], 'task')
        self.assertIs(reserve.call_args.args[4], self.entry.operations.budget_check)
        self.assertEqual(state['budget_events'][0]['reservation_id'], 7)
        # The hold travels with the next role event and reaches the ledger with its outcome.
        self.entry.role_usage(state, 'executor', 'open', {'usage': {}, 'model': 'vercel/deepseek/deepseek-v4-pro-0813'})
        self.assertEqual(state['role_events'][-1]['reservation_id'], 7)
        self.assertNotIn('pending_reservation', state)
        with patch.object(self.entry.learning, 'record_outcome') as record:
            state.update(learning_enabled=True, tier='T1', risk='LOW', project='/p')
            self.entry._record_event(state, state['role_events'][-1], True)
        self.assertEqual(record.call_args.args[1]['reservation_id'], 7)
        with patch.object(self.entry.learning, 'reserve_budget', return_value={'allowed': False}):
            with self.assertRaisesRegex(ValueError, 'explicitly allowed'):
                self.entry._budget(state, 'vercel/deepseek/deepseek-v4-pro-0813')

    def test_budget_without_applicable_cap_skips_ledger_and_disabled_learning_fails_closed_when_capped(self):
        state = {'learning_enabled': False, 'learning_database': '/tmp/aos-ledger.sqlite3',
                 'budget': {}, 'task_id': 'task'}
        with patch.object(self.entry.learning, 'reserve_budget') as reserve:
            self.entry._budget(state, 'vercel/deepseek/deepseek-v4-pro-0813')
        reserve.assert_not_called()
        state['budget'] = {'daily_budget': 1}
        with patch.object(self.entry.learning, 'reserve_budget') as reserve, \
                self.assertRaisesRegex(ValueError, 'requires accounting'):
            self.entry._budget(state, 'vercel/deepseek/deepseek-v4-pro-0813')
        reserve.assert_not_called()

    def test_role_model_mismatch_fails_instead_of_silent_premium_substitution(self):
        state = {'role_models': {'planner': 'anthropic/fable'}}
        with self.assertRaisesRegex(ValueError, 'backend not compatible'):
            self.entry._role_model(state, 'planner', 'codex')

    def test_catalog_era_requires_explicit_role_model_but_legacy_policy_is_compatible(self):
        with self.assertRaisesRegex(ValueError, 'missing from routing decision'):
            self.entry._role_model({'role_models': {}}, 'planner', 'codex')
        policy = json.loads(self.entry.POLICY.read_text())
        policy['model_catalog'] = {}
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / 'policy.json'
            configured.write_text(json.dumps(policy))
            with patch.object(self.entry, 'POLICY', configured):
                self.assertEqual(self.entry._role_model({'role_models': {}}, 'planner', 'codex'),
                                 'openai/gpt-6-astra')

    def test_start_passes_a_budget_copy_to_routing_and_blocks_premium_roles_over_ceiling(self):
        classification = {'tier': 'T2', 'risk': 'MEDIUM', 'capabilities': []}
        budget = {'max_cost_class': 'CHEAP'}
        observed = {}
        actual_route = self.entry.route

        def capture(info, **kwargs):
            observed['info'] = info
            return actual_route(info, **kwargs)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.entry.open_executor, 'resolve', return_value={'runtime': 'claude-code'}), \
                patch.object(self.entry, 'route', side_effect=capture), \
                patch.object(self.entry, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'planner model does not satisfy'):
                self.entry.pipeline_step(None, 'start', {
                    'classification': classification, 'budget': budget, 'text': 'bounded task',
                    'main_host': 'claude-code', 'learning_enabled': False,
                }, directory)
        self.assertIsNot(observed['info'], classification)
        self.assertEqual(observed['info']['budget'], budget)
        self.assertNotIn('budget', classification)
        execute.assert_not_called()

    def test_native_claude_open_refuses_active_dollar_budget_but_premium_review_keeps_budget_check(self):
        policy = json.loads(self.entry.POLICY.read_text())
        policy['budgets'] = {'max_task_cost': None, 'daily_budget': None,
                             'monthly_budget': None, 'premium_budget': None}
        with tempfile.TemporaryDirectory() as directory:
            configured = Path(directory) / 'policy.json'
            configured.write_text(json.dumps(policy))
            state = {'executor_runtime': 'claude-code', 'budget': {'daily_budget': 1},
                     'learning_database': '/tmp/aos-ledger.sqlite3', 'task_id': 'task'}
            with patch.object(self.entry, 'POLICY', configured), \
                    self.assertRaisesRegex(ValueError, 'static estimates are insufficient'):
                self.entry._budget(state, 'vercel/deepseek/deepseek-v4-pro-0813')
            state['budget'] = {'premium_budget': 1}
            with patch.object(self.entry, 'POLICY', configured), \
                    patch.object(self.entry.learning, 'list_outcomes', return_value=[]), \
                    patch.object(self.entry.operations, 'budget_check', return_value={'allowed': True}):
                self.entry._budget(state, 'vercel/deepseek/deepseek-v4-pro-0813')
            with patch.object(self.entry, 'POLICY', configured), \
                    patch.object(self.entry.learning, 'list_outcomes', return_value=[]), \
                    patch.object(self.entry.operations, 'budget_check', return_value={'allowed': True}) as check:
                self.entry._budget(state, 'anthropic/fable', premium=True)
            self.assertTrue(check.called)

    def test_role_usage_attaches_runtime_reported_context_to_prompt_estimate(self):
        state = {'context_events': [{'role': 'planner', 'context_tokens': 100},
                                    {'role': 'executor', 'context_tokens': 200}]}
        self.entry.role_usage(state, 'planner', 'claude',
                              {'usage': {'input_tokens': 6, 'cache_read_input_tokens': 4000,
                                         'cache_creation_input_tokens': 594, 'output_tokens': 10}, 'model': 'anthropic/fable'})
        planner = state['context_events'][0]
        # Cached prompt tokens are context the provider processed: 6 + 4000 + 594.
        self.assertEqual((planner['observed_input_tokens'], planner['hidden_context_tokens']), (4600, 4500))
        self.assertEqual(planner['measurement_scope'], 'prompt_plus_runtime_reported')
        # An unreported counter stays null, never zero; the other role's event is untouched.
        self.entry.role_usage(state, 'executor', 'open', {'usage': {}, 'model': 'vercel/deepseek/deepseek-v4-pro-0813'})
        executor = state['context_events'][1]
        self.assertEqual((executor['observed_input_tokens'], executor['hidden_context_tokens']), (None, None))
        self.assertEqual(executor['measurement_scope'], 'prompt_only')

    def test_json_reply_takes_the_object_alone_and_reports_the_tail_otherwise(self):
        self.assertEqual(self.entry.json_reply({'reply': '```json\n{"a": 1}\n```'}), {'a': 1})
        self.assertEqual(self.entry.json_reply({'reply': '  {"a": 1}\n'}), {'a': 1})
        with self.assertRaisesRegex(ValueError, 'wraps the JSON object in prose'):
            self.entry.json_reply({'reply': 'Here is my review:\n{"findings": []}\nDone.'})
        with self.assertRaisesRegex(ValueError, 'not a JSON object: I could not read the diff'):
            self.entry.json_reply({'reply': 'I could not read the diff'})
        with self.assertRaisesRegex(ValueError, 'JSON object'):
            self.entry.json_reply({'reply': '[1, 2]'})

    def test_unknown_catalog_reference_blocks_instead_of_reaching_the_cli(self):
        with self.assertRaisesRegex(ValueError, 'missing from catalog'):
            self.entry._role_model({'role_models': {'planner': 'anthropic/not-in-catalog'}}, 'planner', 'claude')

    def test_json_reply_rejects_any_prose_around_the_object(self):
        # Round 2 finding: a short sentence passed the 200-character bound, and
        # "I could not review the diff … Example only: {…}" fitted inside it.
        for reply in ('Review unavailable: the diff could not be read, so this is only an illustration. '
                      'Example only: {"attacked":["example"],"findings":[]}',
                      'I could not review the diff because reads were denied. Example only:\n'
                      '{"attacked":["not performed: reads denied"],"findings":[]}',
                      'Done. {"attacked":["tests"],"findings":[]}'):
            with self.assertRaisesRegex(ValueError, 'wraps the JSON object in prose'):
                self.entry.json_reply({'reply': reply})
        # A fenced object with nothing else around it is still the object.
        self.assertEqual(self.entry.json_reply({'reply': '```json\n{"attacked":["tests"],"findings":[]}\n```'}),
                         {'attacked': ['tests'], 'findings': []})

    def test_recorded_observation_keeps_retry_and_context_fields(self):
        state = {'learning_enabled': True, 'learning_database': '/tmp/aos-ledger.sqlite3',
                 'tier': 'T2', 'risk': 'MEDIUM', 'task_id': 'task', 'project': '/project'}
        event = {'role': 'planner', 'runtime': 'claude-code', 'provider': 'anthropic',
                 'model': 'anthropic/fable', 'exit_code': 0, 'cost_class': 'PREMIUM',
                 'cost': None, 'usage': {}, 'retry': 2, 'main_host': 'codex-cli', 'uncertainty': 'HIGH'}
        with patch.object(self.entry.learning, 'record_outcome') as record:
            self.entry._record_event(state, event, False, 'role quality not scored')
        observed = record.call_args.args[1]
        self.assertEqual((observed['retry'], observed['main_host'], observed['uncertainty']),
                         (2, 'codex-cli', 'HIGH'))
        self.assertEqual(observed['verification_status'], 'verified')
        observation = dict(event)
        observation.pop('_outcome_recorded', None)
        state['role_events'] = [observation]
        with patch.object(self.entry.learning, 'record_outcome') as record:
            self.entry._record_role_observation(state)
        self.assertEqual(record.call_args.args[1]['verification_status'], 'not_scored')

    def test_readonly_roles_reuse_verify_agent_tool_restrictions(self):
        self.assertTrue(hasattr(self.entry, 'readonly_command'))
        for backend in ('claude', 'codex'):
            cmd = self.entry.readonly_command(backend, Path('/tmp/report'))
            if backend == 'codex':
                self.assertIn('read-only', cmd)
                self.assertIn('mcp_servers={}', cmd)
            else:
                self.assertIn('Read,Glob,Grep', cmd)
                self.assertIn('--safe-mode', cmd)
            self.assertFalse(any('bypass' in x for x in cmd))

    def test_pipeline_review_requires_current_deterministic_evidence(self):
        self.assertTrue(hasattr(self.entry, 'pipeline_step'))
        with self.assertRaises(ValueError):
            self.entry.pipeline_step({'stage':'execute'}, 'review', {}, ROOT)


if __name__ == '__main__':
    unittest.main()
