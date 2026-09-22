#!/usr/bin/env python3
"""AOS operational helpers for later pipeline integration.

Small, stdlib-only, hermetric utilities the coordinator will compose into the
pipeline. Nothing here talks to a provider, runs a command or reads a secret.
It reuses `bin/aos-context.py` for context evaluation and structural compaction
so the safe compaction rules live in exactly one place.

APIs
----
prepare_context(model, items, state, config_path=None) -> dict
    Evaluate the original context, compact structure when ORANGE/RED, and build
    a bounded structured handoff. Returns {prompt, context, handoff}. Always
    evaluates the original context; compaction uses aos-context.compact_context
    (only droppable roles are removed). Protected content (task, acceptance,
    constraints, retries, findings, security, unresolved) is never truncated; if
    the state is still RED after compaction the call fails closed (ValueError).

budget_check(policy, records, estimate, premium=False, task_id=None, now=None)
    Enforce max_task_cost / daily_budget / monthly_budget / premium_budget from
    `policy` against recorded measured costs plus the reserved `estimate`. All
    windows are UTC. A missing measured cost under a configured cap, or a missing
    reserved estimate under a configured cap, fails closed; over-budget fails
    closed. Premium has no bypass: premium spend checks premium_budget separately.

aggregate(records) -> dict
    Cheap/mid/premium workload with explicit events and tokens denominators,
    premium_execution_ratio, average_cost_by_tier and cost_per_successful_task.
    Missing costs remain null (never silently zero).

summarize_runtime(config, available=()) -> dict
    Read-only report of disabled-runtime reasons, cost classes, the winner and
    context/learning status. Never reads credentials; the config is the data.
"""
import importlib.util
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_CONTEXT_SPEC = importlib.util.spec_from_file_location("aos_context", ROOT / "bin/aos-context.py")
ctx = importlib.util.module_from_spec(_CONTEXT_SPEC)
_CONTEXT_SPEC.loader.exec_module(ctx)

TIERS = ("CHEAP", "MID", "PREMIUM")

# Fields a structured handoff may carry from the pipeline state. This is the full
# safety/continuation surface an executor needs to resume, plus run_dir/directory
# so the host can reattach to a run. Everything else in the state — the event
# history, arbitrary file blobs, credentials — is deliberately excluded so a
# handoff can never leak a full repository or a secret.
HANDOFF_FIELDS = (
    "task", "plan", "checks", "findings", "verdicts", "failures",
    "stage", "role", "model", "subtask", "security_constraints", "modified_files",
    "acceptance_criteria", "do_not_modify", "unresolved_failures",
    "security_findings", "constraints", "retries", "reviewer_findings",
    "escalation", "escalation_pending", "premium_execution_reason",
    "premium_execution_used", "current_execution_premium", "primary",
    "fallback", "mid", "attempts", "open_retry_count", "review_rounds",
    "escalation_events",
    "role_models", "verification", "fix_findings", "main_host",
    "executor_runtime", "classification",
    "run_dir", "directory",
)

_LIFECYCLE = ("evaluate", "compact")
_RUNTIME_KEYS = ("codex-cli", "claude-code")
_RUNTIME_BINARY = {"codex-cli": "codex", "claude-code": "claude"}
_BINARY_TO_KEY = {binary: key for key, binary in _RUNTIME_BINARY.items()}


def _items_text(items):
    parts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        if isinstance(content, str):
            parts.append(content)
    return "\n\n".join(parts)


def _item_signature(item):
    content = item.get("content")
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return (item.get("role"), content)


def _validate_no_conflicting_duplicates(items):
    """Reject duplicate ids whose payloads differ, before dedup can drop data."""
    seen = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = item.get("id")
        if iid is None:
            continue
        signature = _item_signature(item)
        if iid in seen and seen[iid] != signature:
            raise ValueError(
                "conflicting duplicate item id %r would silently discard protected data"
                % (iid,))
        seen[iid] = signature


