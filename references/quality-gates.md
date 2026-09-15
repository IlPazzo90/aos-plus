# Quality Gates — Red Team, Multi-Role, Definition of Done

Read when closing a T2 or T3 task. T0/T1 do not run this workflow; apply SKILL.md
and the risk-specific inline checks in `risk-and-tiers.md`.

## Order

1. Self-review the diff
2. Red team pass
3. Cross-model verification — **only when the trigger below fires**
4. Multi-role pass — **only the roles that apply**
5. Definition of Done, with evidence
6. Score, if it clarifies anything

Fix what is real. Re-verify what you fixed. Then stop.

## 1. Self-review

Read your own diff as a diff, not as the thing you intended to write. Look for:
leftover debug output, commented-out code, an unused import, a renamed thing not
renamed everywhere, a `TODO` you left, a hardcoded value that should be config, a file
you touched by accident.

## 2. Red team

Now stop being the author. The implementation is guilty until proven innocent. Hunt:

- **Logic** — off-by-one, inverted condition, wrong default, wrong operator
- **False assumptions** — "this is always non-empty", "this always arrives sorted",
  "this can't be null"
- **Error paths** — what happens when the network call fails, the file is missing,
  the API returns 429, the response shape changed
- **Unexpected input** — empty, huge, unicode, negative, duplicate, out of order
- **Concurrency** — two runs at once, a re-entrant trigger, a cron overlapping itself
- **Regressions** — what else reads this function, this option, this table, this file
- **Security** — injection, secrets in logs or commits, authz assumed rather than
  checked, data exposed to the wrong tenant or user. Start from the mechanical pass
  (`bash "$AOS_DIR/bin/aos-security.sh"`) so the known traps are already
  off the table, then spend the thinking on what it cannot see: whether the
  permissions are *correct*, not merely present
- **Performance** — a query inside a loop, an unbounded fetch, a full-table scan
- **Cruft** — dead code, duplication, an abstraction with one caller

For each real finding: severity, fix if in scope and safe, add a test if the project
has a place for one, re-run the relevant check.

**Do not manufacture findings to look thorough.** "Red team pass: nothing significant"
is a legitimate and common result. Padding it wastes the user's attention and
devalues the ones that matter.

## 3. Cross-model verification — `verify-agent`

Steps 1 and 2 have a structural weakness: the reviewer is the author. The same model
that wrote the code is hunting for its own blind spots, and a model's blind spots are
correlated with its output. A clean red team is weak evidence, not strong evidence.

`verify-agent` closes that gap. A model from a **different family** (Claude CLI when Codex is main; Codex CLI when Claude is main) receives only the brief and the artifact — never AOS's reasoning, never the
conversation — and is prompted to prove the work wrong. Every finding it returns is
then verified mechanically before it is accepted or refuted. Max 6 rounds, closed early
on convergence or on a sterile round. A quota error, an interrupted stream or an empty
report is a round that did not run — never a PASS. A quota error is the one case with a
second path: the round is retried on a reserve model and comes back marked `degraded`,
which is a weaker thing than a round — see precondition 2.

### When it fires

| Situation | Action |
|-----------|--------|
| Tier ≥ T2 **and** risk ≥ HIGH, before shipping | **Run it.** Announce it, do not ask |
| Tier ≥ T2 **and** risk ≥ HIGH **and** the red team found nothing | **Run it.** "Nothing found" is precisely the blind-spot case |
| T3 complete **at risk ≥ MEDIUM**, or an irreversible action is next | Offer it in one line; run it if the user says yes |
| The user asks to be sure, or doubts the work | Run it |
| T0, T1, or risk LOW | **No.** Cost with no matching exposure |
| Content, commercial or administrative work | No — AOS is not driving that work either |

### Preconditions — check these first, they fail hard

1. **A Git location for the final record.** The requirement to commit BRIEF.md and
   REVIEW-LOG.md applies in both directions. Codex CLI additionally requires a Git
   repository as cwd; Claude CLI can review files outside a repository. Do not call
   the Claude backend unavailable for that reason: establish an existing repository
   where the record belongs. If none is appropriate, state that persistence is
   blocked; an executable review and a completed, versioned gate are distinct.
2. **A working opposite backend.** Claude CLI logged in when Codex is main;
   Codex CLI logged in when Claude is main. Other backends only on explicit user request; never silently substitute the
   principal or a same-family clone for the opposite reviewer.
   **Capability is part of the precondition**, not a detail of the backend: a reviewer
   counts only if it is at least comparable to the model that produced the work. A small
   local model answering PASS is not evidence that the work is sound — most likely it did
   not follow the brief. When the user asks for such a backend, run it and say in the
   report how little the verdict weighs.

   **A spent account is not a finished review.** When the reviewer's quota runs out
   mid-gate, `verify-agent` retries the round once on a reserve model and marks it
   `degraded` — and the weighting is asymmetric, which is the part worth remembering:

   - a **finding** from the reserve model counts in full, because every finding is
     confirmed mechanically by the arbiter anyway, so who found it does not matter;
   - a **PASS** from it does not close the gate, because the absence of findings is
     exactly what depends on the strength of whoever looked.

   So a gate whose only PASS came from the reserve is **VERIFICATO CON RISERVE** at
   best, never VERIFICATO, and the verdict names the model. Do not re-run the earlier
   rounds on the reserve to reach convergence: that manufactures a PASS.
