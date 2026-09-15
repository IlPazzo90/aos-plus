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
# Each literal is split so that this file does not match its own definitions. Two
# review rounds were spent blanking the definition line instead, and both times the
# blanking hid something real — a private path written inside the value itself.
# shellcheck disable=SC2016
PATTERNS='/us''ers/[a-z][a-z0-9._-]*'
PATTERNS="$PATTERNS|-----beg""in [a-z ]*private key"
PATTERNS="$PATTERNS|clau""de-session:"
PATTERNS="$PATTERNS"'|[a-z0-9-]+@[a-z0-9.-]+\.(com|it):[0-9]{2,5}'
TERMS_FILE=.public-sanity-terms
if [ -f "$TERMS_FILE" ]; then
  extra=$(grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$TERMS_FILE" | paste -sd '|' -)
  [ -n "$extra" ] && PATTERNS="$PATTERNS|$extra"
fi

# A term written by hand can be an invalid regex, and grep answers an invalid regex
# the same way it answers "no match": exit 2 and nothing on stdout. Treating that as
# clean disables every check at once, generic ones included.
printf '' | grep -E "$PATTERNS" >/dev/null 2>&1
if [ "$?" -gt 1 ]; then
  echo "public-sanity: espressione non valida in $TERMS_FILE; controllo NON eseguito." >&2
  exit 2
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

# A scan that cannot run must not report a clean tree. Probe git before trusting it.
if ! git diff --cached --name-only >/dev/null 2>&1; then
  echo "public-sanity: impossibile leggere l'indice; controllo NON eseguito." >&2
  exit 2
fi

# No mapfile and no arrays that need bash 4: macOS ships bash 3.2, where this hook
# would have read an empty list and reported success on every commit. No here-doc
# either: on a read-only TMPDIR bash cannot create one, and the failure used to end
# in a green result.
scan() {
  while IFS= read -r -d '' f; do
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
    # Masking this file's content would publish the original: the list of private
    # terms must simply never be committed.
    case "$f" in "$TERMS_FILE")
      report "$f: il file dei termini privati non va committato"
      continue ;;
    esac
    hits=$(printf '%s\n' "$content" | grep -I -n -i -E "$PATTERNS" 2>/dev/null | cut -c1-200)
    if [ "$?" -gt 1 ]; then
      echo "public-sanity: scansione fallita su $f; controllo NON eseguito." >&2
      exit 2
    fi
    while IFS= read -r hit; do
      [ -n "$hit" ] && report "$f:$hit"
    done < <(printf '%s\n' "$hits")
  done
}

# -z, because git quotes names with accents, tabs or newlines and the quoted form is
# not a path: `git show ":\"café.md\""` fails, and a skipped file used to pass.
# T is in the filter too: replacing a tracked symlink with a real file is a type
# change, and its new content is just as publishable.
if [ "$#" -gt 0 ]; then
  staged=0
  scan < <(printf '%s\0' "$@")
else
  staged=1
  scan < <(git diff --cached -z --name-only --diff-filter=ACMRT)
fi

finish
