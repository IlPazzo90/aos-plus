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

The user ranks the models; AOS only decides which work may leave the main session.
The ranking, the names and the reason live in the user's global instructions
(`CLAUDE.md`, `~/.codex/AGENTS.md`), never in AOS: names change, the rule does not.

- **Main session = strongest configured model.** It keeps classification, T2/T3
  implementation, verification, arbitration of findings, the final report and
  anything at risk HIGH/CRITICAL. AOS cannot change the main model: on Claude Code it
  is the user's `/model`, on Codex `model` in `config.toml` or `-m`. When the main
  session runs on a weaker model and the task is T2+ or HIGH+, announce it with the
  tier line and ask for the switch before the first change; do not quietly proceed.
- **Subagent = working model**, for bounded T0/T1 work at risk ≤ MEDIUM that is
  independent of the rest: a file with a known change, a search, a test run, a port.
  Claude Code: the `Agent` tool takes `model`, an alias the host lists; `fork` always
  inherits the parent model, so use it only when the whole context is
  the point. Codex: `spawn_agent` accepts a model, otherwise
  `[agents].default_subagent_model`, otherwise the host default; `codex exec -m` for
  scripted calls; `codex features list` must show `multi_agent` enabled. Read-only
  search may go one step cheaper than the working model.
- **Reviewer = strongest model of the other family.** Verification is the most
  complex step, and a reviewer weaker than the author counts little. verify-agent
  pins the Claude reviewer to the strongest alias of its family; the Codex reviewer
  uses the configured model,
  with the reserve only on an exhausted quota, and its PASS counts less.
- Effort follows the same line: lower effort for bounded work only after
  representative checks show adequacy; higher effort for ambiguity is not a
  guarantee. Do not hardcode a provider ranking, benchmark result or price into AOS.

- **Open runtime = OpenCode on the user's open working model** (2.0.0), for the
  same bounded T0/T1 work at risk ≤ MEDIUM that may leave the main session, and
  only when the main session can check the result by observation: a test, a
  rendered page, a diff against a known expectation. Not eligible: T2+,
  HIGH/CRITICAL, auth, payments, personal data, migrations, deploys, and any
  change whose only check is "looks right". Invocation, from a clean repo:
  `python3 "$AOS_DIR/bin/aos-delegate.py" --repo <path> --model <provider/model> --brief <file> --json`.
  The script runs `opencode run --pure --auto --format json` and prints exit
  code, diff stat (new files included), seconds, the reply and usage summed from
  the JSON events (null when not reported, never estimated); it never runs tests
  or commits. `--auto` approves whatever is not denied, so the per-run config
  denies what a worker never needs: edits outside the repo, web tools, subagents,
  and the shell commands that carry data or changes off the machine (`curl`,
  `ssh`, `git push`, `git commit`, deploy CLIs — `DENIED_BASH` in the script).
  That is a guard against accidents and prompt injection, not a sandbox: the
  worker runs as the user, and an interpreter reads what `cat` may not. Nothing
  secret or production-bound belongs in a repo handed to it.
  The main session runs the check. One failed check → one retry with the
  failure output appended to the brief. A second failure → the main session
  does the work itself and writes `escalated` in the task record. The model
  comes from the user's global instructions (role name: *open working model*);
  AOS never names it and never carries a ranking, benchmark result or price.
  OpenCode reads `~/.claude/skills`, `~/.claude/CLAUDE.md` and the project
  `CLAUDE.md`/`AGENTS.md`, so the brief carries the task, not the rules — and
  every run pays that catalog as input tokens, so a delegated task must be
  worth more than one prompt. Providers live in `~/.config/opencode/opencode.json`;
  any OpenAI-compatible endpoint is one block —
  `{"provider":{"<id>":{"npm":"@ai-sdk/openai-compatible","options":{"baseURL":"…/v1","apiKey":"{file:~/.secrets/<name>}"},"models":{"<model>":{}}}}}`
  — with `zeroDataRetention: true` on models that receive client source. No
  `model` key in that file: every run names its model.

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
