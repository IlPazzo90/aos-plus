# AOS Plus — AI Development Operating System

A reusable process skill for Claude Code and Codex: classify scope and risk, load
relevant specialist skills, verify work with evidence, and organize context using
Interpretable Context Methodology (ICM). Public edition 1.19.0, with a fresh history. Repository: `aos-plus`.
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
without failing, when `tmp/` exceeds 200 MB and, if a `DISTRIBUTION` file names a
checkout of a derived edition, when that checkout is at another version. Tests do not
call model providers or prove cross-model behavior.

You do not have to remember to run the doctor: `aos-profile.sh`, which runs at the
start of the work anyway, prints the loaded AOS version and the state of the link.

### The measurement record

Every T2/T3 task closes with a record, not an impression:

```sh
RECORD="docs/misure/$(date -u +%F)-short-slug.json"
python3 bin/aos-measure.py start --record "$RECORD" \
  --task "What was asked" --runtime codex --model configured --version 1.19.0
python3 bin/aos-measure.py finish --record "$RECORD" --outcome delivered --corrections 1
# later, once the user has said what they think of it:
python3 bin/aos-measure.py judge --record "$RECORD" --verdict accepted
```

`finish --outcome` is one of `delivered | partial | blocked`: what was handed over.
`blocked` exists so a task stopped by a missing capability or an exhausted quota is not
filed as partial. `accepted` and `rejected` are the user's words: `judge` writes them
once, after the user has spoken, and keeps what `finish` had said in `delivered_as`. The
author writing `accepted` at finish — which the first records did — is the author grading
their own work, the judgement the record exists to replace.
Provider counters are optional and must name their `--metric-source`; unknown values stay
null and are never estimated. Nothing is collected automatically, no history is read, and
no provider is contacted. The record is committed with the work — a measurement left in an
ignored directory is a measurement nobody will ever compare anything against.

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
