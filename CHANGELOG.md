# Changelog

## 1.16.0-public — 2026-09-15

**AOS did not measure itself, and it had already said so.** An earlier revision closed
by naming its own next investment: measure a sample of real completed tasks. The tool to
do it, `bin/aos-measure.py`, already existed and was complete — locking, a versioned
schema, mandatory provenance on any provider counter, a record that cannot be completed
twice. The only instruction that named it lived in `references/token-efficiency.md`, a
file loaded **only on request**, and read "use only for a requested comparison". Records
produced in the meantime: **zero**. The tool was never the missing part. The trigger was.

Measurement now lives in the core (`SKILL.md` §5) and applies to **every T2/T3 task**,
not only to a requested comparison. The record goes to `docs/misure/` and is committed
with the work, for the same reason review logs are: an ignored directory is where
evidence goes to disappear. The Definition of Done in `quality-gates.md` checks it.

`--outcome` gains **`blocked`**. Work stopped by a missing capability, an exhausted quota
or a declared gate produced nothing to accept in part; filing it as `partial` was the
only option available and it made the record lie. `start` now also creates the record's
directory — `docs/misure/` will not exist the first time, and that must not become the
convenient excuse for closing without a measurement.

Same defect, same remedy in `output-contract.md`: the learning step said "after
**significant** work". Significance is judged by the author, about their own work, at the
moment they most want to be finished. It is now unconditional at T2/T3 and admits an
explicit empty result — an outcome, not a skipped step. The same trap is documented one
ecosystem over: gstack's skill instructions carry issue #2402, where 43 of 44 learnings
arrived only from an explicit command because "if you discovered" read as optional.

**Drift between the two host copies stops being invisible.** `aos-doctor.py` has always
compared the Claude and Codex installations file by file, but it only ran when somebody
thought to ask — and somebody who has just edited one host does not think to ask. Now
`aos-profile.sh`, which runs at the start of the work anyway, prints the loaded version
and the result of that comparison, reusing aos-doctor rather than inventing a weaker
check. It reports only the drift codes: catalog anomalies are a separate, known condition,
and printing forty of them there would bury the one line that matters.

**The design gate believed a false success.** The viewport rule said "when the driven
browser cannot resize below its own width". Measured: `resize_window` answers
`Successfully resized window ... to 375x812` while `window.innerWidth` stays **1920**;
repeated at 600×800 after a three-second wait, same declared success, same 1920. The
danger is not a resize that refuses, it is one that says yes and does nothing — a
responsive check would have reported "verified at 375px" having measured the desktop.
The rule now requires reading `window.innerWidth` back and comparing it to the target; if
it diverges, the check did not run.

**A deploy claim was corrected.** The profile said a git push does not deploy. With a
Git integration active, every push to the production branch goes live on its own —
observed on a Next.js project whose deployments carried the `…-git-main-…` alias with no
`vercel` command ever issued. An instruction that denies a release is worse than no
instruction: it gets intermediate states committed to `main` in the belief that they stay
local.

Checks: 61 tests (5 new, one per introduced behaviour), `bash -n`, `py_compile`, the
mechanical security pass, and both host installations confirmed identical by
`aos-doctor.py`.

## 1.15.1-public — 2026-09-15

**A spent reviewer account is not a finished review.** Until now a quota running out
mid-gate simply ended the verification: nothing distinguished "the reviewer found
nothing" from "the reviewer's account is empty", and both arrived as a failed round.
The round is now retried once on a reserve model when the provider's own usage-limit
wording is present — only on that wording, because retrying a crashed or silent round on
a weaker model is how a broken round turns into a weak PASS.

The weighting is asymmetric, and that is the part worth keeping:

- a **finding** from the reserve model counts in full, because the arbiter confirms every
  finding mechanically anyway, so who found it does not matter;
- a **PASS** from it does not close the gate, because the absence of findings is exactly
  what depends on the strength of whoever looked.

A gate whose only PASS came from the reserve is VERIFICATO CON RISERVE, never VERIFICATO,
and the verdict names the model. The earlier rounds are not re-run on the reserve to
reach convergence: that manufactures a PASS.


## 1.15.0-public — 2026-09-15

An audit across four axes — design, token cost, context organization, orchestration —
with the measurement kept next to each finding. The first two corrections come from
running the design gate against a real product rather than from reasoning about it.

**Design.** The §5 gate never said to check **both themes**. On the first product it was
run against, all five contrast failures found existed **only in the light theme**, which
had never once been measured. A target also has **two axes**: a min-height utility passed
a height-only check while the element was 11px wide, twice, on two different elements.
The 16px input rule now says `any-pointer`, not `pointer` — `pointer` describes the
primary pointer, so a tablet with a keyboard attached reports fine and keeps the small
text it is being typed into with a thumb.

**New section: how to measure.** A measurement that finds nothing and a measurement that
*cannot* find anything produce the same zero. It is the empty-round trap from
`quality-gates.md` §3 one floor down, and it is answered the same way: plant the defect
the probe is meant to catch, confirm it screams, then remove it. Four traps documented
with their countermeasure — an element's rect is not its touch target, computed colours
come back in `oklch`/`lab` rather than `rgb()`, a rule nested in `@layer` escapes a
non-recursive CSSOM walk, and the tool's viewport is not the user's. When the driven
browser cannot resize below its own width or apply page zoom, the 375px and 200% boxes
are not ticked: the substitution is declared.

