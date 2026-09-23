#!/usr/bin/env python3
"""AOS context budget: measure or estimate context, classify GREEN/YELLOW/ORANGE/RED,
and compact or hand off before a model's window degrades the work.

Provider/model aware, configurable, never hardcoded. Real usage comes from the
caller (runtime-reported usage); text/chars estimates are marked estimated.
Compaction is structural, not a free-text summary: critical items always survive,
and a role this manager does not recognize is kept, never dropped — reducing
context must not weaken any guardrail. The technical context limit is a ceiling
the ratios report against, never an operating target.
"""
import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

CHARS_PER_TOKEN = 4

# Model families recognised by substring, for the model-class fallback. Adding a
# family here is configuration, not code: an unknown model resolves to "other"
# and, absent an "other" class, to defaults.
FAMILIES = (
    ("deepseek", "deepseek"),
    ("qwen", "qwen"),
    ("claude", "claude"),
    ("anthropic", "claude"),
    ("fable", "claude"),
    ("opus", "claude"),
    ("sonnet", "claude"),
    ("haiku", "claude"),
    ("codex", "codex"),
    ("gemini", "gemini"),
    ("gpt", "gpt"),
    ("openai", "gpt"),
    ("o1", "gpt"),
    ("o3", "gpt"),
)


@dataclass(frozen=True)
class ContextPolicy:
    technical_context_limit: int = None
    target_context: int = 0
    soft_limit: int = 0
    hard_limit: int = 0
    source: str = "default"  # model | model_class | default


@dataclass(frozen=True)
class ContextState:
    tokens: int
    token_source: str          # measured | estimated
    state: str                 # GREEN | YELLOW | ORANGE | RED
    target_context: int
    soft_limit: int
    hard_limit: int
    technical_context_limit: int
    policy_source: str
    ratios: dict


def model_class(model_id):
    lowered = (model_id or "").lower()
    for needle, klass in FAMILIES:
        if needle in lowered:
            return klass
    return "other"


def _positive_int(value):
    # bool is an int subclass; a boolean is never a context limit.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _policy(block, source):
    return ContextPolicy(
        technical_context_limit=_positive_int(block.get("technical_context_limit")),
        target_context=_positive_int(block.get("target_context")) or 0,
        soft_limit=_positive_int(block.get("soft_limit")) or 0,
        hard_limit=_positive_int(block.get("hard_limit")) or 0,
        source=source,
    )


def load_context_policy(model_id, config_path=None):
    """Resolve the context policy for a model: model -> model_class -> defaults.

    `source` records which level matched, so telemetry can say a fallback was
    used. An absent or invalid configuration yields a defaults-only policy
    (source="default") whose limits are all zero — backward compatible: nothing
    breaks, the manager just reports it has no limits to enforce.
    """
    section = {}
    if config_path is not None:
        try:
            data = json.loads(Path(config_path).read_text())
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            section = data.get("context_policy") or {}
    if not isinstance(section, dict):
        section = {}
    model = (model_id or "").strip()
    models = section.get("models") if isinstance(section.get("models"), dict) else {}
    if model and isinstance(models.get(model), dict):
        return _policy(models[model], "model")
    classes = section.get("model_classes") if isinstance(section.get("model_classes"), dict) else {}
    klass = model_class(model)
    if klass != "other" and isinstance(classes.get(klass), dict):
        return _policy(classes[klass], "model_class")
    if isinstance(classes.get("other"), dict):
        return _policy(classes["other"], "model_class")
    defaults = section.get("defaults") if isinstance(section.get("defaults"), dict) else {}
    return _policy(defaults, "default")


def measure(tokens=None, text=None, chars=None):
    """(count, source) — measured when a real count is given, estimated otherwise.

    A text/chars estimate is never presented as a measured count.
    """
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        return tokens, "measured"
    if isinstance(chars, int) and not isinstance(chars, bool) and chars >= 0:
        return chars // CHARS_PER_TOKEN, "estimated"
    if isinstance(text, (str, bytes)):
        seq = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
        return len(seq) // CHARS_PER_TOKEN, "estimated"
    return None, None


