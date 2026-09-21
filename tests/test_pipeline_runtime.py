"""Synthetic models, real temporary Git tree and deterministic check processes."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pipeline_entry', ROOT / 'bin/aos-entry.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def finding(**overrides):
    base = dict(id='f1', severity='high', category='logic', file='app.py', location='1',
                finding='answer() returns 41 instead of 42',
                evidence='python -c "import app; assert app.answer() == 42"',
                required_fix='make answer() return 42', confidence='high')
    base.update(overrides)
    return base


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        for argv in (['git', 'init', '-q'],
                     ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-qm', 'baseline']):
            subprocess.run(argv, cwd=self.repo, check=True, capture_output=True)
        self.config = entry.router.load_config(ROOT / 'config/open-models.json')
        self.plan = dict(objective='write one file', scope=['app.py'], files=['app.py'], steps=['write'],
                         acceptance_criteria=['value equals 2'], risks=['regression'], security_constraints=['local only'],
                         tests=['unit', 'diff', 'security'], architecture=[], dependencies=[], uncertainties=[],
                         do_not_modify=['.git'], subtasks=[dict(id='one', objective='write app', files=['app.py'])])
        self.findings = []
        self.attacked = ['acceptance and security']
        self.worker_models = []
        self.worker_write = 'value = 2\n'
        self.state = self.begin('T2')
        self.paid = []
        self.premium = patch.object(entry, 'execute', side_effect=self._premium)
        self.premium.start()
        self.addCleanup(self.premium.stop)
        self.worker = self._worker

    def begin(self, tier='T2', text='write app'):
        self.state = entry.pipeline_step(None, 'start',
            dict(classification=dict(tier=tier, risk='MEDIUM', capabilities=[]), text=text),
            self.repo)
        self.addCleanup(shutil.rmtree, self.state['run_dir'])
        return self.state

    def _premium(self, backend, text, directory, readonly=False):
        self.paid.append((backend, readonly))
        if not readonly:
            (self.repo / 'app.py').write_text(self.worker_write)
            result = dict(completed=True)
        elif 'PREMIUM PLANNER' in text:
            result = self.plan
        else:
            result = dict(findings=list(self.findings), attacked=list(self.attacked))
        return dict(reply=json.dumps(result), usage=dict(input_tokens=3, output_tokens=2))

    def _worker(self, *args, **kwargs):
        self.worker_models.append(args[1])
        (self.repo / 'app.py').write_text(self.worker_write)
        return dict(exit_code=0, head_before='same', head_after='same', model=args[1],
                    usage=dict(input_tokens=4, output_tokens=1))

    def _failing_worker(self, *args, **kwargs):
        self.worker_models.append(args[1])
        return dict(exit_code=1, head_before='same', head_after='same', model=args[1],
                    error='simulated worker failure', usage=dict(input_tokens=1, output_tokens=1))

    def step(self, action, data=None):
        self.state = entry.pipeline_step(self.state, action, data or {}, self.repo)
        return self.state

    def checks(self):
        for name, argv in [('unit', [sys.executable, '-B', '-c', 'import app; assert app.value == 2']),
                           ('diff', ['git', 'diff', '--check']),
                           ('security', ['bash', str(ROOT / 'bin/aos-security.sh')])]:
            self.step('check', dict(id=name, argv=argv))
        return self.step('verify')

    def test_b_real_checks_precede_readonly_cross_review(self):
        self.step('plan')
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        self.assertEqual(self.paid, [('codex', True), ('claude', True)])
        stats = entry.pipeline.metrics(self.state)
        self.assertEqual((stats['planner_tokens'], stats['executor_tokens'], stats['reviewer_tokens']), (5, 5, 5))
        self.assertEqual(stats['premium_executor_tokens'], 0)

    def test_both_main_hosts_use_codex_open_and_opposite_premium_review(self):
        for host, planner, reviewer in [('claude-code', 'claude', 'codex'),
                                        ('codex-cli', 'codex', 'claude')]:
            with self.subTest(host=host):
                self.paid.clear()
                self.state = entry.pipeline_step(None, 'start', dict(
                    classification=dict(tier='T2', risk='MEDIUM', capabilities=[]),
                    text='write app', main_host=host, executor_runtime='codex-cli'), self.repo)
                self.addCleanup(shutil.rmtree, self.state['run_dir'])
                self.step('plan')
                def worker(*args, **kwargs):
                    self.assertEqual(kwargs['runtime'], 'codex-cli')
                    result = self._worker(*args, **kwargs)
                    result.update(runtime='codex-cli', provider='vercel',
                                  runtime_model_pair='codex-cli|vercel|configured-open')
                    return result
                with patch.object(entry.open_executor, 'run', side_effect=worker):
                    self.step('execute')
                self.checks()
                self.step('review')
                self.assertEqual(self.state['stage'], 'pass')
                self.assertEqual(self.paid, [(planner, True), (reviewer, True)])
                stats = entry.pipeline.metrics(self.state)
                self.assertEqual(stats['main_host'], host)
                self.assertEqual(stats['executor_runtime'], 'codex-cli')
                self.assertEqual(stats['executor_provider'], 'vercel')
                self.assertFalse(stats['premium_execution_used'])

    def test_claude_open_runtime_is_independent_of_codex_host(self):
        self.state = entry.pipeline_step(None, 'start', dict(
            classification=dict(tier='T2', risk='MEDIUM', capabilities=[]),
            text='write app', main_host='codex', executor_runtime='claude-code'), self.repo)
        self.addCleanup(shutil.rmtree, self.state['run_dir'])
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self._worker) as worker:
            self.step('execute')
        self.assertEqual(worker.call_args.kwargs['runtime'], 'claude-code')
        self.assertEqual(self.state['planner'], 'codex')
        self.assertEqual(self.state['reviewer'], 'claude')

    def test_modified_tree_invalidates_verification(self):
        self.step('plan')
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        (self.repo / 'app.py').write_text('value = 3\n')
        with self.assertRaisesRegex(ValueError, 'changed after verification'): self.step('review')
        self.assertEqual(len(self.paid), 1)

    def test_worker_refusal_does_not_spend_retry_or_escalate(self):
        self.step('plan')
        with patch.object(entry.delegate, 'run', side_effect=SystemExit(6)):
            with self.assertRaisesRegex(ValueError, 'refused'): self.step('execute')
        self.assertEqual(self.state['failures'], 0)
        self.assertFalse(self.state['premium_execution_used'])

    def test_finding_arbitration_rejects_invented_checks(self):
        self.state.update(stage='arbitrate', findings=[{'id': 'f'}], verification={'revision': entry.snapshot(self.repo)[0]})
        with self.assertRaisesRegex(ValueError, 'mechanical'):
            self.step('arbitrate', dict(verdicts=[dict(id='f', confirmed=True, evidence='claimed', check_ids=['fake'])]))

    def test_c_confirmed_finding_runs_open_fixer_and_passes(self):
        self.state['planner'], self.state['reviewer'] = 'claude', 'codex'
        self.worker_write = 'value = 2\n\ndef answer():\n    return 41\n'
        self.findings = [finding()]
        self.step('plan')
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        self.step('review')
        self.assertEqual((self.state['planner'], self.state['reviewer']), ('claude', 'codex'))
        self.assertEqual(self.state['stage'], 'arbitrate')
        self.step('check', dict(id='answer', argv=[sys.executable, '-B', '-c', 'import app; assert app.answer() == 42']))
        self.assertEqual(self.state['checks'][-1]['exit_code'], 1)
        self.step('arbitrate', dict(verdicts=[dict(id='f1', confirmed=True, evidence='reproduced', check_ids=['answer'])]))
        self.assertEqual((self.state['stage'], self.state['role']), ('execute', 'fixer'))
        self.findings = []
        self.worker_write = 'value = 2\n\ndef answer():\n    return 42\n'
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.step('check', dict(id='answer', argv=[sys.executable, '-B', '-c', 'import app; assert app.answer() == 42']))
        self.assertEqual(self.state['checks'][-1]['exit_code'], 0)
        self.checks()
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        self.assertEqual(self.state['findings_confirmed'], 1)
        self.assertEqual(self.paid[0][0], 'claude')
        self.assertEqual(self.paid[1][0], 'codex')

    def test_c_refuted_finding_passes_without_fixer(self):
        self.worker_write = 'value = 2\n\ndef answer():\n    return 42\n'
        self.findings = [finding(finding='answer() returns 41', required_fix='return 41')]
        self.step('plan')
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        self.step('review')
        self.assertEqual(self.state['stage'], 'arbitrate')
        self.step('check', dict(id='answer', argv=[sys.executable, '-B', '-c', 'import app; assert app.answer() == 42']))
        self.assertEqual(self.state['checks'][-1]['exit_code'], 0)
        self.step('arbitrate', dict(verdicts=[dict(id='f1', confirmed=False, evidence='counterevidence', check_ids=['answer'])]))
        self.assertEqual(self.state['stage'], 'pass')
        self.assertEqual(self.state['findings_refuted'], 1)
        self.assertEqual(len(self.worker_models), 1)
        self.assertEqual(self.state['role'], 'executor')

    def test_d_retry_then_fallback_then_premium_execution_completes(self):
        self.step('plan')
        for _ in range(4):
            with patch.object(entry.delegate, 'run', side_effect=self._failing_worker): self.step('execute')
        self.assertEqual(self.state['stage'], 'escalate')
        self.assertFalse(self.state['premium_execution_used'])
        self.assertEqual(self.worker_models, [self.config.open_primary] * 2 + [self.config.open_fallback] * 2)
        self.step('escalate')
        self.assertTrue(self.state['premium_execution_used'])
        self.checks()
        self.assertEqual(self.state['stage'], 'review')
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        self.assertIn(('codex', False), self.paid)

    def test_e_t3_two_open_subtasks_then_opposite_review(self):
        self.plan['subtasks'].append(dict(id='two', objective='integrate', files=['app.py']))
        self.begin('T3')
        self.step('plan')
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        self.assertEqual((self.state['stage'], self.state['subtask']), ('execute', 1))
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        self.assertEqual(self.state['stage'], 'review')
        self.assertNotEqual(self.state['planner'], self.state['reviewer'])
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')

    def test_t3_subtask_tests_gate_integration_until_final(self):
        self.plan['files'] = ['app.py', 'lib.py']
        self.plan['tests'] = ['unit', 'diff', 'security', 'integration']
        self.plan['subtasks'] = [
            dict(id='one', objective='write app', files=['app.py'], tests=['unit']),
            dict(id='two', objective='integrate lib', files=['lib.py'], tests=['integration']),
        ]
        self.begin('T3')
        self.step('plan')
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.step('check', dict(id='unit', argv=[sys.executable, '-B', '-c', 'import app; assert app.value == 2']))
        self.step('check', dict(id='diff', argv=['git', 'diff', '--check']))
        self.step('check', dict(id='security', argv=['bash', str(ROOT / 'bin/aos-security.sh')]))
        self.assertEqual((self.step('verify')['stage'], self.state['subtask']), ('execute', 1))
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.step('check', dict(id='diff', argv=['git', 'diff', '--check']))
        self.step('check', dict(id='security', argv=['bash', str(ROOT / 'bin/aos-security.sh')]))
        with self.assertRaisesRegex(ValueError, 'missing current deterministic checks'):
            self.step('verify')

    def test_review_receives_original_task_not_only_plan(self):
        self.state = self.begin('T2', text='the app must return exactly 42')
        self.step('plan')
        with patch.object(entry.delegate, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        reviews = []
        def premium(backend, text, directory, readonly=False):
            if readonly and 'PREMIUM PLANNER' in text:
                result = self.plan
            else:
                result = dict(findings=[], attacked=['acceptance and security'])
                if readonly:
                    reviews.append((backend, text))
            return dict(reply=json.dumps(result), usage=dict(input_tokens=3, output_tokens=2))
        with patch.object(entry, 'execute', side_effect=premium):
            self.step('review')
        self.assertTrue(reviews)
        _, text = reviews[-1]
        payload = json.loads(text[text.index('{"task"'):])
        self.assertEqual(payload['task'], 'the app must return exactly 42')
        self.assertNotIn('42', json.dumps(self.plan, sort_keys=True))


if __name__ == '__main__':
    unittest.main()
