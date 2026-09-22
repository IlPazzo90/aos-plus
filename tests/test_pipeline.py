"""Role contracts and synthetic E2E scenarios; no provider calls."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'bin' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

router = load('aos-router')

class RoleTests(unittest.TestCase):
    def setUp(self):
        self.config = router.load_config(ROOT / 'config/open-models.json')

    def test_t1_has_no_planner(self):
        d = router.decide('T1', 'LOW', config=self.config)
        self.assertIsNone(getattr(d, 'planner', 'missing'))
        self.assertEqual(d.executor, 'open')

    def test_t2_and_t3_have_open_executor_and_opposite_reviewer(self):
        for tier in ('T2', 'T3'):
            for planner, reviewer in [('codex', 'claude'), ('claude', 'codex')]:
                d = router.decide(tier, 'MEDIUM', config=self.config, planner=planner)
                self.assertEqual((d.executor, d.planner, d.reviewer, d.fixer),
                                 ('open', planner, reviewer, 'open'))
                self.assertTrue(d.cross_model_review)
                self.assertFalse(d.premium_execution_used)

    def test_retry_then_fallback_then_premium(self):
        ds = [router.decide('T2', 'MEDIUM', config=self.config, failed_open_attempts=n) for n in range(5)]
        self.assertEqual([d.model for d in ds[:4]], [self.config.open_primary]*2 + [self.config.open_fallback]*2)
        self.assertEqual(ds[4].executor, 'premium')
        self.assertTrue(ds[4].premium_execution_used)

    def test_policy_default_and_security(self):
        self.assertFalse(self.config.premium_execution_default)
        self.assertEqual(router.decide('T2', 'HIGH', config=self.config, observable_check=True).verify, 'premium_review')
        self.assertEqual(router.decide('T3', 'CRITICAL', config=self.config, manual_override=True).verify, 'needs_approval')

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'bin/aos-pipeline.py').exists(), 'explicit pipeline is missing')
        self.p = load('aos-pipeline')
        self.plan = dict(objective='fix behavior', scope=['app.py'], files=['app.py'],
                         steps=['implement'], acceptance_criteria=['check passes'], risks=['regression'],
                         security_constraints=['no external effects'], tests=['unit', 'diff', 'security'],
                         architecture=['retain API'], dependencies=[], uncertainties=[], do_not_modify=['credentials'],
                         subtasks=[dict(id='one', objective='fix behavior', files=['app.py'])])

    def start(self, tier='T2'):
        s = self.p.start(tier, 'MEDIUM', 'codex', 'claude', 'open/primary', 'open/fallback', 2)
        if tier in ('T2', 'T3'):
            s = self.p.advance(s, 'plan', self.plan)
        return s

    def verified(self, s, success=True):
        s = self.p.advance(s, 'executed', {'ok': True})
        return self.p.advance(s, 'verified', {'ok': success, 'evidence': 'unit+diff+security observed', 'revision': 'rev1'})

    def test_a_t1_no_premium(self):
        s = self.verified(self.start('T1'))
        self.assertEqual(s['stage'], 'pass')
        self.assertIsNone(s['planner'])

    def test_b_t2_pass_after_cross_review_and_arbitration(self):
        s = self.verified(self.start())
        self.assertEqual(s['stage'], 'review')
        s = self.p.advance(s, 'reviewed', {'findings': [], 'attacked': ['acceptance', 'security']})
        self.assertEqual(s['stage'], 'pass')

    def test_c_confirmed_finding_goes_to_open_fixer(self):
        s = self.verified(self.start())
        finding = dict(id='f1', severity='high', category='security', file='app.py', location='12',
                       finding='missing check', evidence='reproduction', required_fix='add check', confidence='high')
        s = self.p.advance(s, 'reviewed', {'findings': [finding], 'attacked': ['security']})
        s = self.p.advance(s, 'arbitrated', {'verdicts': [{'id':'f1','confirmed':True,'evidence':'check reproduced'}]})
        self.assertEqual((s['stage'], s['role'], s['model']), ('execute', 'fixer', 'open/primary'))
        s = self.verified(s)
        s = self.p.advance(s, 'reviewed', {'findings': [], 'attacked': ['regression']})
        self.assertEqual(s['stage'], 'pass')
        self.assertEqual(s['findings_confirmed'], 1)

    def test_refuted_finding_does_not_fix(self):
        s = self.verified(self.start())
        f = dict(id='f', severity='low', category='logic', file='app.py', location='1', finding='claim', evidence='claim', required_fix='fix', confidence='low')
        s = self.p.advance(s, 'reviewed', {'findings':[f], 'attacked':['logic']})
        s = self.p.advance(s, 'arbitrated', {'verdicts':[dict(id='f',confirmed=False,evidence='counterexample')]})
        self.assertEqual(s['stage'], 'pass')
        self.assertEqual(s['findings_refuted'], 1)

    def test_d_failure_exhaustion(self):
        s = self.start()
        models=[]
        for _ in range(4):
            models.append(s['model'])
            s = self.p.advance(s, 'executed', {'ok':False, 'evidence':'test failed'})
        self.assertEqual(models, ['open/primary']*2 + ['open/fallback']*2)
        self.assertEqual(s['stage'], 'escalate')
        self.assertEqual(s['open_retry_count'], 3)
        self.assertTrue(s['premium_execution_used'] is False)

    def test_mid_model_runs_once_after_the_two_cheap_models(self):
        s = self.p.start('T2', 'MEDIUM', 'codex', 'claude', 'open/primary', 'open/fallback', 2,
                         mid='open/mid')
        s = self.p.advance(s, 'plan', self.plan)
        models = []
        for _ in range(4):
            models.append(s['model'])
            s = self.p.advance(s, 'executed', {'ok': False, 'evidence': 'test failed'})
        self.assertEqual((models, s['stage'], s['model']),
                         (['open/primary'] * 2 + ['open/fallback'] * 2, 'execute', 'open/mid'))
        s = self.p.advance(s, 'executed', {'ok': False, 'evidence': 'test failed'})
        self.assertEqual(s['stage'], 'escalate')

    def test_failure_ladder_records_observed_transitions_without_cost_claims(self):
        s = self.p.start('T2', 'MEDIUM', 'codex', 'claude', 'open/primary', 'open/fallback', 2,
                         mid='open/mid')
        s = self.p.advance(s, 'plan', self.plan)
        for failure in ('primary retry', 'fallback', 'fallback retry', 'mid', 'premium'):
            s = self.p.advance(s, 'executed', {'ok': False, 'evidence': failure})
        events = s['escalation_events']
        self.assertEqual([(event['previous_failure'], event['selected_next_model'], event['target']) for event in events], [
            ('primary retry', 'open/primary', 'open_executor'),
            ('fallback', 'open/fallback', 'open_executor'),
            ('fallback retry', 'open/fallback', 'open_executor'),
            ('mid', 'open/mid', 'open_executor'),
            ('premium', None, 'premium_executor'),
        ])
        self.assertEqual([event['transition'] for event in events], [False, True, False, True, True])
        self.assertTrue(all(event['estimated_cost_increase'] is None for event in events))
        self.assertTrue(all('not guaranteed' in event['expected_capability_gain'] or
                            'no capability gain' in event['expected_capability_gain'] for event in events))

    def test_failed_last_resort_blocks_instead_of_looping_premium(self):
        s = self.start()
        for _ in range(4): s = self.p.advance(s, 'executed', {'ok':False,'evidence':'failure'})
        s = self.p.advance(s, 'escalated', {'ok':True})
        s = self.p.advance(s, 'verified', {'ok':False,'evidence':'still fails','revision':'v2'})
        self.assertEqual(s['stage'],'blocked')

    def test_e_t3_bounded_subtasks_then_integration_review(self):
        self.plan['subtasks'].append(dict(id='two', objective='integration', files=['app.py']))
        s = self.start('T3')
        s = self.verified(s)
        self.assertEqual((s['stage'], s['subtask']), ('execute', 1))
        s = self.verified(s)
        self.assertEqual(s['stage'], 'review')

    def test_later_open_subtask_keeps_retries_after_premium_escalation(self):
        self.plan['subtasks'].append(dict(id='two', objective='integration', files=['app.py']))
        s = self.start('T3')
        for _ in range(4):
            s = self.p.advance(s, 'executed', {'ok': False, 'evidence': 'failure'})
        s = self.p.advance(s, 'escalated', {'ok': True})
        s = self.p.advance(s, 'verified', {'ok': True, 'evidence': 'pass', 'revision': 'r1'})
        s = self.verified(s, success=False)
        self.assertEqual(s['stage'], 'execute')
        self.assertTrue(s['premium_execution_used'])
        self.assertEqual(s['model'], 'open/primary')

    def test_open_fixer_keeps_retries_after_premium_escalation(self):
        self.plan['subtasks'].append(dict(id='two', objective='integration', files=['app.py']))
        s = self.start('T3')
        for _ in range(4):
            s = self.p.advance(s, 'executed', {'ok': False, 'evidence': 'failure'})
        s = self.p.advance(s, 'escalated', {'ok': True})
        s = self.p.advance(s, 'verified', {'ok': True, 'evidence': 'pass', 'revision': 'r1'})
        s = self.verified(s, success=True)
        self.assertEqual(s['stage'], 'review')
        finding = dict(id='f1', severity='high', category='security', file='app.py', location='1',
                       finding='bug', evidence='repro', required_fix='fix', confidence='high')
        s = self.p.advance(s, 'reviewed', {'findings': [finding], 'attacked': ['security']})
        s = self.p.advance(s, 'arbitrated', {'verdicts': [{'id': 'f1', 'confirmed': True, 'evidence': 'repro'}]})
        self.assertEqual((s['stage'], s['role']), ('execute', 'fixer'))
        s = self.p.advance(s, 'executed', {'ok': True})
        s = self.p.advance(s, 'verified', {'ok': False, 'evidence': 'still fails', 'revision': 'r2'})
        self.assertEqual(s['stage'], 'execute')
        self.assertEqual(s['role'], 'fixer')
        self.assertEqual(s['model'], 'open/primary')
        self.assertTrue(s['premium_execution_used'])

    def test_no_review_before_verify_and_no_empty_arbitration(self):
        s = self.start()
        with self.assertRaises(ValueError): self.p.advance(s, 'reviewed', {'findings':[]})
        with self.assertRaises(ValueError): self.p.advance(s, 'verified', {'ok':True})

    def test_invalid_plan_and_unavailable_reviewer_fail_closed(self):
        s = self.p.start('T2','HIGH','codex',None,'open/primary',None,2)
        with self.assertRaises(ValueError): self.p.advance(s, 'plan', {'steps':[]})
        s = self.p.advance(s, 'plan', self.plan)
        self.assertEqual(self.verified(s)['stage'], 'blocked')

if __name__ == '__main__': unittest.main()
