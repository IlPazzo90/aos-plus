# Token efficiency

Load only for setup, compatibility debugging or a cost audit. The everyday rules
live in SKILL.md; do not load this file merely because RTK is installed.

## Requested effectiveness audit

Trigger: “audit AOS”, “ottimizza i consumi”, or an explicit request to improve the
setup. This is a bounded diagnosis, not an automatic recurring job. Reuse an
existing specialist audit if its actual scope matches; a command named doctor
may only diagnose installation health. Never infer behavior from a command name.

1. Define the sample: project, date range, task/session count and available
   evidence. Start with current task artifacts and prior project reports. Read
   personal session histories only when the user explicitly requests that source;
   inspect the smallest relevant range locally. Do not upload raw histories or
   read unrelated accounts. State missing evidence rather than broadening silently.
2. Inventory costs separately: always-loaded instructions, conditional references,
   tool schemas, data/images, generated output, repeated failed attempts, duplicate
   reads/checks and worker startup. Inventory actual loaded material, not all files
   installed on disk. A short router that loads several long skills can cost more.
3. Find recurring corrections or waits. Each finding needs file/session references,
   recurrence count **within the sample**, observed consequence and uncertainty.
   One incident is one incident, not proof of a frequent pattern. Corrected facts
   belong in a compact ledger; do not append every conversation to instructions.
4. Choose the smallest justified intervention:

   | Evidence | Candidate |
   |----------|-----------|
   | Existing skill repeatedly misses the same requirement | Fix its instruction or check, subject to ownership/authorization |
   | Distinct repeated task lacks a suitable skill | New specialist skill with a precise trigger |
   | Stable repetitive operation with deterministic input/output | Script or automation with failure/rollback behavior |
   | One-off task, uncertain benefit, adequate existing capability | No new mechanism |

5. Rank by observed rework/time avoided, implementation effort, execution cost,
   maintenance and risk. For replacing a subscription, compare the **required
   subset** with an existing local/open-source option and total ownership cost.
   “Local” or “open source” does not mean zero operating cost. A demo is not proof
   that collaboration, backup, support and security requirements are covered.
6. Deliver a short Markdown audit: scope/sample, ranked findings, evidence,
   recommended action, acceptance check, cost/benefit uncertainty. Diagnosis alone
   authorizes no changes. Apply only the changes the user has requested; do not
   auto-install tools, delete memory, change models/hooks or schedule audits.

For progress friction, use required host updates first. External completion
notifications require explicit destination/purpose authorization; do not create
Telegram/Slack/email side effects merely because an example recommends them.

## Context and task state

For work spanning phases/sessions, reuse the existing project plan or task record.
Keep an index of authoritative artifacts rather than a new copy of all content:

```text
Outcome / acceptance:
Constraints / authorized scope:
Inputs: path or URL, relevant section, version/date
Decisions: chosen option, reason, unresolved alternative if material
State: changed files, commit/diff identity, current environment
Evidence: check, result, state tested; unresolved hypotheses separately
Next action / blocker / rollback:
```

Update when decisions/state change, before handoff or compaction; no every-tool
journaling. On resume, read this record and inspect the current diff/state before
trusting a cached PASS. Repeat only checks whose relevant inputs changed or whose
freshness is insufficient. A historical PASS is not fresh completion evidence.

Use file paths plus selective parsing for large data, not full data pasted into
prompts. Markdown extraction helps retrieval; original tables, formulas, page
layout and signatures remain authoritative when the task depends on them.
Skill/tool discovery should load descriptions first where the host supports it;
this does not authorize rewriting runtime configuration.

Do not encode fixed “safe context percentages”, cache lifetimes, model rankings
or universal savings from videos. These depend on host, model, task and date.
Keep related work together when useful; do not send artificial keep-alives to
preserve a cache. Handoff/compaction is for continuity and relevant context, not a
timer-driven ritual. Check current primary docs before provider-specific advice.

## Separate the costs

1. Caveman reduces conversational output. Default lite, respecting an explicit
   level or opt-out. It does not compress code, documents, exact evidence or the
   model's internal reasoning. Required host progress updates still apply.
2. RTK filters shell output before it reaches the model. It does not compress MCP
   responses, images, the conversation or every token billed by a provider.
3. Targeted reads and on-demand skill references reduce input context. Loading
   several equivalent skills or forking the full conversation can erase savings.

## Claude Code and Codex

