# AOS Plus — AI Development Operating System

> **3.0.0.** Chiedi «scrivimi un contratto di manutenzione» e AOS risponde da solo:
> dominio LEGAL_COMPLIANCE, le skill legali e di scrittura pertinenti, esecutore MID,
> perché il modello open del catalogo dichiara 0,4 di capacità legale e il router lo
> scarta prima ancora di dargli un punteggio. Il modello non lo sceglie più il tier:
> lo sceglie il costo atteso, che conta anche i retry e l'escalation di un modello
> economico che sbaglia spesso. Sei domini, bundle solo quando servono, un hook che
> classifica ogni richiesta senza spendere token, una matrice di prestazioni che nasce
> dall'uso reale. Come funziona: [adaptive](references/adaptive.md).

A reusable process skill for Claude Code and Codex: classify scope and risk, load
relevant specialist skills, verify work with evidence, and organize context using
Interpretable Context Methodology (ICM). Public edition 1.22.0, with a fresh history. Repository: `aos-plus`.
The installed skill remains named `aos` for Claude Code and Codex compatibility.

## Installation

Requires Bash and Python 3.9+ for helpers. The two tests that exercise TOML catalog
maintenance need stdlib `tomllib`; below Python 3.11 they are skipped and reported
as skipped, not failed.
Clone this repository, inspect the scripts, then install:

```sh
bash bin/aos-install.sh --host claude --from "$PWD" --dry-run
bash bin/aos-install.sh --host claude --from "$PWD"    # the one installation
bash bin/aos-install.sh --host codex --link            # Codex reads it through a link
```

There is one installation, `~/.claude/skills/aos`, backed up before every update.
`~/.agents/skills/aos` is a symlink to it, which is how Codex already shares every other
skill: nothing to keep in step, nothing to drift. Until 1.17.0 the Codex side was a second
copy, and a whole apparatus — hash comparison, "a commit is not an install" — existed to
police a duplication a link avoids. If a real directory is found on the Codex path,
`--link` backs it up under `~/.agents/backups/` and replaces it; `--host codex` alone
verifies the link; `--host codex --uninstall` removes only the link. Restart the agent
session to refresh skill discovery. Invoke `$aos` in Codex or `/aos` in Claude Code. Keep
host-specific permissions and hooks separate. Installation does not grant deployment
authority.

## Prompt hook (optional, 3.0.0)

`bin/aos-prompt-hook.py` adds one classification line (domains, task type, pertinent
skills) to every request, locally, with no model call. It reads the prompt from stdin,
never runs anything, never touches the network and always exits 0; greetings, slash
commands and short prompts stay silent, and `AOS_PROMPT_HOOK=off` turns it off. The
installer does not register it: add a `UserPromptSubmit` entry yourself.

```json
{"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "timeout": 5,
  "command": "[ ! -f \"$HOME/.claude/skills/aos/bin/aos-prompt-hook.py\" ] || python3 \"$HOME/.claude/skills/aos/bin/aos-prompt-hook.py\""}]}]}}
```

Claude Code: merge it into `~/.claude/settings.json`. Codex: merge it into
`~/.codex/hooks.json`; Codex runs a new hook only after you trust its hash (`/hooks` in
the TUI). The line is a hint: tier, risk and executor come from the census and
`bin/aos-orchestrate.py route`.

## The UI/UX role

When a change is something a person looks at, `references/design.md` adds a designer to
the process rather than leaving the appearance to whatever the implementer reaches for
first. Seven stages, each owing one artifact somebody can inspect: plan review, a written
direction, tokens, library choice, variants, motion, and a final review that returns
findings rather than an approval. It routes to design skills that may already be present
in the host — the file names them per stage — and it names the ones a model is not allowed
to invoke, which the user runs instead.

Its acceptance gate is checked on the **rendered** result: a driven browser, a simulator,
the exported file. Not the source, and not one theme only — light and dark do not behave
the same way. Contrast, target size, field font size on touch, focus, reduced motion and
the declared direction are each a checkable line. A stage with no artifact was not
executed, and a gate item checked by reading the CSS was not checked.

## Context and verification