def prepare_context(model, items, state, config_path=None):
    """Evaluate, compact when ORANGE/RED, and hand off the task state.

    Returns {"prompt", "context", "handoff"}. `handoff` is None (and telemetry
    `handoff_created` False) only when no `state` is supplied.
    """
    items = list(items)
    _validate_no_conflicting_duplicates(items)
    before_text = _items_text(items)
    eval_before = ctx.evaluate(model, text=before_text, config_path=config_path)

    state_label = eval_before.get("context_state")
    compaction_triggered = state_label in ("ORANGE", "RED")
    compaction = None
    kept = items
    if compaction_triggered:
        compaction = ctx.compact_context(items)
        kept = list(compaction["kept"])

    after_text = _items_text(kept)
    eval_after = ctx.evaluate(model, text=after_text, config_path=config_path)
    if eval_after.get("context_state") == "RED":
        raise ValueError(
            "context still RED after structural compaction; protected content must not "
            "be truncated — decompose the task or start a new session")

    handoff_created = isinstance(state, dict) and bool(state)
    handoff = None
    if handoff_created:
        handoff = {"schema": 1}
        for field in HANDOFF_FIELDS:
            if field in state:
                handoff[field] = state[field]
        handoff["context"] = {
            "state": eval_after.get("context_state"),
            "tokens": eval_after.get("context_tokens"),
            "token_source": eval_after.get("context_tokens_source"),
            "compaction_triggered": compaction_triggered,
        }

    context = {
        "model": model,
        "context_state_before": eval_before.get("context_state"),
        "context_state_after": eval_after.get("context_state"),
        "context_tokens_before": eval_before.get("context_tokens"),
        "context_tokens_after": eval_after.get("context_tokens"),
        "context_tokens": eval_after.get("context_tokens"),
        "context_tokens_source": eval_after.get("context_tokens_source"),
        "target_context": eval_after.get("target_context"),
        "soft_limit": eval_after.get("soft_limit"),
        "hard_limit": eval_after.get("hard_limit"),
        "technical_context_limit": eval_after.get("technical_context_limit"),
        "context_utilization_ratio": eval_after.get("context_utilization_ratio"),
        "model_context_policy_source": eval_after.get("model_context_policy_source"),
        "context_state": eval_after.get("context_state"),
        "measurement_scope": "prompt_only",
        "compaction_triggered": compaction_triggered,
        "compaction": compaction,
        "handoff_created": handoff_created,
    }
    return {"prompt": after_text, "context": context, "handoff": handoff}


def _utc_now(now):
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _number(value, name):
    if value is None:
        return None
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError(name + " must be a finite nonnegative number")
    return float(value)


def _record_time(record):
    if not isinstance(record, dict):
        raise ValueError("budget record must be an object")
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw.strip():
        raw = record.get("created_at")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("budget record has no usable timestamp")
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _record_cost(record):
    if not isinstance(record, dict):
        raise ValueError("budget record must be an object")
    return _number(record.get("cost"), "record cost")