def classify(tokens, policy, token_source="measured"):
    """ContextState for the current token count against a policy.

    GREEN <= target; YELLOW <= soft; ORANGE <= hard; RED beyond hard. The
    technical limit appears only in the ratios: it is never a target.
    """
    tokens = int(tokens)
    if tokens <= policy.target_context:
        state = "GREEN"
    elif tokens <= policy.soft_limit:
        state = "YELLOW"
    elif tokens <= policy.hard_limit:
        state = "ORANGE"
    else:
        state = "RED"
    ratios = {}
    for name, limit in (("target_context", policy.target_context),
                        ("soft_limit", policy.soft_limit),
                        ("hard_limit", policy.hard_limit),
                        ("technical_context_limit", policy.technical_context_limit)):
        if limit:
            ratios[name] = round(tokens / limit, 3)
    return ContextState(
        tokens=tokens,
        token_source=token_source,
        state=state,
        target_context=policy.target_context,
        soft_limit=policy.soft_limit,
        hard_limit=policy.hard_limit,
        technical_context_limit=policy.technical_context_limit,
        policy_source=policy.source,
        ratios=ratios,
    )


def action(state):
    """The recommended action and whether it is mandatory, from a state.

    GREEN: none. YELLOW: note (prefer targeted retrieval, stop re-inserting).
    ORANGE: compact (cleanup + structured compaction, evaluate a new session if
    the task is separable). RED: compact is mandatory; a new session/subtask is
    the alternative when the task is separable.
    """
    if state.state == "GREEN":
        return {"recommendation": "none", "mandatory": False}
    if state.state == "YELLOW":
        return {"recommendation": "note", "mandatory": False}
    if state.state == "ORANGE":
        return {"recommendation": "compact", "mandatory": False}
    return {"recommendation": "compact", "mandatory": True, "alternative": "new_session"}


def evaluate(model_id, *, tokens=None, text=None, chars=None, config_path=None):
    """Full evaluation: policy resolution + metering + classification + action."""
    policy = load_context_policy(model_id, config_path)
    base = {
        "model": model_id,
        "policy": asdict(policy),
        "model_context_policy_source": policy.source,
        "technical_context_limit": policy.technical_context_limit,
    }
    if policy.soft_limit == 0 and policy.hard_limit == 0:
        # No limits configured anywhere: no enforcement, nothing to compact.
        base.update({"context_tokens": None, "context_tokens_source": None,
                     "context_state": None, "context_utilization_ratio": {},
                     "target_context": None, "soft_limit": None, "hard_limit": None,
                     "action": "none", "action_mandatory": False})
        return base
    count, source = measure(tokens=tokens, text=text, chars=chars)
    if count is None:
        base.update({"context_tokens": None, "context_tokens_source": None,
                     "context_state": None, "context_utilization_ratio": {},
                     "target_context": policy.target_context,
                     "soft_limit": policy.soft_limit, "hard_limit": policy.hard_limit,
                     "action": None, "action_mandatory": False})
        return base
    state = classify(count, policy, source)
    act = action(state)
    base.update({
        "context_tokens": state.tokens,
        "context_tokens_source": state.token_source,
        "context_state": state.state,
        "context_utilization_ratio": state.ratios,
        "target_context": state.target_context,
        "soft_limit": state.soft_limit,
        "hard_limit": state.hard_limit,
        "action": act["recommendation"],
        "action_mandatory": act["mandatory"],
    })
    if "alternative" in act:
        base["action_alternative"] = act["alternative"]
    return base


def normalize_role(role):
    return " ".join((role or "").lower().split())


# Roles that are safe to drop on compaction. Anything else — including a role this
# manager has never seen — is kept, so compaction can only remove what it names.
DROP_ROLES = frozenset({
    "resolved_log", "verbose_output", "superseded_hypothesis",
    "irrelevant_file", "explained_traceback", "repetitive_discussion",
    "unnecessary_tool_output", "stale_snapshot",
})


def _item_text(item):
    content = item.get("content")
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content if isinstance(content, str) else ""


