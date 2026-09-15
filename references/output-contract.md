# Output contract

Load for T2/T3 final reporting. Italian, direct, proportionate; Caveman preserves
substance. Code, comments, commits and durable records retain normal prose.

## Deliver the result, not the process log

T0: one or two lines, change and observed check. T1: outcome and verification,
plus a material assumption/limit if needed. T2/T3 use compact prose or a list:

- What changed and where; T3 briefly account for completed phases.
- What actually ran/was observed, with decisive counts/status and practical limits.
- Non-obvious decisions and unresolved risks, only when real.
- Backlog if real opportunities surfaced; at T3 explicitly address whether any did.
- T3: one next-investment advisory grounded in the work.

Link the durable report for detailed evidence. Do not duplicate it in chat,
write empty sections, list every successful command or invent quality scores.
Follow host formatting rules over a prescribed heading layout.

## Backlog and advisory

Each real proposal states benefit, impact, effort, risk, priority and dependency
when applicable. Prefer the few items supported by repository evidence. Separate
introduced defects from inherited issues. No automatic implementation outside scope.
If nothing real surfaced at T3, say so briefly rather than manufacturing a list.

The T3 advisory answers: with one next investment, what would improve this product
most, and at what maintenance/opportunity cost? Use observed bottlenecks. If no
new capability is justified, recommend measuring or maintaining the current one.

## Learning without instruction accumulation

After **every** T1, T2 and T3 task, identify a wrong assumption, the decisive check
and any recurring correction, and state what the review found — including "nessun
apprendimento durevole" when it genuinely found none. An explicit empty result, not a
skipped step: "after significant work" was the previous wording, and significance is
judged by the author, about their own work, at the moment they most want to be
finished. T1 gets one line in the report; T2/T3 get the DoD box in
`quality-gates.md` §5. Keep a fact only if it changes future decisions. A reusable
proposal needs its source and scope: project-specific convention, personal durable
preference, existing-skill fix, new skill, or automation. Distinguish a one-off
incident from recurrence; link evidence instead of copying a session narrative.

**A durable learning has a destination, or it is a remark.** The 1.10 rewrite dropped
the destinations and kept the exhortation, and on Codex — which has no automatic
memory — nothing since then said where a learning goes:

- Repository convention → the project's `CLAUDE.md` / `AGENTS.md`, or its `docs/`,
  following the file that already exists there. Same on both hosts.
- Personal durable preference or correction → on Claude, a memory file under the
  host's memory directory, in its existing format, with its index line; on Codex,
  propose the entry and the file to the user, since the runtime keeps no memory AOS
  can write to.
- Existing-skill fix, new skill, automation → a proposal with evidence; never applied
  from an ordinary task.

Respect project/host rules for memory. Do not silently expand global instructions
or modify AOS/third-party skills from ordinary tasks. For an explicit AOS audit,
use `token-efficiency.md`; permission to diagnose is not permission to apply all
recommendations. AOS self-edits require the user's request.
