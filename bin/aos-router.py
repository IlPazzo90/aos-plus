"""AOS model router: decide who executes a task, which model, and with which verify.

AOS is the router, not the manual OpenCode main model. This module is a pure
decision function: given the task classification, the available config and the
model availability, it returns the routing decision. It never talks to a provider,
never runs a command and never reads secrets.

Model availability and the main model come from the caller (the host session),
so this module stays hermetic and testable.

Distinctions, documented in references/orchestration.md:
- main session model: the model the host opened with. The OpenCode entry plugin
  can select each native turn's model; Claude/Codex use separate open runs.
  Premium execution uses the configured CLI, not a premium metered API.
- task executor model: chosen by AOS from tier/risk/uncertainty/security impact,
  capability needs, the benchmark winner config and retry/failure history.
- delegated model: a bounded sub-task the main session spins off.

Backward compatibility: a missing "open" config falls back to premium/legacy
behavior (the main session executes). A missing premium reviewer falls back to
no cross-model review (quality-gates fallback applies).
"""

import json
import math
from dataclasses import asdict, dataclass, field, replace


@dataclass(frozen=True)
class RoutingConfig:
    role_pipeline: bool = False
    premium_execution_default: bool = False
    open_primary: str = None
    open_fallback: str = None
    open_mid: str = None
    catalog: dict = field(default_factory=dict)
    premium_reviewer: str = None
    escalation_executor: str = None
    premium_models: dict = field(default_factory=dict)
    open_max_tier: str = "T2"
    open_max_risk: str = "MEDIUM"
    open_t2_high_review: str = "premium"
    open_t2_high_requires_observable_check: bool = True
    retries_before_escalation: int = 2
    t3_premium_planning: bool = True
    t2_medium_deterministic_verify: bool = True
    t2_medium_open_review: bool = True


RESERVED_OPEN = {"T0", "T1"}


def load_config(path=None):
    """Load the versioned routing config (config/open-models.json).

    Absent file, invalid JSON, missing "schema" or non-1 schema all return an
    empty config so the caller falls back to legacy behavior. Same for a file
    that lacks both open models. Unreadable files are treated as absent.
    """
    empty = RoutingConfig()
    if path is None:
        return empty
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or data.get("schema") != 1:
        return empty
    open_ = data.get("open")
    if not isinstance(open_, dict):
        return empty
    primary = open_.get("primary")
    fallback = open_.get("fallback")
    if not isinstance(primary, str) or not primary.strip():
        return empty
    premium = data.get("premium")
    policy = data.get("policy")
    if premium is not None and not isinstance(premium, dict):
        return empty
    if policy is not None and not isinstance(policy, dict):
        return empty
    retries = (policy or {}).get('retries_before_escalation', 2)
    if type(retries) is not int or retries < 1:
        return empty
    catalog = data.get('model_catalog', {})
    if not isinstance(catalog, dict) or any(not isinstance(identity, str) or not isinstance(entry, dict)
                                            for identity, entry in catalog.items()):
        return empty
    model_refs = (premium or {}).get('model_refs', {})
    if not isinstance(model_refs, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                               for key, value in model_refs.items()):
        return empty
    return RoutingConfig(
        role_pipeline=(policy or {}).get("role_pipeline") is True,
        premium_execution_default=False,
        open_primary=primary.strip(),
        open_fallback=fallback.strip() if isinstance(fallback, str) and fallback.strip() else None,
        open_mid=open_.get('mid').strip() if isinstance(open_.get('mid'), str) and open_.get('mid').strip() else None,
        catalog=catalog,
        premium_reviewer=(premium or {}).get("reviewer"),
        escalation_executor=(premium or {}).get("escalation_executor"),
        premium_models=model_refs,
        open_max_tier=(policy or {}).get("open_max_tier", "T2"),
        open_max_risk=(policy or {}).get("open_max_risk", "MEDIUM"),
        open_t2_high_review=(policy or {}).get("open_t2_high_review", "premium"),
        open_t2_high_requires_observable_check=bool((policy or {}).get(
            "open_t2_high_requires_observable_check", True)),
        retries_before_escalation=retries,
        t3_premium_planning=bool((policy or {}).get("t3_premium_planning", True)),
        t2_medium_deterministic_verify=bool((policy or {}).get("t2_medium_deterministic_verify", True)),
        t2_medium_open_review=bool((policy or {}).get("t2_medium_open_review", True)),
    )


