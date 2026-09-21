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
Below the external-gate threshold, no routine cross-model call.

## Availability and host adaptation

1. Use the current catalog. If absent, search through skill-library once for the
   needed capability; read its original SKILL.md, following symlinks. **A catalog hit
   is a claim about a path, not proof the skill is there**: the index is generated once
   and goes stale when a collection moves — one measured install had 20 of 399 entries
   pointing into a plugin cache that no longer existed, while every one of those skills
   was installed and reachable elsewhere. A dead path means the catalog is old, never
   that the capability is missing: check the host's live catalog before saying so.
   **Do not hand-edit the index to make it look fresh:** it is generated from a runtime
   snapshot, and repairing rows by hand leaves it diverging from what the next
   regeneration produces. Regenerate it, or leave it stale and say so.
2. Translate host operations, not names literally: Read/Grep/Bash to local tools;
   Agent/Task only to authorized runtime delegation; questions to host input tools.
3. Check model-invocation restrictions. A user-only command cannot be launched
   indirectly through a shell or another agent.
4. If no suitable skill exists, perform the underlying discipline inline and state
   the missing capability once when material. Missing tooling never removes a gate.

AOS is user-maintained. Do not edit third-party skills or install speculative
replacements. There is one installation, `~/.claude/skills/aos`; `~/.agents/skills/aos`
is a link to it, so an update there is what both hosts read. `bin/aos-install.sh --host
codex --link` creates or repairs the link and `aos-doctor.py` reports a real directory
on that path as `COPIA`: two copies were the defect, not their drift, and a copy left
there is a Codex reading an older AOS, invisibly from inside either session. Do not
transplant host permissions/hooks.

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
manual main model of the OpenCode interface does **not** decide the executor: it is a
fallback runtime model, and it executes only on an explicit user override or in the
cases the routing policy reserves for it. The decision function is
`bin/aos-router.py` (`decide` — pure and tested, decision cases); the executable policy
lives in `config/open-models.json`. Never hardcode a provider ranking, benchmark
result or price into AOS: names and winners are configuration, the rule is the code.

Three models must not be confused:

- **Main session model** is the model selected in the host interface. In the
  OpenCode entry, `chat.message` sets the native turn model before generation;
  the interface's selection label may remain unchanged. The AOS line on the
  response identifies the selected executor. The classifier uses a separate
  tool-free invocation. For premium tasks the native model only calls the bridge;
  Claude or Codex executes in the same working directory using its subscription.
- **Task executor model** comes from `bin/aos-router.py` and
  `config/open-models.json`. OpenCode native open turns preserve skills, tools and
  MCP. From Claude/Codex, bounded open work uses `aos-delegate.py` below.
- **Delegated model** handles a bounded independent subtask selected under AOS.
  Native OpenCode tasks may use subagents; premium executors may decompose work
  but must not route the same parent request back to OpenCode.

Install/check/rollback: `python3 bin/aos-opencode-install.py --help`.
The global loader imports the canonical bridge; shared skills remain in their
original roots. `--pure` deliberately disables plugins and is reserved for isolated
workers/classification, not the everyday entry. A broken classifier stops the turn.
Read `references/opencode-entry.md` for compatibility and recovery.

Routing defaults (`bin/aos-router.py` decides; config changes policy):

| Tier | Planner | Executor | Reviewer | Fixer |
|---|---|---|---|---|
| T0 | none | open primary | deterministic | open |
| T1 | none | open primary | deterministic; optional open review | open |
| T2 | configured premium | open primary | opposite premium | open |
| T3 | premium architecture/decomposition | open bounded subtasks | opposite premium integration review | open |

HIGH retains the existing risk ceiling: T2 open requires the policy's observable
check exception; otherwise premium execution is an explicit risk-policy exception.
HIGH always requires premium review. T3 HIGH is not permission to relax that ceiling;
its bounded subtasks must be classified individually. CRITICAL keeps the existing
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
adapter, and `opencode/aos-bridge.mjs` owns runtime state. `aos_pipeline` actions:

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
permission to route the same parent request back to OpenCode. Host-only tools stay
on their actual runtime. The OpenCode entry automates stage invocation, not proof
that every model follows a plan perfectly.

### Role measurements

Save observed state and import it with `aos-measure.py finish --pipeline <path>`.
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

