#!/usr/bin/env bash
# aos-install.sh — install, update or verify the AOS skill. Idempotent.
#
#   aos-install.sh                    verify the installed skill (default)
#   aos-install.sh --from <dir>       install/update from <dir>, backing up first
#   aos-install.sh --from <dir> -n    dry run: show what would change, touch nothing
#   aos-install.sh --uninstall        back up, then remove the skill
#   aos-install.sh --host codex --link   create or repair the Codex link
#
# --host claude (default) manages the one real installation, ~/.claude/skills/aos.
# --host codex manages ~/.agents/skills/aos, which is a symlink to it: --link
# creates or repairs the link (backing up a real directory found there), verify
# checks it, --uninstall removes only the link. Backups: ~/.agents/backups.
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
    --link)      MODE="link"; shift ;;
    -h|--help)   sed -n '2,14p' "$0"; exit 0 ;;
    *)           echo "opzione sconosciuta: $1" >&2; exit 2 ;;
  esac
done

case "$HOST" in
  claude) ;;
  codex) TARGET="$HOME/.agents/skills/aos"; BACKUP_ROOT="$HOME/.agents/backups" ;;
  *) echo "Host non valido: $HOST (usa claude o codex)" >&2; exit 2 ;;
esac
CLAUDE_ROOT="$HOME/.claude/skills/aos"

say() { echo "$@"; }
# I comandi arrivano come stringa singola perché alcuni contengono glob
# (chmod +x bin/*.sh) che devono espandersi. eval "$*" è la forma corretta.
run() { if [ "$DRY" -eq 1 ]; then echo "  [dry-run] $*"; else eval "$*"; fi; }

REQUIRED_FILES="SKILL.md README.md CHANGELOG.md VERSION
references/orchestration.md
references/context-budget.md
references/learning.md
references/risk-and-tiers.md
references/project-profiles.md
references/quality-gates.md
references/output-contract.md
references/token-efficiency.md
references/design.md
references/adaptive.md
bin/aos-profile.sh
bin/profile-python.py
bin/aos-doctor.py
bin/aos-measure.py
bin/aos-bench.py
bin/aos-context.py
bin/aos-learning.py
bin/aos-orchestrate.py
bin/aos-prompt-hook.py
bin/aos-operations.py
bin/aos-delegate.py
bin/aos-entry.py
bin/aos-pipeline.py
bin/aos-open-executor.py
bin/aos-isolation.py
bin/aos-router.py
bin/aos-status.py
bin/aos-security.sh
bin/aos-install.sh
bin/skill-library.py
catalog/core.json
catalog/index.json
catalog/skill-library/SKILL.md
config/open-models.json
config/adaptive.json
evals/scenarios.json
tests/test_bench.py
tests/test_context.py
tests/test_learning.py
tests/test_orchestrate.py
tests/test_prompt_hook.py
tests/test_operations.py
tests/test_hook_protocol.py
tests/test_delegate.py
tests/test_entry.py
tests/test_pipeline.py
tests/test_open_executor.py
tests/test_pipeline_runtime.py
tests/test_doctor.py
tests/test_install.py
tests/test_measure.py
tests/test_profile.py
tests/test_router.py
tests/test_security.py
tests/test_skill_library.py
tests/test_status.py"

install_router() {
  local link
  link="$(dirname "$TARGET")/skill-library"
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
  # A link holds nothing of its own: backing it up would copy the tree it points to.
  if [ -L "$TARGET" ] || [ ! -d "$TARGET" ]; then say "Nessuna installazione precedente da salvare."; return 0; fi
  local ver stamp dest
  ver="$( [ -f "$TARGET/VERSION" ] && cat "$TARGET/VERSION" || echo unknown )"
  stamp="$(date +%Y%m%d-%H%M%S)"
  dest="$BACKUP_ROOT/aos-$ver-$stamp"
  say "Backup: $TARGET -> $dest"
  run "mkdir -p '$BACKUP_ROOT'"
  run "cp -R '$TARGET' '$dest'"
}

# The Codex host is a link, not a copy. Two copies were the defect: the installer
# copied files, the Codex clone stayed on 1.14.0 with ten dirty files, and the
# doctor, the hash comparison and "a commit is not an install" existed to police a
# duplication that ~/.agents/skills already avoids for every other shared skill.
check_link() {
  say "=== VERIFICA LINK CODEX $TARGET ==="
  if [ ! -L "$TARGET" ]; then
    if [ -d "$TARGET" ]; then say "  COPIA: è una directory, non un link — esegui --host codex --link per sostituirla"
    else say "  MANCANTE: il link non esiste — esegui --host codex --link per crearlo"; fi
    return 1
  fi
  # Both sides must resolve: a dangling link and a missing installation both cd to
  # nothing, and "" = "" passed the check.
  local have want
  have="$(cd "$TARGET" 2>/dev/null && pwd -P)"; want="$(cd "$CLAUDE_ROOT" 2>/dev/null && pwd -P)"
  if [ -z "$want" ]; then
    say "  ERRORE: l'installazione $CLAUDE_ROOT non esiste — installa prima quella"; return 1
  fi
  if [ "$have" != "$want" ]; then
    say "  ERRORE: il link punta a $(readlink "$TARGET"), non a $CLAUDE_ROOT"; return 1
  fi
  say "  ok   $TARGET -> $CLAUDE_ROOT"
  return 0
}

