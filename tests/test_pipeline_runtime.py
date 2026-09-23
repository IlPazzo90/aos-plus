"""Synthetic models, real temporary Git tree and deterministic check processes."""
import importlib.util
import json
import os
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
        # A start without learning_database falls back to ~/.local/state/aos: point
        # HOME elsewhere so no test can write rows into the real ledger (300 fake
        # outcomes had accumulated there before this guard).
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        home_patch = patch.dict(os.environ, HOME=home.name)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.fake_home = Path(home.name)
        fd, database = tempfile.mkstemp(prefix='aos-learning-test-', suffix='.sqlite3')
        os.close(fd)
        Path(database).unlink()
        self.learning_db = Path(database)
        self.addCleanup(lambda: self.learning_db.unlink(missing_ok=True))
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

    def begin(self, tier='T2', text='write app', planner_preference=None):
        self.state = entry.pipeline_step(None, 'start',
            dict(classification=dict(tier=tier, risk='MEDIUM', capabilities=[]), text=text, main_host='claude-code',
                 learning_database=str(self.learning_db), planner_preference=planner_preference),
            self.repo)
        self.addCleanup(shutil.rmtree, self.state['run_dir'])
        return self.state

    def _premium(self, backend, text, directory, readonly=False, model=None):
        self.paid.append((backend, readonly, model))
        if not readonly:
            (self.repo / 'app.py').write_text(self.worker_write)
            result = dict(completed=True)
        elif 'PREMIUM PLANNER' in text:
            result = self.plan
        else:
            result = dict(findings=list(self.findings), attacked=list(self.attacked))
        return dict(reply=json.dumps(result), usage=dict(input_tokens=3, output_tokens=2), model=model)

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
        self.state = self.begin('T2', planner_preference='codex')
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        self.assertEqual(self.paid, [
            (self.state['planner'], True, self.state['role_models']['planner']),
            (self.state['reviewer'], True, self.state['role_models']['reviewer']),
        ])
        stats = entry.pipeline.metrics(self.state)
        self.assertEqual((stats['planner_tokens'], stats['executor_tokens'], stats['reviewer_tokens']), (5, 5, 5))
        cost_classes = {event['role']: event['cost_class'] for event in self.state['role_events']}
        self.assertEqual((stats['planner_cost_class'], stats['executor_cost_class'], stats['reviewer_cost_class']),
                         (cost_classes['planner'], cost_classes['executor'], cost_classes['reviewer']))
        self.assertEqual(stats['premium_executor_tokens'], 0)

    def test_both_main_hosts_use_claude_open_and_opposite_premium_review(self):
        for host, planner, reviewer in [('claude-code', 'codex', 'claude'),
                                        ('codex-cli', 'codex', 'claude')]:
            with self.subTest(host=host):
                self.paid.clear()
                self.state = entry.pipeline_step(None, 'start', dict(
                    classification=dict(tier='T2', risk='MEDIUM', capabilities=[]),
                    text='write app', main_host=host, executor_runtime='claude-code',
                    planner_preference=planner, learning_database=str(self.learning_db)), self.repo)
                self.addCleanup(shutil.rmtree, self.state['run_dir'])
                self.step('plan')
                def worker(*args, **kwargs):
                    self.assertEqual(kwargs['runtime'], 'claude-code')
                    result = self._worker(*args, **kwargs)
                    result.update(runtime='claude-code', provider='vercel',
                                  runtime_model_pair='claude-code|vercel|configured-open')
                    return result
                with patch.object(entry.open_executor, 'run', side_effect=worker):
                    self.step('execute')
                self.checks()
                self.step('review')
                self.assertEqual(self.state['stage'], 'pass')
                self.assertEqual([(backend, readonly) for backend, readonly, _ in self.paid],
                                 [(planner, True), (reviewer, True)])
                stats = entry.pipeline.metrics(self.state)
                self.assertEqual(stats['main_host'], host)
                self.assertEqual(stats['executor_runtime'], 'claude-code')
                self.assertEqual(stats['executor_provider'], 'vercel')
                self.assertFalse(stats['premium_execution_used'])

    def test_claude_open_runtime_is_independent_of_codex_host(self):
        self.state = entry.pipeline_step(None, 'start', dict(
            classification=dict(tier='T2', risk='MEDIUM', capabilities=[]),
            text='write app', main_host='codex', executor_runtime='claude-code',
            planner_preference='codex', learning_database=str(self.learning_db)), self.repo)
        self.addCleanup(shutil.rmtree, self.state['run_dir'])
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self._worker) as worker:
            self.step('execute')
        self.assertEqual(worker.call_args.kwargs['runtime'], 'claude-code')
        self.assertEqual(self.state['planner'], 'codex')
        self.assertEqual(self.state['reviewer'], 'claude')

    def test_modified_tree_invalidates_verification(self):
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        (self.repo / 'app.py').write_text('value = 3\n')
        with self.assertRaisesRegex(ValueError, 'changed after verification'): self.step('review')
        self.assertEqual(len(self.paid), 1)

    def test_worker_refusal_does_not_spend_retry_or_escalate(self):
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=SystemExit(6)):
            with self.assertRaisesRegex(ValueError, 'refused'): self.step('execute')
        self.assertEqual(self.state['failures'], 0)
        self.assertFalse(self.state['premium_execution_used'])

    def test_finding_arbitration_rejects_invented_checks(self):
        self.state.update(stage='arbitrate', findings=[{'id': 'f'}], verification={'revision': entry.snapshot(self.repo)[0]})
        with self.assertRaisesRegex(ValueError, 'mechanical'):
            self.step('arbitrate', dict(verdicts=[dict(id='f', confirmed=True, evidence='claimed', check_ids=['fake'])]))

    def test_c_confirmed_finding_runs_open_fixer_and_passes(self):
        self.state['planner'], self.state['reviewer'] = 'claude', 'codex'
        self.state['role_models'].update(planner=self.config.premium_models['claude'],
                                         reviewer=self.config.premium_models['codex'])
        self.worker_write = 'value = 2\n\ndef answer():\n    return 41\n'
        self.findings = [finding()]
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
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
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
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
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
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

    def _fallback_policy(self):
        """The shipped policy with a cheap open fallback restored (3.0 ships none).

        The ladder mechanism outlives the model that used to fill the slot: these
        tests keep covering it on a policy that still has a fallback.
        """
        raw = json.loads((ROOT / 'config/open-models.json').read_text())
        fallback = 'vercel/alibaba/qwen3-coder-next'
        raw['open']['fallback'] = fallback
        raw['model_catalog'][fallback]['availability'] = 'configured'
        path = self.repo.parent / 'policy-with-fallback.json'
        path.write_text(json.dumps(raw))
        self.addCleanup(path.unlink)
        policy = patch.object(entry, 'POLICY', path)
        policy.start()
        self.addCleanup(policy.stop)
        self.config = entry.router.load_config(path)
        return fallback

    def test_d_without_fallback_two_open_attempts_then_premium(self):
        self.assertIsNone(self.config.open_fallback)
        self.step('plan')
        for _ in range(2):
            with patch.object(entry.open_executor, 'run', side_effect=self._failing_worker): self.step('execute')
        self.assertEqual(self.state['stage'], 'escalate')
        self.assertEqual(self.worker_models, [self.config.open_primary] * 2)
        self.step('escalate')
        self.assertTrue(self.state['premium_execution_used'])

    def test_d_retry_then_fallback_then_premium_execution_completes(self):
        fallback = self._fallback_policy()
        self.begin('T2')
        self.step('plan')
        for _ in range(4):
            with patch.object(entry.open_executor, 'run', side_effect=self._failing_worker): self.step('execute')
        self.assertEqual(self.state['stage'], 'escalate')
        self.assertFalse(self.state['premium_execution_used'])
        self.assertEqual(self.worker_models, [self.config.open_primary] * 2 + [fallback] * 2)
        self.step('escalate')
        self.assertTrue(self.state['premium_execution_used'])
        self.checks()
        self.assertEqual(self.state['stage'], 'review')
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        self.assertIn(('codex', False, 'openai/gpt-6-astra'), self.paid)

    def test_e_t3_two_open_subtasks_then_opposite_review(self):
        self.plan['subtasks'].append(dict(id='two', objective='integrate', files=['app.py']))
        self.begin('T3')
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        self.assertEqual((self.state['stage'], self.state['subtask']), ('execute', 1))
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
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
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
        self.step('check', dict(id='unit', argv=[sys.executable, '-B', '-c', 'import app; assert app.value == 2']))
        self.step('check', dict(id='diff', argv=['git', 'diff', '--check']))
        self.step('check', dict(id='security', argv=['bash', str(ROOT / 'bin/aos-security.sh')]))
        self.assertEqual((self.step('verify')['stage'], self.state['subtask']), ('execute', 1))
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
        self.step('check', dict(id='diff', argv=['git', 'diff', '--check']))
        self.step('check', dict(id='security', argv=['bash', str(ROOT / 'bin/aos-security.sh')]))
        with self.assertRaisesRegex(ValueError, 'missing current deterministic checks'):
            self.step('verify')

    def test_review_receives_original_task_not_only_plan(self):
        self.state = self.begin('T2', text='the app must return exactly 42')
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
        self.checks()
        reviews = []
        def premium(backend, text, directory, readonly=False, model=None):
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

    def test_failed_deterministic_check_creates_project_lesson(self):
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker): self.step('execute')
        self.step('check', dict(id='unit', argv=[sys.executable, '-c', 'raise SystemExit(1)']))
        self.step('check', dict(id='diff', argv=['git', 'diff', '--check']))
        self.step('check', dict(id='security', argv=['bash', str(ROOT / 'bin/aos-security.sh')]))
        self.step('verify')
        self.assertEqual(entry.learning.report(self.learning_db)['lessons']['approved'], 1)

    def test_a_check_that_changes_git_metadata_blocks_the_pipeline(self):
        # Round 3 finding: a check runs the worker's code and could write .git/config
        # while the pipeline reached pass with no fingerprint taken.
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        real = entry.delegate.git_meta
        calls = {'n': 0}

        def drifting(paths):
            calls['n'] += 1
            return {'config': 'modified'} if calls['n'] > 1 else real(paths)
        with patch.object(entry.delegate, 'git_meta', side_effect=drifting):
            with self.assertRaisesRegex(ValueError, 'check changed repository metadata'):
                self.step('check', {'id': 'unit', 'argv': ['python3', '-c', 'pass']})
        self.assertEqual(self.state['checks'], [])

    def test_a_check_that_moves_head_blocks_the_pipeline(self):
        # Round 4: the metadata fingerprint covers config/info/hooks, not HEAD; a
        # check that commits would hand the reviewer a diff without the work.
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        heads = iter(['a' * 40, 'b' * 40])
        with patch.object(entry.delegate, 'git_head', side_effect=lambda d: next(heads)):
            with self.assertRaisesRegex(ValueError, 'check moved HEAD'):
                self.step('check', {'id': 'unit', 'argv': ['python3', '-c', 'pass']})
        self.assertEqual(self.state['checks'], [])

    def test_a_check_that_hides_a_file_through_the_index_blocks_the_pipeline(self):
        # Round 5: `git diff` skips a skip-worktree file, so an index flag removed the
        # work from the artefact handed to the reviewer while HEAD and the metadata
        # fingerprint stayed identical.
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        for argv in (['git', 'add', 'app.py'],
                     ['git', '-c', 'user.name=T', '-c', 'user.email=t@t.test', 'commit', '-qm', 'app']):
            subprocess.run(argv, cwd=self.repo, check=True, capture_output=True)
        (self.repo / 'app.py').write_text('value = 999   # the work\n')
        self.assertIn('999', entry.snapshot(self.repo)[1])
        with self.assertRaisesRegex(ValueError, 'index flags hide tracked files'):
            self.step('check', dict(id='hide', argv=['git', 'update-index', '--skip-worktree', 'app.py']))
        self.assertEqual(self.state['checks'], [])
        # The flag stays on disk: every later snapshot blocks too, it is not a
        # one-off refusal of that one check.
        with self.assertRaisesRegex(ValueError, 'index flags hide tracked files'):
            entry.snapshot(self.repo)

    def test_a_check_that_changes_the_worktree_is_not_recorded(self):
        # Round 5: the revision was taken after the command, so a test that asserted
        # the good code and rewrote it on the way out was recorded as a verification
        # of the version it had just broken.
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        with self.assertRaisesRegex(ValueError, 'check changed the worktree'):
            self.step('check', dict(id='unit', argv=[sys.executable, '-B', '-c',
                'import app; assert app.value == 2; open("app.py","w").write("value = 3\\n")']))
        self.assertEqual(self.state['checks'], [])

    def test_two_worktrees_never_share_one_revision(self):
        # Round 6: the revision hashed the rendered artefact, where a new file whose
        # content spells `NEW FILE <other>` is indistinguishable from two files. A
        # check could then create unverified code and keep its recorded revision.
        (self.repo / 'app.py').write_text('value = 2\n')
        (self.repo / 'a.txt').write_text('harmless\nNEW FILE ab.py\nvalue = 0')
        first, rendered_first = entry.snapshot(self.repo)
        (self.repo / 'a.txt').write_text('harmless')
        (self.repo / 'ab.py').write_text('value = 0')
        second, rendered_second = entry.snapshot(self.repo)
        self.assertEqual(rendered_first, rendered_second)   # the text alone cannot tell them apart
        self.assertNotEqual(first, second)

    def test_context_red_blocks_before_worker(self):
        self.step('plan')
        with patch.object(entry.operations, 'prepare_context', side_effect=ValueError('context still RED')):
            with self.assertRaisesRegex(ValueError, 'RED'):
                self.step('execute')

    def test_budget_with_unknown_catalog_cost_blocks(self):
        self.state['budget'] = {'max_task_cost': 1}
        with self.assertRaisesRegex(ValueError, 'unknown reserved estimate'):
            self.step('plan')

    def test_failed_worker_attempt_is_recorded_before_retry(self):
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self._failing_worker):
            self.step('execute')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0]['success'])
        self.assertEqual(outcomes[0]['error'], 'simulated worker failure')

    def test_t2_success_is_deferred_until_cross_model_review_passes(self):
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        self.assertEqual(entry.learning.list_outcomes(self.learning_db), [])
        self.step('review')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        self.assertEqual(len(outcomes), 1)
        self.assertTrue(outcomes[0]['success'])
        self.assertIsNone(outcomes[0]['error'])

    def test_invalid_arbitration_cannot_write_a_lesson(self):
        revision = entry.snapshot(self.repo)[0]
        self.state.update(stage='arbitrate', findings=[finding(id='one'), finding(id='two')],
                          verification={'revision': revision}, checks=[dict(
                              id='proof', argv=['true'], exit_code=0, output='ok',
                              revision=revision, stage='arbitrate')])
        data = dict(verdicts=[
            dict(id='one', confirmed=True, evidence='proof', check_ids=['proof']),
            dict(id='two', confirmed='not-a-bool', evidence='proof', check_ids=['proof']),
        ])
        with self.assertRaisesRegex(ValueError, 'unique verdict'):
            self.step('arbitrate', data)
        self.assertEqual(entry.learning.report(self.learning_db)['lessons']['total'], 0)

    def test_disabled_learning_does_not_create_outcomes(self):
        self.state['learning_enabled'] = False
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self._failing_worker):
            self.step('execute')
        self.assertEqual(entry.learning.list_outcomes(self.learning_db), [])

    def test_learning_advice_excludes_primary_for_this_pipeline_only(self):
        self._fallback_policy()
        advice = {'excluded_models': [self.config.open_primary], 'recommendations': []}
        selection = {'runtime': 'claude-code'}
        with patch.object(entry.open_executor, 'resolve', return_value=selection), \
             patch.object(entry.learning, 'routing_advice', return_value=advice, create=True):
            state = entry.pipeline_step(None, 'start', dict(
                classification=dict(tier='T2', risk='MEDIUM', capabilities=[]), text='write app', main_host='claude-code',
                executor_runtime='claude-code', learning_database=str(self.learning_db)), self.repo)
        self.addCleanup(shutil.rmtree, state['run_dir'])
        self.assertEqual(state['model'], self.config.open_fallback)
        self.assertEqual(state['routing_advice'], advice)

    def test_t1_uses_host_plan_without_premium_planner(self):
        state = entry.pipeline_step(None, 'start', dict(
            classification=dict(tier='T1', risk='MEDIUM', capabilities=[]), text='write app', main_host='claude-code',
            plan=self.plan, learning_database=str(self.learning_db)), self.repo)
        self.assertEqual(state['stage'], 'execute')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            state = entry.pipeline_step(state, 'execute', {}, self.repo)
        self.assertEqual(self.paid, [])
        self.assertEqual(state['role_events'][-1]['cost_class'], 'CHEAP')

    def test_pipeline_rows_carry_domain_and_bundle(self):
        self.state = self.begin('T2', text='fix the bug in this Python code')
        # A premium that reports an exit_code so the planner/reviewer observations
        # are actually recorded, letting us assert their bundle is None on the rows.
        def premium(backend, text, directory, readonly=False, model=None):
            if 'PREMIUM PLANNER' in text:
                result = self.plan
            else:
                result = dict(findings=[], attacked=['acceptance and security'])
            return dict(reply=json.dumps(result), usage=dict(input_tokens=3, output_tokens=2),
                        model=model, exit_code=0)
        with patch.object(entry, 'execute', side_effect=premium):
            self.step('plan')
            with patch.object(entry.open_executor, 'run', side_effect=self.worker):
                self.step('execute')
            self.checks()
            self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        by_role = {o['role']: o for o in outcomes}
        self.assertIn('ENGINEERING', by_role['executor']['domain'])
        self.assertEqual(by_role['executor']['bundle'], 's1')
        self.assertIsNone(by_role['planner']['bundle'])
        self.assertIsNone(by_role['reviewer']['bundle'])

    def test_pipeline_pass_records_one_verified_task_row(self):
        self.state = self.begin('T2', text='fix the bug in this Python code')
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        rows = entry.learning.list_tasks(self.learning_db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['outcome'], 'verified')
        self.assertEqual(row['first_pass'], 1)
        self.assertEqual(row['failed_attempts'], 0)
        self.assertEqual(row['escalated'], 0)
        self.assertIn('ENGINEERING', row['domain'])
        self.assertEqual(row['bundles'], 1)
        self.assertTrue(self.state.get('_task_recorded'))

    def test_one_worker_failure_marks_not_first_pass(self):
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self._failing_worker):
            self.step('execute')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        rows = entry.learning.list_tasks(self.learning_db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'verified')
        self.assertEqual(rows[0]['failed_attempts'], 1)
        self.assertEqual(rows[0]['first_pass'], 0)

    def test_pipeline_blocked_by_missing_reviewer_records_blocked(self):
        self.state = self.begin('T2')
        self.state['reviewer'] = None
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        self.assertEqual(self.state['stage'], 'blocked')
        rows = entry.learning.list_tasks(self.learning_db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'blocked')
        self.assertEqual(rows[0]['first_pass'], 0)

    def test_disabled_learning_writes_no_task_row(self):
        self.state = self.begin('T2')
        self.state['learning_enabled'] = False
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        self.assertEqual(entry.learning.list_tasks(self.learning_db), [])

    def test_second_call_in_a_terminal_stage_does_not_record_twice(self):
        state = dict(project=str(self.repo), task_id='task-terminal', stage='pass',
                     learning_enabled=True, learning_database=str(self.learning_db),
                     escalation_events=[], role_events=[], findings_confirmed=0,
                     findings_refuted=0, review_rounds=0,
                     plan={'subtasks': [{'id': 'one'}]})
        with patch.object(entry, '_pipeline_step', return_value=state) as impl:
            first = entry.pipeline_step(state, 'verify', {}, self.repo)
            second = entry.pipeline_step(first, 'verify', {}, self.repo)
        self.assertEqual(impl.call_count, 2)
        rows = entry.learning.list_tasks(self.learning_db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'verified')
        self.assertEqual(rows[0]['project'], str(self.repo))

    def test_failed_worker_records_implementation_failure(self):
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self._failing_worker):
            self.step('execute')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        self.assertEqual(outcomes[0]['failure_type'], 'implementation_failure')

    def test_timeout_worker_records_timeout_failure_type(self):
        self.step('plan')
        def timeout_worker(*args, **kwargs):
            return dict(exit_code=1, head_before='same', head_after='same', model=args[1],
                        error='timeout', usage=dict(input_tokens=1, output_tokens=1))
        with patch.object(entry.open_executor, 'run', side_effect=timeout_worker):
            self.step('execute')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        self.assertEqual(outcomes[0]['failure_type'], 'timeout')

    def test_failed_checks_on_second_subtask_record_only_s2(self):
        self.plan['subtasks'].append(dict(id='two', objective='integrate', files=['app.py']))
        self.begin('T3')
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        self.assertEqual((self.state['stage'], self.state['subtask']), ('execute', 1))
        self.assertEqual(entry.learning.list_outcomes(self.learning_db), [])
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.step('check', dict(id='unit', argv=[sys.executable, '-c', 'raise SystemExit(1)']))
        self.step('check', dict(id='diff', argv=['git', 'diff', '--check']))
        self.step('check', dict(id='security', argv=['bash', str(ROOT / 'bin/aos-security.sh')]))
        self.step('verify')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        self.assertEqual([o['bundle'] for o in outcomes], ['s2'])
        self.assertEqual(outcomes[0]['failure_type'], 'test_failure')
        self.assertFalse(outcomes[0]['success'])

    def test_explicit_profile_overrides_domains_and_task_type(self):
        self.state = entry.pipeline_step(None, 'start', dict(
            classification=dict(tier='T2', risk='MEDIUM', capabilities=[]),
            text='fix the bug in this Python code', main_host='claude-code',
            learning_database=str(self.learning_db),
            profile=dict(domains=['LEGAL_COMPLIANCE'], task_type='drafting')), self.repo)
        self.addCleanup(shutil.rmtree, self.state['run_dir'])
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        self.step('review')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        role = next(o for o in outcomes if o['role'] == 'executor')
        self.assertEqual(role['domain'], 'LEGAL_COMPLIANCE')
        self.assertEqual(role['task_type'], 'drafting')

    def test_matrix_reports_domain_from_pipeline_outcome(self):
        self.state = self.begin('T2', text='fix the bug in this Python code')
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        self.step('review')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        executor = next(o for o in outcomes if o['role'] == 'executor')
        matrix = entry.learning.matrix(str(self.learning_db))
        self.assertIn('ENGINEERING', matrix['models'][executor['model']]['domains'])

    def test_premium_executor_row_counts_as_work_in_kpi(self):
        # Open fails, premium rescues the same bundle: the bundle's final attempt is
        # the premium one, so the task is verified but not a first pass.
        common = dict(tier='T2', risk='MEDIUM', task_id='t1', project=str(self.repo),
                      verification_status='verified', bundle='s1')
        entry.learning.record_outcome(str(self.learning_db), dict(
            common, runtime='claude-code', provider='vercel', model='vercel/deepseek/deepseek-v4-pro-0813',
            role='executor', worker_exit=0, test_pass=False, retry=0, error='deterministic checks failed',
            failure_type='test_failure'))
        entry.learning.record_outcome(str(self.learning_db), dict(
            common, runtime='codex-cli', provider='openai', model='openai/gpt-6-astra',
            role='premium_executor', worker_exit=0, test_pass=True, retry=0, escalated=True))
        kpi = entry.learning.kpi(str(self.learning_db))
        self.assertEqual(kpi['verified_success_rate'], 1.0)
        self.assertEqual(kpi['first_pass_success_rate'], 0.0)

    def test_invalid_explicit_profile_fails_before_the_run_exists(self):
        for profile in (dict(domains=['NOT_A_DOMAIN']), dict(domains=['ENGINEERING'], task_type='x' * 41),
                        dict(domains=['ENGINEERING'], capabilities={'coding': 2})):
            with patch.object(entry.tempfile, 'mkdtemp') as mkdtemp, self.assertRaises(ValueError):
                entry.pipeline_step(None, 'start', dict(
                    classification=dict(tier='T2', risk='MEDIUM', capabilities=[]),
                    text='fix the bug in this Python code', main_host='claude-code',
                    learning_database=str(self.learning_db), profile=profile), self.repo)
            mkdtemp.assert_not_called()

    def test_invalid_bounded_plan_fails_before_the_run_exists(self):
        with patch.object(entry.tempfile, 'mkdtemp') as mkdtemp, self.assertRaises(ValueError) as caught:
            entry.pipeline_step(None, 'start', dict(
                classification=dict(tier='T1', risk='LOW', capabilities=[]),
                text='write app', main_host='claude-code', plan={'objective': 'x'},
                learning_database=str(self.learning_db)), self.repo)
        self.assertIn('incomplete structured plan', str(caught.exception))
        mkdtemp.assert_not_called()

    def test_keyword_profile_failure_leaves_rows_without_domain(self):
        with patch.object(entry.orchestrate, 'build_profile', side_effect=RuntimeError('boom')):
            self.state = self.begin('T2', text='fix the bug in this Python code')
        self.assertIsNone(self.state['adaptive_profile'])


    def test_a_start_without_learning_database_never_reaches_the_real_home(self):
        state = entry.pipeline_step(None, 'start', dict(
            classification=dict(tier='T2', risk='MEDIUM', capabilities=[]),
            text='write app', main_host='claude-code'), self.repo)
        self.addCleanup(shutil.rmtree, state['run_dir'])
        database = Path(state['learning_database']).resolve()
        self.assertIn(self.fake_home.resolve(), database.parents)

    def test_a_failed_task_write_does_not_duplicate_outcomes_on_retry(self):
        # Round 1 of the task-record review: the outcome rows were written, the task
        # row failed, the caller kept the old state and the retry wrote them again.
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        before = self.state
        def premium(*args, **kwargs):
            # An observed exit code makes the review call a recorded outcome.
            return dict(self._premium(*args, **kwargs), exit_code=0)
        with patch.object(entry, 'execute', side_effect=premium):
            with patch.object(entry.learning, 'record_task', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    self.step('review')
            self.state = before
            self.step('review')
        self.assertEqual(self.state['stage'], 'pass')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        self.assertEqual([o['role'] for o in outcomes].count('executor'), 1)
        # Round 2: the retried review is a second real call, not a duplicate.
        self.assertEqual(len(self.paid), 3)
        self.assertEqual([o['role'] for o in outcomes].count('reviewer'), 2)
        kpi = entry.learning.kpi(str(self.learning_db))
        self.assertEqual((kpi['tasks'], kpi['task_records']), (1, 1))

    def test_every_role_call_has_its_own_event_id(self):
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        ids = [e['event_id'] for e in self.state['role_events']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(isinstance(i, str) and len(i) == 32 for i in ids))

    def test_a_state_saved_before_event_ids_does_not_duplicate_on_retry(self):
        # Round 3: events of a state saved before event ids existed have none.
        self.step('plan')
        with patch.object(entry.open_executor, 'run', side_effect=self.worker):
            self.step('execute')
        self.checks()
        for event in self.state['role_events']:
            event.pop('event_id', None)
        before = self.state
        with patch.object(entry.learning, 'record_task', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.step('review')
        self.state = before
        self.step('review')
        outcomes = entry.learning.list_outcomes(self.learning_db)
        self.assertEqual([o['role'] for o in outcomes].count('executor'), 1)

if __name__ == '__main__':
    unittest.main()
