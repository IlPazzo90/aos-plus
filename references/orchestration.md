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
   pointing at a skills directory a plugin had since moved out of. A dead path means the
   catalog is old, never that the capability is missing: check the host's live catalog
   before saying so.
2. Translate host operations, not names literally: Read/Grep/Bash to local tools;
   Agent/Task only to authorized runtime delegation; questions to host input tools.
3. Check model-invocation restrictions. A user-only command cannot be launched
   indirectly through a shell or another agent.
4. If no suitable skill exists, perform the underlying discipline inline and state
   the missing capability once when material. Missing tooling never removes a gate.

AOS is user-maintained. Do not edit third-party skills or install speculative
replacements. Claude and Codex copies of AOS are separate installations; use
`bin/aos-install.sh --host claude|codex --from <source>` to update with backup.
**Editing one host's copy leaves the other on the old version.** A commit is not an
install: after changing AOS, run the installer for the *other* host in the same
intervention and confirm with `aos-doctor.py`, which compares the two and reports the
files that differ. This is how one host ends up reading an instruction the other does
not have, and it is invisible from inside either session.
Do not transplant host permissions/hooks. Use `--dry-run` and verify the selected
host; preserve unrelated target state.

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

Model/effort choices are recommendations unless the runtime and user authorize
selection. Keep user overrides. Consider lower effort for bounded work only after
representative checks show adequacy; higher effort for ambiguity is not a guarantee.
Do not hardcode a provider ranking, benchmark result or price into AOS.

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
