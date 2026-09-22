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
    first occurrence. Returns kept/removed items and an estimated token
    before/after (chars/4, marked estimated).
    """
    kept, removed, seen = [], [], set()
    before = after = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        text = _item_text(item)
        before += len(text) // CHARS_PER_TOKEN
        iid = item.get("id")
        duplicate = iid is not None and iid in seen
        if not duplicate and normalize_role(item.get("role")) not in DROP_ROLES:
            kept.append(item)
            if iid is not None:
                seen.add(iid)
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


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "open-models.json"


def _state_text(result):
    if result["context_tokens"] is None:
        return f"{result['model']}: nessun dato di contesto" if result["context_state"] is None \
            else f"{result['model']}: nessuna context policy configurata"
    return (f"{result['model']} {result['context_tokens']} tok "
            f"({result['context_tokens_source']}) -> {result['context_state']} "
            f"[action={result['action']}, mandatory={result['action_mandatory']}]")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, prog="aos-context")
    sub = parser.add_subparsers(dest="command")
    ev = sub.add_parser("state", help="evaluate the context state for a model")
    ev.add_argument("--model", required=True, help="provider/model id as AOS names it")
    ev.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config/open-models.json")
    ev.add_argument("--tokens", type=int, default=None, help="measured context tokens")
    ev.add_argument("--chars", type=int, default=None, help="estimated via character count")
    ev.add_argument("--text-file", default=None, help="estimate from a file's character count")
    ev.add_argument("--json", action="store_true")
    cp = sub.add_parser("compact", help="compact a context item list read as JSON on stdin")
    cp.add_argument("--json", action="store_true")
    ho = sub.add_parser("handoff", help="build a handoff from a JSON task spec on stdin")
    ho.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "state":
        text = None
        if args.text_file:
            try:
                text = Path(args.text_file).read_text()
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
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
