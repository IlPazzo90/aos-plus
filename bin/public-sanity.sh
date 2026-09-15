#!/bin/bash
# Refuse to publish content that names the maintainer, their machine or their clients.
#
# This distribution is assembled by copying files out of a private repository, and
# that copy is where sanitization gets skipped.
#
# The private terms do not live in this file. A guard that lists the names it hides
# publishes them itself — the first version of this script did exactly that. Put them
# in `.public-sanity-terms`, one extended-regex fragment per line, which `.gitignore`
# keeps out of the repository. The generic patterns below name nobody and always run.
#
# Scans staged content by default, or the paths given as arguments.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2

# Shapes, not identities: home paths, key material, session trailers, private hosts.
PATTERNS='/users/[a-z][a-z0-9._-]*|-----begin [a-z ]*private key|claude-session:|[a-z0-9-]+@[a-z0-9.-]+\.(com|it):[0-9]{2,5}'
TERMS_FILE=.public-sanity-terms
if [ -f "$TERMS_FILE" ]; then
  extra=$(grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$TERMS_FILE" | paste -sd '|' -)
  [ -n "$extra" ] && PATTERNS="$PATTERNS|$extra"
fi

fail=0
report() {
  if [ "$fail" -eq 0 ]; then
    echo "Pubblicazione bloccata: contenuto che non appartiene a un repository pubblico." >&2
    echo >&2
  fi
  fail=1
  printf '  %s\n' "$1" >&2
}

# No mapfile and no arrays that need bash 4: macOS ships bash 3.2, where this hook
# would have read an empty list and reported success on every commit.
if [ "$#" -gt 0 ]; then
  list=$(printf '%s\n' "$@")
else
  # Added, copied or modified staged files only; deletions have nothing to scan.
  list=$(git diff --cached --name-only --diff-filter=ACM)
fi

while IFS= read -r f; do
  [ -n "$f" ] && [ -f "$f" ] || continue
  # The script's own pattern list matches the patterns. Skipping it is the same
  # documented self-match every scanner has; everything else is still scanned.
  case "$f" in "$TERMS_FILE"|.githooks/pre-commit|bin/public-sanity.sh) continue ;; esac
  # -I skips binaries. Report file:line so the offending text is findable.
  while IFS= read -r hit; do
    report "$hit"
  done < <(grep -I -n -i -E "$PATTERNS" "$f" 2>/dev/null | sed "s|^|$f:|" | cut -c1-200)
done <<EOF
$list
EOF

# The commit message travels with the commit and git grep never reads it.
if [ "$#" -eq 0 ] && [ -f .git/COMMIT_EDITMSG ]; then
  while IFS= read -r hit; do
    report "messaggio di commit: $hit"
  done < <(grep -I -n -i -E "$PATTERNS" .git/COMMIT_EDITMSG 2>/dev/null | cut -c1-200)
fi

if [ "$fail" -ne 0 ]; then
  echo >&2
  echo "Correggi il testo, oppure aggiungi una motivazione esplicita e usa --no-verify" >&2
  echo "solo se hai verificato tu che quella riga possa essere pubblica." >&2
  exit 1
fi

if [ -f "$TERMS_FILE" ]; then
  echo "public-sanity: nessun riferimento privato nel contenuto in uscita."
else
  echo "public-sanity: solo controlli generici; $TERMS_FILE assente, i termini propri non sono coperti." >&2
fi
