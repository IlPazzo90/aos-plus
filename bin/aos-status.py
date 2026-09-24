#!/usr/bin/env python3
"""Publish the AOS routing decision to the host status bar.

The router is a pure function and the open executor reports tokens, not money:
nothing on disk says which model AOS picked for the task in front of the user.
This writes that decision, plus the tokens an open worker actually burned, to a
per-session file the status line reads. The cost shown is an estimate computed
from the catalog prices in config/open-models.json, never provider billing.

Every command is a no-op when no session id is available, so a missing status
directory can never fail a delegation.
"""
import argparse
import contextlib
import fcntl
import importlib.util
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TTL_SECONDS = 8 * 3600


def status_dir():
    return Path(os.environ.get('AOS_STATUS_DIR') or (Path.home() / '.claude/aos-status'))


SESSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
MODEL_LOCK_WAIT_SECONDS = 0.5
MODEL_RE = re.compile(r'^[A-Za-z0-9._:/\[\] -]{1,80}$')


def session_id(explicit=None):
    # The id becomes a file name: anything that could leave the status directory
    # (a separator, a leading dot, `..`) is treated as no session at all.
    value = explicit or os.environ.get('CLAUDE_CODE_SESSION_ID') or ''
    return value if SESSION_RE.match(value) and '..' not in value else ''


def record_path(session):
    return status_dir() / (session + '.json')


def load(session):
    try:
        data = json.loads(record_path(session).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_live(session):
    """The record, or {} when its expiry is past, missing or not a finite number.

    Every partial update (profile, usage) starts from this: rewriting a dead
    record would give its old routing a fresh TTL.
    """
    state = load(session)
    expires = state.get('expires_at')
    if state and (not isinstance(expires, (int, float)) or isinstance(expires, bool)
                  or not math.isfinite(expires)
                  or expires < datetime.now(timezone.utc).timestamp()):
        return {}
    return state


@contextlib.contextmanager
def locked(session, wait=None):
    """Serialize read-modify-write of one session record across processes.

    wait=None blocks until the lock is free. A number of seconds bounds the
    wait and then raises BlockingIOError: the status line must never freeze on
    a lock held by a stuck process, but ordinary millisecond contention must
    not cost it the write either.
    """
    directory = status_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / (session + '.lock'), 'w') as handle:
        if wait is None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def catalog(model_ref):
    try:
        policy = json.loads((ROOT / 'config/open-models.json').read_text())
    except (OSError, ValueError):
        return {}
    return policy.get('model_catalog', {}).get(model_ref, {}) if model_ref else {}


def peak_multiplier(entry, when=None):
    """Return the peak-pricing multiplier in force at `when` (UTC)."""
    peak = (entry.get('pricing_details') or {}).get('peak_pricing') or {}
    multiplier = peak.get('multiplier')
    windows = peak.get('windows') or []
    if not multiplier or not windows:
        return 1.0
    when = when or datetime.now(timezone.utc)
    minute = when.hour * 60 + when.minute
    for window in windows:
        start, end = window.get('start_minute_utc'), window.get('end_minute_utc')
        if start is None or end is None:
            continue
        inside = start <= minute < end if start <= end else (minute >= start or minute < end)
        if inside:
            return float(multiplier)
    return 1.0


def prices(entry):
    """Per-million token prices: input, output, cached input. None when unknown."""
    detail = entry.get('pricing_details') or {}
    cached = detail.get('input_cache_read')
    return (entry.get('input_cost_per_million'), entry.get('output_cost_per_million'),
            float(cached) * 1_000_000 if cached is not None else None)


def estimate_cost(model_ref, tokens, when=None):
    """Estimated USD for these tokens. None when the catalog has no price."""
    entry = catalog(model_ref)
    price_in, price_out, price_cached = prices(entry)
    if price_in is None or price_out is None:
        return None, 1.0
    multiplier = peak_multiplier(entry, when)
    cached = tokens.get('cache', 0) or 0
    cost = ((tokens.get('input', 0) or 0) * price_in
            + (tokens.get('output', 0) or 0) * price_out
            + cached * (price_cached if price_cached is not None else price_in)) / 1_000_000
    return cost * multiplier, multiplier


def short_model(model_ref):
    """`vercel/deepseek/deepseek-v4-pro-0813` -> `deepseek-v4-pro`."""
    if not model_ref:
        return ''
    name = str(model_ref).rsplit('/', 1)[-1]
    head, _, tail = name.rpartition('-')
    return head if head and tail.isdigit() and len(tail) == 4 else name


def compact(count):
    if count is None:
        return None
    if count >= 1_000_000:
        return '%.1fM' % (count / 1_000_000)
    if count >= 1000:
        return '%dk' % round(count / 1000)
    return str(count)