def compact_context(items):
    """Structural compaction of an ordered context item list.

    `items` is an iterable of dicts with `id`, `role` and `content` (extra fields
    are preserved on kept items). Keeps every item whose role is not an explicit
    droppable role; drops droppable roles and deduplicates by `id`, keeping the
    first occurrence. A repeated `id` whose role or content differs raises
    ValueError: dropping it would silently lose the newer version. Returns
    kept/removed items and an estimated token before/after (chars/4, marked
    estimated).
    """
    if not isinstance(items, (list, tuple)):
        raise ValueError("items must be a list of context items")
    kept, removed, seen = [], [], {}
    before = after = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _item_text(item)
        before += len(text) // CHARS_PER_TOKEN
        iid = item.get("id")
        signature = (item.get("role"), text)
        if iid is not None and iid in seen and seen[iid] != signature:
            raise ValueError(
                "conflicting duplicate item id %r would silently discard data" % (iid,))
        duplicate = iid is not None and iid in seen
        if iid is not None:
            seen[iid] = signature
        if not duplicate and normalize_role(item.get("role")) not in DROP_ROLES:
            kept.append(item)
            after += len(text) // CHARS_PER_TOKEN
        else:
            removed.append(item)
    return {
        "kept": kept,
        "removed": removed,
        "items_before": len(kept) + len(removed),
        "items_after": len(kept),
        "tokens_before_compaction": before,
        "tokens_after_compaction": after,
    }


def build_handoff(*, task, acceptance_criteria=None, decisions=None,
                  relevant_files=None, modified_files=None, tests_passed=None,
                  tests_failed=None, open_issues=None, security_constraints=None,
                  retry_count=0, executor=None, model=None, runtime=None,
                  review_findings=None, escalation_pending=None, context=None):
    """Structured task state for transfer to a new session, subtask or model.

    Complements (does not duplicate) the measure record, which is a measurement:
    this carries what the next executor must know to continue the work.
    """

    def lst(value):
        return list(value) if value else []

    return {
        "schema": 1,
        "task": task,
        "acceptance_criteria": lst(acceptance_criteria),
        "decisions": lst(decisions),
        "relevant_files": lst(relevant_files),
        "modified_files": lst(modified_files),
        "tests": {"passed": lst(tests_passed), "failed": lst(tests_failed)},
        "open_issues": lst(open_issues),
        "security_constraints": lst(security_constraints),
        "retry_count": int(retry_count or 0),
        "executor": executor or model or runtime,
        "model": model,
        "runtime": runtime,
        "review_findings": lst(review_findings),
        "escalation_pending": escalation_pending,
        "context": context,
    }


# Events that mark a natural boundary at which a checkpoint is safe to write.
CHECKPOINT_EVENTS = frozenset({
    "none", "discovery_done", "implementation_done", "tests_green",
    "bundle_done", "decision_done",
})


def _profile_factor(profile):
    """Factor in [0.4, 1.0] from optional 0..1 profile keys; deep ×0.85.

    Higher complexity, uncertainty and evidence volume shrink the working zone
    sooner; deep reasoning shrinks it again by a further 15%. The factor is
    floored at 0.4 so a hard task still gets a meaningful budget.
    """
    if profile is None:
        profile = {}
    if not isinstance(profile, dict):
        raise ValueError("profile must be a JSON object of floats 0..1")

    def ratio(key):
        value = profile.get(key)
        if value is None:
            return 0.0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("profile %r must be a number in 0..1" % (key,))
        value = float(value)
        if not 0.0 <= value <= 1.0:
            raise ValueError("profile %r must be in 0..1" % (key,))
        return value

    factor = (1 - 0.3 * ratio("complexity")
              - 0.2 * ratio("uncertainty")
              - 0.1 * ratio("evidence_volume"))
    reasoning = profile.get("reasoning")
    if reasoning not in (None, "deep", "shallow"):
        raise ValueError('profile "reasoning" must be "deep" or "shallow"')
    if reasoning == "deep":
        factor *= 0.85
    return round(max(factor, 0.4), 4)


def zone(model_id, profile=None, config_path=None):
    """Optimal and degradation zones for a model and a task profile.

    The zones are not one universal number: they depend on the model's policy and
    on how complex, uncertain and evidence-heavy the task is. The hard limit is
    never scaled and never exceeded — it is the boundary the caller may assume is
    rigid regardless of the factor.
    """
    policy = load_context_policy(model_id, config_path)
    if policy.soft_limit == 0 and policy.hard_limit == 0:
        return {
            "model": model_id,
            "factor": 0.0,
            "optimal_until": 0,
            "degradation_from": 0,
            "hard_limit": 0,
            "technical_context_limit": policy.technical_context_limit,
            "policy_source": policy.source,
            "enforced": False,
        }
    factor = _profile_factor(profile)
    optimal_until = int(policy.target_context * factor)
    degradation_from = int(policy.soft_limit * factor)
    hard_limit = policy.hard_limit
    # int() truncation must not reorder the zones or push them past the hard limit.
    optimal_until = min(optimal_until, hard_limit)
    degradation_from = max(degradation_from, optimal_until)
    degradation_from = min(degradation_from, hard_limit)
    return {
        "model": model_id,
        "factor": factor,
        "optimal_until": optimal_until,
        "degradation_from": degradation_from,
        "hard_limit": hard_limit,
        "technical_context_limit": policy.technical_context_limit,
        "policy_source": policy.source,
        "enforced": True,
    }


