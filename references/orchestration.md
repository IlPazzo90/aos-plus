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
`bin/aos-router.py` (`decide` — pure and tested, 16 cases); the executable policy
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

Routing defaults (`bin/aos-router.py` decides; config changes policy, not the code):

- **T0/LOW, T1/LOW-MEDIUM** → open executor (benchmark winner), targeted/regression check by the main session.
- **T2/LOW-MEDIUM** → open executor, deterministic verification and open review;
  premium escalation only after retry exhaustion.
- **T2/HIGH** → open **only** when policy allows it and a result the main session
  can check by observation exists; even then premium review is mandatory. Without
  an observable check, premium executes directly.
- **T3** → premium planning/architecture and premium final review; open runs the
  bounded sub-tasks.
- **CRITICAL** → unchanged: main session plus explicit approval, never routed.
- **Explicit override** ("fallo tu", the user names the main orchestration model
  for this task) → main executes; the record marks `manual_model_override: true`.
  Without it, the routing above has precedence over whatever model the interface
  opened with.

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