**Ponytail** was one routing line behind a trigger nobody fires against themselves — "the
solution looks larger than the problem" — and absent from the file about spending less.
It is now framed with the other two savers, because they work on three different
surfaces: Caveman shortens what goes to the user, RTK what the tools send back, ponytail
what lands in the repository. Only the third saves anything permanently. Its trigger is
mechanical now: before building at T2/T3, and whenever the answer adds a dependency, an
abstraction or a configuration option.

**Stale catalog.** A catalog hit is a claim about a path, not proof the skill is there:
one measured install had 20 of 399 entries pointing into a plugin cache that no longer
existed, while every one of those skills was installed and reachable elsewhere. A dead
path means the index is old — never that the capability is missing. The index is not
repaired by hand: it is generated from a runtime snapshot, and fixing rows manually
leaves it diverging from the next regeneration. Regenerate, or leave it stale and say so.

**A commit is not an install.** Editing one host's copy leaves the other on the old
version, and the drift is invisible from inside either session. After changing AOS,
install for the other host in the same intervention and confirm with `aos-doctor.py`.

**Review logs, and how to count them.** The gate said to move `BRIEF.md` and
`REVIEW-LOG.md` out of `tmp/`, and nothing checked. Counting what sits under `tmp/` turned
out to be the wrong count — the working copy stays next to the one that was preserved. Of
73 directories across four repositories, **71 were already saved and 2 were not**. The
Definition of Done box now compares the slugs instead of checking for emptiness.

**An update is an install.** §7 checked a capability once, at install, and a skill that
updated afterwards shipped new scripts under the trust the old ones earned. Now: not a
re-audit every time, but a checksum list compared against the copy already checked, which
reopens the question only when the executables changed.

**CTO role** added to the multi-role table with its limit written down: T3 only, or when
a choice locks in a dependency, a vendor or a data model. Its output is the single
next-investment advisory, not a fourth opinion on the diff.


## 1.14.0-public — 2026-09-15

**Reviewed across four adversarial rounds before this line was written.** Ten findings,
ten confirmed mechanically, none refuted. Three of them were contradictions inside the
new file itself, and five more were inside the fixes for the first three — which is
where the worst ones always are. What survives is listed below; what it got wrong is
listed with it, because a gate that hides its own corrections teaches nothing.

- The gate first demanded a direction written **before the first component**. No
  existing surface can satisfy that, which would have made the role unusable on
  everything already built. Adopting it on existing work now means writing down the
  direction the shipped result already implies, and naming what departs from it.
- The default direction allowed a near-black canvas and then prescribed near-black
  numerals — 1.06:1 against its own 3:1 floor. Foreground is now stated relative to
  the canvas.
- Verification said "in a browser", which a native screen cannot satisfy; a web
  replica verifies the replica. The medium now picks the check: browser, simulator,
  or the export at final size. That fix then had to be made three times, because it
  was written into two files and not into the checklist that T2/T3 work is measured
  against.
- The gate demanded one accent with no exception while the same file told you to adopt
  an existing brand's palette. A two-accent brand could satisfy the direction or the
  gate, never both. Both counts are now relative to the direction — and the reviewer
  then attacked that relativization and could not empty the gate: contrast, focus and
  reduced-motion stay absolute whatever a direction declares. An unexplained second
  accent still fails, and semantic colours never counted as accents to begin with.

### What the role is

A pile of design skills was installed and none of them said *when* it enters a job,
or what makes the result acceptable. `references/design.md` adds the missing role.

- **The UI/UX role** owns the direction and the acceptance bar, not the pixels of
  every commit. It asks the one question the other roles do not: would someone who
  designs for a living read this as designed, or as assembled? The gap between the
  two is rarely talent — it is that nobody wrote the direction down before the first
  component existed, so every later decision was taken locally and the sum is generic.
- **Seven stages, each with an artifact someone else can read**: plan review,
  direction, tokens, library choice, variants, motion spec, review. A stage with no
  artifact did not run; "I kept it in mind" is not an output. This is the ICM stage
  contract applied to design, with the installed skills mapped to the stage where they
  help rather than listed together.
- **Four of those skills declare `disable-model-invocation: true`** in their
  frontmatter — an agent cannot invoke them, only the user can. Suggest them and say
  so; reporting a stage as complete through a skill that never ran is a false claim.
  Read the frontmatter of anything you plan to route to before promising it.
- **The gate is checked against the rendered result where it runs, not the source.**
  Contrast ratios, 44px touch targets, ≥16px text in any field the user types into
  (below that mobile Safari zooms the page on focus), `prefers-reduced-motion`,
  visible focus, the viewports or OS text sizes for the medium, and the
  empty/loading/error states. Reading the CSS tells you what you wrote, not what shipped.
- **Costs are named from the line that reads the key, not from the line that mentions
  it.** One design skill reads four separate image-generation API keys — `os.environ.get`
  in its generator scripts, an operational read. The image-direction skills name no
  backend of their own and will use whatever the host provides. So no asset-generating
  stage runs unattended, and none runs in a loop: the first unattended run spends real
  money. A fifth key was claimed here in the first draft and the reviewer removed it —
  its only occurrences were in a test that *unsets* the variable, and the script that
  would read it is not shipped. A grep hit is not a read.
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