@dataclass(frozen=True)
class Decision:
    executor: str                 # "open" | "main" | "premium"
    planner: str = field(default=None)
    reviewer: str = field(default=None)
    fixer: str = field(default=None)
    planner_model: str = field(default=None)
    reviewer_model: str = field(default=None)
    fixer_model: str = field(default=None)
    executor_cost_class: str = field(default=None)
    cross_model_review: bool = field(default=False)
    premium_execution_used: bool = field(default=False)
    premium_execution_reason: str = field(default=None)
    pipeline: bool = field(default=False)
    model: str = None             # model string as routed (may be None for premium)
    provider: str = None          # provider part of the model id, when present
    manual_model_override: bool = False
    routed_by_aos: bool = False
    verify: str = "targeted"      # targeted | deterministic | open_review | premium_review
    retries: int = 0
    escalation_target: str = None  # model/family to escalate to, when escalation applies
    rationale: tuple = field(default_factory=tuple)


def _model_provider(model):
    if not model or "/" not in model:
        return None
    return model.split("/", 1)[0]


_COST_CLASSES = ('CHEAP', 'MID', 'PREMIUM')


def choose_model(config, role, *, capability_requirements=None, runtime=None, budget=None,
                 candidates=None):
    """Return the cheapest catalog model sufficient for a role, or None.

    Catalog entries are data, deliberately: availability, price and capability
    facts change independently of the host runtime. Missing catalog entries do
    not invent a replacement model.
    """
    if role not in ('planner', 'executor', 'reviewer', 'fixer'):
        raise ValueError('unknown routing role')
    requirements = capability_requirements or {}
    if not isinstance(requirements, dict) or any(not isinstance(k, str) or type(v) not in (int, float) or not math.isfinite(v) or v < 0
                                                for k, v in requirements.items()):
        raise ValueError('capability requirements must be nonnegative scores')
    budget = budget or {}
    if not isinstance(budget, dict):
        raise ValueError('budget must be an object')
    ceiling = budget.get('max_cost_class', 'PREMIUM')
    if ceiling not in _COST_CLASSES:
        raise ValueError('invalid budget cost class')
    allowed = set(candidates) if candidates is not None else None
    eligible = []
    for identity, entry in config.catalog.items():
        if allowed is not None and identity not in allowed:
            continue
        if role not in entry.get('roles', ()) or entry.get('cost_class') not in _COST_CLASSES:
            continue
        if runtime is not None and runtime not in entry.get('compatible_runtimes', ()):
            continue
        if _COST_CLASSES.index(entry['cost_class']) > _COST_CLASSES.index(ceiling):
            continue
        scores = entry.get('capability_scores', {})
        if any(not isinstance(scores.get(name, 0), (int, float)) or not math.isfinite(scores.get(name, 0))
               or scores.get(name, 0) < score for name, score in requirements.items()):
            continue
        for cost_key in ('input_cost_per_million', 'output_cost_per_million'):
            value = entry.get(cost_key)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                break
        else:
            for cost_key in ('input_cost_per_million', 'output_cost_per_million'):
                limit = budget.get('max_' + cost_key)
                value = entry.get(cost_key)
                if limit is not None and (type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0
                                          or value is None or value > limit):
                    break
            else:
                # A missing benchmark is not a runtime outage: premium subscriptions
                # are deliberately not compared with metered open models. Only an
                # explicit availability false marks a model unavailable.
                if entry.get('availability') is False or entry.get('historical', {}).get('available') is False:
                    continue
                minimum_success_rate = budget.get('minimum_success_rate')
                if minimum_success_rate is not None:
                    if (type(minimum_success_rate) not in (int, float) or not math.isfinite(minimum_success_rate)
                            or not 0 <= minimum_success_rate <= 1):
                        raise ValueError('minimum_success_rate must be between zero and one')
                    success_rate = entry.get('historical_success_rate')
                    if success_rate is not None and (not isinstance(success_rate, (int, float))
                                                     or not math.isfinite(success_rate)
                                                     or success_rate < minimum_success_rate):
                        continue
                input_cost = entry.get('input_cost_per_million')
                output_cost = entry.get('output_cost_per_million')
                if input_cost is None:
                    input_cost = float('inf')
                elif not isinstance(input_cost, (int, float)) or not math.isfinite(input_cost):
                    continue
                if output_cost is None:
                    output_cost = float('inf')
                elif not isinstance(output_cost, (int, float)) or not math.isfinite(output_cost):
                    continue
                eligible.append((
                    _COST_CLASSES.index(entry['cost_class']),
                    input_cost,
                    output_cost, identity))
    return min(eligible)[-1] if eligible else None


