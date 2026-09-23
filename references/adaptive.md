# Adaptive orchestration (3.0)

Source: the AOS Adaptive Orchestration spec of 2026-09-23. Objective: **the most verified
result per unit of cost and context**. Not the strongest model, not the cheapest:
the cheapest model with a high enough probability of getting this result right,
counting the expected cost of retries, escalation and review.

Logic is stable, knowledge is adaptive. The code (`bin/aos-orchestrate.py`) holds
rules; `config/open-models.json` holds models, classes, tools and declared
capabilities; `config/adaptive.json` holds domains, keywords, skill routes, checks,
failure taxonomy and scoring parameters; the learning ledger holds history. No model
name in code.

## Flow

```text
request → prompt hook (keywords, 0 tokens) → census tier/risk → route:
  profile → bundles → skills → eligible models → score → executor, planner, reviewer
→ execution contract → worker (owns the bundle) → deterministic checks
→ one review → one batched fix → verify → record-outcome → matrix
```

## Profile

`route` builds a TaskProfile: domains (not exclusive), task type, capability needs
0–1 over a fixed vocabulary, and ten scores 0–1: complexity, risk_score, uncertainty,
volume, architecture_impact, security_impact, domain_specialization,
tool_requirement, context_requirement, reversibility. The census tier and risk you
announced override the derived ones: pass `--tier` and `--risk`. A richer profile
(`--profile file.json`) overrides the keyword guess; write one when the keywords
missed the point.

## Bundles: delegate outcomes, not steps

A bundle is the largest coherent unit one executor can safely own: discovery,
change, tests, debug, fix, evidence. Decompose only across different domains or
capabilities with a real dependency (requirements → implementation → joint
verification), never because the task is long. Default one writer; parallel workers
only with independent scope, independent state and a safe merge (disjoint files,
separate worktrees).

**Worker lease.** Once a bundle is handed over, the worker owns it until completion,
hard failure, timeout or contract violation. The root does not rediscover, edit the
same files, poll for updates or read intermediate logs without a reason.

**Thin premium root.** The premium model understands the problem, decides the
architecture, writes acceptance criteria and the execution contract, then reviews.
It does not read hundreds of files, write boilerplate or babysit tests. The measure
record splits premium tokens into planning, execution and review: execution is the
one to shrink.

## Choosing the executor

1. **Eligibility** (before any score): available and not DEMOTE; executor role; class
   risk ceiling (`class_max_risk`; CRITICAL only PREMIUM and always behind human
   approval); required tools (files, shell, browser, subagents, images); capability
   floor; context need within the model's hard limit; invocation (T0 never goes to
   an open worker; a host subagent must belong to the host's runtime).
2. **Score**, every term printed: effective capability blends the declared prior with
   the observed matrix by its confidence; capability match; difficulty;
   p_success, refined by overall, per-domain and per-task-type performance, each
   weighted by its confidence; context and WATCH penalties; initial cost by class
   units × volume; `expected_cost = initial + (1−p)·retry + (1−p)²·escalation`.
   The weakest domain or task type decides, whatever the order. Recent failures
   enter through the observed scores (with recency decay); a success that needed
   retries, an escalation or review findings earns reduced credit (`learning`
   factors in `config/adaptive.json`).
3. **Pick** the lowest expected cost among candidates with p_success above the risk
   threshold. None above it: the best p, flagged `below_threshold`, to report.
4. **Exploration**, at most 5%: only LOW risk, reversible, verifiable, tier ≤ T2,
   with a task id; deterministic from the id. Never on critical work.

A high score from one sample does not beat a slightly lower one from thirty:
confidence weights the observed value against the prior.

**Reviewer.** Required at T2/T3. Family opposite to the executor (to the host when
the executor is an open model); premium at HIGH; below HIGH the cheapest MID or
premium of that family with enough review capability. An open or LOW model is never
the final reviewer. No reviewer available: the declared fallback of quality-gates,
never faked independence.

## Verification by domain

