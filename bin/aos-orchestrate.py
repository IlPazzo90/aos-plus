#!/usr/bin/env python3
"""AOS adaptive orchestrator: capability-driven, performance-aware routing.

This is a pure decision module: it classifies a task into domains and a task type,
builds a TaskProfile, decomposes multi-domain work into ordered bundles, and picks
the cheapest model whose capability, risk ceiling, tools, context budget and
invocation constraints make it eligible, while a transparent score exposes the
trade-off between capability match, reliability and expected cost.

No model names, tiers or risk thresholds are hardcoded: everything tunable lives in
config/adaptive.json (capabilities, class ladder, cost units, floors, exploration)
and config/open-models.json (the catalog: class, family, invocation, tools,
declared capabilities, context policy). The module never talks to a provider, never
runs a command and never reads secrets.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

DEFAULT_MODELS_PATH = Path(__file__).resolve().parents[1] / "config" / "open-models.json"
DEFAULT_ADAPTIVE_PATH = Path(__file__).resolve().parents[1] / "config" / "adaptive.json"

RISKS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
TIERS = ("T0", "T1", "T2", "T3")
CLASSES = ("LOW", "OPEN", "MID", "HIGH", "PREMIUM")

# The ten 0..1 profile scores, in a fixed order for stable output/tests.
SCORE_FIELDS = (
    "complexity", "risk_score", "uncertainty", "volume", "architecture_impact",
    "security_impact", "domain_specialization", "tool_requirement",
    "context_requirement", "reversibility",
)

# Host CLI -> model family. A host subagent only runs on its own runtime; a premium
# host_session of the *other* family still runs through that family's CLI.
FAMILY_OF_HOST = {"claude": "anthropic", "codex": "openai"}
OPPOSITE = {"anthropic": "openai", "openai": "anthropic"}

_CONTEXT_MODULE = None


def _context_module():
    """Load bin/aos-context.py once, so we reuse (not re-implement) its policy."""
    global _CONTEXT_MODULE
    if _CONTEXT_MODULE is None:
        path = Path(__file__).resolve().parent / "aos-context.py"
        spec = importlib.util.spec_from_file_location("aos_context_for_orchestrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTEXT_MODULE = module
    return _CONTEXT_MODULE


def _strip_accents(text):
    # NFKD splits every accented letter into its base glyph + a combining mark;
    # dropping the combining marks is a faithful, stdlib-only accent fold that
    # keeps "funzionalità" and the accent-free keyword "funzionalita" comparable.
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c))


def _normalize(text):
    return _strip_accents((text or "").lower())


def _match_phrase(text, phrase):
    if not phrase:
        return False
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text) is not None


def _hits(text, keywords):
    return sum(1 for kw in keywords if _match_phrase(text, kw))


def _need_vector(domains, task_type, adaptive):
    """Element-wise max of the selected domains' vectors and the task type's."""
    vector = {}
    domains_cfg = adaptive.get("domains", {}) if isinstance(adaptive, dict) else {}
    for domain in domains:
        block = domains_cfg.get(domain) if isinstance(domains_cfg.get(domain), dict) else {}
        caps = block.get("capabilities") if isinstance(block.get("capabilities"), dict) else {}
        for cap, value in caps.items():
            vector[cap] = max(vector.get(cap, 0.0), value)
    task_types = adaptive.get("task_types", {}) if isinstance(adaptive, dict) else {}
    block = task_types.get(task_type) if isinstance(task_types.get(task_type), dict) else {}
    caps = block.get("capabilities") if isinstance(block.get("capabilities"), dict) else {}
    for cap, value in caps.items():
        vector[cap] = max(vector.get(cap, 0.0), value)
    return vector


def load_registry(models_path=None, adaptive_path=None):
    """Load the model catalog and the adaptive policy.

    Returns {"models": {...catalog...}, "adaptive": {...}, "models_path": str,
    "models_config": {...full open-models dict...}}. A missing or invalid file
    raises ValueError with a clear message.
    """
    mp = Path(models_path) if models_path else DEFAULT_MODELS_PATH
    ap = Path(adaptive_path) if adaptive_path else DEFAULT_ADAPTIVE_PATH
    try:
        models_config = json.loads(Path(mp).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("model registry not loadable from %s: %s" % (mp, error)) from None
    try:
        adaptive = json.loads(Path(ap).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("adaptive config not loadable from %s: %s" % (ap, error)) from None
    if not isinstance(models_config, dict):
        raise ValueError("model registry is not an object: %s" % mp)
    if not isinstance(adaptive, dict):
        raise ValueError("adaptive config is not an object: %s" % ap)
    catalog = models_config.get("model_catalog")
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError("model registry has no model_catalog: %s" % mp)
    return {"models": catalog, "adaptive": adaptive,
            "models_path": str(mp), "models_config": models_config}


def classify(text, adaptive):
    """Classify free text into domains, a task type and a capability need vector.

    Domains are not mutually exclusive: every domain with >= 1 keyword hit and a
    score at least 50% of the top score is kept, ordered by score. No hit yields
    ["GENERAL"]. The capability vector is the max over the selected domains' and
    the task type's vectors.
    """
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    normalized = _normalize(text)
    domains_cfg = adaptive.get("domains", {})
    scores = {}
    for domain in domains_cfg:
        if domain == "GENERAL":
            continue
        block = domains_cfg[domain] if isinstance(domains_cfg.get(domain), dict) else {}
        keywords = block.get("keywords") or []
        hits = _hits(normalized, keywords)
        if hits:
            scores[domain] = hits
    if not scores:
        domains = ["GENERAL"]
    else:
        top = max(scores.values())
        domains = [d for d in scores if scores[d] >= top * 0.5]
        domains.sort(key=lambda d: (-scores[d], list(scores).index(d)))
    task_type = None
    best = 0
    task_types = adaptive.get("task_types", {})
    for name in task_types:
        block = task_types[name] if isinstance(task_types.get(name), dict) else {}
        hits = _hits(normalized, block.get("keywords") or [])
        if hits > best:
            best, task_type = hits, name
    capabilities = _need_vector(domains, task_type, adaptive)
    return {"domains": domains, "task_type": task_type,
            "scores": scores, "capabilities": capabilities}


def _derive_risk(risk_score, security_impact, reversibility):
    level = max(risk_score, security_impact)
    if level < 0.25:
        risk = "LOW"
    elif level < 0.5:
        risk = "MEDIUM"
    elif level < 0.75:
        risk = "HIGH"
    else:
        risk = "CRITICAL"
    if reversibility < 0.2:
        risk = RISKS[min(RISKS.index(risk) + 1, len(RISKS) - 1)]
    return risk


def _derive_tier(complexity, volume, architecture_impact):
    mean = (complexity + volume + architecture_impact) / 3.0
    if mean < 0.2:
        return "T0"
    if mean < 0.45:
        return "T1"
    if mean < 0.75:
        return "T2"
    return "T3"


def build_profile(text=None, profile=None, adaptive=None, tier=None, risk=None):
    """Build a TaskProfile from text and/or an explicit profile dict.

    Explicit profile values win over classification. Missing scores default to 0.3
    (reversibility 0.8). Scores must be finite in [0,1], tier in T0..T3, risk in
    LOW..CRITICAL, else ValueError. When tier/risk are not given they are derived
    from the scores.
    """
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    explicit = dict(profile) if isinstance(profile, dict) else {}
    cls = classify(text, adaptive) if text else {
        "domains": [], "task_type": None, "capabilities": {}}
    domains = explicit.get("domains", cls["domains"] or ["GENERAL"])
    if not isinstance(domains, (list, tuple)) or not domains:
        domains = ["GENERAL"]
    domains = list(domains)
    task_type = explicit.get("task_type", cls["task_type"])
    explicit_capabilities = explicit.get("capabilities") if isinstance(explicit.get("capabilities"), dict) else {}
    capabilities = dict(cls["capabilities"])
    for name, value in explicit_capabilities.items():
        capabilities[name] = max(capabilities.get(name, 0.0), value) \
            if isinstance(value, (int, float)) and not isinstance(value, bool) else capabilities.get(name, 0.0)

    scores = {}
    for name in SCORE_FIELDS:
        default = 0.8 if name == "reversibility" else 0.3
        scores[name] = explicit.get(name, default)
    for name, value in scores.items():
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0.0 <= value <= 1.0):
            raise ValueError("profile score must be a finite number in [0,1]: " + name)

    if tier is not None:
        final_tier = str(tier).upper()
    else:
        final_tier = explicit.get("tier")
    if final_tier is None:
        final_tier = _derive_tier(scores["complexity"], scores["volume"],
                                  scores["architecture_impact"])
    else:
        final_tier = str(final_tier).upper()
    if final_tier not in TIERS:
        raise ValueError("invalid tier: " + str(final_tier))

    if risk is not None:
        final_risk = str(risk).upper()
    else:
        final_risk = explicit.get("risk")
    if final_risk is None:
        final_risk = _derive_risk(scores["risk_score"], scores["security_impact"],
                                  scores["reversibility"])
    else:
        final_risk = str(final_risk).upper()
    if final_risk not in RISKS:
        raise ValueError("invalid risk: " + str(final_risk))

    # Exploration needs an observable check; a keyword guess cannot promise one.
    verifiable = explicit.get("verifiable", False) is True
    tools_required = explicit.get("tools_required", [])
    if not isinstance(tools_required, (list, tuple)):
        tools_required = [tools_required]
    tools_required = list(tools_required)

    result = {
        "domains": domains, "task_type": task_type, "capabilities": capabilities,
        "explicit_capabilities": dict(explicit_capabilities),
        "tier": final_tier, "risk": final_risk, "verifiable": verifiable,
        "tools_required": tools_required,
    }
    result.update(scores)
    return result


def _outcome(domains, kind):
    return "%s: %s" % (kind, ", ".join(domains))


def decompose(profile, adaptive):
    """Decompose a profile into an ordered bundle list.

    A profile with >= 1 requirement domain AND >= 1 production domain decomposes
    into requirements -> implementation (depends on requirements) ->
    joint_verification (depends on implementation). Otherwise a single "main"
    bundle holds every domain, however long the task.
    """
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    requirement_domains = set(adaptive.get("requirement_domains") or [])
    production_domains = set(adaptive.get("production_domains") or [])
    domains = list(profile.get("domains") or []) or ["GENERAL"]
    req = [d for d in domains if d in requirement_domains]
    prod = [d for d in domains if d in production_domains]
    task_type = profile.get("task_type")
    # Only needs the caller gave (--profile) are floors on every bundle; the ones the
    # classifier inferred stay per bundle, or decomposition would lose its point.
    explicit = profile.get("explicit_capabilities")
    explicit = explicit if isinstance(explicit, dict) else {}

    def needs(selected):
        # The caller's explicit needs (--profile) are floors, never discarded.
        vector = _need_vector(selected, task_type, adaptive)
        for capability, value in explicit.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                vector[capability] = max(vector.get(capability, 0.0), value)
        return vector

    # An analysis over two domains is still one coherent outcome: only building
    # something (config decompose_task_types) has requirements to settle first.
    if req and prod and task_type in (adaptive.get("decompose_task_types") or []):
        return [
            {"id": "requirements", "domains": req,
             "outcome": _outcome(req, "requirements"),
             "capabilities": needs(req),
             "depends_on": []},
            {"id": "implementation", "domains": prod,
             "outcome": _outcome(prod, "implementation"),
             "capabilities": needs(prod),
             "depends_on": ["requirements"]},
            {"id": "joint_verification", "domains": domains,
             "outcome": _outcome(domains, "joint verification"),
             "capabilities": needs(domains),
             "depends_on": ["implementation"]},
        ]
    return [{"id": "main", "domains": domains,
             "outcome": _outcome(domains, "main"),
             "capabilities": needs(domains),
             "depends_on": []}]


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


def _matrix_entry(matrix, model_id):
    """The model's entry in `aos-learning.py matrix` output: {"models": {id: {...}}}."""
    models = matrix.get("models") if isinstance(matrix, dict) else None
    entry = models.get(model_id) if isinstance(models, dict) else None
    return entry if isinstance(entry, dict) else None