def _eligible_open_model(config, model, *, capability_requirements=None, runtime=None, budget=None):
    if not model:
        return False
    if not config.catalog:
        return True
    return choose_model(config, 'executor', capability_requirements=capability_requirements,
                        runtime=runtime, budget=budget, candidates=(model,)) == model


def _model_cost_class(config, model):
    return config.catalog.get(model, {}).get('cost_class') if model else None


_FAMILY_PROVIDER = {'claude': 'anthropic', 'codex': 'openai'}
_FAMILY_RUNTIME = {'claude': 'claude-code', 'codex': 'codex-cli'}


def _role_requirements(role, requirements, tier, risk, uncertainty):
    result = dict(requirements or {})
    elevated = tier == 'T3' or risk == 'HIGH' or str(uncertainty).upper() in ('HIGH', 'UNSETTLED')
    if elevated:
        for name in ('planning', 'reasoning', 'review'):
            result[name] = max(result.get(name, 0), 5)
    elif tier == 'T2':
        if role == 'planner':
            for name in ('planning', 'reasoning'):
                result[name] = max(result.get(name, 0), 3)
        elif role == 'reviewer':
            result['review'] = max(result.get('review', 0), 3)
    return result


def _role_budget(budget, tier, risk, uncertainty):
    result = dict(budget or {})
    ceiling = 'PREMIUM' if tier == 'T3' or risk == 'HIGH' or str(uncertainty).upper() in ('HIGH', 'UNSETTLED') else 'MID'
    requested = result.get('max_cost_class')
    if requested is None:
        result['max_cost_class'] = ceiling
    elif requested in _COST_CLASSES:
        result['max_cost_class'] = _COST_CLASSES[min(_COST_CLASSES.index(requested), _COST_CLASSES.index(ceiling))]
    return result


def _select_model_for_role(config, role, family, families, capability_requirements, budget, tier, risk, uncertainty,
                           premium_only=False):
    if not family or family not in families:
        return None
    provider = _FAMILY_PROVIDER.get(family, family)
    candidates = tuple(identity for identity, entry in config.catalog.items()
                       if (_model_provider(identity) == provider and role in entry.get('roles', ())
                           and (not premium_only or entry.get('cost_class') == 'PREMIUM')))
    return choose_model(config, role, capability_requirements=_role_requirements(
        role, capability_requirements, tier, risk, uncertainty), runtime=_FAMILY_RUNTIME.get(family),
        budget=_role_budget(budget, tier, risk, uncertainty), candidates=candidates)


def _model_sort_key(config, model):
    entry = config.catalog[model]
    price = lambda name: entry.get(name) if isinstance(entry.get(name), (int, float)) else float('inf')
    return (_COST_CLASSES.index(entry['cost_class']), price('input_cost_per_million'),
            price('output_cost_per_million'), model)


def _requirements(capability_requirements, uncertainty, security_impact):
    result = dict(capability_requirements or {})
    if uncertainty and str(uncertainty).upper() in ('HIGH', 'UNSETTLED'):
        result['reasoning'] = max(result.get('reasoning', 0), 4)
    if security_impact and str(security_impact).upper() in ('HIGH', 'CRITICAL'):
        result['security'] = max(result.get('security', 0), 4)
    return result


