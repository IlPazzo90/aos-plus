"""Pure role pipeline. Host adapters supply observed evidence, never permission."""
from copy import deepcopy

PLAN_FIELDS = ('objective', 'scope', 'files', 'steps', 'acceptance_criteria', 'risks',
               'security_constraints', 'tests', 'architecture', 'dependencies',
               'uncertainties', 'do_not_modify', 'subtasks')
FINDING_FIELDS = ('id', 'severity', 'category', 'file', 'location', 'finding',
                  'evidence', 'required_fix', 'confidence')


def start(tier, risk, planner, reviewer, primary, fallback, attempts):
    if tier not in ('T0', 'T1', 'T2', 'T3') or risk not in ('LOW', 'MEDIUM', 'HIGH'):
        raise ValueError('pipeline does not authorize CRITICAL or unknown classifications')
    if not primary or type(attempts) is not int or attempts < 1:
        raise ValueError('open model and positive attempt budget required')
    complex_task = tier in ('T2', 'T3')
    return dict(tier=tier, risk=risk, planner=planner if complex_task else None,
                reviewer=reviewer if complex_task or risk == 'HIGH' else None,
                stage='plan' if complex_task else 'execute', role='executor',
                primary=primary, fallback=fallback, model=primary, attempts=attempts,
                failures=0, subtask=0, plan=None, findings=[], verdicts=[], review_rounds=0,
                findings_total=0, findings_confirmed=0, findings_refuted=0,
                open_retry_count=0, premium_execution_used=False,
                current_execution_premium=False,
                premium_execution_reason=None, history=[])


def validate_plan(plan):
    if not isinstance(plan, dict) or any(k not in plan for k in PLAN_FIELDS):
        raise ValueError('incomplete structured plan')
    if not isinstance(plan['objective'], str) or not plan['objective'].strip():
        raise ValueError('plan objective required')
    for key in PLAN_FIELDS[1:-1]:
        values = plan[key]
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError('plan fields must be string lists: ' + key)
    for key in ('scope', 'files', 'steps', 'acceptance_criteria', 'security_constraints', 'tests'):
        if not plan[key]:
            raise ValueError('empty required plan field: ' + key)
    subtasks = plan['subtasks']
    if not isinstance(subtasks, list) or not subtasks or len(subtasks) > 20:
        raise ValueError('one to twenty bounded subtasks required')
    ids = set()
    for task in subtasks:
        if not isinstance(task, dict) or not all(task.get(k) for k in ('id','objective','files')):
            raise ValueError('subtask id/objective/files required')
        if not isinstance(task['id'], str) or task['id'] in ids:
            raise ValueError('unique subtask ids required')
        ids.add(task['id'])
        if not isinstance(task['files'], list) or any(f not in plan['files'] for f in task['files']):
            raise ValueError('subtask exceeds plan file scope')
        task_tests = task.get('tests')
        if task_tests is not None and (not isinstance(task_tests, list) or not task_tests
                                       or any(not isinstance(t, str) or not t.strip() for t in task_tests)):
            raise ValueError('subtask tests must be a nonempty string list')
    return plan


def fail(state, evidence):
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError('failure evidence required')
    state['failures'] += 1
    count, limit = state['failures'], state['attempts']
    if count < limit:
        state.update(stage='execute', model=state['primary'])
    elif state['fallback'] and count < limit * 2:
        state.update(stage='execute', model=state['fallback'])
    else:
        state.update(stage='escalate', premium_execution_reason='open_attempts_exhausted')


def review_or_pass(state):
    if state['tier'] in ('T2','T3') or state['risk'] == 'HIGH':
        state['stage'] = 'review' if state['reviewer'] else 'blocked'
    else:
        state['stage'] = 'pass'


