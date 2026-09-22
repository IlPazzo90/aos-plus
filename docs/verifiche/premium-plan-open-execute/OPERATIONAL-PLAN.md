# Operational validation — 2026-09-22

Outcome: run the existing AOS private and public development branches through
cost-aware role selection, guarded cheap execution, deterministic verification,
independent review, cheap fixes and verified persistent learning.

Authoritative inputs: current repository, the user's FULLY OPERATIONAL specification,
config/open-models.json and existing HANDOFF/REVIEW-LOG. Baselines: private f68b1cb,
public 11542a1, VERSION 2.4.0. Main must remain unchanged until every release gate passes.

Architecture: retain router, state machine and runtime adapters. Add a small SQLite
evidence ledger and operational context/budget helpers. The host owns checks and
arbitration; workers cannot approve lessons, change policy or expand permissions.
Unknown cost, unavailable review and incomplete isolation remain explicit.

1. Open worker: role-specific catalog selection and finite capability/cost validation;
   tests for MID, cross-family, budget and exhausted escalation.
2. Open worker: persistent lessons, rejected candidates, scoped history and reversible
   applications. Require host check evidence; global changes require repeated evidence
   or explicit approval.
3. Open worker: reuse context compaction, preserve unresolved state and enforce budgets;
   tests for RED context, unknown costs and aggregate denominator correctness.
4. Integrate these components through the entry pipeline, telemetry and doctor. Probe
   actual model availability before configuring MID candidates. Keep benchmark winner.
5. Validate Stop hook schema against installed runtime, security negative probes using
   disposable canaries, real benchmark pairs and both host pipeline scenarios.
6. Mechanical checks before cross-family review; arbitrate findings with reproducible
   commands, send confirmed fixes to open workers, rerun relevant checks.
7. Mirror DISTRIBUTION files; sanitize public documentation. Commit and push both
   branches, verify remote hashes. Report every unmet acceptance criterion.

Rollback: revert task commits, retaining evidence records; never reset unrelated work.
No global permissions, denylist or Security Gate relaxation. No worker deployment,
push, secrets, production access or policy mutation. No model-output-only learning.

Validation: Python and Node suites; Security Gate; doctor; distribution equality;
public sanity; live worker health separate from project tests; independent review;
context/budget boundary cases; evidence-backed lesson application and repeated case.