Open execution mechanics (unchanged, `aos-delegate.py`, 2.0.1): from a clean repo,
`python3 "$AOS_DIR/bin/aos-delegate.py" --repo <path> --model <provider/model> --brief <file> --json`.
The script runs `opencode run --pure --auto --format json` and prints exit code,
diff stat, seconds and usage read from the JSON events (never estimated); it never
runs tests or commits. `--auto` approves whatever is not denied, so the per-run
config denies what a worker never needs: edits outside the repo, web tools,
subagents, and the shell commands that carry data or changes off the machine
(`curl`, `ssh`, `git push`, `git commit`, deploy CLIs — `DENIED_BASH`). The copy
keeps only the providers and the reworked permission map of the user's config, with
sharing disabled and the run's model as `small_model` too; the run's layer only, in
a temporary XDG root. Agents, tools, MCP servers, plugins, skills, instructions are
not there. A repo with its own `opencode.json`/`opencode.jsonc`/`.opencode` up to its
git toplevel is refused (exit 5): that is a guard against accidents and prompt
injection, not a sandbox — patterns match the first word (`/usr/bin/curl`, `env curl`,
`git -C . commit` pass). Nothing secret or production-bound belongs in a repo handed
to a worker. Providers live in `~/.config/opencode/opencode.json`; any
OpenAI-compatible endpoint is one block
`{"provider":{"<id>":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"…/v1","apiKey":"{file:~/.secrets/<name>}"},"models":{"<model>":{}}}}}`
with `zeroDataRetention: true` on models that receive client source. No `model` key
in that file: every run names its model. `zeroDataRetention` is a per-model option
(`models.<model>.options.zeroDataRetention`), not a provider-level key; the doctor
reports its status (`configured`/`not_configured`/`unknown`) per open model, and AOS
never claims `verified` (provider-side) or `unsupported` (schema dropped the key)
from a read-only local check. The main session runs the check. One failed
check → one retry with the failure output appended to the brief. The retry runs on
the dirty tree the previous attempt left: pass the same `--state-file` to
`aos-delegate.py` on both attempts, and the delegate records the baseline HEAD and
the paths the task owns, allowing a retry only when HEAD is unchanged and every
dirty path is worker-owned. A dirty repo before the first attempt is still refused
(exit 3); a retry that finds an external change refuses too (exit 6), never
overwriting, stashing or resetting user work. A second failure →
the fallback open model gets its retries; exhaustion escalates to premium — always
recorded in the task record (`escalation_count`, `escalation_reason`). AOS never
carries a ranking, benchmark result or price in code: the winner lives in
`config/open-models.json`, the reason in the benchmark record
(`docs/misure/bench/`), and missing/invalid config means the legacy behavior below.

Fallback chain when open is unavailable:

- `open.primary` missing/failing → `open.fallback` (Qwen today).
- both open missing/unreachable → premium escalation executor
  (`premium.escalation_executor`), never a fake open run.
- `config/open-models.json` absent or invalid → legacy: the main session executes
  the task and can still delegate bounded T0/T1 to a subagent (host working model).
- reviewer (`premium.reviewer`) absent → no cross-model premium review is claimed;
  quality-gates fallback applies. Same-family or self-review is never cross-model.

**Premium stays on the subscriptions already paid for.** The premium executor and the
premium reviewer are the user's own Claude Code and Codex CLI sessions, covered by the
subscriptions already paid for them: escalation and cross-model review never open a
separate metered API key of their own. The open executor is the one metered path, and
it is the default the router prefers for eligible work — premium is spent where it
adds value, not by default. A missing subscription is an unavailable reviewer, which
follows the fallback below like any other unavailability.

Model availability must be observed, not assumed: a provider key in `opencode.json`
is a claim about a configuration, not proof the endpoint answers. When the router's
open branch is chosen, the main session confirms the model responds to the call
before trusting its result; a failing open run is a failed attempt, counted.

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
model. Reviewers stay the strongest model of the other family: verify-agent pins the
Claude reviewer to the strongest alias of its family; the Codex reviewer uses the
 configured model, with the reserve only on an exhausted quota, and its PASS counts
less.

## Context budget

The model chooses who runs; the budget keeps that run inside a window where the
model still works well. `bin/aos-context.py` reads `context_policy` from
`config/open-models.json` and classifies GREEN/YELLOW/ORANGE/RED from target, soft
and hard limits; compaction is structural and handoff carries the state a new
session or subtask needs. Rules and roles are in `references/context-budget.md`.
The executor's context is not the orchestrator's: a delegated `opencode run` starts
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
and **main_host**. The host chooses the preferred premium planner family, not the
open model. Claude Code and Codex CLI can both host the pipeline and both serve as
open-model harnesses. OpenCode remains supported and optional.

