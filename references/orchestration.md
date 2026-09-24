# Orchestration and compatibility

Load for overlapping skills, missing capabilities or consequential delegation
choices. The live session catalog is authoritative; do not preload an inventory
of every installed collection or assume historical version/path information.

## Choose one owner per job

| Job | Default | Use an alternate when |
|-----|---------|-----------------------|
| Clarify requirements | superpowers:brainstorming | User requests gstack spec or another specific method |
| Plan | superpowers:writing-plans | User invokes Wayfinder for a persistent decision map |
| Diagnose | superpowers:systematic-debugging | diagnosing-bugs for a hard performance/multi-system diagnosis; investigate if requested |
| Behavior tests | superpowers:test-driven-development | User requests tdd/integration-first style |
| Standards/spec review | code-review | gstack review for its landing workflow |
| Cross-model verification | verify-agent | No same-family substitute; use quality-gates fallback if unavailable |
| Web QA/design/release | Relevant specialist from current catalog | Match user request and project instructions |

A phase already satisfied with current evidence does not need a second workflow.
Explicit user requests still win. A code-review request may include parallel
reviewers as instructed by that skill; do not add another equivalent review.
T2/T3 role pipelines require cross-model review even below HIGH. Simple tasks retain deterministic verification.

## Availability and host adaptation

1. Use the current catalog. If absent, search through skill-library once for the
   needed capability; read its original SKILL.md, following symlinks. **A catalog hit
   is a claim about a path, not proof the skill is there**: the index is generated once
   and goes stale when a collection moves. Measured on 2026-09-15: 20 of 399 entries
   pointed into a Codex plugin cache that no longer exists, while every one of those
   skills was installed and reachable elsewhere. A dead path means the catalog is old,
   never that the capability is missing — check the host's live catalog before saying so.
   **Do not hand-edit the index to make it look fresh:** it is generated, it belongs to
   a runtime snapshot, and repairing twenty rows by hand leaves it diverging from what
   the next regeneration produces. Regenerate it, or leave it stale and say so.
2. Translate host operations, not names literally: Read/Grep/Bash to local tools;
   Agent/Task only to authorized runtime delegation; questions to host input tools.
3. Check model-invocation restrictions. A user-only command cannot be launched
   indirectly through a shell or another agent.
4. If no suitable skill exists, perform the underlying discipline inline and state
   the missing capability once when material. Missing tooling never removes a gate.

AOS is user-maintained. Do not edit third-party skills or install speculative
replacements. There is one installation, `~/.claude/skills/aos`, which is also the
repository; `~/.agents/skills/aos` is a link to it, so a committed change is what both
hosts read. `bin/aos-install.sh --host codex --link` creates or repairs the link and
`aos-doctor.py` reports a real directory on that path as `COPIA`: two copies were the
defect, not their drift, and a copy left there is a Codex reading an older AOS,
invisibly from inside either session. Do not transplant host permissions/hooks.

## Wayfinder

Wayfinder is user-invoked (`disable-model-invocation: true`), not callable by AOS.
For unresolved work larger than a session, suggest `/wayfinder` once. If the user
continues here, use brainstorming and writing-plans with the project's existing
state record. This does not reproduce Wayfinder's issue-tracker map/frontier.
Do not turn a suggestion into a stop when useful authorized work remains.

## Delegation economics

Use the host's permission rules first. Authorization to use an orchestration skill
does not authorize arbitrary external messages, model changes or extra workers.

- **Local execution:** default for coupled changes and T0/T1. One owner avoids
  duplicate orientation and merging.
- **Advisor:** a narrow unresolved decision with alternatives and relevant evidence;
  request a recommendation and risks, not a second implementation. Use only when
  authorized and a capable advisor is actually available.
- **Workers:** independently testable deliverables, distinct ownership, stable
  interfaces. Delegate only when parallel benefit exceeds setup and coordination.
  The parent continues useful independent work, then inspects returned evidence.
- **Reviewer:** evaluates a finished artifact against the original contract.
  Same-family agents may provide another perspective; only the opposite family
  satisfies AOS's cross-model gate.

Minimal packet: objective, exact source/artifact paths, applicable constraints,
permitted side effects, acceptance criteria, expected evidence. A returning worker
reports changed files, checks and unresolved facts. Send deltas for follow-ups.
Do not fork the entire conversation by default or ask workers to rediscover plans.
Do not launch reviewer chains. Respect the existing 3/5/6 limits.

