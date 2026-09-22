# BRIEF — AOS 2.4.0: runtime-independent open execution, release gates

Repository: `~/.claude/skills/aos` (private edition; the public edition
`~/Progetti/Strumenti/aos-plus` mirrors the files listed in `DISTRIBUTION`).
Branch under review: `upgrade/runtime-independent-open-execution`, 16 commits ahead of
`main`, 68 files, +11 864 / −2 559. Codex reviewer reads the repository read-only.

## What the branch must deliver (the contract)

AOS is the development process skill loaded by Claude Code and Codex CLI. This branch
adds a **role pipeline** with cost-aware model routing and must be operational from
either host:

1. **Roles and routing.** `bin/aos-router.py` `decide()` picks planner / executor /
   reviewer / fixer models from `config/open-models.json` (catalog with cost class
   CHEAP/MID/PREMIUM, capability scores, compatible runtimes, prices). T0/T1: open
   executor + deterministic checks. T2: MID planner, open executor, cross-family MID
   reviewer, open fixer. T3/HIGH: premium planning and mandatory review. CRITICAL:
   explicit approval. Host identity never chooses the planner family; an explicit
   `planner_preference` may. The benchmark winner `vercel/deepseek/deepseek-v4-pro-0813`
   and fallback Qwen stay; no model name is promoted in code. Missing catalog models
   block instead of silently substituting a premium model.
2. **Pipeline state machine.** `bin/aos-pipeline.py` (pure) + `bin/aos-entry.py pipeline`
   (CLI, JSON in/out): start → plan → execute → check/verify → review → arbitrate →
   fix → reverify → pass, at most 6 review rounds, open retries then fallback then MID
   then premium only after exhaustion; permission denials, dirty tree and `.git`
   metadata changes are blockers, never escalation reasons. Findings are confirmed or
   refuted with mechanical checks at the current revision. The host owns state and
   authorizes every check argv; model output is data.
3. **Open executor.** `bin/aos-open-executor.py` runs the open model inside the
   Claude Code harness (file tools only, no Bash), with role, runtime, provider and
   model separated. Guards of `bin/aos-delegate.py` (clean tree,
   retry ownership, git metadata fingerprint, timeouts, step caps, denylist, Security
   Gate) are unchanged. **Runtime eligibility follows live negative probes**
   (`bin/aos-isolation.py`, records in `docs/verifiche/premium-plan-open-execute/isolation/`):
   Claude Code enabled with `os_isolation: seatbelt` (macOS `sandbox-exec` profile built
   by `seatbelt_profile`) after a probe denying 13/13 forbidden targets with both
   controls performed. **OpenCode was removed from the project** after its probe leaked
   `.env`, the symlink target and an external file; its plugin, installer, runner and
   permission map are gone and the user's OpenCode config was rolled back. **Codex CLI
   is not an open harness**: it edits through its shell tool, whose command policy let
   `npx` run, and without that shell it has no file tools (probe: 0 tool calls); it
   stays the premium host, planner, cross-family reviewer and escalation. A disabled runtime can run only inside the
   probe's fixture (`probe_root`), never in production. Secrets never enter versioned
   files or command arguments; the provider credential comes from `api_key_env` or
   `api_key_file`, read by the host and passed to the harness as an environment
   variable the shell-less worker cannot read back.
4. **Verified learning.** `bin/aos-learning.py`: host-owned SQLite ledger (outside the
   worktree) of outcomes, lessons (with reproduced check evidence, rejected candidates
   kept, dedup that keeps contradictory actions apart), applications with rollback
   identifiers, scoped routing advice only after ≥5 independent observations; no
   automatic global policy change. **Budget reservations** (`reserve_budget`): one
   `BEGIN IMMEDIATE` transaction sums outcomes + open holds + estimate so two
   concurrent sessions cannot both pass a cap that admits one; holds settle on outcome
   and expire after an hour. Unknown cost under an active cap blocks.
5. **Context management.** `bin/aos-operations.py` + `bin/aos-context.py`: evaluate
   GREEN/YELLOW/ORANGE/RED before each role call, structural compaction that never
   drops protected items, handoff JSON; the prompt estimate is recorded next to the
   runtime-reported input (`observed_input_tokens`, `hidden_context_tokens`, null when
   unreported, never zero).
6. **Benchmark.** `bin/aos-bench.py`: executor pairs `{runtime, provider, model}`,
   `--planners` (plan in the brief, one plan per worktree, invalid plan recorded and
   left out) and `--review-replay` (candidate reviewers on recorded diffs; same-file
   overlap labelled a raw indicator). A green project test never turns a timed-out or
   errored worker into a pass; costs null when unreported, never estimated as zero.
