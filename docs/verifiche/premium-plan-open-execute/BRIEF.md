# Premium plan / open execute implementation plan

Goal: separate planning, execution, review and fixes in the existing AOS entry.
Contract: user request dated 2026-09-21, sections 1–24. Premium plans and reviews;
open executes and fixes. Preserve benchmark models, worker guards, permissions,
Security Gate, Verify Agent, manual override and legacy provider fallback.

Architecture: extend the pure router with role assignments; a small state machine
owns ordered stages, subtask and retry counters. The existing OpenCode bridge calls
read-only premium roles and guarded open workers. Verification commands require
host bash permission and run without shell interpolation. No arbitrary planner
command runs automatically. The coordinator arbitrates findings with recorded
mechanical checks; unresolved findings block completion. Native host permissions
remain authoritative. CLI adapters and tool results are the inspectable artifacts.

Acceptance: T0/T1 no premium planner; T2/T3 premium plan, open work and opposite
premium review. Confirmed findings go to open fix; refuted findings require
counterevidence. Primary retries precede fallback retries and premium execution.
HIGH review and CRITICAL approval stay mandatory. Missing metrics stay null.

Scope/files: bin/aos-router.py, bin/aos-entry.py, bin/aos-pipeline.py,
bin/aos-measure.py, config/open-models.json, opencode/aos-bridge.mjs, tests,
SKILL.md, references/orchestration.md, CHANGELOG.md and this task's records.
Do not modify aos-delegate guards, Security Gate, Verify Agent installation or
unrelated configuration. No dependencies. No benchmark reranking.

Tasks (sequential, same owner):
- [ ] Write routing/pipeline tests covering all 15 requested cases and A–E traces.
- [ ] Implement role routing and stage transitions, preserving legacy decisions.
- [ ] Wire read-only plan/review and guarded execution into the current bridge.
- [ ] Extend explicit measurements and document role/token semantics.
- [ ] Run Python/Node regression, security and opposite-family review; arbitrate.
- [ ] Update changelog/measurement, commit scoped files, push and verify remote.

Checks: python3 -m unittest discover -s tests -p 'test*.py';
node --test tests/opencode-entry.test.mjs; git diff --check;
bash bin/aos-security.sh. New synthetic E2Es use real transitions with fake model
responses and deterministic command evidence, no paid model sweep.
Rollback: revert only this intervention's commits; shared Claude/Codex links
continue pointing to this repository. Preserve other agents' work.

Risks/uncertainties: provider availability is observed on invocation; missing
opposite premium family blocks a required review with a documented limitation.
Planner output is data, never authorization. Host-only capabilities retain their
existing route. T3 subtask plan is validated before writes. No token-savings or
quality ranking claimed without a measured baseline. Prose: lead with behavior
and evidence; omit invented objections and repeated conclusions.

Review artifact: uncommitted diff against a2f6410, including new bin/aos-pipeline.py and tests/test_pipeline*.py. Security guards and Verify Agent must remain unchanged. Check all user acceptance paths and adapter integration, permission boundaries, telemetry, provider fallback, state continuity. Measurement record is open and will be finished before final commit. Current checks: Python 253 passed (4 skipped); Node 21 passed. Synthetic models are explicitly permitted, do not claim paid E2E. Distribution port and changelog are pending.

## Addendum 2026-09-22: runtime-independent CSM recovery

The final recovery closes the observed Codex CLI rule-inheritance regression by
failing closed: `codex-cli` cannot be selected or invoked as an open worker.
Codex remains a premium host, planner and reviewer through configured model
references. The catalog in `config/open-models.json` is the sole model source:
each entry declares provider, compatible runtimes, cost class, capability scores,
context, optional prices and benchmark/historical availability. Routing selects
the least costly sufficient candidate per planner, executor, reviewer and fixer,
subject to risk, uncertainty, security requirements and budget. Execution uses
cheap primary, cheap fallback, an explicitly configured MID, then premium.

The benchmark regression is part of this recovery: a worker timeout or error is
not a pass even if project tests are green. No live benchmark is authorized until
the security closure remains verified. Distribution files must be byte-identical;
public prose must contain no private measurements or raw reviewer traces. The
cross-family reviewer is required for PASS. If unavailable, record INCOMPLETE
after the mechanical checks rather than claim independent approval.
