"""Executable entry contracts: classification is data, never a command."""
import importlib.util
import json
from pathlib import Path
import unittest

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
        info = dict(tier='T0', risk='LOW', domain='general', capabilities=[], reason='simple')
        result = self.entry.route(info, primary_available=False)
        config = self.entry.router.load_config(self.entry.POLICY)
        self.assertEqual(result['model'], config.open_fallback)
        self.assertEqual(result['coordinator_model'], config.open_fallback)

    def test_premium_model_names_come_from_policy(self):
        policy = json.loads(self.entry.POLICY.read_text())
        for backend in ('codex', 'claude'):
            command = self.entry.premium_command(backend)
            self.assertIn(policy['premium'][backend + '_model'], command)

    def test_codex_cli_requirement_routes_to_premium_without_claiming_app_tools(self):
        info = dict(tier='T0', risk='LOW', domain='general', capabilities=['codex_runtime'], reason='CLI required')
        parsed = self.entry.parse_classification(json.dumps(info))
        result = self.entry.route(parsed)
        self.assertEqual((result['executor'], result['backend']), ('premium', 'codex'))


if __name__ == '__main__':
    unittest.main()