[SKILL.md](SKILL.md) is the entrypoint. Supporting references load only when needed.
[ICM guidance](references/orchestration.md#icm) covers selective CONTEXT.md routing,
canonical sources, stage contracts, run records and dependent-stage verification.
AOS retains its own risk and review gates. The [ICM paper](https://arxiv.org/abs/2603.16021)
is the methodology reference; this distribution is an adaptation, not an upstream
implementation or a controlled performance benchmark.

```sh
python3.12 -m unittest discover -s tests -v
bash bin/aos-install.sh --host claude    # verify the installation
bash bin/aos-install.sh --host codex     # verify the link
python3 bin/aos-doctor.py
```

The doctor checks the Codex link and the maintained files of the one installation; a
real directory on the Codex path is reported as `COPIA` even when its contents are
identical, because a copy is a Codex that will read an older AOS the day the
installation changes. Without Codex, ignore that one line. The doctor also warns,
without failing, when `tmp/` exceeds 200 MB, when `~/.claude/backups` or
`~/.agents/backups` exceed 1 GB, and, if a `DISTRIBUTION` file names a checkout of a
derived edition and the files the two keep identical, when that checkout is at another
version or one of those files differs. Tests do not call model providers or prove
cross-model behavior.

You do not have to remember to run the doctor: `aos-profile.sh`, which runs at the
start of the work anyway, prints the loaded AOS version and the state of the link.

### The measurement record

Every T2/T3 task closes with a record, not an impression:

```sh
RECORD="docs/misure/$(date -u +%F)-short-slug.json"
python3 bin/aos-measure.py start --record "$RECORD" \
  --task "What was asked" --runtime codex --model configured --version 1.22.0
python3 bin/aos-measure.py finish --record "$RECORD" --outcome delivered --corrections 1
# later, once the user has said what they think of it:
python3 bin/aos-measure.py judge --record "$RECORD" --verdict accepted
```

`finish --outcome` is one of `delivered | partial | blocked`: what was handed over.
`blocked` exists so a task stopped by a missing capability or an exhausted quota is not
filed as partial. `accepted` and `rejected` are the user's words: `judge` writes them
once, after the user has spoken, and keeps what `finish` had said in `delivered_as`. The
author writing `accepted` at finish — which the first records did — is the author grading
their own work, the judgement the record exists to replace. Since 1.21.0 `start` lists the
sibling records that were delivered and never judged, with the command to record the
verdict: the start of the next task on the same repository is when someone is there to
answer, and a record nobody asks about stays `delivered` forever.
Provider counters are optional and must name their `--metric-source`; unknown values stay
null and are never estimated. Nothing is collected automatically, no history is read, and
no provider is contacted. The record is committed with the work — a measurement left in an
ignored directory is a measurement nobody will ever compare anything against.

Since 2.1.1 the record also carries who really executed the task: `main_executor_runtime`,
`main_executor_model`, `main_executor_provider`, `routed_by_aos`, `manual_model_override`,
`delegated_open_tasks`, the open and premium token counts, `escalation_count`,
`escalation_reason` and the `workload_open_ratio` / `premium_dependency_ratio`, so the
share of work actually done by open models is measured rather than asserted. The executor
identity is set at `start` when the router chose; the token counts and ratios at `finish`.

## Role pipeline (2.4.0, verification pending)

Eligible T2/T3 work uses a read-only premium planner, open execution and fixes,
deterministic checks, then an opposite-family premium reviewer. Findings need
mechanical confirmation or counterevidence. Premium execution requires exhausted
open attempts or an existing explicit policy exception. See the
[role contract and diagram](references/orchestration.md#explicit-role-pipeline).

## Model routing and the open runtime (2.1.1)

**AOS is the router.** It chooses the task executor from tier, risk, complexity,
uncertainty, security impact, required capabilities, the configured benchmark winner
and the retry/failure history; the manual main model of the host session is a
fallback runtime model, not the default. `bin/aos-router.py` `decide()` is the pure,
tested decision function and `config/open-models.json` is the executable policy:
T0/T1 and T2 at risk ≤ MEDIUM go to the open benchmark winner; T2/HIGH only with an
observable check, and then a premium review is mandatory; T3 keeps premium planning
and final review with open bounded subtasks; CRITICAL stays on the main session with
approval; an explicit user override sends the work to the main model. Missing or
invalid config falls back to the legacy behaviour, where the main session executes.
Premium escalation and review run on the subscriptions already paid for Claude Code
and Codex CLI, never a separate metered API key: the open executor is the only metered
path, and the one the router prefers for eligible work.

The open model has no host of its own: it runs inside the **Claude Code harness** as a
bounded worker with file tools only (Read/Glob/Grep/Edit/Write, no Bash), through
`bin/aos-open-executor.py`, which reports exit code, diff, seconds, usage read from the
CLI's own events (null when not reported, never estimated) and the tool calls the worker
made. `bin/aos-delegate.py` keeps the guards around each attempt: a clean tree or only
worker-owned dirt, retry ownership, a fingerprint of the repository metadata (config,
hooks, includes and the pointers git follows — no `git` of ours runs on a repo whose
metadata the worker changed), a process-group timeout and a step cap. Around the process
there is a macOS `sandbox-exec` profile: the home, the temp trees and every write are
denied, the repository and the run's own directories are re-allowed, then the secrets
inside the repository and `.git`/`.claude`/`.codex` are denied again. The provider
credential is read by the host from `api_key_env` or `api_key_file` and handed to the
process as an environment variable a shell-less worker cannot read back.

`bin/aos-isolation.py` is what makes that a claim with evidence rather than a
configuration: it builds a disposable fixture (a repository with a `.env`, a
certificate, a symlink pointing outside, a canary in the real home) and asks the worker
to read and write every forbidden target. The verdict is mechanical — a target counts
as attempted only when a tool call of the matching family names it in a path field, and
the two permitted controls must succeed, because a worker that did nothing proves
nothing. A guard, not a sandbox (2.0.1). No model name lives in the code: the benchmark winner is chosen with `bin/aos-bench.py`, which replays real commits from the
user's own repositories against each model — the commit's test is the spec the worker
reads and the judge it cannot rewrite — and writes first-pass, retry, escalation,
seconds, tokens, cost and reviewer findings side by side, and its winner is written
into `config/open-models.json` as `open.primary` with a `fallback`; the rule is in the
code, the names are configuration. The corpus and the records
are the user's and stay out of this distribution.

## Optional integrations

When the reviewer's quota runs out mid-gate, the round is retried once on a reserve
model and comes back marked `degraded`: **its findings count, its PASS does not**, because
the absence of defects is what depends on the strength of whoever looked. A gate closed
that way is VERIFICATO CON RISERVE, never VERIFICATO.

RTK, Caveman, ponytail, Superpowers, verify-agent and the design skills are discovered
separately, not bundled. The three cost tools work on three different surfaces and are
easy to confuse: Caveman shortens what the agent writes to you, RTK shortens what the
shell writes back to the agent, ponytail shortens what the agent writes into your
repository. Only the last one saves anything after the session ends. `references/design.md` lists the skills each stage can use; where none are
installed, the stages and the gate still apply — they cost more attention, not less
correctness.
Missing dependencies must be reported according to the applicable gate. No external
review is supplied merely by installing AOS. Do not copy another user's permissions.

`catalog/index.json` ships empty and `catalog/core.json` names only AOS, its router
and the four skills `SKILL.md` routes to by name. Generate a local index with
`python3 bin/skill-library.py refresh --discover`, which starts a local `codex
app-server` and asks it for `skills/list`; a snapshot obtained otherwise is passed as
the argument. Generated indexes can expose local paths and installed tools: keep them
out of public commits. Refresh may modify
Codex configuration when explicitly applied; inspect its preview first.

This package excludes private project history, workstation catalogs and internal
review transcripts. Maintain local customizations separately; inspect every public
release payload and its history. See [CHANGELOG.md](CHANGELOG.md).

## Open execution

The open model runs through the `claude-code` adapter in `bin/aos-open-executor.py`,
with the configured open provider/model, independently of whether the main session is
Claude Code or Codex. It has file tools only; the deterministic tests run in the host's
Verify stage, on the safe side of the boundary. See [runtime configuration and
boundaries](references/orchestration.md#runtime-independent-open-executor).

The `codex-cli` adapter is present but the policy keeps it out of open execution:
Codex edits files through its shell tool, whose command policy did not hold a negative
probe, and without that shell it has no file tools at all. Codex remains the premium
host, planner, cross-family reviewer and escalation — which is where the independent
gate lives. OpenCode was removed from the project on 2026-09-22, after its probe read
a denied `.env`, followed a symlink out of the repository and wrote through it.

The measured model winner of the 2026-09-20 benchmark remains the default; no model or
runtime is promoted without a complete, reviewed comparison.

Verified lesson ledger, context handoff and budget admission: [learning](references/learning.md).