## Model routing

**AOS is the router.** It decides which task executor runs a task and with which
review, from tier, risk, complexity, uncertainty, security impact, the capabilities
the task needs, the configured benchmark winner and the retry/failure history. The
manual main model of the host session does **not** decide the executor: it is a
fallback runtime model, and it executes only on an explicit user override or in the
cases the routing policy reserves for it. The decision function is
`bin/aos-router.py` (`decide` — pure and tested, decision cases); the executable policy
lives in `config/open-models.json`. Never hardcode a provider ranking, benchmark
result or price into AOS: names and winners are configuration, the rule is the code.

Three models must not be confused:

- **Main session model** is the model the host (Claude Code or Codex CLI) opened
  with. It coordinates, verifies and arbitrates; the AOS line on the response
  identifies the selected executor. The classifier uses a separate tool-free
  invocation of the open model. Premium roles run on the host's own subscription CLI
  in the same working directory.
- **Task executor model** comes from `bin/aos-router.py` and
  `config/open-models.json`. Bounded open work runs through
  `bin/aos-open-executor.py`: the open model inside the Claude Code harness with
  file tools only, guarded by `aos-delegate.py`. A repository under a `.claude` or
  `.codex` path is refused before any spend: the harness marks those paths
  sensitive and denies every write, whatever the permission mode. Delegate from a
  worktree outside them (`git worktree add --detach <path>`).
- **Premium executor** is the main session itself when the router's family matches
  the host; the other family's CLI runs only when the router names that family.
  When the router returns `pipeline: true`, follow `aos-entry.py pipeline`
  (§Explicit role pipeline); otherwise the full loop of SKILL.md §4.
- **Delegated model** handles a bounded independent subtask selected under AOS.
  Premium executors may decompose work but must not route the same parent request
  back to the pipeline.

OpenCode was removed from the project on 2026-09-22 (entry plugin, installer, runner
and its permission-map isolation): its probe leaked `.env`, symlink and external
reads, and the two subscription CLIs already cover every role.

Routing defaults (`bin/aos-router.py` decides; config changes policy):

| Tier | Planner | Executor | Reviewer | Fixer |
|---|---|---|---|---|
| T0 | none | host session: LOW/MEDIUM on the host's cheaper subagent (`host_subagents` in the policy), HIGH on the main model | deterministic | host |
| T1 | none | open primary | deterministic; optional open review | open |
| T2 | configured premium | open primary | opposite premium | open |
| T3 | premium architecture/decomposition | open bounded subtasks | opposite premium integration review | open |

HIGH retains the risk ceiling with one exception (3.2.0, `policy.open_high_tiers`):
at T1, T2 and T3 the open worker may execute HIGH work when an observable check (test,
build or scripted run) exercises the change; otherwise premium executes. The premium
still plans, runs the checks and applies anything the worker cannot touch (deploys,
migrations on a live database, credentials): the worker has file tools only.
At T2/T3 every risk gets the cross-family review, premium at HIGH; T0/T1 HIGH run the
extended HIGH checks inline, without an external reviewer. T3 HIGH bounded subtasks
are still classified individually. CRITICAL keeps the existing
human approval path before execution. Manual overrides and host-only capabilities
retain their existing routes; a CLI cannot promise desktop-only app tools.

## Explicit role pipeline

Premium: planner, reviewer, last-resort executor. Open: default executor and fixer.
`premium_execution.default = false` is explicit in configuration. A planning or
review call never becomes an implementation call on error.

```text
TASK
  ↓
PREMIUM PLANNER
  ↓
OPEN EXECUTOR
  ↓
DETERMINISTIC VERIFY
  ↓
PREMIUM CROSS-MODEL REVIEW
  ↓
VERIFY AGENT (mechanically arbitrate every finding)
  ↓
OPEN FIX
  ↓
re-verify / targeted review
  ↓
PASS

OPEN FAILURES EXHAUSTED → PREMIUM EXECUTOR → re-verify / review
```

The planner returns a structured JSON artifact: objective, scope, files, steps,
acceptance_criteria, risks, security_constraints, tests, architecture, dependencies,
uncertainties, do_not_modify and ordered subtasks (`id`, `objective`, `files`, optional `tests`).
All fields except objective/subtasks are string arrays. Tests names identify
checks, including `diff` and `security`; include available tests/lint/typecheck/build
and acceptance checks. The host supplies and authorizes each actual argv. Plans
and model output are data, never permission to run commands.