7. **Telemetry and doctor.** `aos-measure.py finish --pipeline` imports role events;
   `aos-doctor.py` reports runtime status with os_isolation and the isolation evidence
   record, warning when the record is missing or `sandbox-exec` is absent.
8. **Documentation** in `SKILL.md`, `references/orchestration.md`,
   `references/learning.md`, `references/context-budget.md` must describe what the
   code does, including limits (prompt-only estimates, no cost for subscription roles,
   probes bound targets on this host, not every attack).

## Constraints

No relaxation of guards, denylist, Security Gate, permissions or hook vendor files.
No secrets in Git. No worker deploys, pushes or policy mutations. Compatibility with
both hosts (Claude Code and Codex CLI) for every shared script. Python 3.9+ stdlib
only for `bin/*.py` (the bench runs under the system 3.9 on this host).

## Mechanical checks already run (round 2)

- `python3.12 -m unittest discover -s tests -p 'test*.py'`: 423 tests OK (4 skipped).
  The Node suite belonged to the removed OpenCode entry and went with it.
- `bash bin/aos-security.sh`: exit 0, no signal.
- `python3 bin/aos-doctor.py`: 0 anomalies; 6 warnings are DISTRIBUTION drift
  (public mirror synced at release) and local-only notes.
- Live evidence: `docs/verifiche/premium-plan-open-execute/isolation/*.json`
  (probe records), `live/claude-host-t2-state.json` (Claude host T2 pipeline to PASS
  with one confirmed finding fixed by the open fixer), `live/codex-host-t2-state.json`
  (Codex host: plan and execute ran, stopped by Gateway 402 budget; not a PASS).

## Manifest of the artefact

`git diff main...HEAD` in the repository (68 files). Entry points to read in full:
`bin/aos-open-executor.py`, `bin/aos-isolation.py`, `bin/aos-delegate.py`, `bin/aos-entry.py`,
`bin/aos-pipeline.py`, `bin/aos-router.py`, `bin/aos-learning.py`,
`bin/aos-operations.py`, `bin/aos-bench.py`, `bin/aos-doctor.py`,
`config/open-models.json`, `references/orchestration.md`, `references/learning.md`,
`references/context-budget.md`, `docs/verifiche/premium-plan-open-execute/*.md`.
Tests under `tests/`. Everything under `tmp/` is ignored by Git and out of scope.


## What round 1 found and what changed since

Round 1 (Codex, `round1-codex.md`) returned 2 BLOCKER and 4 MAJOR. All six were
reproduced mechanically, fixed and covered by regressions:

1. the probe certified targets a worker never attempted → an attempt now requires a
   recorded tool call naming the target (every spelling of the path), the two
   permitted controls must succeed, and the brief says a step reported without a tool
   call invalidates the run;
2. production could disable seatbelt and still report `isolation_verified` → only the
   probe may vary `os_isolation`, and `isolation_verified` follows the applied layer;
3. `probe_root="/"` unlocked a disabled runtime for any repository → a fixture is a
   directory under the temp tree carrying the probe's marker;
4. one outcome settled every reservation of a task → an outcome settles the hold it
   names, and holds travel with the role event that took them;
5. missing catalog models escalated to premium silently → they block;
6. a reply explaining that the review did not happen, with an example object inside,
   became PASS → prose around the object is bounded and `attacked` must be a nonempty
   list of strings.

Attack these fixes, the OpenCode removal (anything left depending on it, guards
weakened relative to `main`, dead code, tests that no longer prove what they claim)
and the single-harness decision.

## Round 2: findings and fixes

Round 2 (`round2-codex.md`) returned 1 BLOCKER and 2 MAJOR, all reproduced and fixed:

1. the probe counted the report's own text as an attempt — a `Write` of
   `probe-report.txt` mentioning `.env` counted as a read of `.env`, so two control
   writes could certify thirteen denials → an attempt now requires a tool of the
   matching family (read vs write/edit) acting on a **path field** of its input;
   the shell case is judged on the command alone. Probe re-taken under the new
   criterion: 13/13 denied, both controls performed, 17 tool calls;