3. **Where the log will end up.** Raw traces belong in ignored `tmp/verify/`.
   Commit a sanitized brief and review record under `docs/verifiche/<slug>/`.
   Check exclusion rules before writing traces; never publish sensitive review data.

A blocking precondition missing → **do not silently skip the gate.** Say which one it is,
run the red team properly inline, and record in the report that cross-model
verification was unavailable and why.
This fallback completes the available assessment, **not the mandatory external
gate**. Prepare all remaining authorized work. If release depends on this gate,
leave release pending until the backend works or the user explicitly accepts the
missing independent review, subject to host/project rules. Do not silently turn
an unavailable reviewer into permission to ship or a VERIFIED verdict.

### Feed it the brief AOS already has

The verdict is only as good as the contract it is measured against. Where AOS produced
a plan or a spec, **that document is the brief** — hand it over instead of letting the
brief be re-derived from the conversation. A T2/T3 task that never wrote a brief down
has a bigger problem than verification.

### Reading the verdict

- **VERIFICATO** — a real gate passed. Say so. Not available when the PASS came from a
  `degraded` round: check `status.json` before writing this word.
- **VERIFICATO CON RISERVE** — the reservations go in the report's `## Rischi aperti`,
  one per line. They do not disappear because the verdict was not red.
- **INCOMPLETO** — a required backend or packet did not complete. Preserve completed
  evidence, state missing coverage and do not declare a complete PASS.
- **BOCCIATO** — a confirmed BLOCKER. This is **not** done. Back into the loop, and it
  counts against the 5-cycle bound in `SKILL.md`.

### A finding may point outside the artifact

The reviewer attacks the work against the brief, and the brief describes a *purpose*.
It will therefore sometimes land on a defect in a file the change never touched — one
the change inherited and built on. Report it, but **say which it is**: a defect
*introduced* here is this task's problem, one *inherited* is a decision for the user.
Collapsing the two either inflates the diff's guilt or buries a real hole.

The same distinction sets the fix boundary. Correct what this change caused; for the
inherited one, fix it only if it is small and safe, and otherwise put it in the report
with what it would take. Do not let an inherited defect quietly turn a bounded task
into a refactor.

A reviewer finding is a **claim, not a fact** — external models hallucinate too.
Confirm each one mechanically before changing code, and write the counter-proof when
you refute one. Never edit the artifact to placate a finding you could not reproduce.

## 4. Multi-role — use the roles that apply, not all of them

Pick the two or three that fit the change. Six sections on a form validation fix is
bureaucracy.

| Role | The one question it asks |
|------|--------------------------|
| **Architect** | Does this fit the structure that is already here, or fight it? |
| **Senior dev** | Would a competent colleague reading this in six months understand it immediately? |
| **QA** | Which behaviour is now untested, and which edge case did we skip? |
| **Security** | Where is the trust boundary, and what crosses it unchecked? |
| **DevOps/SRE** | How does this deploy, how does it fail, and how do we roll it back? |
| **UX/Product** | Does this actually solve the user's problem, or just the ticket? |
| **Designer (UI/UX)** | Would someone who designs for a living read this as designed, or as assembled? (`design.md`) |
| **Future maintainer** | What will surprise the next person, and is it written down? |
| **CTO** | Is this worth owning for the next two years, and what does keeping it alive cost? Only at T3 or when a choice locks in a dependency, a vendor or a data model — its output is the one next-investment advisory in `output-contract.md`, not a fourth opinion on the diff |

Synthesise findings into one list. Do not write one report per role.

## 5. Definition of Done

Every applicable box, backed by something that was actually run or observed. An
unchecked box is not a failure — an unverified check is.

- [ ] The agreed requirement is implemented — re-read the original request
- [ ] The behaviour was **observed**, not inferred from the code
- [ ] The project's checks are green (see `project-profiles.md` for what those are here)
- [ ] Tests added or updated where the project has tests and the change warrants it
- [ ] No known regression introduced
- [ ] Errors are handled the way this codebase handles errors
- [ ] The relevant edge cases are covered
- [ ] The solution matches local conventions
- [ ] No unjustified complexity, no unrequested scope
- [ ] Security reviewed proportionally to risk
- [ ] Cross-model verification run where section 3 required it — or its absence
      declared, with the reason