def _overall(matrix_entry):
    block = matrix_entry.get("overall") if matrix_entry else None
    return block if isinstance(block, dict) else {}


def _overall_score(matrix_entry):
    return _number(_overall(matrix_entry).get("score"))


def _overall_confidence(matrix_entry):
    value = _number(_overall(matrix_entry).get("confidence"))
    return min(max(value, 0.0), 1.0) if value is not None else 0.0


def _effective_capability(entry, matrix_entry, capability):
    """c_k = w * observed + (1 - w) * declared, w = per-capability matrix confidence."""
    declared = (entry.get("capabilities") or {}).get(capability, 0.0)
    if not matrix_entry:
        return declared
    caps = matrix_entry.get("capabilities") or {}
    block = caps.get(capability)
    if not isinstance(block, dict):
        return declared
    observed = block.get("score")
    weight = block.get("confidence", 0.0)
    if _number(observed) is None:
        return declared
    weight = min(max(_number(weight) or 0.0, 0.0), 1.0)
    return max(0.0, weight * observed + (1.0 - weight) * declared)


def _context_policy(model_id, models_path=None):
    module = _context_module()
    path = str(models_path) if models_path else str(DEFAULT_MODELS_PATH)
    return module.load_context_policy(model_id, path)


def eligible(model_id, entry, bundle, profile, adaptive, host, matrix=None,
             exclude=(), models_path=None):
    """(eligible, reason) — every admission constraint, in priority order."""
    if entry.get("availability") is False:
        return False, "unavailable"
    if model_id in (exclude or ()):
        return False, "excluded"
    matrix_entry = _matrix_entry(matrix, model_id)
    if matrix_entry and matrix_entry.get("status") == "DEMOTE":
        return False, "demoted"
    if "executor" not in (entry.get("roles") or []):
        return False, "missing executor role"

    class_ = entry.get("class")
    risk = profile["risk"]
    if risk == "CRITICAL":
        if class_ != "PREMIUM":
            return False, "CRITICAL requires PREMIUM"
    else:
        ceiling = adaptive.get("class_max_risk", {}).get(class_, "LOW")
        if ceiling not in RISKS or RISKS.index(ceiling) < RISKS.index(risk):
            return False, "risk above class ceiling"

    tools = set(entry.get("tools") or [])
    missing = [t for t in profile.get("tools_required", []) if t not in tools]
    if missing:
        return False, "missing tools: " + ",".join(missing)

    floor = adaptive.get("capability_floor", {})
    need_at_least = floor.get("need_at_least", 0.7)
    max_shortfall = floor.get("max_shortfall", 0.25)
    for capability, need in (bundle.get("capabilities") or {}).items():
        if need >= need_at_least:
            if _effective_capability(entry, matrix_entry, capability) < need - max_shortfall:
                return False, "capability shortfall " + capability

    estimated = profile.get("context_requirement", 0.3) \
        * adaptive.get("context_tokens_at_full_requirement", 200000)
    policy = _context_policy(model_id, models_path)
    if policy.hard_limit and estimated > policy.hard_limit:
        return False, "context over hard limit"

    invocation = entry.get("invocation")
    if profile["tier"] == "T0" and invocation == "open_worker":
        return False, "T0 cannot use open_worker"
    if invocation == "host_subagent":
        runtimes = entry.get("compatible_runtimes") or []
        host_runtime = {"claude": "claude-code", "codex": "codex-cli"}.get(host)
        if host_runtime and host_runtime not in runtimes:
            return False, "host_subagent runtime incompatible"
    return True, ""


