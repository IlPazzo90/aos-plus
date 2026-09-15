# AOS Plus — AI Development Operating System

A reusable process skill for Claude Code and Codex: classify scope and risk, load
relevant specialist skills, verify work with evidence, and organize context using
Interpretable Context Methodology (ICM). Public edition 1.13.0, with a fresh history. Repository: `aos-plus`.
The installed skill remains named `aos` for Claude Code and Codex compatibility.

## Installation

Requires Bash and Python 3.9+ for helpers. The two tests that exercise TOML catalog
maintenance need stdlib `tomllib`; below Python 3.11 they are skipped and reported
as skipped, not failed.
Clone this repository, inspect the scripts, then choose a host:

```sh
bash bin/aos-install.sh --host codex --from "$PWD" --dry-run
bash bin/aos-install.sh --host codex --from "$PWD"
bash bin/aos-install.sh --host claude --from "$PWD"
```

Targets are `~/.agents/skills/aos` and `~/.claude/skills/aos`. Existing installations
are backed up. Install only the hosts you use; restart the agent session to refresh
skill discovery. Invoke `$aos` in Codex or `/aos` in Claude Code. Keep host-specific
permissions and hooks separate. Installation does not grant deployment authority.

## Context and verification

[SKILL.md](SKILL.md) is the entrypoint. Supporting references load only when needed.
[ICM guidance](references/orchestration.md#icm) covers selective CONTEXT.md routing,
canonical sources, stage contracts, run records and dependent-stage verification.
AOS retains its own risk and review gates. The [ICM paper](https://arxiv.org/abs/2603.16021)
is the methodology reference; this distribution is an adaptation, not an upstream
implementation or a controlled performance benchmark.

```sh
python3.12 -m unittest discover -s tests -v
bash bin/aos-install.sh --host codex
bash bin/aos-install.sh --host claude
python3 bin/aos-doctor.py
```

The doctor compares two installations; with one host it will report the missing
other host. Use the selected-host installer verification when only one is installed.
Tests do not call model providers or prove cross-model behavior.

## Optional integrations

RTK, Caveman, Superpowers and verify-agent are discovered separately, not bundled.
Missing dependencies must be reported according to the applicable gate. No external
review is supplied merely by installing AOS. Do not copy another user's permissions.

`catalog/index.json` ships empty and `catalog/core.json` contains only AOS/router
names. Generate a local index from your runtime's skills/list response using
`python3 bin/skill-library.py refresh --help`. Generated indexes can expose local
paths and installed tools: keep them out of public commits. Refresh may modify
Codex configuration when explicitly applied; inspect its preview first.

This package excludes private project history, workstation catalogs and internal
review transcripts. Maintain local customizations separately; inspect every public
release payload and its history. See [CHANGELOG.md](CHANGELOG.md).