Deterministic checks come before the reviewer and are not replaced by it. The
domain lists live in `config/adaptive.json` `verification`: tests/lint/build for
engineering; parties, defined terms, cross references, dates and cited norms for
legal; recomputed numbers for business; dated primary sources for research; row
counts, schema and independently recomputed totals for data.

**Batched review.** One complete review, every finding at once, one correction
bundle, one verification. Another cycle only for material findings left. No
review during execution.

## Failure taxonomy and escalation

`aos-orchestrate.py escalate --failure-type <type> --model <id>`. Understand *why*
before choosing *who*:

| Failure | Action | Counts against the model |
|---|---|---|
| reasoning_failure, hallucination | next class on the ladder | yes |
| implementation_failure, test_failure | one retry, then escalate | yes |
| instruction_failure | retry with a clarified contract | yes |
| tool_failure, timeout | retry the same model | no (infra) |
| context_failure | retry with more context | no (AOS) |
| routing_failure, capability_mismatch | reroute excluding the model | no (AOS) |
| architecture_ambiguity | premium planner | no (AOS) |
| review_failure | escalate the reviewer | yes |
| security_violation | stop and ask the human | yes |

Ladder LOW/OPEN → MID → HIGH → PREMIUM; an empty class is skipped. Escalation is not
"a stronger model" by default.

## Context

`aos-context.py zone --model <id> --profile <file>` gives the optimal zone and where
degradation starts for this model and this task (complexity, uncertainty, evidence
volume, deep reasoning shrink them; the hard limit never moves). `checkpoint when`
says whether to continue, prepare, wait for the next natural checkpoint (end of
discovery, implementation, green tests, bundle, decision) or compact now (past the
hard limit only). Before releasing a context, `checkpoint validate` must pass: goal,
current state, decisions, unresolved work, verification state, next action; logs and
duplicated tool output do not belong in it. `effectiveness` measures resume failures
and rediscovery: if every compaction forces the worker to rediscover the repository,
the compaction is wrong.

## Learning

Every routed attempt is a datapoint (`aos-learning.py record-outcome`): model (the
catalog id, e.g. `vercel/deepseek/…`, as the pipeline records it — the router looks it
up by that id), `verification_status: verified` (anything else never moves a score),
`bundle` (the id `route` gave it, so an independent deliverable is not mistaken for a
retry; rows without it are read as one bundle, the ledger cannot tell them apart),
domain, task type, needs, failure type, retries, escalation, findings, tokens by
role, compactions, user acceptance. No artificial benchmark beyond 1–3 smoke runs
for a new model or class.

- `matrix`: per model score, sample size, confidence and last update, overall, per
  capability and per domain; recency decay (history kept for audit). Status NEW,
  KEEP, WATCH (drift: recent much worse than historical), PROMOTE, DEMOTE (only
  with enough observations and confidence, never after one failure).
- Task record: when the pipeline reaches `pass` or `blocked` it writes one row per task
  (`task_outcomes`: outcome, failed attempts, escalation, review rounds, findings,
  bundles). `kpi` takes the verdict from it; tasks without one (older or manual data) are
  still inferred from attempt rows, and `task_records` says how many were explicit. Each
  pipeline call carries an event id, so retrying a step never writes the same attempt twice.
- `kpi`: first-pass and verified success, retry, escalation, findings, cost and
  tokens per verified task, premium dependency and leverage, worker success,
  compaction rate and recovery, routing accuracy, user acceptance. Missing data is
  null, never zero.
- `recommend`: observation → hypothesis → routing recommendation, by evidence
  count. One observation is never a rule.

Quantitative adjustments are automatic because the matrix is derived from the
ledger. Changing the router's logic, classes, thresholds or domains is a release:
evidence, tests, rollback and log, like this one.

Levels of memory stay separate: session (the conversation), task (the task record
and checkpoint), project (its docs and memory), global AOS knowledge (this ledger and
the configs). A project detail is never promoted to global knowledge automatically.

## Budget

`budgets` in `config/open-models.json` bound cost; the learning ledger reserves and
settles metered calls. A budget that would make a required verification impossible
is reported as a blocker; the verification is never skipped to fit it.