def _escalation_class(class_, available_classes, adaptive):
    ladder = adaptive.get("escalation_ladder", {})
    nxt = ladder.get(class_)
    while nxt is not None and (available_classes is not None and nxt not in available_classes):
        nxt = ladder.get(nxt)
    return nxt if nxt else "PREMIUM"


def score(model_id, entry, bundle, profile, adaptive, matrix=None,
          available_classes=None):
    """Transparent score with every term in the output."""
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    matrix_entry = _matrix_entry(matrix, model_id)
    needs = bundle.get("capabilities") or {}

    total_need = 0.0
    weighted = 0.0
    for capability, need in needs.items():
        if need <= 0:
            continue
        cap = _effective_capability(entry, matrix_entry, capability)
        total_need += need
        weighted += need * min(cap / need, 1.0)
    capability_match = weighted / total_need if total_need else 1.0

    difficulty = (profile.get("complexity", 0.3) + profile.get("uncertainty", 0.3)
                  + profile.get("domain_specialization", 0.3)) / 3.0
    p_prior = capability_match ** (1.0 + 2.0 * difficulty)

    confidence = _overall_confidence(matrix_entry)
    p_success = p_prior
    observed = _overall_score(matrix_entry)
    if observed is not None:
        p_success = confidence * observed + (1.0 - confidence) * p_prior

    # Domain and task-type performance refine the overall reliability, each weighted
    # by its own confidence: a model strong overall but failing on this domain drops.
    specific = []
    blocks = matrix_entry.get("domains") if matrix_entry and isinstance(matrix_entry.get("domains"), dict) else {}
    for domain in bundle.get("domains") or []:
        specific.append(("domain:" + domain, blocks.get(domain)))
    blocks = matrix_entry.get("task_types") if matrix_entry and isinstance(matrix_entry.get("task_types"), dict) else {}
    if profile.get("task_type"):
        specific.append(("task_type:" + profile["task_type"], blocks.get(profile["task_type"])))
    # Each block is blended with the same base, and the weakest one decides: the
    # result is independent of domain order, and a model failing systematically in
    # one of the bundle's domains is not rescued by another it is good at.
    performance = {}
    base = p_success
    for label, block in specific:
        observed_k = _number(block.get("score")) if isinstance(block, dict) else None
        if observed_k is None:
            continue
        weight = min(max(_number(block.get("confidence")) or 0.0, 0.0), 1.0)
        blended = weight * observed_k + (1.0 - weight) * base
        performance[label] = {"score": observed_k, "confidence": weight, "blended": blended}
        p_success = min(p_success, blended)

    penalties = []
    estimated = profile.get("context_requirement", 0.3) \
        * adaptive.get("context_tokens_at_full_requirement", 200000)
    target_context = entry.get("target_context")
    if target_context and estimated > target_context:
        p_success *= 0.85
        penalties.append("context_over_target")
    if matrix_entry and matrix_entry.get("status") == "WATCH":
        p_success *= 0.9
        penalties.append("watch")
    p_success = min(1.0, max(0.0, p_success))

    class_ = entry.get("class", "LOW")
    cost_units = adaptive.get("class_cost_units", {})
    volume = profile.get("volume", 0.3)
    initial_cost = cost_units.get(class_, 1.0) * (0.5 + volume)
    escalation_class = _escalation_class(class_, available_classes, adaptive)
    escalation_cost = cost_units.get(escalation_class, cost_units.get("PREMIUM", 10.0)) \
        * (0.5 + volume)
    expected_cost = initial_cost + (1.0 - p_success) * initial_cost \
        + (1.0 - p_success) ** 2 * escalation_cost

    return {
        "model": model_id,
        "class": class_,
        "invocation": entry.get("invocation"),
        "capability_match": capability_match,
        "difficulty": difficulty,
        "p_success": p_success,
        "initial_cost": initial_cost,
        "expected_cost": expected_cost,
        "confidence": confidence,
        "performance": performance,
        "penalties": penalties,
    }