```text
TASK → classification → PREMIUM PLANNER → structured plan
                                       ↓
                                 OPEN EXECUTOR
                         ┌─────────────┼─────────────┐
                      Codex CLI    Claude Code    OpenCode
                         └─────────────┼─────────────┘
                          configured benchmark model
                                       ↓
                           DETERMINISTIC VERIFY
                                       ↓
                       PREMIUM CROSS-MODEL REVIEW
                                       ↓
                                VERIFY AGENT
                                       ↓
                                  OPEN FIXER
                                       ↓
                                  reverify → PASS

open primary → retry → open fallback → retry → premium executor
```

### Configuration and use from either host

`config/open-models.json` keeps `open.primary` and `open.fallback` unchanged.
`executors.default_runtime` keeps the measured incumbent; `runtime_order` supplies
an installed runtime when it is absent. `--runtime` explicitly selects a harness
without changing the configured model. Runtime availability does not prove provider
authentication. No selection silently substitutes an OpenAI or Anthropic model.

```sh
python3 bin/aos-open-executor.py --repo /path/to/repo --brief /path/to/brief.md --runtime codex-cli
python3 bin/aos-open-executor.py --repo /path/to/repo --brief /path/to/brief.md --runtime claude-code
python3 bin/aos-open-executor.py --repo /path/to/repo --brief /path/to/brief.md --runtime opencode --slot fallback
```

The brief contains the task, structured premium plan, acceptance criteria, relevant
files, security constraints and required tests. The CLI emits JSON with the role,
runtime, provider, model reference, executor model, task ID, result, changed files,
usage, cost, exit code and existing delegate evidence. `tests: []` means no host
verification has been observed yet. A worker's prose does not constitute a test PASS.

For T2/T3, call `aos-entry.py pipeline --directory /path/to/repo` with a JSON
`start` action and data containing `main_host` (`claude-code` or `codex-cli`),
`executor_runtime`, `classification` and `text`. Continue one authorized stage at a
time using the returned state. The host owns state and authorization; model-generated
state is not trusted. An explicit `codex-cli`/`claude-code` open runtime satisfies
that harness capability without selecting its premium model. Host-only app/MCP
integrations remain outside the restricted worker.

Codex uses the configured provider's Responses endpoint, `wire_api=responses`,
`model_provider=aos_open`, and the model ID after its provider prefix. Claude uses
the provider's Anthropic-compatible endpoint and the same model ID. Vercel endpoints
are `/codex/v1` and `/claude-code`; other providers require explicit HTTPS endpoints
for the corresponding protocol. A generic Chat Completions-only endpoint is not
sufficient for Codex CLI. Credentials come from the configured environment-variable
name or, for migration, the existing OpenCode provider credential reference; installing
OpenCode is not necessary to read that JSON configuration. Secrets never enter the
versioned policy or command arguments. Premium roles retain subscription authentication.
Vercel account-level ZDR must remain enabled when using native CLI adapters: their
protocols do not automatically forward OpenCode's per-model provider options.

### Permission and capability boundaries

The existing delegate still enforces clean-tree/retry ownership, metadata integrity,
process-group timeout and step limits. Its OpenCode denylist and Security Gate are
unchanged. Alternative runtimes refuse untranslatable custom OpenCode permissions
and project/ancestor runtime configuration; refusal is a blocker, never a reason to
escalate privileges. GREEN is the only implemented native worker profile. YELLOW
network/dependency operations require a separately authorized host action; RED and
CRITICAL do not gain permission from an adapter.

Codex uses a workspace-only native permission profile, read-only Git/runtime metadata,
no network, no credential environment inheritance, no apps/plugins/hooks/subagents,
and task-scoped native command deny rules. The task-scoped trust map activates only
the adapter's own rules; it is removed after the run. No global runtime configuration
is modified. System toolchain directories are readable, not writable.

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
continues to work. Results group by `runtime|provider|model`; the same model under
two runtimes is two candidates. `--no-review` explicitly records `not_run`, never
zero findings or reviewer acceptance. Revalidate the historical task before scoring:
tests must fail on the parent and pass on the reference commit.

Native CLI usage is recorded where reported. Codex does not report gateway dollars;
Claude's custom-model pricing basis can be unknown. Both costs remain null and a
requested dollar cap is rejected instead of pretended. Timeout and step caps remain.
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