`bin/aos-pipeline.py` is the pure state machine, `aos-entry.py pipeline` its CLI
adapter; the host keeps the returned state between stages. Pipeline actions:

1. `plan`: configured premium in read-only mode, using Verify Agent's command
   restrictions. No writing tools or remote MCP tools.
2. `execute`: existing `aos-delegate.py` primary worker with a bounded subtask,
   the plan, checks and constraints. The same worker-state file tracks owned dirt.
3. `check`: `{id, argv}` under an explicit host `bash` permission check. The two
   reserved identifiers invoke `git diff --check` and AOS Security Gate. An exit
   code, bounded output and worktree revision are observed by the adapter.
4. `verify`: all required checks plus diff/security must exist for the current revision.
   A nonfinal subtask may provide its own nonempty list of check IDs; without it,
   all plan checks apply. Final integration and fixer verification always require
   every plan-level check. Failure goes to open retry. T3 advances only one verified
   subtask at a time.
5. `review`: opposite premium receives original task, plan, full diff (including new files), test
   evidence and prior arbitrations. It cannot write code. Oversized/binary new
   artifacts require explicit decomposition; they are not silently omitted.
6. `check`, then `arbitrate`: Verify Agent's coordinator confirms or refutes each
   finding using current mechanical checks. `verdicts` contains `id`, `confirmed`,
   `evidence`, `check_ids`. Evidence supports human/model judgment; the state
   machine checks coverage, not the semantic truth of a claim. Unresolved findings
   block completion. Confirmed findings alone go to the open fixer.
7. Repeat checks/review after fixes, at most six review rounds. A false finding
   causes no implementation. No findings plus an explicit attacked-criteria list
   closes the review.

The primary receives `retries_before_escalation` attempts, then the configured
fallback receives the same budget. Execution and fixer attempts are separate;
permission denials, dirty-tree refusal and repository-metadata tampering are
blockers, not retries. `escalate` is available only after exhaustion and requires
host permission before calling the existing premium executor. Capability/risk
exceptions and absent providers keep legacy routing; automatic historical-success
escalation is not enabled without a validated category dataset.

Codex planner prefers Claude reviewer; Claude planner prefers Codex reviewer.
One unavailable family means an incomplete cross-model gate, never a disguised
same-family PASS. With no premium subscription a complex plan/review blocks;
T0/T1 open work remains available. With no open configuration legacy main/premium
routing remains available. Model names remain exclusively configuration-driven.

The coordinator must not implement via native tools while a pipeline owns changes.
Workers retain all existing denylist and permission restrictions. Stage calls are
serialized; an interrupted/failed side-effecting stage needs inspection before an
explicit new request. Completed stage output can restore state after a server
restart; no automatic replay of an unfinished worker. New user turns reclassify
and do not silently reuse previous authorization. Existing dirty work may require
an isolated checkout before restarting; never use `--allow-dirty` as a shortcut.

Claude/Codex direct hosts use the same contract and `aos-entry.py pipeline` JSON
in/out stages when useful, invoking check commands only under their own shell
permissions. They may orchestrate manually using AOS/Verify Agent; this is not
permission to route the same parent request back to the pipeline. Host-only tools
stay on their actual runtime. Driving the stages is not proof that every model
follows a plan perfectly.

### Role measurements