if [ "$HOST" = "codex" ]; then
  case "$MODE" in
    verify)
      check_link || exit 1
      verify "$CLAUDE_ROOT"; exit $?
      ;;
    install)
      say "--from non si applica a --host codex: la sorgente è sempre $CLAUDE_ROOT (usa --link)"; exit 2
      ;;
    link)
      [ -d "$CLAUDE_ROOT" ] || { say "Installazione Claude assente in $CLAUDE_ROOT: installa prima quella."; exit 1; }
      if [ -L "$TARGET" ] && check_link >/dev/null; then
        say "Link già corretto: $TARGET -> $CLAUDE_ROOT"
      else
        backup
        if [ -L "$TARGET" ]; then run "rm '$TARGET'"
        elif [ -d "$TARGET" ]; then say "Rimuovo la copia $TARGET (backup sopra)"; run "rm -rf '$TARGET'"
        elif [ -e "$TARGET" ]; then
          # Not a link, not a directory: keep it, out of the way, and say where.
          stray="$BACKUP_ROOT/aos-file-$(date +%Y%m%d-%H%M%S)"
          say "$TARGET è un file, non un link: lo sposto in $stray"
          run "mkdir -p '$BACKUP_ROOT'"; run "mv '$TARGET' '$stray'"
        fi
        run "mkdir -p '$(dirname "$TARGET")'"
        run "ln -s '$CLAUDE_ROOT' '$TARGET'"
      fi
      install_router || exit 1
      if [ "$DRY" -eq 1 ]; then say "Dry run: nessuna modifica scritta."; exit 0; fi
      check_link || exit 1
      verify "$CLAUDE_ROOT"; exit $?
      ;;
    uninstall)
      if [ -L "$TARGET" ]; then
        if [ "$(readlink "$(dirname "$TARGET")/skill-library")" = "$TARGET/catalog/skill-library" ]; then
          run "rm '$(dirname "$TARGET")/skill-library'"
        fi
        say "Rimuovo il link $TARGET"; run "rm '$TARGET'"
      elif [ -d "$TARGET" ]; then
        # Only the link is ours to remove. A real directory here may be somebody's
        # clone with uncommitted work: --link backs it up and replaces it on request.
        say "$TARGET è una directory, non un link: non la rimuovo. Usa --host codex --link per sostituirla con il link (backup incluso), o rimuovila tu."
        exit 1
      else
        say "AOS non è collegato per Codex."
      fi
      exit 0
      ;;
  esac
fi

case "$MODE" in
  verify)
    verify "$TARGET"; exit $?
    ;;

  link)
    say "--link riguarda solo --host codex: l'installazione Claude è la sorgente."; exit 2
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
    # A file dropped from the manifest must leave the target too: the copy step alone
    # left bin/codex-hook-adapter.py and its test alive on the Codex host after 1.17.0
    # removed them, and the doctor, which compares maintained files, could not see it.
    # Only files the target's own previous manifest named are candidates for removal.
    # shellcheck disable=SC2086  # word splitting is the point: one name per word
    current=" $(printf '%s ' $REQUIRED_FILES)"
    if [ -f "$TARGET/bin/aos-install.sh" ]; then
      previous="$(sed -n '/^REQUIRED_FILES="/,/"/p' "$TARGET/bin/aos-install.sh" | tr -d '"' | sed 's/^REQUIRED_FILES=//')"
      # Flattened on one line: the manifest is newline-separated, and matching
      # " name " against it found only the names on the first line — the dry run
      # of that version would have removed every other maintained file.
      for f in $previous; do
        case "$current" in *" $f "*) ;; *)
          case "$f" in */..*|/*) continue ;; esac
          [ -f "$TARGET/$f" ] && { say "Rimuovo (uscito dal manifesto): $f"; run "rm '$TARGET/$f'"; } ;;
        esac
      done
    fi
    # tests/ is AOS-owned in full and entered the manifest only in 1.17.0: a test file
    # the previous manifest never named would otherwise survive the removal of the
    # module it imports and break discovery on the upgraded host.
    for f in "$TARGET"/tests/test_*.py; do
      [ -f "$f" ] || continue
      rel="tests/$(basename "$f")"
      case "$current" in *" $rel "*) ;; *) say "Rimuovo (test fuori manifesto): $rel"; run "rm '$TARGET/$rel'" ;; esac
    done
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
    # The Codex link points here: left behind it dangles. Only links are removed,
    # and only the ones that point at this installation.
    CODEX_LINK="$HOME/.agents/skills/aos"
    if [ -L "$CODEX_LINK" ] && [ "$(readlink "$CODEX_LINK")" = "$TARGET" ]; then
      if [ -L "$HOME/.agents/skills/skill-library" ] \
         && [ "$(readlink "$HOME/.agents/skills/skill-library")" = "$CODEX_LINK/catalog/skill-library" ]; then
        run "rm '$HOME/.agents/skills/skill-library'"
      fi
      say "Rimuovo il link Codex $CODEX_LINK"; run "rm '$CODEX_LINK'"
    fi
    if [ "$DRY" -eq 1 ]; then
      say "Dry run: niente rimosso."
    else
      say "Fatto. Il backup resta in $BACKUP_ROOT."
    fi
    ;;
esac
