# Context budget

Do not fill a model's context window to its technical limit: past a point the
model degrades — worse answers, higher latency, higher cost, lost relevance, a
stale history, and a compaction forced too late to be useful. AOS treats the
technical window as a ceiling it reports against, never as an operating target.

The manager is `bin/aos-context.py`. It is provider/model aware, configurable and
never hardcoded: policy lives in `config/open-models.json` under `context_policy`.
It never talks to a provider — real usage is read from the runtime stream by the
caller; text or character counts are marked `estimated`, never passed off as
measured.

## Limits

| Field | Meaning |
|-------|---------|
| `technical_context_limit` | maximum the model supports (informational; reported in ratios only) |
| `target_context` | the ideal working size — stay under it on purpose |
| `soft_limit` | beyond here AOS must start pruning/compaction |
| `hard_limit` | never keep accumulating past here without intervention |

The resolver chain, in order, records which level matched
(`model_context_policy_source`):

1. `context_policy.models.<provider/model>` — model-specific;
2. `context_policy.model_classes.<class>` — class (deepseek, qwen, claude, …);
3. `context_policy.defaults` — global fallback.

A model with no matching policy uses the fallback and says so. A missing or
invalid configuration disables enforcement (backward compatible): the manager
reports no limits and never refuses work.

## States

| State | Condition | Action |
|-------|-----------|--------|
| GREEN | ≤ target | none |
| YELLOW | ≤ soft | note — prefer targeted retrieval, stop re-inserting what is already there |
| ORANGE | ≤ hard | compact — cleanup, structured compaction; new session if separable |
| RED | > hard | compaction is mandatory; a new session/subtask is the alternative |

## Metering

`measure(tokens=…)` is `measured`; `measure(text=…)`/`measure(chars=…)` is
`estimated` (4 chars ≈ 1 token, a rough order-of-magnitude, never pretended to be
exact). Prefer real usage from the provider when it exists.

The pipeline keeps both numbers per role call. `context_tokens` is the estimate of
the prompt AOS supplied; `observed_input_tokens` is what the runtime reported back
(input plus cache reads and writes, since a cached prompt is still context the
provider processed); `hidden_context_tokens` is the difference — the runtime's system
prompt, tool schemas, files the role read on its own. It is null when the runtime
reported nothing, never zero. Measured on 2026-09-22 (docs/verifiche/premium-plan-open-execute/live/claude-host-t2-state.json):
a 441-token planner prompt became 28 914 observed tokens; a 2 596-token review prompt
became 200 958, because the read-only reviewer reads the repository itself. The
estimate therefore governs admission of what AOS sends; the observed number is what
to look at when a role runs out of context.

## Compaction

Compaction is structural, not a free-text summary. Items are `{id, role, content}`,
and the manager drops only roles it names as droppable — any role it does not
recognize is kept, so reducing context can never remove something it cannot name.

Droppable: `resolved_log`, `verbose_output`, `superseded_hypothesis`,
`irrelevant_file`, `explained_traceback`, `repetitive_discussion`,
`unnecessary_tool_output`, `stale_snapshot`.

Everything else survives, including the items that must never be lost: the task,
acceptance criteria, architectural and taken decisions, relevant and modified
files, diff summary, failed and passed tests, open errors, constraints, security
constraints, the permission profile, the model/runtime, retries, verify-agent
findings and any pending escalation. Duplicates are collapsed by `id`, keeping the
first occurrence. A resolved log can go; an open security finding cannot.

## Handoff

`build_handoff(…)` (or `aos-context.py handoff`, JSON spec on stdin) produces the
structured state a new session, subtask, model or runtime needs to continue:
task, acceptance criteria, decisions, files (relevant and modified), tests
(passed/failed), open issues, security constraints, retry count, executor, review
findings, pending escalation and the current context state. It complements the
measure record — that is a measurement, this is the transfer.

## Long tasks and T3

Do not try to hold a whole T3 in one session. Prefer: planning session →
decomposition → bounded subtasks → structured handoff → final integration →
premium/open review, so each piece stays inside a controlled window.

## Telemetry

Each evaluation reports `context_tokens`, `context_tokens_source`, `target_context`,
`soft_limit`, `hard_limit`, `context_utilization_ratio`, `context_state`,
`model_context_policy_source` and the recommended action (with `mandatory`). Each
compaction reports `tokens_before_compaction`, `tokens_after_compaction` and item
counts. The main session records these in the task record to see whether budget
management moves first-pass success, retries, cost, latency or escalation.