- [ ] Where it ran: `BRIEF.md` and `REVIEW-LOG.md` moved out of `tmp/` into
      `docs/verifiche/<slug>/` and **committed** — a verdict left in an ignored
      directory is a verification that did not happen. Check it, do not remember it —
      and check the right thing. **Counting what sits in `tmp/` does not count what was
      lost**, because the working copy usually stays next to the one that was preserved.
      Compare the slugs:

      ```sh
      for d in tmp/verify/*/; do
        [ -d "docs/verifiche/$(basename "$d")" ] || echo "NEVER SAVED: $d"
      done
      ```

      Counted once across four active repositories: 73 directories under ignored `tmp/`,
      of which **71 were already preserved and 2 were not**. The first version of this
      line called all 73 lost
- [ ] Performance checked where it matters
- [ ] Where a person looks at the result: the design gate in `references/design.md` §5
      was run against the rendered thing where it runs — a browser for the web, a
      simulator or device for native, the export at final size — not against the source
- [ ] The measurement record exists and is complete — `bin/aos-measure.py finish` ran
      with `delivered`, `partial` or `blocked`, the file is committed with the work, and
      `accepted`/`rejected` waits for `judge`, after the user has spoken
      (`SKILL.md` §5). A task closed without one leaves nothing to compare the next one
      against, which is how "is this process working?" stays unanswerable for a year
- [ ] The learning line was written (`output-contract.md` §Learning): a wrong assumption,
      the decisive check, or the explicit «nessun apprendimento durevole» — and a durable
      one has a named destination, not a mention in chat
- [ ] Docs updated where the change makes existing docs wrong
- [ ] Config, migration and rollback handled where relevant
- [ ] Final verification run against **real output**, in this turn

**Never** say done, fixed, working, or passing on the strength of having written the
code. Run the check, read the output, then speak.

## 6. Scoring — diagnostic, never decorative

Score only where it sharpens the picture: Correctness, Test confidence,
Maintainability, Security, Performance, Observability, Documentation, Architectural
fit.

- **90–100** production-ready for this scope
- **80–89** good; non-blocking improvements exist
- **70–79** needs attention before closing, if the area matters here
- **< 70** do not declare done without an explicit reason

A low score in an area **critical to this task** is a blocking gate. A low score in an
irrelevant area is noise — omit it.

Do not invent precise numbers where no measurement exists, and do not chase 100/100.
Prose beats a fake metric: "no test coverage on the error path" says more than
"Test confidence: 72".

## 7. Installed capability is an attack surface

A skill, hook, MCP server or plugin added from outside is not documentation: it is
configuration the agent executes, with the agent's own permissions. The install
output usually says so in one line that scrolls past — *"Review skills before use;
they run with full agent permissions."*

Check a newly installed capability before relying on it, and record what you found:

1. **What it executes.** `find -L <dir> -type f \( -name '*.sh' -o -name '*.py' -o
   -name '*.mjs' -o -name '*.js' \)`. A set of Markdown files is instructions; a set
   of scripts is software you did not review. Count them before you trust the folder,
   and count them on every path the agent can reach: an installer that writes one copy
   and symlinks the rest leaves `find` without `-L` reporting zero scripts on the
   linked path — zero on exactly the directory the agent loads.
2. **What it asks for.** Grep the tree for `API_KEY`, `TOKEN`, `SECRET`. Credentials
   the skill expects are costs and blast radius: a media skill wanting four different
   generation keys will spend real money the first time it runs unattended.
3. **Where it sends.** Grep for `https://` and for telemetry calls. A deploy helper
   that uploads the project directory to a third-party endpoint is doing exactly what
   it says and still moves your source off the machine; know which endpoint, and what
   the archive excludes.
4. **What it can reach.** A capability that can deploy, publish or message can do it
   to production. Decide that before it is installed, not at the moment an agent
   reaches for it mid-task.

None of this proves a capability is safe. It establishes what it *can* do, which is
the part a clean scan never gives you. Do not extend an authorization to a new
capability because a similar one already had it.

**An update is an install.** The check above fires once, at install, and a skill that
updates afterwards ships new scripts under the trust the old ones earned — silently,
because nothing announces it. Do not re-audit on every update; re-audit when the answer
to point 1 or 2 changed. That is one command against the copy you already checked:

```sh
find -L <dir> -type f \( -name '*.sh' -o -name '*.py' -o -name '*.mjs' -o -name '*.js' \) \
  -exec shasum {} + | sort -k2 > /tmp/<name>.now
diff /tmp/<name>.checked /tmp/<name>.now
```

A new script, or a changed one, puts the capability back at point 1. An unchanged list
means only that the executables are the same — it says nothing about what the Markdown
now instructs the agent to do with them.

## Anti-patterns this file exists to prevent

- A green build treated as proof of quality
- Editing the test until it passes, when the behaviour is what is wrong
- Skipping the red team because the change "felt clean"
- Treating a self-review as independent review — the author is not a second opinion
- Running `verify-agent` on a T1 fix, or skipping it silently on a live deploy
- Accepting an external reviewer's finding without reproducing it
- Six role reports on a two-line fix
- Numbers presented as measurement when they are impressions
- Endless polish with no marginal value — the loop bounds in `SKILL.md` are binding