def _premium_of_family(registry, family):
    for model_id, entry in sorted(registry["models"].items()):
        if entry.get("class") == "PREMIUM" and entry.get("family") == family \
                and entry.get("availability") is not False:
            return model_id
    return None


def _choose_reviewer(executor_entry, profile, registry, host, adaptive):
    host_family = FAMILY_OF_HOST.get(host)
    executor_family = executor_entry.get("family")
    if executor_family in OPPOSITE and executor_family != host_family:
        reviewer_family = OPPOSITE[executor_family]
    else:
        reviewer_family = OPPOSITE.get(host_family, host_family)
    risk = profile["risk"]
    allowed = ("PREMIUM",) if risk in ("HIGH", "CRITICAL") else ("MID", "PREMIUM")
    cost_units = adaptive.get("class_cost_units", {})
    candidates = []
    for model_id, entry in registry["models"].items():
        if entry.get("availability") is False:
            continue
        if entry.get("class") not in allowed:
            continue
        if entry.get("family") != reviewer_family:
            continue
        if (entry.get("capabilities") or {}).get("review", 0.0) >= 0.75:
            candidates.append((cost_units.get(entry.get("class"), 99), model_id))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def select_skills(bundle, profile, adaptive):
    """Skills from skill_routes by domain then task type (falling back to "*")."""
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    routes = adaptive.get("skill_routes", {})
    task_type = profile.get("task_type")
    skills = []
    for domain in bundle.get("domains") or []:
        domain_routes = routes.get(domain) if isinstance(routes.get(domain), dict) else {}
        for skill in domain_routes.get(task_type) or domain_routes.get("*") or []:
            if skill not in skills:
                skills.append(skill)
    writing = adaptive.get("writing_skills", {})
    threshold = writing.get("threshold", 0.7)
    if (bundle.get("capabilities") or {}).get("writing", 0.0) >= threshold:
        for skill in writing.get("skills") or []:
            if skill not in skills:
                skills.append(skill)
    return skills