def advance(current, event, payload):
    state = deepcopy(current)
    stage = state['stage']
    if not isinstance(payload, dict):
        raise ValueError('event payload must be an object')
    if event == 'plan' and stage == 'plan':
        state['plan'] = validate_plan(payload)
        if not state['planner']:
            raise ValueError('premium planner unavailable')
        state['stage'] = 'execute'
    elif event == 'executed' and stage == 'execute':
        if type(payload.get('ok')) is not bool:
            raise ValueError('observed execution result required')
        if state['failures']:
            state['open_retry_count'] += 1
        if payload['ok']:
            state['stage'] = 'verify'
        else:
            fail(state, payload.get('evidence'))
    elif event == 'verified' and stage == 'verify':
        if type(payload.get('ok')) is not bool or not payload.get('evidence') or not payload.get('revision'):
            raise ValueError('observed checks and revision required')
        state['verification'] = payload
        if not payload['ok']:
            if state['current_execution_premium']:
                state['stage'] = 'blocked'
            else:
                fail(state, payload['evidence'])
        elif state['role'] == 'executor' and state['plan'] and state['subtask'] + 1 < len(state['plan']['subtasks']):
            state.update(subtask=state['subtask'] + 1, stage='execute', failures=0,
                         model=state['primary'], current_execution_premium=False)
        else:
            review_or_pass(state)
    elif event == 'reviewed' and stage == 'review':
        findings = payload.get('findings')
        if not isinstance(findings, list) or not payload.get('attacked'):
            raise ValueError('completed review needs findings and attacked criteria')
        ids = set()
        for finding in findings:
            if not isinstance(finding, dict) or any(not isinstance(finding.get(k), str) or not finding[k].strip() for k in FINDING_FIELDS):
                raise ValueError('structured finding incomplete')
            if finding['id'] in ids:
                raise ValueError('duplicate finding id')
            ids.add(finding['id'])
        state['review_rounds'] += 1
        state['findings_total'] += len(findings)
        state.update(findings=findings, stage='arbitrate' if findings else 'pass')
    elif event == 'arbitrated' and stage == 'arbitrate':
        verdicts = payload.get('verdicts')
        if not isinstance(verdicts, list) or len(verdicts) != len(state['findings']):
            raise ValueError('every finding requires a verdict')
        ids = {f['id'] for f in state['findings']}
        seen = set()
        for v in verdicts:
            if (not isinstance(v, dict) or v.get('id') not in ids or v['id'] in seen
                    or type(v.get('confirmed')) is not bool or not isinstance(v.get('evidence'), str) or not v['evidence'].strip()):
                raise ValueError('unique verdict with mechanical evidence required')
            seen.add(v['id'])
        confirmed = sum(v['confirmed'] for v in verdicts)
        state['findings_confirmed'] += confirmed
        state['findings_refuted'] += len(verdicts) - confirmed
        state['verdicts'].extend(verdicts)
        state['fix_findings'] = [f for f in state['findings'] if any(v['id']==f['id'] and v['confirmed'] for v in verdicts)]
        if confirmed:
            state.update(stage='execute' if state['review_rounds'] < 6 else 'blocked',
                         role='fixer', failures=0, model=state['primary'],
                         current_execution_premium=False)
        else:
            state['stage'] = 'pass'
    elif event == 'escalated' and stage == 'escalate':
        if payload.get('ok') is not True:
            raise ValueError('premium execution did not complete')
        state.update(stage='verify', premium_execution_used=True, current_execution_premium=True)
    else:
        raise ValueError('event not allowed in stage: ' + stage)
    state['history'].append(dict(event=event, from_stage=stage, to_stage=state['stage']))
    return state


def metrics(state):
    """Measured role totals. Unknown usage is never silently counted as zero."""
    events = state.get('role_events', [])
    result = {'tier': state.get('tier'), 'main_host': state.get('main_host'), 'role_events': events}
    for role in ('planner','executor','reviewer','fixer','premium_executor'):
        selected = [e for e in events if e.get('role') == role]
        values = [e.get('tokens') for e in selected]
        if any(v is not None and (type(v) is not int or v < 0) for v in values):
            raise ValueError('invalid role token counter')
        result[role + '_model'] = selected[-1].get('model') if selected else None
        result[role + '_runtime'] = selected[-1].get('runtime') if selected else None
        result[role + '_provider'] = selected[-1].get('provider') if selected else None
        result[role + '_tokens'] = None if any(v is None for v in values) else sum(values)
    for key in ('premium_execution_used','premium_execution_reason','findings_total',
                'findings_confirmed','findings_refuted','open_retry_count'):
        result[key] = state.get(key)
    result['cross_model_review'] = bool(any(e.get('role') == 'reviewer' for e in events)
                                       and state.get('planner') != state.get('reviewer'))
    result['premium_review_tokens'] = result['reviewer_tokens']
    open_parts = [result['executor_tokens'],result['fixer_tokens']]
    result['open_executor_tokens'] = sum(open_parts) if None not in open_parts else None
    open_tokens, premium_exec = result['open_executor_tokens'], result['premium_executor_tokens']
    result['workload_open_ratio'] = (round(open_tokens / (open_tokens + premium_exec), 3)
                                    if None not in (open_tokens,premium_exec) and open_tokens + premium_exec else None)
    premium_parts = [result['planner_tokens'],result['reviewer_tokens'],premium_exec]
    total_parts = premium_parts + [open_tokens]
    result['premium_dependency_ratio'] = (round(sum(premium_parts) / sum(total_parts),3)
                                          if None not in total_parts and sum(total_parts) else None)
    open_events = [e for e in events if e.get('role') in ('executor', 'fixer')]
    result['runtime_model_pair'] = open_events[-1].get('runtime_model_pair') if open_events else None
    result['benchmark_pair'] = result['runtime_model_pair']
    result['open_executor_success'] = sum(e.get('exit_code') == 0 for e in open_events)
    result['open_executor_retry'] = state.get('open_retry_count', 0)
    result['open_fallback_used'] = any(e.get('model') == state.get('fallback') for e in open_events)
    result['estimated_premium_tokens_saved'] = None  # Requires a comparable measured baseline.
    return result