def budget_check(policy, records, estimate, premium=False, task_id=None, now=None):
    """Enforce configured task/daily/monthly/premium cost caps (UTC, fail closed)."""
    if not isinstance(policy, dict):
        raise ValueError("budget policy must be an object")
    now = _utc_now(now)

    caps = {
        "max_task_cost": _number(policy.get("max_task_cost"), "max_task_cost"),
        "daily_budget": _number(policy.get("daily_budget"), "daily_budget"),
        "monthly_budget": _number(policy.get("monthly_budget"), "monthly_budget"),
        "premium_budget": _number(policy.get("premium_budget"), "premium_budget"),
    }
    for name, cap in caps.items():
        if cap is None and policy.get(name) is not None:
            # A configured-but-invalid cap must not silently disable enforcement.
            raise ValueError(name + " must be a nonnegative number")

    estimate = _number(estimate, "reserved estimate")

    # Parse every record once so a bad timestamp is a hard stop, not a silent skip.
    parsed = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("budget record must be an object")
        parsed.append((record, _record_time(record), _record_cost(record)))

    def day_window(entry):
        _, when, _ = entry
        return (when.year, when.month, when.day) == (now.year, now.month, now.day)

    def month_window(entry):
        _, when, _ = entry
        return (when.year, when.month) == (now.year, now.month)

    def is_premium(entry):
        record, when, _ = entry
        return (bool(record.get("premium")) or record.get("cost_class") == "PREMIUM") and month_window(entry)

    measured = {
        "max_task_cost": [entry for entry in parsed
                          if task_id is not None and entry[0].get("task_id") == task_id],
        "daily_budget": [entry for entry in parsed if day_window(entry)],
        "monthly_budget": [entry for entry in parsed if month_window(entry)],
        "premium_budget": [entry for entry in parsed if is_premium(entry)],
    }

    reserved = {
        "max_task_cost": estimate,
        "daily_budget": estimate,
        "monthly_budget": estimate,
        "premium_budget": estimate if premium else 0.0,
    }

    budgets = {}
    for name, cap in caps.items():
        if cap is None:
            continue
        relevant = measured[name]
        if any(entry[2] is None for entry in relevant):
            raise ValueError(name + " cannot be enforced with unknown measured cost")
        if reserved[name] is None:
            raise ValueError(name + " cannot be enforced with unknown reserved estimate")
        measured_total = sum(entry[2] or 0.0 for entry in relevant)
        total = measured_total + reserved[name]
        budgets[name] = {
            "limit": cap,
            "measured": measured_total,
            "reserved": reserved[name],
            "total": total,
            "remaining": round(cap - total, 4),
        }
        if total > cap:
            raise ValueError(name + " exceeded: " + "%.4f" % total + " > " + "%.4f" % cap)

    return {
        "allowed": True,
        "now": now.isoformat().replace("+00:00", "Z"),
        "premium": bool(premium),
        "task_id": task_id,
        "reserved_estimate": estimate,
        "budgets": budgets,
    }


def _record_tokens(record):
    """Use explicit role-event tokens, else a fully measured ledger pair."""
    if "tokens" in record:
        return record["tokens"]
    incoming, outgoing = record.get("input_tokens"), record.get("output_tokens")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 for value in (incoming, outgoing)):
        return None
    return incoming + outgoing


def _tier_total(records, field):
    """Per-tier sum of `field`, None if any included value is None."""
    totals = {tier: [] for tier in TIERS}
    for record in records:
        if not isinstance(record, dict):
            continue
        tier = record.get("cost_class")
        if tier not in TIERS:
            continue
        value = _record_tokens(record) if field == "tokens" else record.get(field)
        totals[tier].append(value)
    result = {}
    for tier, values in totals.items():
        result[tier] = None if any(v is None for v in values) else sum(values)
    return result


def aggregate(records):
    """Cheap/mid/premium workload and cost metrics; missing costs stay null.

    Unknown cost classes are excluded from the tier denominators but reported in
    an explicit ``unknown`` bucket so uncategorised work is never silently lost.
    ``cost_per_successful_task`` includes the cost of failed attempts: it is the
    total spend divided by the number of successful tasks.
    """
    records = list(records)
    event_sums = {tier: 0 for tier in TIERS}
    unknown_records = 0
    unknown_events = 0
    unknown_tokens = []
    unknown_costs = []
    for record in records:
        if not isinstance(record, dict):
            continue
        tier = record.get("cost_class")
        if tier not in TIERS:
            unknown_records += 1
            events = record.get("events", 1)
            if not isinstance(events, int) or isinstance(events, bool) or events < 0:
                raise ValueError("event counts must be nonnegative integers")
            unknown_events += events
            unknown_tokens.append(_record_tokens(record))
            unknown_costs.append(record.get("cost"))
            continue
        events = record.get("events", 1)
        if not isinstance(events, int) or isinstance(events, bool) or events < 0:
            raise ValueError("event counts must be nonnegative integers")
        event_sums[tier] += events
    total_events = sum(event_sums.values())

    token_sums = _tier_total(records, "tokens")
    total_tokens = None if any(v is None for v in token_sums.values()) else sum(token_sums.values())

    cost_sums = _tier_total(records, "cost")

    by_events = {tier: (round(event_sums[tier] / total_events, 4) if total_events else None)
                 for tier in TIERS}
    by_tokens = {tier: (round(token_sums[tier] / total_tokens, 4)
                        if total_tokens not in (None, 0) and token_sums[tier] is not None else None)
                 for tier in TIERS}
    premium_execution_ratio = (round(event_sums["PREMIUM"] / total_events, 4)
                               if total_events else None)

    average_cost_by_tier = {}
    for tier in TIERS:
        numerator = cost_sums[tier]
        denominator = event_sums[tier]
        average_cost_by_tier[tier] = (round(numerator / denominator, 4)
                                      if numerator is not None and denominator else None)

    work_records = [r for r in records if isinstance(r, dict)
                    and r.get("cost_class") in TIERS]
    successful = [r for r in work_records if r.get("success") is True]
    if successful and all(r.get("cost") is not None for r in work_records):
        cost_per_successful_task = round(sum(r["cost"] for r in work_records) / len(successful), 4)
    else:
        cost_per_successful_task = None

    unknown = {
        "records": unknown_records,
        "events": unknown_events,
        "tokens": None if any(v is None for v in unknown_tokens) else sum(unknown_tokens),
        "cost": None if any(v is None for v in unknown_costs) else sum(unknown_costs),
    }

    return {
        "by_events": by_events,
        "by_tokens": by_tokens,
        "premium_execution_ratio": premium_execution_ratio,
        "average_cost_by_tier": average_cost_by_tier,
        "cost_per_successful_task": cost_per_successful_task,
        "unknown": unknown,
        "totals": {
            "events": total_events,
            "tokens": total_tokens,
            "cost": None if any(v is None for v in cost_sums.values()) else sum(cost_sums.values()),
        },
    }


