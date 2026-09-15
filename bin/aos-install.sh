#!/usr/bin/env bash
# aos-install.sh — install, update or verify the AOS skill. Idempotent.
#
#   aos-install.sh                    verify the installed skill (default)
#   aos-install.sh --from <dir>       install/update from <dir>, backing up first
#   aos-install.sh --from <dir> -n    dry run: show what would change, touch nothing
#   aos-install.sh --uninstall        back up, then remove the skill
#
# --host claude (default) or --host codex selects one installation.
# Codex target: ~/.agents/skills/aos; backups: ~/.agents/backups.
# Never deletes another skill. Never needs sudo.

set -u

TARGET="$HOME/.claude/skills/aos"
BACKUP_ROOT="$HOME/.claude/backups"

HOST="claude"
SOURCE=""
DRY=0
MODE="verify"

while [ $# -gt 0 ]; do
  case "$1" in
    --host)      [ $# -ge 2 ] || { echo "--host richiede claude o codex" >&2; exit 2; }; HOST="$2"; shift 2 ;;
    --from)      [ $# -ge 2 ] || { echo "--from richiede una directory" >&2; exit 2; }; SOURCE="$2"; MODE="install"; shift 2 ;;
    -n|--dry-run) DRY=1; shift ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help)   sed -n '2,12p' "$0"; exit 0 ;;
    *)           echo "opzione sconosciuta: $1" >&2; exit 2 ;;
  esac
done

case "$HOST" in
  claude) ;;
  codex) TARGET="$HOME/.agents/skills/aos"; BACKUP_ROOT="$HOME/.agents/backups" ;;
  *) echo "Host non valido: $HOST (usa claude o codex)" >&2; exit 2 ;;
esac

say() { echo "$@"; }
# I comandi arrivano come stringa singola perché alcuni contengono glob
# (chmod +x bin/*.sh) che devono espandersi. eval "$*" è la forma corretta.
run() { if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $*"; else eval "$*"; fi; }

REQUIRED_FILES="SKILL.md README.md CHANGELOG.md VERSION
references/orchestration.md
references/risk-and-tiers.md
references/project-profiles.md
references/quality-gates.md
references/output-contract.md
references/token-efficiency.md
bin/aos-profile.sh
bin/profile-python.py
bin/aos-doctor.py
bin/aos-measure.py
bin/aos-security.sh
bin/aos-install.sh
bin/skill-library.py
bin/codex-hook-adapter.py
catalog/core.json
catalog/index.json
catalog/skill-library/SKILL.md
evals/scenarios.json"

install_router() {
  local link="$(dirname "$TARGET")/skill-library"
  if [ -e "$link" ] || [ -L "$link" ]; then
    [ "$(readlink "$link")" = "$TARGET/catalog/skill-library" ] || { say "Router esistente non gestito: $link"; return 1; }
  else
    run "ln -s '$TARGET/catalog/skill-library' '$link'"
  fi
}

verify() {
  local root="$1" fail=0
  say "=== VERIFICA AOS in $root ==="
  if [ ! -d "$root" ]; then say "  MANCANTE: la directory non esiste"; return 1; fi

  for f in $REQUIRED_FILES; do
    if [ -f "$root/$f" ]; then
      say "  ok   $f"
    else
      say "  MANCA $f"; fail=1
    fi
  done

  # frontmatter must open the file and contain name + description
  if [ -f "$root/SKILL.md" ]; then
    if head -1 "$root/SKILL.md" | grep -q '^---$'; then say "  ok   frontmatter aperto"
    else say "  ERRORE SKILL.md non inizia con '---'"; fail=1; fi
    for key in name description; do
      if awk 'NR>1 && /^---$/{exit} NR>1' "$root/SKILL.md" | grep -q "^$key:"; then
        say "  ok   frontmatter: $key"
      else
        say "  ERRORE frontmatter senza '$key:'"; fail=1
      fi
    done
  fi

  # shell scripts must parse
  for s in "$root"/bin/*.sh; do
    [ -f "$s" ] || continue
    if bash -n "$s" 2>/dev/null; then say "  ok   sintassi $(basename "$s")"
    else say "  ERRORE sintassi in $(basename "$s")"; fail=1; fi
    [ -x "$s" ] || { say "  nota  $(basename "$s") non eseguibile (si usa comunque con 'bash')"; }
  done

  [ -f "$root/VERSION" ] && say "  versione: $(cat "$root/VERSION")"

  if [ "$fail" -eq 0 ]; then say "=== OK: AOS integro ==="; return 0
  else say "=== FALLITO: correggi i punti sopra ==="; return 1; fi
}

backup() {
  [ -d "$TARGET" ] || { say "Nessuna installazione precedente da salvare."; return 0; }
  local ver stamp dest
  ver="$( [ -f "$TARGET/VERSION" ] && cat "$TARGET/VERSION" || echo unknown )"
  stamp="$(date +%Y%m%d-%H%M%S)"
  dest="$BACKUP_ROOT/aos-$ver-$stamp"
  say "Backup: $TARGET -> $dest"
  run "mkdir -p '$BACKUP_ROOT'"
  run "cp -R '$TARGET' '$dest'"
}

case "$MODE" in
  verify)
    verify "$TARGET"; exit $?
    ;;

  install)
    if [ -z "$SOURCE" ] || [ ! -d "$SOURCE" ]; then
      say "--from richiede una directory esistente"; exit 2
    fi
    SOURCE="$(cd "$SOURCE" && pwd)"
    if [ "$SOURCE" = "$TARGET" ]; then
      say "Sorgente e destinazione coincidono ($TARGET): niente da copiare, eseguo la verifica."
      install_router || exit 1
      verify "$TARGET"; exit $?
    fi
    say "Verifico la sorgente prima di installare."
    verify "$SOURCE" || { say "Sorgente non valida: installazione annullata."; exit 1; }
    say
    backup
    say "Installo: $SOURCE -> $TARGET"
    run "mkdir -p '$TARGET'"
    # Copy maintained files only; preserve the target's Git history and local logs.
    for f in $REQUIRED_FILES; do
      run "mkdir -p '$TARGET/$(dirname "$f")'"
      run "cp '$SOURCE/$f' '$TARGET/$f'"
    done
    run "chmod +x '$TARGET'/bin/*.sh"
    install_router || exit 1
    say
    if [ "$DRY" -eq 1 ]; then say "Dry run: nessuna modifica scritta."; exit 0; fi
    verify "$TARGET"; exit $?
    ;;

  uninstall)
    [ -d "$TARGET" ] || { say "AOS non è installato."; exit 0; }
    backup
    if [ "$(readlink "$(dirname "$TARGET")/skill-library")" = "$TARGET/catalog/skill-library" ]; then
      run "rm '$(dirname "$TARGET")/skill-library'"
    fi
    say "Rimuovo $TARGET"
    run "rm -rf '$TARGET'"
    if [ "$DRY" -eq 1 ]; then
      say "Dry run: niente rimosso."
    else
      say "Fatto. Il backup resta in $BACKUP_ROOT."
    fi
    ;;
esac