def _decide(tier, risk, *, config=None, manual_override=False,
           open_primary_available=True, open_fallback_available=True,
           premium_reviewer_available=True, observable_check=False,
           failed_open_attempts=0, complexity=None, uncertainty=None,
           security_impact=None, capabilities=None, capability_requirements=None,
           budget=None, executor_runtime=None, mid_available=True):
    """Pure decision. `tier` in T0/T1/T2/T3, `risk` in LOW/MEDIUM/HIGH/CRITICAL.

    Args mirror what the orchestrating session observes. Extra classification
    fields carry the caller's assessment. Runtime-specific capabilities require
    a premium executor; tier and risk remain the policy's complexity axes.
    """
    if config is None:
        config = RoutingConfig()
    tier = (tier or "").upper()
    risk = (risk or "").upper()

    tiers = ('T0', 'T1', 'T2', 'T3')
    risks = ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
    if tier not in tiers or risk not in risks:
        raise ValueError('invalid tier or risk')
    capability_requirements = _requirements(capability_requirements, uncertainty, security_impact)
    if risk == 'CRITICAL':
        return Decision(executor='main', verify='needs_approval',
                        rationale=('CRITICAL risk requires human approval',))

    # 1. Explicit override wins over everything.
    if manual_override:
        return Decision(executor="main", manual_model_override=True,
                        verify="reported", rationale=("manual override",))

    if set(capabilities or ()) & {'codex_app', 'codex_runtime', 'claude_runtime'}:
        return Decision(executor='premium', verify=_high_review(config, premium_reviewer_available),
                        escalation_target=config.escalation_executor,
                        rationale=('required premium runtime capability',))

    # 3. e.g. T3: premium planning/final review, open only on bounded subtasks
    #    (which the orchestrator routes separately). Executor for the plan itself
    #    is premium/main.
    if tier == "T3" and not config.role_pipeline:
        return Decision(executor="premium",
                        verify="premium_review" if config.premium_reviewer and premium_reviewer_available else "deterministic",
                        escalation_target=config.premium_reviewer,
                        rationale=("t3 premium planning", "open is for bounded subtasks only"))

    # 4. Risk above the open policy ceiling: premium/main, never open.
    if risk in ("HIGH", "CRITICAL"):
        open_allowed = tier == "T2" and config.open_t2_high_requires_observable_check and observable_check
        if not open_allowed:
            review = _high_review(config, premium_reviewer_available)
            return Decision(executor="premium", verify=review,
                            escalation_target=config.premium_reviewer,
                            rationale=("risk above open ceiling", "mandatory premium review"))
        return _open_decision(config, open_primary_available, open_fallback_available,
                              failed_open_attempts, verify="premium_review",
                              capability_requirements=capability_requirements, budget=budget,
                              runtime=executor_runtime, mid_available=mid_available,
                              rationale=("open allowed by policy", "observable check present",
                                         "premium review mandatory"))

    # 5. General open eligibility: tier and risk within policy, and an open model
    #    must actually exist. No config.models means legacy behavior below.
    if (config.open_primary and config.open_max_tier in tiers and config.open_max_risk in risks
            and tiers.index(tier) <= tiers.index(config.open_max_tier)
            and risks.index(risk) <= risks.index(config.open_max_risk)):
        if tier in ("T2", "T3"):
            verify = "deterministic" if config.t2_medium_deterministic_verify else "targeted"
            if config.t2_medium_open_review and config.premium_reviewer:
                verify = "open_mixed"  # deterministic + open review
            return _open_decision(config, open_primary_available, open_fallback_available,
                                  failed_open_attempts, verify=verify,
                                  capability_requirements=capability_requirements, budget=budget,
                                  runtime=executor_runtime, mid_available=mid_available,
                                  rationale=(tier, risk, "open executor"))
        # T0/T1
        return _open_decision(config, open_primary_available, open_fallback_available,
                              failed_open_attempts, verify="targeted",
                              capability_requirements=capability_requirements, budget=budget,
                              runtime=executor_runtime, mid_available=mid_available,
                              rationale=(tier, risk, "open executor"))

    # 6. Legacy fallback (backward compatibility): the task stays on the main
    #    session when no open executor is configured.
    return Decision(executor="main", verify="deterministic",
                    rationale=("not eligible for open", tier, risk))


