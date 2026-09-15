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
# Modes:
#   (no arguments)        scan the staged content, as a pre-commit hook
#   --message <file>      scan a commit message, as a commit-msg hook
#   <path> [path...]      scan files on disk, for a manual check
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2

# Shapes, not identities: home paths, key material, session trailers, private hosts.
# shellcheck disable=SC2016
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

finish() {
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
  exit 0
}

# A commit message never reaches the index, so git grep can never see it, and
# pre-commit runs before it exists: this is the commit-msg hook's job.
if [ "${1:-}" = "--message" ]; then
  [ -f "${2:-}" ] || { echo "public-sanity: file del messaggio mancante" >&2; exit 2; }
  while IFS= read -r hit; do
    report "messaggio di commit: $hit"
  done < <(grep -I -n -i -E "$PATTERNS" "$2" 2>/dev/null | cut -c1-200)
  finish
fi

# This script's own pattern line matches the patterns it defines. Blanking that one
# line keeps the file scanned: the incident this guard exists for was a private name
# added to a protected file, and skipping those files entirely would let it back in.
scrub_self() {
  sed -e "s|^PATTERNS=.*|PATTERNS=<definizione>|" -e "s|^\(\s*\)extra=.*|\1extra=<definizione>|"
}

# No mapfile and no arrays that need bash 4: macOS ships bash 3.2, where this hook
# would have read an empty list and reported success on every commit.
if [ "$#" -gt 0 ]; then
  list=$(printf '%s\n' "$@")
  staged=0
else
  # Added, copied, modified or renamed; a deletion has nothing left to scan.
  list=$(git diff --cached --name-only --diff-filter=ACMR)
  staged=1
fi

while IFS= read -r f; do
  [ -n "$f" ] || continue
  # The staged blob is what gets published. The working tree can differ from it in
  # both directions: cleaned after `git add` (a false pass) or dirty before it (a
  # false block). Read the index.
  if [ "$staged" -eq 1 ]; then
    content=$(git show ":$f" 2>/dev/null) || continue
  else
    [ -f "$f" ] || continue
    content=$(cat "$f" 2>/dev/null) || continue
  fi
  case "$f" in "$TERMS_FILE") content=$(printf '%s\n' "$content" | sed 's|.*|<termini privati>|') ;; esac
  case "$f" in bin/public-sanity.sh) content=$(printf '%s\n' "$content" | scrub_self) ;; esac
  while IFS= read -r hit; do
    report "$f:$hit"
  done < <(printf '%s\n' "$content" | grep -I -n -i -E "$PATTERNS" 2>/dev/null | cut -c1-200)
done <<EOF
$list
EOF

finish