def _rank(s):
    return (s["expected_cost"], -s["p_success"], s["model"])


def route(profile, registry, host, matrix=None, task_id=None, exclude=(),
          models_path=None):
    """Choose an executor (and planner/reviewer) for each decomposed bundle."""
    adaptive = registry["adaptive"]
    models = registry["models"]
    models_path = models_path or registry.get("models_path")
    available_classes = {entry.get("class") for entry in models.values()
                         if entry.get("availability") is not False}
    bundles = decompose(profile, adaptive)
    risk = profile.get("risk", "LOW")
    tier = profile.get("tier", "T1")

    needs_plan = (tier in ("T2", "T3")
                  or profile.get("uncertainty", 0.3) >= 0.6
                  or profile.get("architecture_impact", 0.3) >= 0.6)

    result_bundles = []
    for bundle in bundles:
        rejected = {}
        scored = []
        for model_id, entry in sorted(models.items()):
            ok, reason = eligible(model_id, entry, bundle, profile, adaptive, host,
                                  matrix=matrix, exclude=exclude, models_path=models_path)
            if not ok:
                rejected[model_id] = reason
                continue
            scored.append(score(model_id, entry, bundle, profile, adaptive,
                                matrix=matrix, available_classes=available_classes))
        scored.sort(key=_rank)

        threshold = adaptive.get("min_success_probability", {}).get(risk, 0.7)
        below_threshold = False
        if scored:
            above = [s for s in scored if s["p_success"] >= threshold]
            if above:
                executor = min(above, key=_rank)
            else:
                executor = max(scored, key=lambda s: (s["p_success"], -s["expected_cost"], s["model"]))
                below_threshold = True
        else:
            executor = None

        if executor is not None:
            exploration = adaptive.get("exploration", {})
            if (task_id and RISKS.index(risk) <= RISKS.index(exploration.get("max_risk", "LOW"))
                    and profile.get("reversibility", 0.8) >= exploration.get("min_reversibility", 0.7)
                    and TIERS.index(tier) <= TIERS.index(exploration.get("max_tier", "T2"))
                    and profile.get("verifiable") is True):
                key = str(task_id) + str(bundle["id"])
                draw = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 10000
                if draw < exploration.get("rate", 0.05) * 10000:
                    others = [s for s in scored
                              if s["model"] != executor["model"]
                              and s["confidence"] <= exploration.get("max_confidence", 1.0)
                              and s["p_success"] >= 0.9 * threshold]
                    if others:
                        executor = dict(min(others, key=_rank))
                        executor["exploration"] = True

        planner = None
        if needs_plan:
            planner = _premium_of_family(registry, FAMILY_OF_HOST.get(host))

        reviewer = None
        review_fallback = None
        if tier in ("T2", "T3") and executor is not None:
            reviewer = _choose_reviewer(models[executor["model"]], profile, registry,
                                        host, adaptive)
            if reviewer is None:
                review_fallback = "declared fallback in quality-gates, never fake independence"

        verification = []
        verification_cfg = adaptive.get("verification", {})
        for domain in bundle.get("domains") or []:
            for item in verification_cfg.get(domain) or []:
                if item not in verification:
                    verification.append(item)

        context = {}
        if executor is not None:
            policy = _context_policy(executor["model"], models_path)
            context = {"target": policy.target_context, "soft": policy.soft_limit,
                       "hard": policy.hard_limit}

        result_bundles.append({
            "id": bundle["id"],
            "domains": bundle["domains"],
            "outcome": bundle["outcome"],
            "depends_on": bundle["depends_on"],
            "skills": select_skills(bundle, profile, adaptive),
            "executor": (executor | {"below_threshold": below_threshold}) if executor else None,
            "candidates": scored,
            "rejected": rejected,
            "planner": planner,
            "reviewer": reviewer,
            "review_fallback": review_fallback,
            "verification": verification,
            "context": context,
        })

    return {"profile": profile, "bundles": result_bundles,
            "needs_approval": risk == "CRITICAL"}