def decide(tier, risk, *, config=None, planner=None, premium_families=None, **kwargs):
    """Assign roles separately from the existing permission/risk decision."""
    tier, risk = (tier or '').upper(), (risk or '').upper()
    config = config or RoutingConfig()
    decision = _decide(tier, risk, config=config, **kwargs)
    premium_used = decision.executor == 'premium'
    decision = replace(decision, premium_execution_used=premium_used,
                       premium_execution_reason='; '.join(decision.rationale) if premium_used else None,
                       executor_cost_class=_model_cost_class(config, decision.model))
    if not config.role_pipeline or kwargs.get('manual_override') or risk == 'CRITICAL':
        return decision
    if not config.catalog:
        return _legacy_role_decision(decision, config, tier, risk, planner, premium_families, kwargs)
    families = ('codex', 'claude') if premium_families is None else tuple(premium_families)
    if kwargs.get('premium_reviewer_available') is False:
        families = ()
    capability_requirements = _requirements(kwargs.get('capability_requirements'), kwargs.get('uncertainty'), kwargs.get('security_impact'))
    budget = kwargs.get('budget')
    needs_plan = tier in ('T2', 'T3')
    needs_review = needs_plan or risk == 'HIGH'
    planner_model = None
    planner_family = None
    if needs_plan:
        candidate_families = (planner,) if planner is not None else families
        candidates = []
        for family in candidate_families:
            model = _select_model_for_role(config, 'planner', family, families, capability_requirements,
                                           budget, tier, risk, kwargs.get('uncertainty'))
            if model:
                candidates.append((model, family))
        if candidates:
            planner_model, planner_family = min(candidates, key=lambda item: _model_sort_key(config, item[0]))

    reviewer_family = None
    reviewer_model = None
    if needs_review and planner_family:
        reviewer_family = 'claude' if planner_family == 'codex' else 'codex'
        reviewer_model = _select_model_for_role(
            config, 'reviewer', reviewer_family, families, capability_requirements, budget, tier, risk,
            kwargs.get('uncertainty'), premium_only=risk == 'HIGH')
        if reviewer_model is None:
            reviewer_family = None
    elif needs_review:
        candidates = []
        for family in families:
            model = _select_model_for_role(
                config, 'reviewer', family, families, capability_requirements, budget, tier, risk,
                kwargs.get('uncertainty'), premium_only=risk == 'HIGH')
            if model:
                candidates.append((model, family))
        if candidates:
            reviewer_model, reviewer_family = min(candidates, key=lambda item: _model_sort_key(config, item[0]))
    return replace(decision, planner=planner_family if needs_plan else None,
                   reviewer=reviewer_family if needs_review else None,
                   fixer='open' if decision.executor == 'open' else decision.executor,
                   planner_model=planner_model,
                   reviewer_model=reviewer_model,
                   fixer_model=decision.model if decision.executor == 'open' else None,
                   cross_model_review=needs_plan and planner_family is not None and reviewer_family is not None,
                   verify='premium_review' if needs_review else 'deterministic',
                   pipeline=decision.executor == 'open' and needs_plan)


def _legacy_role_decision(decision, config, tier, risk, planner, premium_families, kwargs):
    if not config.role_pipeline or kwargs.get('manual_override') or risk == 'CRITICAL':
        return decision
    needs_plan = tier in ('T2', 'T3')
    needs_review = needs_plan or risk == 'HIGH'
    families = ('codex', 'claude') if premium_families is None else tuple(premium_families)
    if kwargs.get('premium_reviewer_available') is False:
        families = ()
    planner_family = planner if planner in families else None
    if planner_family is None and planner is None:
        planner_family = config.escalation_executor
        if planner_family not in families:
            planner_family = next((f for f in families if f in ('codex', 'claude')), None)
    reviewer_family = None
    if reviewer_family is None and planner_family:
        reviewer_family = 'claude' if planner_family == 'codex' else 'codex'
        if reviewer_family not in families:
            reviewer_family = None
    return replace(decision, planner=planner_family if needs_plan else None,
                   reviewer=reviewer_family if needs_review else None,
                   fixer=decision.executor,
                   planner_model=None,
                   reviewer_model=None,
                   cross_model_review=False,
                   pipeline=False)