Save observed state and import it with `aos-measure.py finish --pipeline <path>`.
`start` takes only the main executor identity (`--main-executor-runtime/model/provider`,
`--routed-by-aos`, `--manual-model-override`); the role fields arrive at `finish`,
from `--pipeline` or from explicit counters (`--open-executor-tokens`,
`--premium-executor-tokens`, `--premium-review-tokens`, `--planner-tokens`,
`--delegated-open-tasks`,
`--escalation-count/-reason`). Explicit counters win over the imported ones, and the
ratios are computed once from the final values. The record then exposes
`planner_model/provider/tokens`, `executor_*`, `reviewer_*`, `fixer_*`,
`premium_execution_used/reason`, `cross_model_review`, finding counts,
`open_retry_count`, `estimated_premium_tokens_saved`, `main_executor_*`,
`routed_by_aos`, `manual_model_override`, `delegated_open_tasks`,
`open_executor_tokens`, `premium_executor_tokens`, `premium_review_tokens`,
`escalation_count/reason`, `workload_open_ratio` and `premium_dependency_ratio`.
`role_events` retains per-call model/provider/usage, including fallback workers;
flat role fields show the last model and total known tokens for that role.
`premium_executor_tokens` is separate from planner/reviewer tokens.
`workload_open_ratio` covers execution plus fixes; `premium_dependency_ratio`
includes premium planning, review and execution in the total model-token share.
Token ratios use the reported input_tokens + output_tokens counters; provider cache
semantics differ, so these are operational indicators, not billed-token or time
shares. Raw usage is retained for audit. Unknown counters keep the ratio null.
Finding counts expose confirmed/refuted
claims and reviewer false positives; retries are recorded separately. Interpret
planner comparisons as observed task outcomes, not causal quality rankings.
`estimated_premium_tokens_saved` stays null until a comparable measured premium
baseline exists. Premium subscriptions do not provide per-task dollar costs;
only reported open cost is measurable. Failed/interrupted premium calls may lack
usage and cannot be counted as zero cost or a completed review.

Open execution mechanics: `bin/aos-open-executor.py --repo <path> --brief <file>
--runtime claude-code` runs the configured open model inside the harness and prints exit code, diff stat, seconds, usage as the CLI reported it (never
estimated) and the tool calls it made; it never runs tests or commits. The harness
gets Read/Glob/Grep/Edit/Write, pre-approved with `--allowedTools`; the host runs
every check. The Codex adapter is still in the code and disabled by policy. `aos-delegate.py` keeps the guards around each
attempt: clean tree or worker-owned dirt only, retry ownership state, git metadata
fingerprint (config, info, hooks, includes, pointers — no git of ours runs on a repo
whose metadata the worker changed), process-group timeout, step cap. A repo or
ancestor carrying its own `.claude`/`.codex` configuration is refused before a
worker runs. Nothing secret or production-bound belongs in a repo handed to a worker.
The provider credential comes from the `api_key_env` variable or the `api_key_file`
named in `config/open-models.json`, read by the host and passed to the harness
process; it never appears in versioned files, command arguments or the worker's
tools. Model availability must be observed, not assumed: a configured endpoint is a
claim, not proof it answers. When the router's open branch is chosen, the main
session confirms the model responds before trusting its result; a failing open run
is a failed attempt, counted.

**Premium stays on the subscriptions already paid for.** The premium executor and the
premium reviewer are the user's own Claude Code and Codex CLI sessions, covered by the
subscriptions already paid for them: escalation and cross-model review never open a
separate metered API key of their own. The open executor is the one metered path, and
it is the default the router prefers for eligible work — premium is spent where it
adds value, not by default. A missing subscription is an unavailable reviewer, which
follows the fallback below like any other unavailability.


Where the main session still handles things itself (premium planning, arbitration,
HIGH/CRITICAL, final report, override), the manual main model is used — that is the
fallback role, and it is never a routing failure. The recorded executor identity
(`main_executor_model`, `routed_by_aos`) shows who really ran each task; the
telemetry section of `aos-measure.py` carries the open/premium split.

