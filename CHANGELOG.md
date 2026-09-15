# Changelog

## 1.12.2-public — 2026-09-15

- `aos-profile.sh`: the Python minimum bar no longer disappears just because the project
  declares some test command. `make test`, `npm run test` or a loose script dropped the
  `py_compile` guidance even when none of them exercises the Python in the project. The
  condition is now the absence of a *Python* runner; when a command exists but is not
  proven to cover the Python, the profile says so instead of going quiet.
- `aos-profile.sh`: recognizing an n8n workflow file says nothing about whether that
  workflow is active on the instance. The line asserted HIGH from detection alone; it now
  reports what it recognized and makes the consequence conditional on activation. The risk
  signal stays.
- `evals/scenarios.json` is installed and verified (`REQUIRED_FILES`, 22 files): the README
  promised it while the installer never shipped it. `aos-doctor.py` checked backticked
  paths only under `references/`, `bin/` and `catalog/`, so it could not see the broken
  promise; `evals/` is now covered, with a test.
- `references/quality-gates.md`: reviewer capability is back among the gate preconditions.
  A reviewer counts only if it is at least comparable to the model that produced the work.
- A `[pytest]` section, or pytest among the dependencies, still removed the minimum bar:
  `profile-python.py` printed a pytest command from configuration alone. It now states on
  a dedicated line whether the evidence comes from **test files** or only from
  configuration, and the bar stays up in the second case.
- The n8n line no longer presumes the effects either: a workflow of Manual Trigger and Set
  publishes nothing, so the text reports what was recognized and defers to reading the nodes.
- A found test file does not prove the proposed command runs it either: `addopts`,
  `testpaths`, `norecursedirs` and `collect_ignore` can exclude exactly what was found.
  When the pytest configuration narrows collection, the profile says so and keeps the
  minimum bar. This reads the options; it does not emulate collection.
- `collect_ignore` was named in that check but lives in `conftest.py`, which was never
  read; and the narrowing options were searched across the whole file, so an unrelated
  section with its own `testpaths` produced a false warning. Both fixed: conftest files
  are read for `collect_ignore`, and the options count only inside the pytest section.
- Known and inherited: backticked reference checking cannot tell an illustrative path from a
  promise. This already applied to `references/`, `bin/` and `catalog/`; `evals/` joins the
  same class.
- Validation: 48 tests (were 43) OK under Python 3.13, OK with 2 skipped under Python 3.9.6.
  Six of the seven new tests fail against the code they were written for, checked before
  each fix; the seventh is a guard for behavior that already held.

## 1.12.1-public — 2026-09-15

- The two tests that call `refresh()` directly now skip, instead of erroring, when the running interpreter has no stdlib `tomllib`. The CLI path is unaffected: `ensure_refresh_runtime()` re-execs into an installed Python 3.11+. The 1.12.0 entry recorded "43 tests passed" without naming the interpreter.
- `test_process_channels` no longer inherits the runner's stdin. `codex-hook-adapter.py` reads stdin to EOF by design; the second subprocess call passed no `input=`, so the whole suite blocked whenever it was started with an open stdin. Reproduced deterministically: killed at the timeout with stdin open, 0.057s with stdin closed.
- README states the interpreter requirement as a skip condition rather than a hard prerequisite.
- ICM guidance unchanged; its documented limits were re-checked against arXiv 2603.16021v2 §4.6, §5.2 and §6, and the stage folder naming against upstream `_core/CONVENTIONS.md`.
- Validation: 43 tests OK under Python 3.13; 43 OK with 2 skipped under Python 3.9.6. `catalog/index.json` still ships empty; no workstation paths, inventories or review transcripts are included.

## 1.12.0-public — 2026-09-14

- Initial public distribution with fresh history, generic examples and no workstation inventory or internal audit records.
- Includes ICM context guidance, scope/risk classification, verification helpers and existing regression tests.
- Deployment hints defer to project tooling and environment; no assumed private infrastructure.
- Optional integrations remain separately installed and subject to their own instructions.
- Made the skill-search test independent of any workstation catalog using temporary fixtures.
- Validation: 43 tests passed, isolated two-host installation/doctor passed, skill and secret scans passed. Independent review unavailable; owner explicitly authorized public release as aos-plus.
- Published package name: aos-plus; installed skill name remains aos.