def money(value):
    """Italian decimal comma, two digits, three when the estimate is tiny."""
    digits = 3 if 0 < value < 0.01 else 2
    return ('%.*f' % (digits, value)).replace('.', ',') + ' $'


DOMAIN_SHORT = {'ENGINEERING': 'ENG', 'LEGAL_COMPLIANCE': 'LEGAL', 'BUSINESS_OPERATIONS': 'BIZ',
                'RESEARCH': 'RES', 'DATA_ANALYTICS': 'DATA', 'GENERAL': 'GEN'}


def profile_label(state):
    """'ENG+LEGAL·bug_fix' from the prompt hook's classification, or ''."""
    domains = '+'.join(DOMAIN_SHORT.get(d, d) for d in (state.get('domains') or []) if d)
    return '·'.join(part for part in (domains, state.get('task_type')) if part)


def render(state):
    """One status-bar segment; '' when there is nothing worth showing."""
    tier, risk = state.get('tier'), state.get('risk')
    head = ' '.join(part for part in (profile_label(state), '/'.join(p for p in (tier, risk) if p)) if part)
    executor = state.get('executor')
    chain = [state.get('planner'), short_model(state.get('model')) or executor, state.get('reviewer')]
    chain = ' → '.join(part for part in chain if part)
    tokens = state.get('tokens') or {}
    counts = None
    if tokens.get('input') or tokens.get('output'):
        counts = '%s/%s' % (compact(tokens.get('input') or 0), compact(tokens.get('output') or 0))
    # Money actually spent on a worker wins over 'sub': a pipeline started from a
    # main-executor classification still burns gateway tokens.
    if state.get('cost_usd') is not None:
        price = '~' + money(state['cost_usd'])
    elif executor in ('main', 'premium'):
        price = 'sub'
    else:
        price = None
    parts = [part for part in (head, chain, counts, price) if part]
    if not parts:
        return ''
    segment = '🤖 ' + ' · '.join(parts)
    # Warn only when the router would have sent this task to an open worker but
    # the caller published something else. Premium-vs-main is deliberately
    # silent: the main session already runs a premium-grade model, so that
    # warning would fire on nearly every HIGH task and train the reader to
    # ignore the symbol.
    if state.get('routed_executor') == 'open' and executor != 'open':
        reason = state.get('override_reason')
        if reason:
            # A real override (router said open, executor is not open) has a
            # stored reason: show it even once money has been spent, the cost
            # must not hide why the open route was overridden.
            segment += ' ⚠ open: ' + reason[:40]
        elif state.get('cost_usd') is None:
            # Records written by older versions carry no reason: the bare marker
            # is all we have, and money spent silences it as before.
            segment += ' ⚠ open'
    if state.get('no_check_reason'):
        # The caller skipped the open route at HIGH without an observable check:
        # show the stored reason, after the open-override marker if any.
        segment += ' ⚠ no check: ' + state['no_check_reason'][:40]
    return segment


def routed_executor(tier, risk, observable_check=False, **router_state):
    """What the router would pick for this classification, or None.

    Cosmetic: any failure here must leave the bar exactly as it was, so the
    caller never learns that the router was unavailable.
    """
    tier = (tier or '').upper()
    risk = (risk or '').upper()
    if tier not in ('T0', 'T1', 'T2', 'T3') or risk not in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL'):
        return None
    try:
        router = _load_router()
        config = router.load_config(ROOT / 'config/open-models.json')
        decision = router.decide(tier, risk, config=config, observable_check=observable_check, **router_state)
        return decision.executor
    except Exception:  # noqa: BLE001 - the bar is cosmetic, the router failing is not
        # The bar is cosmetic: a router that failed to load or to answer must
        # not change what the bar shows.
        return None


def _load_router():
    """Import the router module the same way routed_executor does."""
    spec = importlib.util.spec_from_file_location('aos_router', ROOT / 'bin/aos-router.py')
    router = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(router)
    return router


def open_high_tiers():
    """The router's open_high_tiers, or None when the router cannot load.

    Used by cmd_set to gate the HIGH/observable-check requirement on the same
    policy the router actually applies.
    """
    try:
        router = _load_router()
        config = router.load_config(ROOT / 'config/open-models.json')
        # load_config answers an unreadable policy with the bare defaults, as the
        # router CLI knows: treat that as no policy, so the caller fails closed.
        if config == router.RoutingConfig():
            return None
        return config.open_high_tiers
    except Exception:  # noqa: BLE001 - cosmetic; skip the check when the router is gone
        return None


