#!/usr/bin/env bash
# aos-profile.sh — deterministic project orientation for AOS.
# Prints stack, real commands, instruction files and deploy target in one call,
# so the agent stops guessing. Read-only: never writes, never hits the network.
#
# Usage: aos-profile.sh [path]   (default: current directory)

SCRIPT_SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SCRIPT_SOURCE" ]; do
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
  SCRIPT_SOURCE="$(readlink "$SCRIPT_SOURCE")"
  case "$SCRIPT_SOURCE" in /*) ;; *) SCRIPT_SOURCE="$SCRIPT_DIR/$SCRIPT_SOURCE" ;; esac
done
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SOURCE")" && pwd)"
# Host Python parses metadata only; never import project code.
host_python=""
for candidate in python3.12 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    host_python="$(command -v "$candidate")"; break
  fi
done
DIR="${1:-$PWD}"

if [ ! -d "$DIR" ]; then
  echo "aos-profile: '$DIR' is not a directory" >&2
  exit 1
fi
cd "$DIR" || exit 1

has() { [ -e "$1" ]; }
# any file matching a glob, at depth <= 2, ignoring vendor dirs
anyfile() {
  find . -maxdepth 2 \
    \( -name node_modules -o -name .git -o -name vendor -o -name .next \) -prune -o \
    -name "$1" -print 2>/dev/null | head -1
}
# come anyfile ma scende piu' a fondo: i file di test stanno spesso sotto
# src/qualcosa/__tests__/, che a due livelli non si vede
anyfile_deep() {
  find . -maxdepth 4 \
    \( -name node_modules -o -name .git -o -name vendor -o -name .next \
       -o -name dist -o -name build -o -name .venv \) -prune -o \
    -name "$1" -print 2>/dev/null | head -1
}
anydir_deep() {
  find . -maxdepth 4 \
    \( -name node_modules -o -name .git -o -name vendor -o -name .next \
       -o -name dist -o -name build -o -name .venv \) -prune -o \
    -type d -name "$1" -print 2>/dev/null | head -1
}
# recursive grep that never descends into dependency trees (a match inside
# node_modules is not a fact about this project)
sgrep() {
  grep -rls --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=vendor \
    --exclude-dir=.next --exclude-dir=dist --exclude-dir=build "$@" . 2>/dev/null
}

echo "=== AOS PROJECT PROFILE ==="
echo "path: $(pwd)"
echo

# ---------------------------------------------------------------- instructions
echo "--- INSTRUCTIONS (these outrank AOS) ---"
found_instr=0
# README.md sta fuori di proposito: e' documentazione, non istruzione, e non
# scavalca niente. Elencarlo qui faceva passare per vincolante un file che non lo e'.
for f in CLAUDE.md AGENTS.md CONTEXT.md .cursorrules; do
  if has "$f"; then echo "  $f ($(wc -l < "$f" | tr -d ' ') righe)"; found_instr=1; fi
done
[ -d docs ] && echo "  docs/ ($(find docs -maxdepth 1 -mindepth 1 2>/dev/null | wc -l | tr -d ' ') voci)"
[ "$found_instr" -eq 0 ] && echo "  (nessuno)"
echo

# ---------------------------------------------------------------------- stack
echo "--- STACK ---"
stack=""
add_stack() { stack="$stack $1"; echo "  $1"; }

if has package.json; then
  pm="npm"
  has pnpm-lock.yaml && pm="pnpm"
  has yarn.lock && pm="yarn"
  { has bun.lockb || has bun.lock; } && pm="bun"
  declared_pm=$("$host_python" -I -c 'import json; p=json.load(open("package.json")).get("packageManager", ""); print(p.split("@")[0])' 2>/dev/null)
  case "$declared_pm" in npm|pnpm|yarn|bun) pm="$declared_pm" ;; esac
  add_stack "node (package manager: $pm)"
  { has next.config.js || has next.config.mjs || has next.config.ts; } && add_stack "next.js"
  has tsconfig.json && add_stack "typescript"
  has drizzle.config.ts && add_stack "drizzle orm"
  has prisma && add_stack "prisma"
fi
is_python=0
python_runner=0
if has requirements.txt || has pyproject.toml || has setup.py || [ -n "$(anyfile_deep '*.py')" ]; then
  is_python=1
  python_cmd="$host_python"
  for candidate in .venv/bin/python .venv/bin/python3 venv/bin/python venv/bin/python3; do
    if [ -x "$candidate" ]; then python_cmd="./$candidate"; break; fi
  done
  if [ -n "$python_cmd" ]; then
    add_stack "python (interprete eseguibile: $python_cmd; compatibilita e dipendenze da verificare)"
  else
    add_stack "python (interprete non disponibile nel PATH)"
  fi
fi
is_wp=0
if has composer.json || [ -n "$(anyfile '*.php')" ]; then
  add_stack "php"
  if has wp-config.php || [ -d wp-content ] || [ -n "$(sgrep --include='*.php' -e 'add_action' -e 'add_filter' | head -1)" ]; then
    is_wp=1
    add_stack "wordpress (plugin/theme code)"
  fi
fi
has Dockerfile && add_stack "docker"
has docker-compose.yml && add_stack "docker-compose"
{ has vercel.json || has vercel.ts; } && add_stack "vercel"
{ [ -d supabase ] || [ -n "$(sgrep --include='*.ts' --include='*.py' -e 'supabase' | head -1)" ]; } \
  && add_stack "supabase"
n8n=$(sgrep --include='*.json' -e '"connections"' | head -3)
if [ -n "$n8n" ]; then
  # shellcheck disable=SC2001  # prefisso per riga: sed è più chiaro dell'espansione
  echo "$n8n" | sed 's/^/  n8n workflow?: /'
fi
[ -z "$stack" ] && echo "  (non riconosciuto — ispeziona a mano)"
echo

# ------------------------------------------------------------------- commands
echo "--- COMMANDS (use these, do not invent) ---"
npm_scripts=""
if has package.json; then
  "$host_python" -I - "$pm" <<'PY' 2>/dev/null || echo "  (package.json illeggibile)"
import json, sys
try:
    s = json.load(open("package.json")).get("scripts", {})
except Exception:
    raise SystemExit(1)
if not s:
    print("  package.json senza scripts")
for k in ("test", "lint", "typecheck", "build", "dev", "start"):
    if k in s:
        print(f"  {sys.argv[1]} run {k:<10} -> {s[k]}")
extra = [k for k in s if k not in ("test", "lint", "typecheck", "build", "dev", "start")]
if extra:
    print("  altri script: " + ", ".join(sorted(extra)))
PY
  # gli stessi nomi, ma leggibili da bash: servono a sapere se un comando di
  # test esiste davvero, non solo se esiste package.json
  npm_scripts=$("$host_python" -I -c 'import json;print(" ".join(json.load(open("package.json")).get("scripts",{})))' 2>/dev/null)
fi
has Makefile && grep -E '^[a-zA-Z0-9_-]+:' Makefile 2>/dev/null | head -8 | sed 's/^/  make /'

# Un typecheck c'e' sempre, se c'e' TypeScript: si invoca il binario locale.
# `npx tsc` scarica o risolve altro quando il pacchetto non e' in dipendenza
# diretta, e fallisce per motivi che non c'entrano con il codice.
if has tsconfig.json && ! printf '%s' "$npm_scripts" | grep -qw typecheck; then
  echo "  ./node_modules/.bin/tsc --noEmit   (nessuno script typecheck)"
fi

# ------------------------------------------------------------ come si verifica
# Le convenzioni di test non stanno solo in package.json: un progetto può avere
# una suite intera in script sciolti e nessuno script `test`. Tacere in quel
# caso è peggio che dire "non c'è niente": chi legge conclude che non serve
# verificare, e si inventa un comando.
test_cmd=""
if printf '%s' "$npm_scripts" | grep -qw test; then
  test_cmd="$pm run test"
fi
if has Makefile && grep -qE '^test:' Makefile 2>/dev/null; then
  echo "  make test   (target dichiarato)"
  [ -z "$test_cmd" ] && test_cmd="make test"
fi
if [ "$is_python" -eq 1 ] && [ -n "$host_python" ]; then
  python_raw=$("$host_python" -I "$SCRIPT_DIR/profile-python.py" "$python_cmd")
  python_evidence=$(printf '%s\n' "$python_raw" | sed -n 's/^PYTHON_TESTS_EVIDENCE=//p')
  python_tests=$(printf '%s\n' "$python_raw" | grep -v '^PYTHON_TESTS_EVIDENCE=')
  if [ -n "$python_tests" ]; then
    printf '%s\n' "$python_tests"
    # Only test files prove there is Python to run; a pytest section in a config
    # file, or pytest in the dependencies, proves neither.
    [ "$python_evidence" = "files" ] && python_runner=1
    [ -z "$test_cmd" ] && test_cmd="runner Python rilevato"
  fi
fi

# Script di verifica sciolti: si elencano comunque, anche quando un `npm test`
# esiste — spesso sono la barra vera del progetto.
sciolti=$(find scripts -maxdepth 1 \( -name 'verifica-*' -o -name 'test-*' -o -name 'check-*' \) \
  -type f 2>/dev/null | sort | head -6)
if [ -n "$sciolti" ]; then
  quanti=$(find scripts -maxdepth 1 \( -name 'verifica-*' -o -name 'test-*' -o -name 'check-*' \) \
    -type f 2>/dev/null | wc -l | tr -d ' ')
  echo "  script di verifica in scripts/ ($quanti) — campione alfabetico, non una selezione:"
  echo "    scegli quelli che toccano cio' che hai cambiato, non i primi sei"
  # shellcheck disable=SC2001  # prefisso per riga: sed è più chiaro dell'espansione
  echo "$sciolti" | sed 's/^/    /'
  [ "$quanti" -gt 6 ] && echo "    ... e altri $((quanti - 6))"
  [ -z "$test_cmd" ] && test_cmd="script in scripts/"
fi

# File di test con i nomi soliti, ma senza un comando che li lanci.
if [ -z "$test_cmd" ]; then
  campione=$(anyfile_deep '*.test.*'); [ -z "$campione" ] && campione=$(anyfile_deep '*.spec.*')
  [ -z "$campione" ] && campione=$(anydir_deep '__tests__')
  if [ -n "$campione" ]; then
    echo "  ci sono file di test ($campione) ma nessuno script che li lanci:"
    echo "    cerca il runner nelle dipendenze prima di inventare un comando"
    test_cmd="da capire"
  fi
fi

echo
echo "--- DEPLOY ---"
found_deploy=0
if [ -n "$(anyfile 'deploy*.sh')" ]; then
  echo "  script: $(anyfile 'deploy*.sh')"; found_deploy=1
fi
if has vercel.json || has vercel.ts || has .vercel; then
  echo "  Vercel: verifica integrazione Git, ambiente e procedura di deploy del progetto"; found_deploy=1
fi
if [ "$is_wp" -eq 1 ]; then
  echo "  WordPress: verifica tooling e destinazione nelle istruzioni del progetto"
  echo "  (sintassi PHP, backup, verifica salute e rollback prima di un deploy autorizzato)"
  found_deploy=1
fi
[ "$found_deploy" -eq 0 ] && echo "  (nessun bersaglio riconosciuto — chiedi prima di rilasciare)"
echo

# The bar depends on the absence of a *Python* runner, not on the absence of any
# test command: `make test` or `npm run test` may not touch a line of this
# project's Python, and they used to be enough to remove it.
if [ "$is_python" -eq 1 ] && [ "$python_runner" -eq 0 ]; then
  if [ "$python_evidence" = "limited" ]; then
    echo "  I test Python esistono, ma la configurazione pytest puo' escluderli dalla raccolta."
  elif [ "$python_evidence" = "unverified" ]; then
    echo "  I test Python esistono, ma nessuno ha verificato che pytest li raccolga davvero."
  elif [ "$python_evidence" = "config" ]; then
    echo "  Il comando pytest viene da configurazione o dipendenze, non da test trovati."
  elif [ -n "$test_cmd" ]; then
    echo "  '$test_cmd' non e' provato che verifichi il Python di questo progetto."
  fi
  echo "  Nessuna prova che i test Python vengano eseguiti. Verifica configurazione e istruzioni; barra minima:"
  echo "    ${python_cmd:-<interprete da individuare>} -m py_compile <file modificati>"
  echo "    ${python_cmd:-<interprete da individuare>} -c 'import <modulo>'"
  echo "    esegui su input reale in scratchpad, MAI su dati di produzione"
fi

if [ -z "$test_cmd" ]; then
  echo "  NESSUN comando di test in questo progetto. La verifica NON e' opzionale:"
  echo "  leggi references/project-profiles.md e usa la ricetta dello stack."
fi
echo

# ------------------------------------------------------------------------ git
echo "--- GIT ---"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "  branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  dirty=$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  echo "  modifiche non committate: $dirty"
  [ "$dirty" -gt 0 ] && git status --porcelain 2>/dev/null | head -8 | sed 's/^/    /'
  echo "  ultimo commit: $(git log -1 --format='%h %s' 2>/dev/null)"
else
  echo "  NON è un repository git — nessun rollback via git."
  echo "  Prima di sovrascrivere un file, salvane una copia."
fi
echo

# ---------------------------------------------------------------------- notes
echo "--- RISK NOTES ---"
risky=0
envs=$(find . -maxdepth 2 -name '.env*' ! -name '*.example' ! -name '*.sample' \
  -not -path './node_modules/*' 2>/dev/null | head -4)
if [ -n "$envs" ]; then
  echo "  segreti presenti ($(echo "$envs" | tr '\n' ' ')): non stamparli ne' committarli"
  risky=1
fi
# WordPress si dichiara dal codice, non dal nome di un dominio: un README che
# di purgare la cache lì non significa niente.
if [ "$is_wp" -eq 1 ]; then
  echo "  codice WordPress: valuta modifica e ambiente; un deploy sul sito live richiede verifiche e rollback"; risky=1
fi
if [ -d supabase ] || [ -n "$(sgrep --include='*.ts' --include='*.py' -e 'supabase' | head -1)" ]; then
  echo "  Supabase: valuta ambiente, dati e reversibilita; migrazioni richiedono controlli, perdita dati o azioni irreversibili richiedono autorizzazione"; risky=1
fi
# n8n veniva riconosciuto sopra ma non arrivava mai qui: e' il segnale che conta
# marcatori: un JSON qualsiasi con "connections" non e' un workflow.
# `while read`, non `for wf in $(...)`: "Editorial Flow.json" veniva spezzato in
# due percorsi inesistenti e la nota HIGH non usciva.
# Here-doc e non pipe: dentro una pipe il `while` gira in una subshell e il
# `risky=1` non ne esce, quindi sarebbe uscito l'avviso n8n E la riga
# "nessun segnale automatico".
WF_JSON=$(sgrep --include='*.json' -e '"connections"' | head -5)
while IFS= read -r wf; do
  [ -n "$wf" ] || continue
  if [ -f "$wf" ] && grep -q '"nodes"' "$wf" 2>/dev/null; then
    # The file says the workflow exists, not that it is active on the instance nor
    # what its nodes do. Neither the severity nor the effects follow from detection.
    echo "  workflow n8n ($wf): riconosciuto il file, non lo stato sull'istanza"
    echo "  ne' cosa fanno i suoi nodi. Prima di eseguirlo o attivarlo leggi i nodi"
    echo "  e la destinazione: alcuni scrivono davvero fuori. Non attivarlo senza dirlo"
    risky=1; break
  fi
done <<EOF
$WF_JSON
EOF
[ "$risky" -eq 0 ] && echo "  (nessun segnale automatico — classifica comunque a mano)"
echo
echo "=== END PROFILE ==="