2. a short sentence still wrapped a non-review into a PASS ("I could not review the
   diff … Example only: {…}") → any prose around the object is refused; a markdown
   fence is not prose;
3. `resolve(model='vercel/missing-model')` reached the harness because the block
   added in round 1 sat on the router path only → the open resolver blocks a
   provider/model the catalog does not list.

The benchmark run of 2026-09-22 was stopped by the user at 3 of 40 tasks: the
2026-09-20 benchmark stays authoritative and no model ranking changes here. Its three
records remain under `docs/misure/bench/2026-09-22-claude-code/` as partial evidence.

Attack these three fixes and anything they moved rather than closed.

## Round 3: findings and fixes

Round 3 (`round3-codex.md`) returned 1 BLOCKER and 3 MAJOR, all reproduced and fixed:

1. the probe judged a read of a target with no canary of ours by the worker's own
   word, so a `Read` that returned an SSH config counted as denied because the report
   said REFUSED → every call is correlated with the runtime's own tool result, and a
   successful read of a forbidden target is a leak whatever the report claims; the
   Codex adapter reports file-change and command outcomes the same way;
2. a check runs the worker's code and could change `.git/config` unnoticed, reaching
   `pass` → the repository metadata is fingerprinted around every check command and a
   change blocks before any further git of ours runs in that repository;
3. a provider override moved the executed identity away from the checked one
   (`anthropic/fable` + `provider=vercel` ran as `vercel/fable`) → the catalog
   validates the identity that actually runs;
4. `bin/aos-install.sh` shipped the tests that import the probe but not the probe →
   the manifest includes it.

Probe re-taken under the new criterion: 13/13 denied, both controls performed, 18
tool calls. Suite 429 tests OK, Security Gate exit 0, doctor 0 anomalies.

This is round 4: only BLOCKER and MAJOR remain in play. Attack the three fixes above
and whatever they moved instead of closing — in particular whether the metadata
fingerprint around checks can be evaded, whether the tool-result correlation can be
faked by a runtime that omits ids, and whether the catalog binding still lets an
identity through on any path (`--model`, `--slot fallback`, provider override,
probe fixture).

## Round 4: findings and fixes

Round 4 (`round4-codex.md`) returned 1 BLOCKER and 1 MAJOR, both reproduced and fixed:

1. the probe turned the absence of a correlated tool result into a denial, so a
   runtime that omits ids — or a record truncated past the result (20 results kept
   against 60 calls) — certified a successful read of a forbidden target as denied →
   a call whose result never arrived is `unknown`, `unknown` never certifies
   isolation, the verdict carries the list of unknown targets, and calls and results
   are kept to the same depth;
2. the metadata fingerprint around checks covers config/info/hooks but not HEAD, so a
   check that commits moved the baseline and handed the reviewer a diff without the
   work → HEAD is compared around every check command.

Probe re-taken: 13/13 denied, 0 unknown, both controls performed, 17 correlated tool
calls. Suite 431 tests OK, Security Gate exit 0, doctor 0 anomalies.

## Round 5: findings and fixes

Round 5 (`round5-codex.md`) returned 1 BLOCKER and 2 MAJOR, all reproduced before the
fix and covered by regressions:

1. the probe judged a forbidden **write** by its final effect on disk alone, so a
   write the runtime reported as successful — and then undone, or restored — was
   certified as denied, and a write whose result never arrived counted as a denial
   too → writes now follow the rule reads got in round 4: a reported success is a
   leak, an uncorrelated call is `unknown`, and `unknown` never certifies isolation.
   Probe re-taken under the new criterion (`isolation/claude-code-seatbelt.json`,
   the round-4 record kept under `isolation/history/`): 13/13 denied, 0 unknown,
   both controls performed, 17 correlated tool calls;
2. `git diff` skips a tracked file marked `skip-worktree` or `assume-unchanged`, so
   a check could remove the work from the artefact handed to the reviewer while HEAD
   and the metadata fingerprint stayed identical → every `snapshot()` refuses an
   index flag that hides a tracked file, so the artefact and the disk cannot diverge;
3. the revision of a check was taken **after** the command, so a test that asserted
   the good code and rewrote it on the way out was recorded as a verification of the
   version it had just broken → the worktree is compared around every check command
   and a check that changes it is not recorded at all.

Suite 434 tests OK, Security Gate exit 0, doctor 0 anomalies.

This is round 6, the last one; only BLOCKER and MAJOR remain in play. Attack these
three fixes and what they moved rather than closed: whether a forbidden write can
still reach `denied` by any path (a spelling the matcher misses, a target whose
on-disk check is the only signal, the two controls), whether the artefact can still
diverge from the disk (index flags are one way — are there others that leave HEAD and
the fingerprint alone?), and whether a check can still be credited to code it never
ran against (a check that changes and restores the tree, an ignored file, a revision
that collides). Then the release surface as a whole: anything in verify → review that
still trusts a report over an observation, and anything in the public edition mirror
(`DISTRIBUTION`, `bin/public-sanity.sh`) that would ship a private path or a secret.