def _models_in_class(cls, registry, host=None):
    """Available models of a class; with a host, those it can run first."""
    host_runtime = {"claude": "claude-code", "codex": "codex-cli"}.get(host)

    def runnable(entry):
        # A host subagent runs only on its own runtime; sessions and workers anywhere.
        return (host_runtime is None or entry.get("invocation") != "host_subagent"
                or host_runtime in (entry.get("compatible_runtimes") or []))

    models = registry["models"]
    return sorted(model_id for model_id, entry in models.items()
                  if entry.get("class") == cls and entry.get("availability") is not False
                  and runnable(entry))


def escalate(failure_type, model_id, registry, attempts=1, host=None):
    """Map a failure type to an action and a target, from failure_types."""
    adaptive = registry["adaptive"]
    models = registry["models"]
    failure = adaptive.get("failure_types", {}).get(failure_type)
    if not isinstance(failure, dict):
        raise ValueError("unknown failure type: " + str(failure_type))
    action = failure.get("action")
    attribution = failure.get("attribution")
    max_retries = adaptive.get("max_same_model_retries", 1)
    counts_against_model = attribution == "model"

    target_class = None
    target_model = None

    entry = models.get(model_id) if isinstance(models.get(model_id), dict) else {}
    available_classes = {e.get("class") for e in models.values()
                         if e.get("availability") is not False}

    if action == "retry_then_escalate":
        if attempts <= max_retries:
            action = "retry_same"
        else:
            action = "escalate"
            target_class = _escalation_class(entry.get("class"), available_classes, adaptive)
            target_model = (_models_in_class(target_class, registry, host) or [None])[0]
    elif action == "escalate":
        target_class = _escalation_class(entry.get("class"), available_classes, adaptive)
        target_model = (_models_in_class(target_class, registry, host) or [None])[0]
    elif action == "escalate_reviewer":
        # The reviewer's family was chosen opposite to the executor: keep it, or the
        # escalation hands the review back to the family that wrote the work.
        target_class = "PREMIUM"
        same_family = [m for m in _models_in_class("PREMIUM", registry, host)
                       if models[m].get("family") == entry.get("family")]
        target_model = same_family[0] if same_family else None
    elif action == "premium_planner":
        target_class = "PREMIUM"
        target_model = (_models_in_class("PREMIUM", registry, host) or [None])[0]
    # retry_same, retry_same_with_more_context, retry_same_with_clarified_contract,
    # reroute and stop_and_ask_human keep target_class/target_model as None.

    return {
        "action": action,
        "attribution": attribution,
        "counts_against_model": counts_against_model,
        "target_class": target_class,
        "target_model": target_model,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog="aos-orchestrate")
    sub = parser.add_subparsers(dest="command")

    cls = sub.add_parser("classify", help="classify a task description")
    cls.add_argument("--text", required=True)
    cls.add_argument("--models")
    cls.add_argument("--adaptive")

    rt = sub.add_parser("route", help="build a profile and route to a model")
    src = rt.add_mutually_exclusive_group(required=True)
    src.add_argument("--text")
    src.add_argument("--profile")
    rt.add_argument("--tier", choices=list(TIERS))
    rt.add_argument("--risk", choices=list(RISKS))
    rt.add_argument("--host", choices=["claude", "codex"], default="claude")
    rt.add_argument("--matrix")
    rt.add_argument("--task-id")
    rt.add_argument("--exclude", nargs="*", default=[])
    rt.add_argument("--verifiable", action="store_true",
                    help="an observable check exists: allows exploration on LOW reversible work")
    rt.add_argument("--models")
    rt.add_argument("--adaptive")

    es = sub.add_parser("escalate", help="map a failure type to its action")
    es.add_argument("--failure-type", required=True)
    es.add_argument("--model", required=True)
    es.add_argument("--attempts", type=int, default=1)
    es.add_argument("--host", choices=["claude", "codex"])
    es.add_argument("--models")
    es.add_argument("--adaptive")

    args = parser.parse_args(argv)
    try:
        if args.command == "classify":
            registry = load_registry(args.models, args.adaptive)
            print(json.dumps(classify(args.text, registry["adaptive"]),
                             ensure_ascii=False, indent=2))
            return 0
        if args.command == "route":
            registry = load_registry(args.models, args.adaptive)
            if args.profile:
                profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
                profile = build_profile(profile=profile, adaptive=registry["adaptive"],
                                        tier=args.tier, risk=args.risk)
            else:
                profile = build_profile(text=args.text, adaptive=registry["adaptive"],
                                        tier=args.tier, risk=args.risk)
            if args.verifiable:
                profile["verifiable"] = True
            matrix = None
            if args.matrix:
                matrix = json.loads(Path(args.matrix).read_text(encoding="utf-8"))
            result = route(profile, registry, args.host, matrix=matrix,
                           task_id=args.task_id, exclude=args.exclude)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "escalate":
            registry = load_registry(args.models, args.adaptive)
            print(json.dumps(escalate(args.failure_type, args.model, registry,
                                      attempts=args.attempts, host=args.host),
                             ensure_ascii=False, indent=2))
            return 0
        parser.print_help()
        return 1
    except ValueError as error:
        print("ERRORE: " + str(error), file=sys.stderr)
        return 2
    except OSError as error:
        print("ERRORE: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
