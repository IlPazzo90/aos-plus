# Changelog

## 1.14.0-public — 2026-09-15

A pile of design skills was installed and none of them said *when* it enters a job,
or what makes the result acceptable. `references/design.md` adds the missing role.

- **The UI/UX role** owns the direction and the acceptance bar, not the pixels of
  every commit. It asks the one question the other roles do not: would someone who
  designs for a living read this as designed, or as assembled? The gap between the
  two is rarely talent — it is that nobody wrote the direction down before the first
  component existed, so every later decision was taken locally and the sum is generic.
- **Six stages, each with an artifact someone else can read**: direction, tokens,
  library choice, variants, motion spec, review. A stage with no artifact did not run;
  "I kept it in mind" is not an output. This is the ICM stage contract applied to
  design, with the skills that help mapped one per stage rather than listed together.
- **Four of those skills declare `disable-model-invocation: true`** in their
  frontmatter — an agent cannot invoke them, only the user can. Suggest them and say
  so; reporting a stage as complete through a skill that never ran is a false claim.
  Read the frontmatter of anything you plan to route to before promising it.
- **The gate is checked against the rendered result, in a browser, not the source.**
  Contrast ratios, 44px touch targets, ≥16px text in any field the user types into
  (below that mobile Safari zooms the page on focus), `prefers-reduced-motion`,
  visible focus, three viewports plus 200% zoom, and the empty/loading/error states.
  Reading the CSS tells you what you wrote, not what shipped.
- **Costs are named from what the skills read, not from what they say.** One design
  skill reads four separate image-generation API keys; another reads a font-catalog
  key; the image-direction skills name no backend of their own and will use whatever
  the host provides. So no asset-generating stage runs unattended, and none runs in a
  loop — the first unattended run spends real money.
- Designer row in `references/quality-gates.md` §4, next to UX/Product, which asks a
  different question, plus the matching DoD box.

## 1.13.0-public — 2026-09-15

- Routing gains three rows: reach for an anti-over-engineering pass when the solution
  looks larger than the problem, ask a documentation service for library and framework
  APIs instead of trusting a remembered signature, and run prose through an AI-tell
  pass before publishing. Discovery of everything else stays with `skill-library`.
- **Installed capability is executable configuration** (SKILL.md §3, detail in
  `references/quality-gates.md` §7). A skill, hook or MCP server added from outside is
  not documentation: it ships scripts, may require paid API keys and may send data off
  the machine, with the agent's own permissions. The check has four points — what it
  executes, what it asks for, where it sends, what it can reach — and comes from
  measuring a real install: 213 `.mjs`, 42 `.py` and 2 `.sh` inside folders that read
  as plain Markdown, eleven distinct API keys expected across image, music and speech
  generation, a deploy helper that uploads the project directory to a third-party
  endpoint, and analytics that post to a third-party product-analytics host with the
  project key embedded in the skill. The check says to follow symlinks: an installer
  that writes one copy and links the rest makes an unqualified `find` report zero
  scripts on the path the agent actually loads.
- The guard was then taken apart twice by a reviewer and rebuilt: it reads the staged
  blob rather than the working tree, handles git-quoted filenames (`-z`), includes
  type changes in the filter, checks the commit message from a `commit-msg` hook
  instead of reading the previous one from `pre-commit`, blanks only its own exact
  pattern assignment rather than every similar line, and exits non-zero when it cannot
  read the index — before, a scan that failed printed a clean result.
- A pre-commit check now refuses to publish content that names the maintainer, their
  machine or their clients: this distribution is assembled by copying files out of a
  private repository, and that copy is where sanitization gets skipped. Enable it in a
  fresh clone with `git config core.hooksPath .githooks`. The private terms live in an
  ignored `.public-sanity-terms`, never in the script — a guard that lists the names it
  hides publishes them itself, which is what its first version did.
- Compaction is chosen, not suffered (§3): compact at a breakpoint you pick, not
  mid-implementation and not at the automatic threshold, where the paths and partial
  state still in play are exactly what gets dropped.


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
- Those options are now read with real parsers instead of line patterns: `tomllib` for
  `pyproject.toml`, `configparser` for the ini files, with the previous scan kept as a
  fallback when a file cannot be parsed. A `[`-looking line inside a multiline TOML
  string is not a table, and `collect_ignore` written with a type annotation is still
  an exclusion.
- Two residual ways past that check: picking one candidate section per file meant an
  empty `[pytest]` could shadow the `[tool:pytest]` that setup.cfg actually uses, and
  the header regex gated the TOML parser, so a quoted table name was never parsed at
  all. Any candidate section now counts, and `pyproject.toml` goes to the parser first.
- Structural change, after five rounds each found another option missing from the list:
  pytest evidence no longer removes the minimum bar at all. What pytest collects depends
  on options this reader cannot enumerate — `python_files`, markers, a conftest, a plugin
  — and settling it needs pytest itself, which the reader never runs. Absence of a
  recognized restriction is not proof. Only a unittest discovery command, which names the
  files it will run, still counts as proven. The recognized restrictions now only change
  how the doubt is worded.
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