def natural_checkpoint(tokens, zone_result, event):
    """Action at a token count, deferred until the next natural boundary.

    The zone sets urgency; the event decides whether the work sits at a point
    where a checkpoint/compaction is safe to make. Only the hard limit flips to
    compact_now without an event: it is never crossed while waiting.
    """
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise ValueError("tokens must be a nonnegative integer")
    if event is None:
        event = "none"
    if event not in CHECKPOINT_EVENTS:
        raise ValueError("unknown event %r" % (event,))
    if not zone_result.get("enforced", True):
        return {"position": "optimal", "action": "continue", "event": event, "tokens": tokens}
    optimal_until = zone_result["optimal_until"]
    degradation_from = zone_result["degradation_from"]
    hard_limit = zone_result["hard_limit"]
    if tokens <= optimal_until:
        position, action = "optimal", "continue"
    elif tokens <= degradation_from:
        position, action = "approaching", "prepare_checkpoint"
    elif tokens <= hard_limit:
        if event != "none":
            position, action = "degradation", "compact_at_checkpoint"
        else:
            position, action = "degradation", "wait_for_checkpoint"
    else:
        position, action = "over_hard", "compact_now"
    return {"position": position, "action": action, "event": event, "tokens": tokens}


# Fields a checkpoint document carries — all preserved across compaction.
CHECKPOINT_FIELDS = (
    "objective",
    "acceptance_criteria",
    "constraints",
    "architecture_decisions",
    "artifacts_changed",
    "known_facts",
    "open_hypotheses",
    "completed_work",
    "failed_attempts",
    "test_evidence",
    "review_findings",
    "pending_work",
    "rollback_state",
    "next_action",
)

_CHECKPOINT_STRING_FIELDS = frozenset({"objective", "rollback_state", "next_action"})


def checkpoint_template():
    """An empty checkpoint document: every field present, nothing filled in."""
    doc = {"schema": 1}
    for field in CHECKPOINT_FIELDS:
        doc[field] = "" if field in _CHECKPOINT_STRING_FIELDS else []
    return doc


def validate_checkpoint(doc):
    """Gate a checkpoint before the old context may be released.

    A key that is missing is an error; a key that is present but empty fails the
    release gate. The gate names what a successor needs: the goal, some current
    state, the decisions taken, the work still outstanding (an explicit empty
    list is allowed only when next_action says the task is complete), some
    verification evidence, and a next action. Oversized fields are warnings, not
    errors: raw logs and redundant tool output do not belong in a checkpoint.
    """
    missing = []
    gate = []
    warnings = []
    if not isinstance(doc, dict):
        return {"ok": False, "missing": list(CHECKPOINT_FIELDS),
                "empty_gate": gate, "warnings": warnings}
    for field in CHECKPOINT_FIELDS:
        if field not in doc:
            missing.append(field)
            continue
        value = doc[field]
        if isinstance(value, str) and len(value) > 4000:
            warnings.append("field %r is %d characters (limit 4000)" % (field, len(value)))
        if isinstance(value, (list, tuple)) and len(value) > 200:
            warnings.append("field %r has %d items (limit 200)" % (field, len(value)))
    if missing:
        return {"ok": False, "missing": missing, "empty_gate": gate, "warnings": warnings}

    def nonempty(value):
        # [""] or [{}] carries nothing: a list counts only through a real item.
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple)):
            return any(nonempty(item) for item in value)
        if isinstance(value, dict):
            return any(nonempty(item) for item in value.values())
        return value is not None and value is not False

    next_action = doc.get("next_action")
    next_lower = next_action.strip().lower() if isinstance(next_action, str) else ""
    complete = next_lower.startswith("done") or next_lower.startswith("completo")

    if not nonempty(doc.get("objective")):
        gate.append("goal")
    if not (nonempty(doc.get("completed_work")) or nonempty(doc.get("known_facts"))):
        gate.append("current_state")
    if not nonempty(doc.get("architecture_decisions")):
        gate.append("decisions")
    if not nonempty(doc.get("pending_work")) and not complete:
        gate.append("unresolved_work")
    if not (nonempty(doc.get("test_evidence")) or nonempty(doc.get("review_findings"))):
        gate.append("verification_state")
    if not nonempty(next_action):
        gate.append("next_action")

    return {"ok": not gate, "missing": missing, "empty_gate": gate, "warnings": warnings}