**Host subagents (legacy and complementary):** when a bounded T0/T1 at risk ≤ MEDIUM
is delegated away from the main session on Claude Code or Codex, the working model
is the host's: Claude `Agent` takes `model` (an alias the host lists; `fork` always
inherits the parent model, use it only when the whole context is the point); Codex
`spawn_agent` accepts a model, otherwise `[agents].default_subagent_model`, otherwise
the host default; `codex exec -m` for scripted calls; `codex features list` must show
`multi_agent` enabled. Read-only search may go one step cheaper than the working
model. The verify-agent gate reviewer is the strongest model of the other family (the
router's review role below HIGH may be a MID of that family): verify-agent pins the
Claude reviewer to the strongest alias of its family; the Codex reviewer uses the
 configured model, with the reserve only on an exhausted quota, and its PASS counts
less.

## Context budget

The model chooses who runs; the budget keeps that run inside a window where the
model still works well. `bin/aos-context.py` reads `context_policy` from
`config/open-models.json` and classifies GREEN/YELLOW/ORANGE/RED from target, soft
and hard limits; compaction is structural and handoff carries the state a new
session or subtask needs. Rules and roles are in `references/context-budget.md`.
The executor's context is not the orchestrator's: a delegated worker run starts
a fresh window per task, so budget checks apply to the session that accumulates —
the main session on long T2/T3, and each premium review round. Never fill a window
to its technical limit; target and soft/hard are budgets below it.

## Independence and persistence

Codex main calls Claude Code; Claude main calls Codex, using verify-agent's
`--caller`. That external skill owns packet coverage, completion checks and verdict protocol; AOS owns
when the gate applies and its durable record. Both need Git for BRIEF/REVIEW-LOG;
only Codex CLI needs a repo cwd. Follow `quality-gates.md` for the threshold,
preconditions, arbitration, six-round cap and unavailable-review fallback.

## ICM

Use this section when organizing projects or running repeatable multi-stage work.
Source: [Interpretable Context Methodology, arXiv v2](https://arxiv.org/pdf/2603.16021v2),
[paper record](https://arxiv.org/abs/2603.16021). This is a proportionate AOS adaptation,
not a verbatim template or a claim of full upstream conformance. Use the project's adopted standard together with the portable rules below.

- **Entry and scope:** identify canonical cwd, repository and target environment.
  Read an existing area/project CONTEXT.md as a routing index, not a prompt to load
  every linked file. Follow relevant links to their authoritative source. Preserve
  operational paths, aliases and repository boundaries; do not merge repositories
  merely because they share an area or workspace.
- **One source:** keep stable instructions/references separate from plans, run data
  and outputs. Reuse existing documents rather than copying them into each stage.
  Add a short CONTEXT.md only where a missing map materially helps orientation.
- **Stage contract:** for a real repeatable sequence use the existing process
  layout, or stages/01-<name>/CONTEXT.md with references and outputs as needed.
  Declare Inputs (exact sources and upstream outputs), Process, Outputs and
  acceptance checks. Record dependencies in one direction. A small edit needs no
  numbered folders or empty scaffolding; use its existing task record.
- **Inspectable state:** distinguish runs by task/run identifier, record source
  versions or commits plus verification results, and preserve human edits as the
  next stage's inputs. Output existence alone does not establish completion.
  When an input changes, invalidate and rerun the dependent stages; reuse current
  unaffected evidence. Repair recurring errors at their authoritative source
  within the authorized scope, not by silently patching downstream artifacts.
- **Concurrent agents:** assign an owner and a distinct output/worktree boundary
  before concurrent writes. The directory layout does not provide locks or resolve
  conflicts. Follow runtime delegation permissions; no automatic agent fan-out.
- **Review and versioning:** make intermediate artifacts reviewable and apply AOS
  acceptance/security gates. Honor existing authorization; ICM does not introduce
  repeated approval requests. Follow the owner's Git policy: verified changes,
  changelog, scoped commit and push, with remote readback; private repository for
  each new owned project when authorized. Keep sensitive data/history out of the
  published payload and document concrete blockers. A remote commit is neither
  proof of deployment nor a complete backup of excluded local files.

The paper describes sequential, human-reviewable workflows. Its preliminary
observations are not a controlled benchmark or evidence of equivalent Claude/Codex
behavior; high concurrency needs additional coordination. Do not promise token
savings from folder layout. Treat automatic semantic tracing as future work, not
an implemented AOS capability. Compatibility here means shared instructions and
verified installation files; model behavior requires separate observations.


## Runtime-independent Open Executor

`bin/aos-open-executor.py` separates **role**, **runtime**, **provider**, **model**
and **main_host**. Host identity does not choose the planner family or executor.
An explicit `planner_preference` may constrain the planner family; otherwise the
catalog selects the cheapest sufficient role model. Claude Code and Codex CLI both
host the pipeline. The open harness is Claude Code, under the OS seatbelt layer
described below; the Codex adapter is present but the policy keeps it out of open
execution (see below). Claude and Codex remain the two premium families: the router
picks the planner, the reviewer is always the other family, escalation goes to
`premium.escalation_executor`.

```text
TASK → classification → appropriate PLANNER (MID / PREMIUM for T2/T3)
                                 ↓ structured plan
                         CHEAP / OPEN EXECUTOR
                      Claude Code file-tools runtime
                         benchmark primary/fallback
                                 ↓
                       DETERMINISTIC VERIFY
                                 ↓
                      CROSS-FAMILY REVIEWER
                                 ↓
                  VERIFY AGENT validates findings
                                 ↓
                        CHEAP / OPEN FIXER
                                 ↓
                           reverify → PASS

primary → retry → fallback → retry → eligible MID → last-resort PREMIUM
```

Codex CLI is not an open harness: it edits files through its shell tool, whose
command policy did not hold a negative probe, and without that shell it has no file
tools at all (probe 2026-09-22: 0 tool calls). Re-enabling it as a worker requires a
green probe taken with the shell on. An explicit runtime override cannot bypass this
block. This changes
the eligible runtime, not the historical model winner. That winner was measured under
OpenCode on 2026-09-20; under Claude Code only 3 of 20 tasks have been re-run
(`docs/misure/bench/2026-09-22-claude-code/`), so it holds by transfer, not by
measurement, until the benchmark is completed. Main host capabilities and
subscription planner/reviewer sessions remain separate.

### Isolation probe and OS layer

`bin/aos-isolation.py --runtime <r> --os-isolation none|seatbelt` is the negative
probe: a disposable fixture (git repo with `.env`, `certs/server.pem`, a symlink to a
sibling directory, a canary file in the real home, `.git` metadata) and a brief that
asks the worker to read and write every forbidden target and two permitted controls.
The verdict is mechanical — canaries in the reply or tool results, effects on disk —
and a runtime is `isolation_verified` only when all forbidden targets were attempted
and denied and both controls succeeded. A disabled runtime may run inside the probe's
fixture only; production policy is neither consulted nor changed by a probe.
Runtimes with a shell also get command targets (`curl`, `npx`).

`executors.runtime_status.<runtime>.os_isolation = "seatbelt"` wraps the worker
process in a macOS `sandbox-exec` profile (`seatbelt_profile`): allow default, deny
the home, `/Users`, temp trees and volumes, deny every write, re-allow the repo and
the run's own directories, then deny `.env*`/`*.pem`/`*.key` inside the repo and
writes to `.git`, `.claude`, `.codex`. Rules are last-match-wins on resolved paths, so
a symlink out of the repo is denied by its target and a rule on the `/tmp` symlink
itself must not appear (it shadows later allows). The provider credential is read by
the host and handed over as an environment variable, which a worker without a shell
cannot read. Codex refuses to start inside an outer seatbelt; its own workspace
sandbox is its OS layer.

Records of 2026-09-22 under `docs/verifiche/premium-plan-open-execute/isolation/`:
Claude Code 13/13 denied with and without seatbelt, both controls performed, 22 tool
calls; Codex CLI with the shell on denied the filesystem and network but ran `npx`,
and with the shell off attempted nothing. The OpenCode records are kept as history:
it leaked `.env`, the symlink target and the external file without seatbelt, and the
project dropped it.
`isolation_verified` in the policy names its record in `isolation_evidence`; the
doctor warns when the record is missing or `sandbox-exec` is absent. A green probe
is a bound on these targets on this host, not a certificate against every attack.


### Configuration and use from either host

`config/open-models.json` keeps `open.primary` and `open.fallback` unchanged.
`executors.default_runtime` selects the security-eligible default; `runtime_order`
supplies an enabled installed runtime when it is absent. `--runtime` explicitly selects a harness
without changing the configured model. Runtime availability does not prove provider
authentication. No selection silently substitutes an OpenAI or Anthropic model.

```sh
python3 bin/aos-open-executor.py --repo /path/to/project --worktree auto --brief /path/to/brief.md \
  --task-id <slug> --tier T<n> --risk <LEVEL> --runtime claude-code
python3 bin/aos-open-executor.py --repo /path/to/worktree --brief /path/to/brief.md \
  --task-id <slug> --tier T<n> --risk <LEVEL> --runtime claude-code --slot fallback
```

The brief contains the task, structured premium plan, acceptance criteria, relevant
files, security constraints and required tests. The CLI emits JSON with the role,
runtime, provider, model reference, executor model, task ID, result, changed files,
usage, cost, exit code and existing delegate evidence. `tests: []` means no host
verification has been observed yet. A worker's prose does not constitute a test PASS.

Ledger (3.2.0): each completed CLI run writes one `pending` outcome in
`~/.local/state/aos/learning.sqlite3` (project = main repository, task = `--task-id` or
the brief's stem) and prints its `ledger_event_key`. `--tier` and `--risk` are required
for that, and the run is refused before any token otherwise; `--no-record` or
`AOS_LEARNING=off` skip it (benchmarks and the pipeline call the library, which does
not record). After the host checks:

```sh
python3 bin/aos-learning.py verify-outcome --database ~/.local/state/aos/learning.sqlite3 \
  --event-key <ledger_event_key> --test-pass true|false [--failure-type <type>] [--error <text>]
```

A pending row never steers routing, the matrix or history; in KPI it counts as an
unverified task (`pending_outcomes` tallies them). A failure caused by the brief or by
AOS takes an AOS-attributed type (`context_failure`, `routing_failure`, ...), so the
model stays routable.

`--worktree auto` (3.3.0) branches `aos-open-<task>-<hex>` from the repository's `HEAD`
under `$AOS_WORKTREE_ROOT`, else `~/Progetti/Worktree`, else the system temp directory,
and runs the worker there; `source_dirty: true` warns that uncommitted work stayed
behind. The worktree is never removed by the executor: the result carries `worktree`,
`branch` and the `cleanup` command for after the merge.

At T1–T3 HIGH the router and `aos-status.py set` refuse (exit 2) unless the caller
declares `--observable-check` or `--no-observable-check "<why>"` (3.3.0): omitting the
flag used to route to premium silently. `aos-measure.py start` reads the session's status
record and fills version, tier, risk, routed and published executor and both reasons.

Publishing a different executor than the router's `open` needs
`aos-status.py set ... --override-reason "<why>"` (exit 2 otherwise, on both hosts); the
reason stays on the Claude status bar. The prompt hook appends the session model to its
hint when it is not an accepted session model (`premium.session_models`, read from the
payload or the transcript tail); a premium planner (Fable) as the session model is told it
burns premium tokens. Under Claude Code it also reminds a session whose status record
already carries a tier to route a new task again (`routed_at`, written by `set`), flags a
fix/feature prompt when no tier is recorded at all, reads the session model from the status
record (`aos-status.py model`, fed by the status line) when the transcript has none yet, and on
both hosts it reports an ORANGE/RED context from the transcript's last usage
(`context_policy`), as a `systemMessage` to the user too under Claude Code.

For T2/T3, call `aos-entry.py pipeline --directory /path/to/repo` with a JSON
`start` action and data containing `main_host` (`claude-code` or `codex-cli`),
`executor_runtime`, `classification` and `text`. Continue one authorized stage at a
time using the returned state. The host owns state and authorization; model-generated
state is not trusted. An enabled explicit open runtime satisfies its harness
capability without selecting its premium model. Host-only app/MCP
integrations remain outside the restricted worker.

The Codex adapter is configured for the provider's Responses endpoint, `wire_api=responses`,
`model_provider=aos_open`, and the model ID after its provider prefix. Claude uses
the provider's Anthropic-compatible endpoint and the same model ID. Vercel endpoints
are `/codex/v1` and `/claude-code`; other providers require explicit HTTPS endpoints
for the corresponding protocol. A generic Chat Completions-only endpoint is not
sufficient for Codex CLI. Credentials come from the configured environment-variable
name or from `api_key_file`, read by the host. Secrets never enter the versioned
policy or command arguments. Premium roles retain subscription authentication.
Vercel account-level ZDR must remain enabled: the native CLI protocols carry no
per-model retention option.

### Permission and capability boundaries

The existing delegate still enforces clean-tree/retry ownership, metadata integrity,
process-group timeout and step limits. The Bash denylist (`DENIED_BASH`, applied to
Claude's permission settings) and the Security Gate are unchanged. Harnesses refuse
project/ancestor runtime configuration; refusal is a blocker, never a reason to
escalate privileges. GREEN is the only implemented native worker profile. YELLOW
network/dependency operations require a separately authorized host action; RED and
CRITICAL do not gain permission from an adapter.

The Codex native restrictive rule layer did not enforce a negative command probe
(`npx --version` ran on 2026-09-22 while every file and network target was denied),
and Codex has no file tools outside that shell, so the policy keeps it out of open
execution entirely. Configured permission maps alone are not evidence of isolation. Global runtime
configuration and the original delegate guard are unchanged; direct legacy delegate
use is not certified by this upgrade.

Claude uses restricted/safe mode and only Read/Glob/Grep/Edit/Write tools.
**Its open adapter currently declares `shell=false`.** Negative sandbox probes found
that enabling Bash under the strict profile breaks the CLI's cwd bookkeeping. The
adapter therefore keeps Bash denied; the host's deterministic Verify stage runs tests.
This does not restrict the main Claude session or premium reviewer. A request that
requires shell capability must choose a compatible runtime. Planner and reviewer
remain read-only and cannot automatically become fixers.

A required opposite-family premium reviewer that is absent or quota-blocked leaves
the new role pipeline incomplete. Legacy single-host routing and manual override
remain available; same-family self-review is never labeled cross-model verification.

### Runtime/model benchmark and telemetry

`aos-bench.py --executors executors.json --out results` accepts a list of
`{runtime, provider, model}`; model excludes the provider prefix. Legacy `--models`
continues to work. `--planners planners.json` (`{backend, model}`) runs every
executor once more with that planner's read-only plan in the brief, labelled
`...+plan:<model>`, so first-pass with and without a plan sit side by side.
`--review-replay <results> --reviewers reviewers.json` re-reviews the recorded diffs
with candidate reviewers and counts their findings next to the reference reviewer's;
the same-file overlap in its summary is a raw indicator, and candidate findings must
be arbitrated before they count as agreement. Results retain `role=executor` and group by `runtime|provider|model`; the same model under
two runtimes is two candidates. `--no-review` explicitly records `not_run`, never
zero findings or reviewer acceptance. Revalidate the historical task before scoring:
tests must fail on the parent and pass on the reference commit.

Native CLI usage is recorded where reported. Codex does not report gateway dollars;
Claude's custom-model pricing basis can be unknown. Both costs remain null and a
requested dollar cap is rejected instead of pretended. Timeout and step caps remain.
The model catalog in `config/open-models.json` supplies provider, compatible runtimes,
cost class, capability scores, context, input/output prices and the availability of
benchmark or historical evidence. The router selects the cheapest sufficient candidate
within that data and the stated budget. The execution ladder is cheap primary, cheap
fallback, an explicitly configured MID model, then premium. No MID entry means no MID
attempt. Codex CLI is excluded from open execution until a real negative restrictive-
policy probe passes; it remains available as the premium host and reviewer.
Do not promote a runtime from connectivity tests, two-task samples, missing review
or unknown cost. The existing benchmark winner remains authoritative until a complete,
comparable benchmark and its review support changing the configuration.

Telemetry adds main_host; planner/executor/reviewer/fixer runtime, provider, model
and tokens; runtime_model_pair/benchmark_pair; open success/retry/fallback facts.
Existing premium-execution reasons, finding arbitration counts and token ratios stay.
Savings and missing billing counters remain null without a measured baseline.

Sources: [Codex provider configuration](https://learn.chatgpt.com/docs/config-file/config-reference),
[Codex permission profiles](https://learn.chatgpt.com/docs/permissions),
[Vercel Codex endpoint](https://vercel.com/docs/ai-gateway/coding-agents/openai-codex),
[Vercel Claude endpoint](https://vercel.com/docs/ai-gateway/coding-agents/claude-code),
[Claude sandbox boundaries](https://code.claude.com/docs/en/sandboxing).

## Verified learning and operational limits

The host pipeline evaluates prompt context before each role call, writes structured
handoff state and records role telemetry. `aos-operations.py` preserves protected
context and blocks remaining RED requests. Its token estimate covers the supplied
prompt, not all hidden runtime context. Cost caps are point-in-time checks; unknown
billing under an active cap blocks execution rather than skipping required review.
Concurrent sessions reserve against the caps through `aos-learning.reserve_budget`
(one `BEGIN IMMEDIATE` transaction; see [learning](learning.md)).

`aos-learning.py` admits scoped lessons only with complete observed host checks,
keeps rejected candidates, and exposes project/date-filtered reports. Application
records require evidence and a rollback reference. Model claims are never enough.
Project-scoped routing advice requires multiple independent verified tasks; it does
not overwrite the global benchmark winner. See [learning](learning.md).

The 2026-09-22 live validation used open workers followed by MID recovery for failed
bounded tasks. Native open editing and security denials were observed. Full release
readiness still requires the complete host pipelines and independent review; see
[validation](../docs/verifiche/premium-plan-open-execute/OPERATIONAL-VALIDATION.md).