| Runtime | Normal route | Verification |
|---------|--------------|--------------|
| Claude Code | Existing RTK PreToolUse hook for Bash; explicit RTK also works | Exercise `rtk hook claude` with a harmless Bash payload. Parse JSON; confirm `updatedInput.command`. Already-prefixed input must not become `rtk rtk`. |
| Codex | Explicit `rtk <supported-command>` through the host shell tool | Execute a read-only command and check result/exit code. Do not assume a Claude Bash matcher intercepts exec_command. |
| Either, RTK missing | Native commands with bounded searches and reads | Report absence once only if material. Keep working; no automatic installation. |

Use installed `rtk --help` or `rtk <command> --help` when syntax is uncertain.
`rtk rewrite 'git status --short'` previews a known rewrite without executing it.
No rewrite is a reason to use native execution or `rtk proxy`, not to invent syntax.
Do not modify RTK-generated host instructions or third-party skills from AOS.

## Output selection

- Good candidates: directory/status summaries, repeated compiler/test output,
  broad discovery with `rtk rg` followed by exact reading of relevant files.
- Keep raw: instructions, source being edited, final review diffs, protocol JSON,
  structured service responses, security decisions, exact-match/absence checks.
- Preserve the project's command, arguments, environment and exit status.
  No `--skip-env`, silent error handling, or pipeline that loses the check's status.
- Recover a filter's saved output when available; otherwise repeat only a safe
  read. For mutating commands inspect logs/state, never rerun solely for verbosity.
- Verified with RTK 0.49.0: `rtk test python3 <unittest-file>` kept exit 1 and
  `FAILED (failures=1)` but elided the assertion message. Its `rtk recall <hash>`
  recovered the message. A failure summary alone is insufficient for diagnosis.
- Do not feed RTK's summary into a parser expecting the original output.
- RTK is not a secrets scrubber. Sensitive command output must remain protected.

## Measure, do not promise

For an audit, capture `rtk gain --format json` before and after a representative
task. Report deltas of commands and estimated input/output tokens; concurrent
sessions contaminate global deltas. Use `--project` where appropriate. Never reset
the user's counters. RTK estimates tokens; this is not provider billing telemetry.
Do not translate its percentages into weekly quota or euros saved.

Compare the same files/commands before and after. Report bytes/words as such when
no tokenizer is available. A smaller SKILL.md measures prompt size, not proof that
an LLM follows every rule. Distinguish mechanical checks from real host sessions.
Loading Caveman for the first time also costs input tokens: amortize it across
the session and never claim that shorter AOS alone reduces the entire cold start.

Sources: installed CLI help is authoritative for its version. Upstream:
https://github.com/rtk-ai/rtk (output filtering and measurement limitations).

## Explicit measurement record

`bin/aos-measure.py` runs on **every T2/T3 task**, not only on a requested comparison,
and `SKILL.md` §5 is where that obligation lives. `start` creates a task record;
`finish` adds outcome, corrections and elapsed wall time. Provider counters are
optional, attributed to `--metric-source`; unknown values remain null. RTK saved-token
estimates have their own field/source and are never added to provider usage or
converted to money. No personal histories or runtime configuration are collected.
See `--help` on the selected subcommand.

**Why it is not optional, measured.** Until 2026-09-15 the only instruction naming this
tool was this section, and it read "use only for a requested comparison" — inside a file
that is itself loaded only on request. Records produced in the meantime, across the whole
machine: **zero**. AOS 1.10 had already named measuring real tasks as its own next
investment and shipped the tool to do it. The tool was never the missing part; the
trigger was. The same trap is documented one ecosystem over: gstack's skill instructions
carry issue #2402, where 43 of 44 learnings arrived only from an explicit command because
"if you discovered" read as optional. A conditional step is a step the author never fires
against themselves.

```bash
RECORD="docs/misure/$(date -u +%F)-fix-csv-export.json"
python3 "$AOS_DIR/bin/aos-measure.py" start --record "$RECORD" \
  --task "Fix CSV export" --runtime codex --model configured --version 1.16.0
python3 "$AOS_DIR/bin/aos-measure.py" finish --record "$RECORD" \
  --outcome accepted --corrections 1
```

The record belongs in the project's `docs/misure/` and gets committed with the work, for
the reason the review logs do: an ignored directory is where evidence goes to disappear.
Outside a repository, put it where that project's durable task record already lives and
say where. `--outcome blocked` exists so a task stopped by a missing capability, an
exhausted quota or a declared gate is not filed as `partial`.

For comparison use the same task acceptance and comparable model/runtime; record
both successes and rejected outcomes. A single record is a baseline, not evidence
of savings. The measurement helper does not auto-select or bill model calls.

Behavior scenarios for instruction changes live in `evals/scenarios.json` in the
source repository. Evaluate actual agent decisions against the expectations;
these are not deterministic unittest cases or a word-matching score.