def _high_review(config, premium_reviewer_available):
    """Map the policy label ("premium") onto the verify vocabulary.

    open_t2_high_review is "premium" by policy; the verify field must say
    exactly how to review ("premium_review" when a reviewer exists, otherwise
    the deterministic gate, never a fake premium).
    """
    if config.premium_reviewer and premium_reviewer_available:
        return "premium_review"
    return "deterministic"


def _open_decision(config, primary_available, fallback_available, failed_attempts, *,
                   verify, rationale, capability_requirements=None, budget=None, runtime=None,
                   mid_available=True):
    # Semantics: the primary gets `retries` attempts; beyond that the fallback
    # gets `retries` more; when both are used up (or unavailable) it escalates
    # to premium — never a bogus open run with no model.
    retries = config.retries_before_escalation
    primary = config.open_primary
    fallback = config.open_fallback if config.open_fallback else None
    primary = config.open_primary if primary_available and _eligible_open_model(
        config, config.open_primary, capability_requirements=capability_requirements, runtime=runtime, budget=budget) else None
    usable_fallback = fallback if fallback_available and _eligible_open_model(
        config, fallback, capability_requirements=capability_requirements, runtime=runtime, budget=budget) else None
    mid = config.open_mid if mid_available and _eligible_open_model(
        config, config.open_mid, capability_requirements=capability_requirements, runtime=runtime, budget=budget) else None
    if failed_attempts >= retries * 2:
        if mid and failed_attempts == retries * 2:
            return Decision(executor='open', model=mid, provider=_model_provider(mid), routed_by_aos=True,
                            verify=verify, retries=retries, rationale=(*rationale, 'mid open'))
        return Decision(executor="premium", verify=verify,
                        escalation_target=config.escalation_executor,
                        rationale=(*rationale, "open unavailable or exhausted"))
    if not primary and not usable_fallback and not mid:
        return Decision(executor="premium", verify=verify,
                        escalation_target=config.escalation_executor,
                        rationale=(*rationale, "open unavailable or exhausted"))
    if failed_attempts >= retries or not primary:
        if usable_fallback:
            return Decision(executor="open", model=fallback,
                            provider=_model_provider(fallback),
                            routed_by_aos=True, verify=verify, retries=retries,
                            rationale=(*rationale, "fallback open"))
        return Decision(executor="premium", verify=verify,
                        escalation_target=config.escalation_executor,
                        rationale=(*rationale, "open unavailable or exhausted"))
    return Decision(executor="open", model=primary,
                    provider=_model_provider(primary),
                    routed_by_aos=True, verify=verify, retries=retries,
                    rationale=rationale)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, prog="aos-router")
    parser.add_argument("--tier", required=True, choices=["T0", "T1", "T2", "T3"])
    parser.add_argument("--risk", required=True, choices=["LOW", "MEDIUM", "HIGH", "CRITICAL"])
    parser.add_argument("--config", default=None, help="path to config/open-models.json")
    parser.add_argument("--manual-override", action="store_true")
    parser.add_argument("--no-open-primary", action="store_true")
    parser.add_argument("--no-open-fallback", action="store_true")
    parser.add_argument("--observable-check", action="store_true")
    parser.add_argument("--failed-open-attempts", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    decision = decide(
        args.tier, args.risk,
        config=load_config(args.config),
        manual_override=args.manual_override,
        open_primary_available=not args.no_open_primary,
        open_fallback_available=not args.no_open_fallback,
        observable_check=args.observable_check,
        failed_open_attempts=args.failed_open_attempts,
    )
    if args.json:
        print(json.dumps(asdict(decision), indent=2))
    else:
        print(f"executor={decision.executor} model={decision.model} verify={decision.verify} "
              f"escalation={decision.escalation_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
