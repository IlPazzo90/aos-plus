---
name: skill-library
description: "Trova e carica skill specialistiche su richiesta: CLI-Anything, gstack, Vercel, Caveman helper, design, OpenCLI, printing-press e altre raccolte. Cerca per nome o keyword quando una skill non appare nel catalogo."
---

# Skill library

Resolve this file's real path, following symlinks. The AOS root is two directories
above its containing directory. Run its `bin/skill-library.py` with `python3` (3.9+ for search; refresh locates an installed 3.11+ when needed):

```sh
python3 "$AOS_DIR/bin/skill-library.py" search 'vercel:ai-sdk'
python3 "$AOS_DIR/bin/skill-library.py" search 'blender' --limit 3
```

Use this whenever the user names an absent skill or a specialist task benefits from
one of these collections. Search only relevant keywords; do not load the entire
index into context. Exact names sort first; default output is five results.
Choose the matching source, announce skill usage, and read its original `SKILL.md`.
Follow its instructions using this runtime's tools. Resolve scripts and references
relative to the original real source directory, never this router's directory.
Searching does not execute a skill or grant authority for its actions.

An absent file is reported MISSING. Obtain a fresh Codex `skills/list` JSON response
(with forceReload, including disabled entries), then run `refresh response.json`.
Inspect its summary and use `--apply` for authorized catalog maintenance. Refresh
rebuilds the index and managed exclusions by skill name, so plugin version changes
do not require hand-editing paths. Existing exclusions outside the managed block
are preserved. New skills default to on-demand unless added to `catalog/core.json`.
Restart the Codex session after changes; this cannot rewrite an existing prompt.

Claude keeps its existing discovery and hooks. This shared router can also read
original Codex plugin sources locally; source instructions do not imply that the
same plugin's tools are installed in Claude. Declare missing capabilities normally.