def compaction_effectiveness(events):
    """Judge whether compaction actually helped, from post-compaction events.

    Each event reports whether a resume failed, whether discovery was duplicated,
    how many information-recovery requests and post-compaction errors occurred,
    and the before/after token counts. Context saved is clamped per event at
    zero: compaction can never be credited with saving negative tokens.
    """
    if not isinstance(events, (list, tuple)):
        raise ValueError("events must be a list of objects")
    if not events:
        return {
            "counts": {"resume_failure": 0, "duplicated_discovery": 0,
                       "information_recovery_requests": 0, "post_compaction_errors": 0},
            # No compaction observed: a rate is unknown, not zero.
            "rates": {"resume_failure_rate": None, "duplicated_discovery_rate": None,
                      "mean_information_recovery_requests": None, "post_compaction_error_rate": None},
            "context_tokens_saved": 0,
            "verdict": "no_data",
        }

    def field(event, key, high=None):
        value = event.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("event %r must be a nonnegative integer" % (key,))
        if value < 0 or (high is not None and value > high):
            raise ValueError("event %r is out of range" % (key,))
        return value

    n = resume_failures = duplicated = info_total = error_total = saved = 0
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("each event must be an object")
        n += 1
        resume_failures += field(event, "resume_failure", 1)
        duplicated += field(event, "duplicated_discovery", 1)
        info_total += field(event, "information_recovery_requests")
        error_total += field(event, "post_compaction_errors")
        before = field(event, "tokens_before")
        after = field(event, "tokens_after")
        saved += max(before - after, 0)

    resume_failure_rate = round(resume_failures / n, 4)
    duplicated_discovery_rate = round(duplicated / n, 4)
    mean_info = round(info_total / n, 4)
    error_rate = round(error_total / n, 4)
    verdict = ("ineffective"
               if resume_failure_rate > 0.3 or duplicated_discovery_rate > 0.3
               else "effective")
    return {
        "counts": {"resume_failure": resume_failures, "duplicated_discovery": duplicated,
                   "information_recovery_requests": info_total, "post_compaction_errors": error_total},
        "rates": {"resume_failure_rate": resume_failure_rate,
                  "duplicated_discovery_rate": duplicated_discovery_rate,
                  "mean_information_recovery_requests": mean_info,
                  "post_compaction_error_rate": error_rate},
        "context_tokens_saved": saved,
        "verdict": verdict,
    }


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "open-models.json"


def _state_text(result):
    if result["soft_limit"] is None:
        return f"{result['model']}: nessuna context policy configurata"
    if result["context_tokens"] is None:
        return f"{result['model']}: nessun dato di contesto"
    return (f"{result['model']} {result['context_tokens']} tok "
            f"({result['context_tokens_source']}) -> {result['context_state']} "
            f"[action={result['action']}, mandatory={result['action_mandatory']}]")