def summarize_runtime(config, available=()):
    """Read-only runtime summary; never reads credentials or env."""
    if not isinstance(config, dict):
        raise ValueError("runtime config must be an object")
    available_keys = set()
    for name in available:
        key = _BINARY_TO_KEY.get(name, name)
        if key in _RUNTIME_KEYS:
            available_keys.add(key)

    executors = config.get("executors", {}) if isinstance(config.get("executors"), dict) else {}
    status = executors.get("runtime_status", {}) if isinstance(executors.get("runtime_status"), dict) else {}

    runtimes = {}
    disabled = []
    for key in _RUNTIME_KEYS:
        entry = status.get(key) if isinstance(status.get(key), dict) else {}
        open_execution = entry.get("open_execution", True)
        reason = entry.get("reason") if open_execution is False else None
        runtimes[key] = {
            "available": key in available_keys,
            "binary": _RUNTIME_BINARY[key],
            "open_execution": open_execution,
            "disabled_reason": reason,
        }
        if reason is not None:
            disabled.append({"runtime": key, "reason": reason})

    catalog = config.get("model_catalog", {}) if isinstance(config.get("model_catalog"), dict) else {}
    classes = {}
    for model, entry in catalog.items():
        if isinstance(entry, dict):
            classes.setdefault(entry.get("cost_class"), []).append(model)

    open_ = config.get("open", {}) if isinstance(config.get("open"), dict) else {}
    primary = open_.get("primary")
    primary_entry = catalog.get(primary, {}) if isinstance(primary, str) and isinstance(catalog.get(primary), dict) else {}
    benchmark = primary_entry.get("benchmark", {}) if isinstance(primary_entry.get("benchmark"), dict) else {}
    winner = {
        "model": primary,
        "fallback": open_.get("fallback"),
        "cost_class": primary_entry.get("cost_class"),
        "source": open_.get("source"),
    }

    context_policy = config.get("context_policy", {}) if isinstance(config.get("context_policy"), dict) else {}
    context = {
        "configured": bool(context_policy),
        "model_classes": sorted(k for k in (context_policy.get("model_classes") or {}).keys()),
        "defaults": (context_policy.get("defaults") or {}) if isinstance(context_policy.get("defaults"), dict) else {},
    }

    learning = {
        "benchmark_winner": benchmark.get("status") == "winner",
        "source": open_.get("source"),
    }

    return {
        "runtimes": runtimes,
        "disabled": disabled,
        "classes": classes,
        "winner": winner,
        "context": context,
        "learning": learning,
    }