def write(session, state, ttl):
    if not session:
        return {}
    state = dict(state)
    state['segment'] = render(state)
    state['updated_at'] = int(datetime.now(timezone.utc).timestamp())
    state['expires_at'] = state['updated_at'] + ttl
    directory = status_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = record_path(session)
    fd, temporary = tempfile.mkstemp(dir=str(directory), prefix=session + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(state, ensure_ascii=False))
        os.replace(temporary, str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return state


def cmd_set(args):
    # At HIGH in an open tier the open route is the premium path: skipping it
    # without a reason is the same silent premium as omitting the flag. This
    # check runs BEFORE the override check and BEFORE the session gate, so it
    # applies on Codex (no session id) too. Nothing is written on refusal.
    no_check_reason = (args.no_observable_check or '').strip()
    tiers = open_high_tiers()
    if tiers is None:
        # Fail closed: an unreadable policy must not turn the declaration off.
        tiers = ('T1', 'T2', 'T3')
    high_open_tier = ((args.risk or '').upper() == 'HIGH'
                      and (args.tier or '').upper() in tiers)
    if not high_open_tier:
        # Only a skipped open route at HIGH is worth a marker on the bar.
        no_check_reason = ''
    if high_open_tier and not args.observable_check and not no_check_reason:
        print('aos-status: at %s/HIGH declare --observable-check or '
              '--no-observable-check "<why>"' % (args.tier or '').upper(), file=sys.stderr)
        raise SystemExit(2)
    # Check the router BEFORE the session gate: Codex has no session id, but an
    # override still costs a reason. Nothing is written on refusal.
    # The same inputs the router was given: after an exhausted or unavailable open
    # worker the router itself says premium, and that is not an override.
    routed = routed_executor(args.tier, args.risk, observable_check=args.observable_check,
                             failed_open_attempts=args.failed_open_attempts,
                             open_primary_available=not args.no_open_primary,
                             open_fallback_available=not args.no_open_fallback)
    reason = (args.override_reason or '').strip()
    if (routed == 'open' and args.executor != 'open' and not reason):
        print('aos-status: the router chose open for %s/%s; run the open worker or '
              'pass --override-reason "<why>"' % (args.tier, args.risk), file=sys.stderr)
        raise SystemExit(2)
    session = session_id(args.session)
    if not session:
        return {}
    with locked(session):
        return _set(session, args, routed, reason, no_check_reason)


def _set(session, args, routed, reason=None, no_check_reason=None):
    state = load(session)
    # A new classification replaces the routing but keeps the tokens already spent
    # in this session: the bar shows the session's cost, not the last task's.
    state.update(tier=args.tier, risk=args.risk, executor=args.executor, model=args.model,
                 planner=args.planner, reviewer=args.reviewer,
                 routed_executor=routed)
    if reason and routed == 'open' and args.executor != 'open':
        state['override_reason'] = reason[:120]
    else:
        # An executor that matches the route needs no reason: drop any stale one.
        state.pop('override_reason', None)
    if no_check_reason:
        state['no_check_reason'] = no_check_reason[:120]
    else:
        # A check declared (or not HIGH) needs no reason: drop any stale one.
        state.pop('no_check_reason', None)
    # When this task was routed: the prompt hook reminds a session that keeps
    # working on an old routing to route the next task again.
    state['routed_at'] = int(datetime.now(timezone.utc).timestamp())
    state.setdefault('tokens', {'input': 0, 'output': 0, 'cache': 0})
    state.setdefault('cost_usd', None)
    return write(session, state, args.ttl)


def set_profile(session, domains, task_type, ttl=DEFAULT_TTL_SECONDS):
    """Store the prompt hook's classification; keeps routing and tokens."""
    if not session:
        return {}
    with locked(session):
        state = load_live(session)
        state.update(domains=list(domains or []), task_type=task_type)
        return write(session, state, ttl)


def set_session_model(session, model, ttl=DEFAULT_TTL_SECONDS):
    """Store the model the session is running, keeping every other key.

    The status line calls this on every render, so an unchanged model must not
    rewrite the file. A record whose expiry is past is not revived: the model is
    stored on a fresh record with no routing. Any invalid input writes nothing.
    """
    # A public entry point: validate here, not only in the CLI wrapper.
    if not isinstance(session, str) or not session or session_id(session) != session:
        return {}
    if not isinstance(model, str):
        return {}
    model = model.strip()
    if not MODEL_RE.match(model):
        return {}
    try:
        # The status line calls this in the foreground: wait at most half a
        # second for a busy lock, then skip; the next render tries again.
        with locked(session, wait=MODEL_LOCK_WAIT_SECONDS):
            state = load_live(session)
            if state.get('session_model') == model:
                return {}
            state['session_model'] = model
            return write(session, state, ttl)
    except BlockingIOError:
        return {}


def add_usage(session, model_ref, tokens, ttl):
    if not session:
        return {}
    with locked(session):
        return _add_usage(session, model_ref, tokens, ttl)


def _add_usage(session, model_ref, tokens, ttl):
    state = load_live(session)
    total = state.get('tokens') or {}
    for key in ('input', 'output', 'cache'):
        total[key] = (total.get(key) or 0) + (tokens.get(key) or 0)
    state['tokens'] = total
    cost, multiplier = estimate_cost(model_ref or state.get('model'), tokens)
    if cost is not None:
        state['cost_usd'] = (state.get('cost_usd') or 0) + cost
        state['cost_basis'] = 'estimate from config/open-models.json'
        state['peak_multiplier'] = multiplier
    if model_ref:
        state['model'] = model_ref
    return write(session, state, ttl)


def tokens_from_result(result):
    usage = result.get('usage') or {}
    return {'input': usage.get('input_tokens') or 0, 'output': usage.get('output_tokens') or 0,
            'cache': usage.get('cache_read_tokens') or 0}


def cmd_usage(args):
    session = session_id(args.session)
    if not session:
        return {}
    if args.from_result:
        text = sys.stdin.read() if args.from_result == '-' else Path(args.from_result).read_text()
        try:
            result = json.loads(text)
        except ValueError:
            return {}
        if not isinstance(result, dict):
            return {}
        return add_usage(session, result.get('model_ref') or args.model,
                         tokens_from_result(result), args.ttl)
    tokens = {'input': args.input, 'output': args.output, 'cache': args.cache}
    return add_usage(session, args.model, tokens, args.ttl)


def cmd_show(args):
    session = session_id(args.session)
    state = load(session) if session else {}
    if not state or state.get('expires_at', 0) < datetime.now(timezone.utc).timestamp():
        return ''
    return state.get('segment') or ''


def cmd_clear(args):
    session = session_id(args.session)
    if session:
        # Under the lock, and the lock file stays: deleting it would let a waiting
        # writer and a new one hold locks on two different inodes.
        with locked(session):
            record_path(session).unlink(missing_ok=True)
    return ''


def cmd_model(args):
    # An explicit --session must be valid as given: "" or "../x" never falls
    # back to the environment's session.
    if args.session is not None and session_id(args.session) != args.session:
        return {}
    session = session_id(args.session)
    if not session:
        return {}
    return set_session_model(session, args.model, args.ttl)


def _count(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be >= 0')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', help='defaults to $CLAUDE_CODE_SESSION_ID')
    parser.add_argument('--ttl', type=int, default=DEFAULT_TTL_SECONDS,
                        help='seconds the status line keeps showing this record')
    sub = parser.add_subparsers(dest='command', required=True)

    setter = sub.add_parser('set', help='publish the routing decision')
    setter.add_argument('--tier', required=True)
    setter.add_argument('--risk', required=True)
    setter.add_argument('--executor', required=True, choices=('main', 'open', 'premium'))
    setter.add_argument('--model')
    setter.add_argument('--planner')
    setter.add_argument('--reviewer')
    setter.add_argument('--override-reason', help='why the router\'s open route is being overridden')
    check = setter.add_mutually_exclusive_group()
    check.add_argument('--observable-check', action='store_true',
                       help='pass observable_check to the router')
    check.add_argument('--no-observable-check', metavar='REASON',
                       help='why the open route is skipped: at HIGH in an open tier '
                            'one of the two must be given')
    setter.add_argument('--failed-open-attempts', type=_count, default=0,
                        help='as given to aos-router.py')
    setter.add_argument('--no-open-primary', action='store_true', help='as given to aos-router.py')
    setter.add_argument('--no-open-fallback', action='store_true', help='as given to aos-router.py')
    setter.set_defaults(handler=cmd_set)

    usage = sub.add_parser('usage', help='add the tokens an open worker burned')
    usage.add_argument('--model')
    usage.add_argument('--input', type=_count, default=0)
    usage.add_argument('--output', type=_count, default=0)
    usage.add_argument('--cache', type=_count, default=0)
    usage.add_argument('--from-result', help='aos-open-executor.py JSON, or - for stdin')
    usage.set_defaults(handler=cmd_usage)

    show = sub.add_parser('show', help='print the segment, empty when stale or absent')
    show.set_defaults(handler=cmd_show)

    clear = sub.add_parser('clear', help='drop this session record')
    clear.set_defaults(handler=cmd_clear)

    model = sub.add_parser('model', help='store the model the session is running')
    model.add_argument('--model', required=True)
    model.set_defaults(handler=cmd_model)

    args = parser.parse_args(argv)
    result = args.handler(args)
    if args.command == 'show':
        print(result)
    elif isinstance(result, dict) and result.get('segment'):
        print(result['segment'])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        # The status bar is cosmetic: never fail a delegation because of it.
        sys.exit(0)
