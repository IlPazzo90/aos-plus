"""AOS model router: decide who executes a task, which model, and with which verify.

AOS is the router, not the manual main model of the host. This module is a pure
decision function: given the task classification, the available config and the
model availability, it returns the routing decision. It never talks to a provider,
never runs a command and never reads secrets.

Model availability and the main model come from the caller (the host session),
so this module stays hermetic and testable.

Distinctions, documented in references/orchestration.md:
- main session model: the model the host opened with. Claude/Codex run the
  open model in a separate bounded worker, never in the main session.
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
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

# The versioned policy that ships with the skill. The CLI defaults to it because
# "no config" answers "main" for every tier and risk, which is the most plausible
# wrong answer there is: it reads like a routing decision instead of a missing file.
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/open-models.json"


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
    open_high_tiers: tuple = ("T2",)
    open_t2_high_review: str = "premium"
    open_t2_high_requires_observable_check: bool = True
    retries_before_escalation: int = 2
    t3_premium_planning: bool = True
    t2_medium_deterministic_verify: bool = True
    t2_medium_open_review: bool = True
    host_subagents: dict = field(default_factory=dict)


RESERVED_OPEN = {"T0", "T1"}


def _open_high_tiers(value):
    """Parse policy "open_high_tiers": a list of tier strings among T0..T3.

    Absent, malformed, or any non-string / out-of-range entry falls back to the
    default ("T2",), the behavior before the key existed. T0 is accepted but
    ignored by the caller (T0 is decided before the HIGH step).
    """
    if not isinstance(value, (list, tuple)) or not value:
        return ("T2",)
    if not all(isinstance(item, str) and item in ("T0", "T1", "T2", "T3") for item in value):
        return ("T2",)
    return tuple(value)


def _valid_entry(entry):
    # Shapes choose_model relies on: a string `roles` would match by substring.
    return (isinstance(entry, dict)
            and isinstance(entry.get('roles', ()), (list, tuple))
            and isinstance(entry.get('compatible_runtimes', ()), (list, tuple))
            and isinstance(entry.get('capability_scores', {}), dict)
            and isinstance(entry.get('historical', {}), dict))


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
    if not isinstance(catalog, dict) or any(not isinstance(identity, str) or not _valid_entry(entry)
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
        open_high_tiers=_open_high_tiers((policy or {}).get("open_high_tiers")),
        open_t2_high_review=(policy or {}).get("open_t2_high_review", "premium"),
        open_t2_high_requires_observable_check=bool((policy or {}).get(
            "open_t2_high_requires_observable_check", True)),
        retries_before_escalation=retries,
        t3_premium_planning=bool((policy or {}).get("t3_premium_planning", True)),
        t2_medium_deterministic_verify=bool((policy or {}).get("t2_medium_deterministic_verify", True)),
        t2_medium_open_review=bool((policy or {}).get("t2_medium_open_review", True)),
        host_subagents=data.get('host_subagents') if isinstance(data.get('host_subagents'), dict) else {},
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
    host_model_class: str = None  # T0 on the host: "cheap" | "mid" subagent, None = main model
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

    # 2b. T0 stays on the host session: a worker's brief, sandbox and startup cost
    #     more than the edit. LOW/MEDIUM go to the host's cheaper subagent class
    #     (config host_subagents.by_risk); HIGH keeps the main session model.
    if tier == 'T0':
        by_risk = config.host_subagents.get('by_risk') if isinstance(config.host_subagents.get('by_risk'), dict) else {}
        klass = by_risk.get(risk) if by_risk.get(risk) in ('cheap', 'mid') else None
        return Decision(executor='main', host_model_class=klass, routed_by_aos=True,
                        verify='targeted' if risk in ('LOW', 'MEDIUM') else 'deterministic',
                        rationale=('T0 on the host session',
                                   'host subagent: ' + klass if klass else 'main session model'))

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
        open_allowed = (tier in config.open_high_tiers
                        and config.open_t2_high_requires_observable_check
                        and observable_check and risk == "HIGH")
        if not open_allowed:
            review = _high_review(config, premium_reviewer_available)
            return Decision(executor="premium", verify=review,
                            escalation_target=config.premium_reviewer,
                            rationale=("risk above open ceiling", "mandatory premium review"))
        if tier == "T1":
            return _open_decision(config, open_primary_available, open_fallback_available,
                                  failed_open_attempts, verify="deterministic",
                                  capability_requirements=capability_requirements, budget=budget,
                                  runtime=executor_runtime, mid_available=mid_available,
                                  rationale=("open allowed by policy", "observable check present",
                                             "HIGH checks inline by the main session"))
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


def decide(tier, risk, *, config=None, planner=None, premium_families=None, host=None, **kwargs):
    """Assign roles separately from the existing permission/risk decision.

    `host` ("claude" | "codex") resolves a T0 host subagent class to the model
    name configured for that host; without it the class alone is returned.
    """
    tier, risk = (tier or '').upper(), (risk or '').upper()
    config = config or RoutingConfig()
    decision = _decide(tier, risk, config=config, **kwargs)
    if decision.host_model_class and host:
        names = config.host_subagents.get(host)
        name = names.get(decision.host_model_class) if isinstance(names, dict) else None
        if isinstance(name, str) and name.strip():
            decision = replace(decision, model=name.strip(),
                               provider={'claude': 'anthropic', 'codex': 'openai'}.get(host))
    premium_used = decision.executor == 'premium'
    decision = replace(decision, premium_execution_used=premium_used,
                       premium_execution_reason='; '.join(decision.rationale) if premium_used else None,
                       executor_cost_class=_model_cost_class(config, decision.model))
    if kwargs.get('manual_override') and tier in ('T2', 'T3') and risk != 'CRITICAL':
        # Choosing the executor by hand does not waive the review every T2/T3
        # owes: the reviewer is the family opposite to the host doing the work.
        # Same availability and budget rules as the pipeline's reviewer; no model,
        # no promised review.
        families = ('codex', 'claude') if premium_families is None else tuple(premium_families)
        if kwargs.get('premium_reviewer_available') is False:
            families = ()
        reviewer = {'claude': 'codex', 'codex': 'claude'}.get(host)
        reviewer_model = _select_model_for_role(
            config, 'reviewer', reviewer, families, {}, kwargs.get('budget'), tier, risk,
            kwargs.get('uncertainty'), premium_only=risk == 'HIGH') if reviewer and config.catalog else None
        if reviewer_model is None:
            reviewer = None
        return replace(decision, reviewer=reviewer, reviewer_model=reviewer_model,
                       cross_model_review=reviewer is not None,
                       verify='premium_review' if reviewer else 'reported')
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
    # The external cross-model review belongs to T2/T3 at every risk (HIGH
    # included); T0/T1 HIGH get the extended HIGH checks inline instead.
    needs_review = needs_plan
    planner_model = None
    planner_family = None
    if needs_plan:
        candidate_families = (planner,) if planner is not None else families
        candidates = []
        for family in candidate_families:
            # At HIGH the plan is premium, like the review: it is what makes open
            # execution of HIGH work acceptable at all.
            model = _select_model_for_role(config, 'planner', family, families, capability_requirements,
                                           budget, tier, risk, kwargs.get('uncertainty'),
                                           premium_only=risk == 'HIGH')
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
    if (risk == 'HIGH' and decision.executor == 'open' and needs_plan
            and not (planner_model and reviewer_model)):
        # HIGH work reaches the open worker only under a premium plan and a premium
        # review of the opposite family; without either, the premium executes.
        return decide(tier, risk, config=config, planner=planner, premium_families=premium_families,
                      host=host, **dict(kwargs, observable_check=False))
    return replace(decision, planner=planner_family if needs_plan else None,
                   reviewer=reviewer_family if needs_review else None,
                   fixer='open' if decision.executor == 'open' else decision.executor,
                   planner_model=planner_model,
                   reviewer_model=reviewer_model,
                   fixer_model=decision.model if decision.executor == 'open' else None,
                   cross_model_review=needs_plan and planner_family is not None and reviewer_family is not None,
                   # Never promise a premium review nobody can perform.
                   verify='premium_review' if needs_review and reviewer_family else 'deterministic',
                   pipeline=decision.executor == 'open' and needs_plan)


def _legacy_role_decision(decision, config, tier, risk, planner, premium_families, kwargs):
    if not config.role_pipeline or kwargs.get('manual_override') or risk == 'CRITICAL':
        return decision
    needs_plan = tier in ('T2', 'T3')
    needs_review = needs_plan
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
    if not primary and not usable_fallback and not mid:
        if config.catalog and not any(m in config.catalog for m in (config.open_primary, config.open_fallback) if m):
            # A catalog that names none of the configured open models is a broken
            # policy: block instead of running premium with zero open attempts.
            raise ValueError('configured open models are missing from the model catalog')
    # Each failed-attempt count owns a fixed slot: primary x retries, configured
    # fallback x retries, one MID — the same budget as aos-pipeline.fail. An
    # unavailable slot hands its turn to the next available rung, never to an
    # earlier one. The router is stateless and cannot know whether a MID already
    # stood in for an unavailable slot: the caller that ran it passes
    # mid_available=False (CLI --no-open-mid); the pipeline tracks it itself.
    slots = [(primary, rationale)] * retries \
        + ([(usable_fallback, (*rationale, "fallback open"))] * retries if fallback else []) \
        + [(mid, (*rationale, "mid open"))]
    for model, why in slots[failed_attempts:]:
        if model:
            return Decision(executor="open", model=model, provider=_model_provider(model),
                            routed_by_aos=True, verify=verify, retries=retries, rationale=why)
    return Decision(executor="premium", verify=verify,
                    escalation_target=config.escalation_executor,
                    rationale=(*rationale, "open unavailable or exhausted"))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, prog="aos-router")
    parser.add_argument("--tier", required=True, choices=["T0", "T1", "T2", "T3"])
    parser.add_argument("--risk", required=True, choices=["LOW", "MEDIUM", "HIGH", "CRITICAL"])
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="path to config/open-models.json (default: the skill's own policy)")
    parser.add_argument("--manual-override", action="store_true")
    parser.add_argument("--no-open-primary", action="store_true")
    parser.add_argument("--no-open-fallback", action="store_true")
    parser.add_argument("--no-open-mid", action="store_true",
                        help="the MID model already ran (or is down): do not offer it again")
    parser.add_argument("--observable-check", action="store_true")
    parser.add_argument("--failed-open-attempts", type=int, default=0)
    parser.add_argument("--host", choices=["claude", "codex"],
                        help="resolve a T0 host subagent class to this host's model name")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if config == RoutingConfig():
        # An unusable policy routes everything to main: the most plausible wrong
        # answer. Say so instead of printing it.
        print(f"ERRORE: politica di routing non utilizzabile: {args.config}", file=sys.stderr)
        return 2
    decision = decide(
        args.tier, args.risk,
        config=config,
        manual_override=args.manual_override,
        open_primary_available=not args.no_open_primary,
        open_fallback_available=not args.no_open_fallback,
        mid_available=not args.no_open_mid,
        observable_check=args.observable_check,
        failed_open_attempts=args.failed_open_attempts,
        host=args.host,
    )
    if args.json:
        print(json.dumps(asdict(decision), indent=2))
    else:
        print(f"executor={decision.executor} model={decision.model} verify={decision.verify} "
              f"escalation={decision.escalation_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
