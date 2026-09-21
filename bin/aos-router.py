"""AOS model router: decide who executes a task, which model, and with which verify.

AOS is the router, not the manual OpenCode main model. This module is a pure
decision function: given the task classification, the available config and the
model availability, it returns the routing decision. It never talks to a provider,
never runs a command and never reads secrets.

Model availability and the main model come from the caller (the host session),
so this module stays hermetic and testable.

Distinctions, documented in references/orchestration.md:
- main session model: the model the OpenCode interface opened the session with.
  It cannot switch at runtime (verified: opencode run has -m/--model, the
  interactive session has no model switch). It remains the orchestrator, the
  verifier and the arbiter, and it executes only on explicit override or in the
  cases below.
- task executor model: chosen by AOS from tier/risk/uncertainty/security impact,
  capability needs, the benchmark winner config and retry/failure history.
- delegated model: a bounded sub-task the main session spins off.

Backward compatibility: a missing "open" config falls back to premium/legacy
behavior (the main session executes). A missing premium reviewer falls back to
no cross-model review (quality-gates fallback applies).
"""

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoutingConfig:
    open_primary: str = None
    open_fallback: str = None
    premium_reviewer: str = None
    escalation_executor: str = None
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
    telemetry = data.get("telemetry")
    return RoutingConfig(
        open_primary=primary.strip(),
        open_fallback=fallback.strip() if isinstance(fallback, str) and fallback.strip() else None,
        premium_reviewer=(premium or {}).get("reviewer"),
        escalation_executor=(premium or {}).get("escalation_executor"),
        open_max_tier=(policy or {}).get("open_max_tier", "T2"),
        open_max_risk=(policy or {}).get("open_max_risk", "MEDIUM"),
        open_t2_high_review=(policy or {}).get("open_t2_high_review", "premium"),
        open_t2_high_requires_observable_check=bool((policy or {}).get(
            "open_t2_high_requires_observable_check", True)),
        retries_before_escalation=int((policy or {}).get("retries_before_escalation", 2)),
        t3_premium_planning=bool((policy or {}).get("t3_premium_planning", True)),
        t2_medium_deterministic_verify=bool((policy or {}).get("t2_medium_deterministic_verify", True)),
        t2_medium_open_review=bool((policy or {}).get("t2_medium_open_review", True)),
    )


@dataclass(frozen=True)
class Decision:
    executor: str                 # "open" | "main" | "premium"
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


def decide(tier, risk, *, config=None, manual_override=False,
           open_primary_available=True, open_fallback_available=True,
           premium_reviewer_available=True, observable_check=False,
           failed_open_attempts=0, complexity=None, uncertainty=None,
           security_impact=None, capabilities=None):
    """Pure decision. `tier` in T0/T1/T2/T3, `risk` in LOW/MEDIUM/HIGH/CRITICAL.

    Args mirror what the orchestrating session observes. Extra classification
    fields (complexity, uncertainty, security_impact, capabilities) are accepted
    for callers that carry them and feed the rationale, not the executor choice:
    the executor axes are tier and risk, as policy declares.
    """
    if config is None:
        config = RoutingConfig()
    tier = (tier or "").upper()
    risk = (risk or "").upper()

    # 1. Explicit override wins over everything.
    if manual_override:
        return Decision(executor="main", manual_model_override=True,
                        verify="reported", rationale=("manual override",))

    # 2. CRITICAL never routes away from the main session without approval.
    if risk == "CRITICAL":
        return Decision(executor="main", verify="needs_approval",
                        rationale=("CRITICAL risk stays on main",))

    # 3. e.g. T3: premium planning/final review, open only on bounded subtasks
    #    (which the orchestrator routes separately). Executor for the plan itself
    #    is premium/main.
    if tier == "T3":
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
                              rationale=("open allowed by policy", "observable check present",
                                         "premium review mandatory"))

    # 5. General open eligibility: tier and risk within policy, and an open model
    #    must actually exist. No config.models means legacy behavior below.
    if config.open_primary and (tier in RESERVED_OPEN or (tier == config.open_max_tier and risk <= config.open_max_risk)):
        if tier == "T2":
            verify = "deterministic" if config.t2_medium_deterministic_verify else "targeted"
            if config.t2_medium_open_review and config.premium_reviewer:
                verify = "open_mixed"  # deterministic + open review
            return _open_decision(config, open_primary_available, open_fallback_available,
                                  failed_open_attempts, verify=verify,
                                  rationale=(tier, risk, "open executor"))
        # T0/T1
        return _open_decision(config, open_primary_available, open_fallback_available,
                              failed_open_attempts, verify="targeted",
                              rationale=(tier, risk, "open executor"))

    # 6. Legacy fallback (backward compatibility): the task stays on the main
    #    session when no open executor is configured.
    return Decision(executor="main", verify="deterministic",
                    rationale=("not eligible for open", tier, risk))


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
                   verify, rationale):
    # Semantics: the primary gets `retries` attempts; beyond that the fallback
    # gets `retries` more; when both are used up (or unavailable) it escalates
    # to premium — never a bogus open run with no model.
    retries = config.retries_before_escalation
    primary = config.open_primary
    fallback = config.open_fallback if config.open_fallback else None
    usable_fallback = fallback if fallback_available else None
    if failed_attempts >= retries * 2 or (not primary_available and not usable_fallback):
        return Decision(executor="premium", verify=verify,
                        escalation_target=config.escalation_executor,
                        rationale=(*rationale, "open unavailable or exhausted"))
    if failed_attempts >= retries or not primary_available:
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
        print(json.dumps({"executor": decision.executor, "model": decision.model,
                          "provider": decision.provider,
                          "manual_model_override": decision.manual_model_override,
                          "routed_by_aos": decision.routed_by_aos,
                          "verify": decision.verify, "retries": decision.retries,
                          "escalation_target": decision.escalation_target,
                          "rationale": list(decision.rationale)}, indent=2))
    else:
        print(f"executor={decision.executor} model={decision.model} verify={decision.verify} "
              f"escalation={decision.escalation_target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())