def _nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def main(argv=None):
    try:
        return _main(argv)
    except (ValueError, TypeError) as error:
        # Bad input on stdin or in a file is a usage error, not a traceback.
        print(f"ERRORE: {error}", file=sys.stderr)
        return 1


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog="aos-context")
    sub = parser.add_subparsers(dest="command")
    ev = sub.add_parser("state", help="evaluate the context state for a model")
    ev.add_argument("--model", required=True, help="provider/model id as AOS names it")
    ev.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config/open-models.json")
    ev.add_argument("--tokens", type=_nonnegative, default=None, help="measured context tokens")
    ev.add_argument("--chars", type=_nonnegative, default=None, help="estimated via character count")
    ev.add_argument("--text-file", default=None, help="estimate from a file's character count")
    ev.add_argument("--json", action="store_true")
    cp = sub.add_parser("compact", help="compact a context item list read as JSON on stdin")
    cp.add_argument("--json", action="store_true")
    ho = sub.add_parser("handoff", help="build a handoff from a JSON task spec on stdin")
    ho.add_argument("--json", action="store_true")
    zn = sub.add_parser("zone", help="compute optimal/degradation zones for a model")
    zn.add_argument("--model", required=True, help="provider/model id as AOS names it")
    zn.add_argument("--profile", default=None, help="path to a JSON task profile")
    zn.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config/open-models.json")
    ck = sub.add_parser("checkpoint", help="checkpoint documents and the release quality gate")
    ck_sub = ck.add_subparsers(dest="checkpoint_command")
    ck_template = ck_sub.add_parser("template", help="print the empty checkpoint template")
    ck_validate = ck_sub.add_parser("validate", help="validate a checkpoint JSON file")
    ck_validate.add_argument("file", help="path to the checkpoint JSON document")
    ck_when = ck_sub.add_parser("when", help="natural_checkpoint over the model's zones")
    ck_when.add_argument("--model", required=True, help="provider/model id as AOS names it")
    ck_when.add_argument("--tokens", type=_nonnegative, required=True, help="current token count")
    ck_when.add_argument("--event", default="none", help="natural boundary reached")
    ck_when.add_argument("--profile", default=None, help="path to a JSON task profile")
    ck_when.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config/open-models.json")
    ef = sub.add_parser("effectiveness", help="assess compaction effectiveness from a JSON list")
    ef.add_argument("file", help="path to a JSON list of post-compaction events")
    args = parser.parse_args(argv)

    if args.command == "state":
        text = None
        if args.text_file:
            try:
                text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                print(f"ERRORE: {error}", file=sys.stderr)
                return 1
        result = evaluate(args.model, tokens=args.tokens, chars=args.chars,
                          text=text, config_path=args.config)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(_state_text(result))
        return 0
    if args.command == "compact":
        try:
            data = json.load(sys.stdin)
        except ValueError as error:
            print(f"JSON non valido sullo stdin: {error}", file=sys.stderr)
            return 1
        items = data.get("items") if isinstance(data, dict) else data
        result = compact_context(items)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"items {result['items_before']} -> {result['items_after']} · "
                  f"token stimati {result['tokens_before_compaction']} -> {result['tokens_after_compaction']}")
        return 0
    if args.command == "handoff":
        try:
            spec = json.load(sys.stdin)
        except ValueError as error:
            print(f"JSON non valido sullo stdin: {error}", file=sys.stderr)
            return 1
        if not isinstance(spec, dict):
            print("ERRORE: serve un oggetto JSON", file=sys.stderr)
            return 1
        result = build_handoff(**{k: v for k, v in spec.items()})
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"handoff: task={result['task']!r}, modified={len(result['modified_files'])}, "
                  f"failed_tests={len(result['tests']['failed'])}, open_issues={len(result['open_issues'])}")
        return 0
    if args.command == "zone":
        profile = None
        if args.profile:
            try:
                profile = json.loads(Path(args.profile).read_text())
            except (OSError, ValueError) as error:
                print(f"ERRORE: {error}", file=sys.stderr)
                return 1
        result = zone(args.model, profile=profile, config_path=args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "checkpoint":
        if args.checkpoint_command == "template":
            print(json.dumps(checkpoint_template(), ensure_ascii=False, indent=2))
            return 0
        if args.checkpoint_command == "validate":
            try:
                doc = json.loads(Path(args.file).read_text())
            except (OSError, ValueError) as error:
                print(f"ERRORE: {error}", file=sys.stderr)
                return 2
            result = validate_checkpoint(doc)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 2
        if args.checkpoint_command == "when":
            profile = None
            if args.profile:
                try:
                    profile = json.loads(Path(args.profile).read_text())
                except (OSError, ValueError) as error:
                    print(f"ERRORE: {error}", file=sys.stderr)
                    return 1
            zr = zone(args.model, profile=profile, config_path=args.config)
            result = natural_checkpoint(args.tokens, zr, args.event)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        ck.print_help()
        return 1
    if args.command == "effectiveness":
        try:
            data = json.loads(Path(args.file).read_text())
        except (OSError, ValueError) as error:
            print(f"ERRORE: {error}", file=sys.stderr)
            return 1
        result = compaction_effectiveness(data)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
