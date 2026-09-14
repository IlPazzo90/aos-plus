#!/usr/bin/env bash
# Mechanical security pass over the work in progress.
#
# This is NOT proof that the change is safe. It is a set of deterministic checks
# for the classes of defect that have actually shown up in these projects, run in
# one call so that the security role fires every time instead of when somebody
# remembers it. Every signal it prints is a question to answer, not a verdict.
#
# What it deliberately does not do: judge business logic, prove authorization is
# correct, or replace reading the diff. A clean run means "none of these known
# traps", never "secure".
#
# Read-only. No network. Values of matched secrets are never printed.
#
# Note: risky identifiers below are written with a character class in the middle
# (dangerousl[y]...) so that editor and hook scanners do not trip over this file
# for containing the very patterns it looks for. The regex matches the same.
set -uo pipefail

cd "${1:-.}" || exit 1
SIGNALS=0

# I commenti si tolgono con perl e non con sed: un blocco `/* ... */` su piu' righe
# sopravviveva a una sostituzione riga per riga, e la riga interna
# `if (checkAuthApi()) return;` insegnava una guardia, la faceva trovare nella rotta
# e ne simulava anche l'uso. Vale per JS e per SQL.
# Si tolgono anche le STRINGHE, non solo i commenti: `const doc = "checkAuthApi("`
# insegnava una guardia e ne simulava l'uso, e `SELECT 'ALTER TABLE … ENABLE ROW
# LEVEL SECURITY'` faceva risultare protetta una tabella che non lo era. Il testo
# fra virgolette non viene eseguito, e non deve contare come codice.
# Due letture, perche' servono due cose diverse.
#
# `senza_commenti_solo`: via i commenti, stringhe intatte. Serve a riconoscere i
# NOMI dei segreti — `x-api-key`, `calendar_token` — che nel codice vero sono
# sempre stringhe: toglierle rendeva nude rotte davvero protette.
#
# `senza_commenti`: via anche le stringhe. Serve alle CHIAMATE, dove una stringa
# come `"checkAuthApi("` fabbricava una guardia che non esiste.
#
# In entrambe le stringhe si tolgono PRIMA dei commenti di riga: `//` dentro
# `https://api.telegram.org` non e' un commento, e troncava la riga.
# Una passata sola, con stringhe e commenti nella STESSA alternanza: vince chi
# comincia prima. Toglierli in due passate separate lasciava che un `/*` dentro una
# stringa aprisse un commento finto che si mangiava il codice vero fino al `*/`
# della stringa successiva — compresa la chiamata alla guardia in mezzo.
TOKENS='("(?:\\.|[^"\\])*")|(\x27(?:\\.|[^\x27\\])*\x27)|(`(?:\\.|[^`\\])*`)|(/\*.*?\*/)|(//[^\n]*)'
# Il modificatore `s` serve: senza, `.` non attraversa gli a-capo e un commento
# `/* ... */` su piu' righe resta intero. L'avevo perso unendo le sostituzioni.
senza_commenti_solo() {  # via i commenti, stringhe intatte: serve ai NOMI dei segreti
  perl -0777 -pe "s{$TOKENS}{ (defined(\$4)||defined(\$5)) ? '' : \$& }ges" "$1" 2>/dev/null
}
senza_commenti() {       # via anche le stringhe: serve alle CHIAMATE
  perl -0777 -pe "s{$TOKENS}{ (defined(\$4)||defined(\$5)) ? '' : '\"\"' }ges" "$1" 2>/dev/null
}
senza_commenti_sql() {
  perl -0777 -pe 's{(\x27(?:\x27\x27|[^\x27])*\x27)|(/\*.*?\*/)|(--[^\n]*)}{ defined($1) ? "\x27\x27" : "" }ges' "$1" 2>/dev/null
}

hr() { printf '\n--- %s ---\n' "$1"; }
flag() { SIGNALS=$((SIGNALS + 1)); printf '  [!] %s\n' "$1"; }
ok()   { printf '      %s\n' "$1"; }
cut_value() { sed -E 's/(:[0-9]+:).*/\1 .../'; }

# Scope: what changed. Falls back to the whole tree, and says so, because a
# silent change of scope is how a check starts lying about what it covered.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  FILES=$( { git diff HEAD --name-only 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null; } | grep -v '^$' | sort -u)
  SCOPE="modifiche non committate"
  if [ -z "$FILES" ]; then
    FILES=$(git ls-files); SCOPE="TUTTO il repository (niente di non committato)"
  fi
else
  FILES=$(find . -type f -not -path '*/node_modules/*' -not -path '*/.git/*' 2>/dev/null)
  SCOPE="cartella (non e' un repository git)"
fi

LIVE=$(printf '%s\n' "$FILES" | while read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done)
COUNT=$(printf '%s\n' "$LIVE" | grep -vc '^$')

echo "=== AOS SECURITY PASS ==="
echo "path:   $(pwd)"
echo "ambito: $SCOPE — $COUNT file"

scan() { [ -z "$LIVE" ] && return 0; printf '%s\n' "$LIVE" | tr '\n' '\0' | xargs -0 grep -nIE "$@" 2>/dev/null; }

hr "1. Segreti in chiaro"
# Due famiglie: i formati riconoscibili di per se' (JWT, chiavi OpenAI/GitHub/
# Google/Slack/AWS) e le assegnazioni a un nome che dichiara cosa contiene —
# `const databasePassword = "..."`. La seconda mancava del tutto, e copre il caso
# piu' banale di tutti.
# -i perche' `databasePassword` non e' `password`: senza, la meta' dei nomi veri
# sfuggiva per una lettera maiuscola.
HITS=$(scan -i -e '(eyJ[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[A-Z0-9]{16}|[A-Za-z_]*(password|passwd|secret|api_?key|auth_?token|access_?token|authorization|bearer)[A-Za-z_]*[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"'$][^"'"'"']{7,})' \
  | grep -vE '\.(md|lock):|(package|pnpm|yarn|bun)-?lock[^:]*:|\.example[:.]|(^|/)(sample|fixtures?)/' \
  | grep -vE '(process\.env|import\.meta\.env|os\.environ|getenv)' \
  | sed -E 's/(:[0-9]+:).*/\1 <credenziale, valore non stampato>/' | head -10)
# Nota sui due filtri qui sopra (fuori dalla continuazione di riga: un commento in
# mezzo a un `\` la spezza, e la volta scorsa ha disattivato proprio l'oscuramento
# del valore). Si escludono i file dichiaratamente d'esempio e i valori presi
# dall'ambiente, NON qualunque riga che contenga la parola "example": bastava un
# commento `// not an example` per far sparire una credenziale vera.
if [ -n "$HITS" ]; then flag "credenziali in chiaro:"; printf '%s\n' "$HITS" | sed 's/^/      /'
else ok "nessun formato di credenziale noto"; fi

ENVT=$(git ls-files 2>/dev/null | grep -E '(^|/)\.env($|\.)' | grep -v example | head -5)
[ -n "$ENVT" ] && { flag "file .env TRACCIATI da git (finiscono su GitHub):"; printf '%s\n' "$ENVT" | sed 's/^/      /'; }

hr "2. Supabase — la RLS non si deduce, si legge"
MIG=$(printf '%s\n' "$LIVE" | grep -E 'supabase/migrations/.*\.sql$')
BEFORE_MIG=$SIGNALS
MIG_LETTE=0
if [ -n "$MIG" ]; then
  # L'elenco delle tabelle che ottengono la RLS, cercato in TUTTE le migration del
  # repository e non solo nel file in esame. Pretendere che stesse nella stessa
  # migration era una raffinatezza che su a larger application — 41 `create table`
  # e 41 `enable row level security`, raccolte in una migration dedicata —
  # produceva 27 segnali falsi. Quello che conta e' se la RLS c'e', non dove.
  TUTTE_MIG=$( { git ls-files 'supabase/migrations/*.sql' 2>/dev/null; } )
  [ -z "$TUTTE_MIG" ] && TUTTE_MIG="$MIG"
  # Lo schema si conserva: senza, la RLS su `auth.users` veniva attribuita anche a
  # `public.users`. Chi non lo dichiara finisce in `public`, come fa Postgres.
  # Vale l'ULTIMA operazione, non "e' mai comparso un disable": una cronologia
  # enable → disable → enable finisce protetta, e sottrarre la dichiarava scoperta.
  # Le migration si leggono in ordine di nome, che e' l'ordine in cui si applicano.
  # `sort -V` e non `sort`: l'ordine lessicografico mette `10_disable.sql` prima di
  # `2_enable.sql`, mentre chi applica le migration le ordina per numero. Con la
  # numerazione non riempita di zeri i due ordini danno stati finali opposti.
  RLS_ON=$(printf '%s\n' "$TUTTE_MIG" | sort -V | while IFS= read -r f; do
      [ -f "$f" ] && senza_commenti_sql "$f"
    done | tr 'A-Z' 'a-z' | tr '\n' ' ' \
    | grep -oE 'alter +table +(if +exists +)?(only +)?(("[^"]+"|[a-z0-9_]+)\.)?("[^"]+"|[a-z0-9_]+) +(enable|disable) +row +level +security' \
    | sed -E 's/^alter +table +(if +exists +)?(only +)?//; s/"//g; s/ +row +level +security$//' \
    | awk 'NF==2 { n=$1; if (index(n,".")==0) n="public." n; last[n]=$2 }
           END { for (k in last) if (last[k]=="enable") print k }' | sort -u)

  # `SET search_path` cambia lo schema implicito, e qui lo schema si deduce dal
  # nome: dove compare, la deduzione non e' affidabile e va detto invece di
  # lasciar credere il contrario.
  if printf '%s\n' "$TUTTE_MIG" | while IFS= read -r f; do [ -f "$f" ] && senza_commenti_sql "$f"; done \
     | grep -qiE '^[[:space:]]*set +search_path'; then
    flag "una migration usa SET search_path: lo schema qui si deduce dal nome, quindi su queste tabelle il controllo RLS NON e' affidabile — verificale a mano"
  fi
  # `while read`, non `for m in $MIG`: un nome con uno spazio veniva spezzato in
  # tre percorsi inesistenti, grep falliva in silenzio e la sezione dichiarava
  # comunque "RLS attivata dove serve". Una rassicurazione falsa e' peggio del
  # controllo mancante.
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    if [ ! -f "$m" ]; then flag "migration NON esaminata (file non leggibile): $m"; continue; fi
    MIG_LETTE=$((MIG_LETTE + 1))
    # Appiattito: `create table` e il nome possono stare su righe diverse, e
    # l'identificatore puo' essere fra virgolette.
    # I commenti si tolgono PRIMA di appiattire: `-- ALTER TABLE ... ENABLE RLS`
    # commentato faceva passare la tabella per protetta.
    piatto=$(senza_commenti_sql "$m" | tr '\n' ' ')
    # Un `DISABLE` esplicito e' un segnale per conto suo: prima si guardava solo
    # se le tabelle *create qui* fossero protette, quindi una migration che si
    # limitava a spegnere la RLS passava senza dire niente, con exit 0.
    if printf '%s' "$piatto" | grep -qiE 'disable +row +level +security'; then
      flag "$m: qui c'e' un DISABLE ROW LEVEL SECURITY — spegnere la RLS e' una decisione, non un dettaglio"
    fi
    for t in $(printf '%s' "$piatto" \
        | tr 'A-Z' 'a-z' \
        | grep -oE 'create +table +(if +not +exists +)?(("[^"]+"|[a-z_][a-z0-9_]*)\.)?("[^"]+"|[a-z_][a-z0-9_]*)[[:space:]]*\(' \
        | sed -E 's/[[:space:]]*\($//; s/^create +table +(if +not +exists +)?//; s/"//g' \
        | awk '{ print index($0,".") ? $0 : "public." $0 }'); do
      # Il nome va delimitato: senza, `users_archive` soddisfaceva la ricerca per
      # `users` e lasciava `users` senza RLS.
      # Confronto esatto sul nome: cercarlo dentro una riga faceva sì che
      # `users_archive` soddisfacesse la ricerca per `users`.
      printf '%s\n' "$RLS_ON" | grep -qxF "$t" \
        || flag "$m: tabella '$t' — nessun ENABLE ROW LEVEL SECURITY in nessuna migration (se in produzione la RLS c'e', allora non e' scritta qui: repository e database divergono)"
    done
  done <<EOF
$MIG
EOF
  migrep() { printf '%s\n' "$MIG" | tr '\n' '\0' | xargs -0 grep -niE "$1" 2>/dev/null | head -5; }
  OPEN=$(migrep 'using[[:space:]]*\([[:space:]]*true|with check[[:space:]]*\([[:space:]]*true')
  [ -n "$OPEN" ] && { flag "policy che non filtrano niente (USING true):"; printf '%s\n' "$OPEN" | sed 's/^/      /'; }
  # `TO authenticated, anon` concede lo stesso ad anon: il ruolo va cercato in tutto
  # l'elenco dei destinatari, non solo come primo.
  GRANTS=$(migrep 'grant [^;]* to [^;]*(public|anon)([^a-z0-9_]|$)')
  [ -n "$GRANTS" ] && { flag "GRANT a public/anon — chiunque abbia la chiave anon la chiama:"; printf '%s\n' "$GRANTS" | sed 's/^/      /'; }
  # Una sezione muta si legge come una sezione non eseguita. Dire cosa e' stato
  # guardato costa una riga e vale la fiducia nel resto dell'output.
  [ "$SIGNALS" -eq "$BEFORE_MIG" ] \
    && ok "$MIG_LETTE migration esaminate: RLS attivata dove serve, nessun GRANT a public/anon"
else
  ok "nessuna migration fra i file in ambito"
fi

# `migrations/` senza prefisso escludeva anche `src/migrations/admin.ts`, che e'
# codice che gira, non SQL da applicare una volta.
SRK=$(scan -e 'SERVICE_ROLE|service_role' | grep -vE '\.md:|supabase/migrations/' | head -5)
[ -n "$SRK" ] && { flag "service_role fuori dalle migration — bypassa la RLS, mai lato client:"; printf '%s\n' "$SRK" | cut_value | sed 's/^/      /'; }

hr "3. Rotte che accettano richieste da fuori"
# Do not hardcode the names of auth helpers: every project invents its own, and a
# guessed list flags 134 routes out of 174 in the application, which uses
# requirePermissionApi() - a stronger guard than the getUser() we would look for.
# Instead, learn this repository's guards from the routes themselves and flag the
# outliers: the interesting route is the one that does not do what its 170
# neighbours do.
ROUTES=$(printf '%s\n' "$LIVE" | grep -E 'app/api/.*route\.(ts|js)$|pages/api/.*\.(ts|js)$')
if [ -n "$ROUTES" ]; then
  NR=$(printf '%s\n' "$ROUTES" | wc -l | tr -d ' ')
  # Every route file in the repo, not only the ones in scope: the convention is a
  # property of the project, and a one-route diff would otherwise learn nothing.
  ALLR=$( { git ls-files 2>/dev/null || printf '%s\n' "$LIVE"; } | grep -E 'app/api/.*route\.(ts|js)$|pages/api/.*\.(ts|js)$')
  [ -z "$ALLR" ] && ALLR="$ROUTES"
  # A guard must be a CALL, not a word: matching bare 'guard[A-Za-z]*' picked up
  # the Italian 'guardia' and 'guardato' from comments, which would have let an
  # unguarded route pass for protected. A false negative here is worse than noise.
  #
  # And no frequency threshold. Requiring 3+ uses learned 'requireAdmin' but not
  # 'requireAdminApi', and 'requireAdmin(' does not match 'requireAdminApi(' - so
  # the MFA reset route, which calls requireAdminApi() on its second line, was
  # reported as unguarded. The name shape below is specific enough on its own.
  # Anche l'apprendimento ignora i commenti: un `// if (checkAuth()) return;`
  # insegnava una guardia che il progetto non usa da nessuna parte.
  NOMI=$(printf '%s\n' "$ALLR" | while IFS= read -r q; do [ -f "$q" ] && senza_commenti "$q"; done | grep -ohE \
      '\b((require|assert|ensure|check|can|get)[A-Za-z]*(Auth|Admin|Permission|Role|Azienda|Session|Access)[A-Za-z]*|get(User|Claims|Session)|(verify|match|compare)[A-Za-z]*(Token|Signature|Webhook|Key|Secret|Event)[A-Za-z]*|keyMatches|constructEvent)[[:space:]]*\(' 2>/dev/null \
    | tr -d ' ' | sed 's/($//' | sort -u)
  # Solo i nomi *imperativi* possono essere applicati con `await` da soli: una
  # `requireX()` che lancia interrompe la richiesta, una `getUser()` restituisce un
  # valore e `await getUser();` con l'esito buttato via non protegge niente.
  CALLS_IMP=$(printf '%s\n' "$NOMI" | grep -E '^(require|assert|ensure|can|check)' | sed 's/$/[[:space:]]*\\(/' | paste -sd'|' -)
  GUARDS=$(printf '%s\n' "$NOMI" | grep -v '^$' | sed 's/$/[[:space:]]*\\(/' | paste -sd'|' -)
  # Not every guard is a call. A webhook proves itself with a signature header, a
  # cron with a shared secret, an ICS feed with an unguessable token in the path.
  # Verified by reading ten flagged routes: eight were guarded and this pattern
  # list was what could not see them - keyMatches(), Stripe constructEvent(),
  # getDocumentsAuth(), and a plain x-api-key compared against the environment.
  # Solo nomi di header, variabili d'ambiente e colonne. Le funzioni che *sono* un
  # confronto stanno fuori di proposito: sono gestite sopra e solo se chiamate,
  # altrimenti bastava nominarne una in una stringa per non essere piu' "nuda".
  SECRETS='CRON_SECRET|x-webhook-signature|x-api-key|x-intake-key|stripe-signature|calendar_token'
  # `CALLS` resta separato: la prova del "risultato usato" vale per una chiamata
  # di funzione, non per il nome di un header o di una colonna. Su
  # `.eq('calendar_token', token)` non c'e' nessun esito da usare, e giudicarla
  # con lo stesso metro produceva un falso positivo.
  CALLS="$GUARDS"
  GUARDS=$(printf '%s' "${GUARDS:+$GUARDS|}$SECRETS")
  if [ -z "$GUARDS" ]; then
    ok "nessuna convenzione di guardia riconoscibile: le rotte vanno lette a mano"
  else
    ok "guardie usate da questo progetto: $(printf '%s' "$GUARDS" | sed 's/\[\[:space:\]\]\*\\(//g' | tr '|' ' ')"
    # Con due o tre rotte in tutto, la rotta in esame definisce da sola cosa
    # conta come guardia: l'apprendimento diventa tautologico e va detto.
    NALL=$(printf '%s\n' "$ALLR" | grep -vc '^$')
    [ "$NALL" -lt 3 ] && ok "attenzione: solo $NALL rotte nel progetto, la convenzione non e' deducibile — leggile a mano"

    NAKED=""; IGNORATA=""; DA_LEGGERE=""
    while IFS= read -r r; do
      [ -f "$r" ] || continue
      # I commenti si tolgono per primi: `// if (checkAuth()) return;` insegnava
      # la guardia, la faceva trovare nella rotta e ne simulava anche l'uso,
      # perche' contiene `if (` e `return`. Una rotta nuda passava per protetta.
      CODICE=$(senza_commenti "$r")
      CON_STRINGHE=$(senza_commenti_solo "$r")

      # Un segreto vale come difesa solo se viene CONFRONTATO, e il confronto deve
      # stare vicino: cercarlo ovunque nel file faceva bastare un
      # `process.env.NODE_ENV === "production"` a dieci righe di distanza.
      # Le funzioni che *sono* un confronto (keyMatches, timingSafeEqual,
      # constructEvent, createHmac) valgono da sole.
      # Tre stati, non due. Le funzioni che *sono* un confronto valgono da sole. Un
      # nome di segreto da solo non dimostra niente: leggerlo e non confrontarlo
      # lascia la rotta aperta, ma pretendere il confronto sulla stessa riga
      # segnalerebbe come nude rotte davvero protette, dove la chiave si legge in
      # una riga e si confronta due righe sotto. Quando non si puo' sapere, si dice.
      SEGRETO=no
      # `createHmac` esce: **calcola** un MAC, non lo verifica — si usa anche solo
      # per firmare una risposta. `constructEvent` lancia se la firma non torna,
      # quindi chiamarla basta. `timingSafeEqual` e `keyMatches` restituiscono un
      # booleano: chiamarle e buttare via l'esito non protegge niente.
      if printf '%s\n' "$CODICE" | grep -qE 'constructEvent[[:space:]]*\('; then
        SEGRETO=si
      elif printf '%s\n' "$CODICE" | grep -qE '(=|return|&&|\|\||\?\?|!|if[[:space:]]*\()[[:space:]]*!*[[:space:]]*(await[[:space:]]+)?(timingSafeEqual|keyMatches)[[:space:]]*\('; then
        SEGRETO=si
      elif printf '%s\n' "$CON_STRINGHE" | grep -qE "$SECRETS"; then
        SEGRETO=forse
      fi

      # Le chiamate: si scartano le righe di sola dichiarazione e gli import.
      RIGHE=""
      [ -n "$CALLS" ] && RIGHE=$(printf '%s\n' "$CODICE" | grep -E "$CALLS" 2>/dev/null \
        | grep -vE '^[[:space:]]*import[[:space:]]' \
        | grep -vE '^[[:space:]]*((export[[:space:]]+)?(async[[:space:]]+)?function[[:space:]]|(export[[:space:]]+)?(const|let|var)[[:space:]]+[A-Za-z_]+[[:space:]]*=[[:space:]]*(async[[:space:]]*)?(\(|function))[^;]*$')

      if [ -z "$RIGHE" ] && [ "$SEGRETO" = forse ]; then
        DA_LEGGERE="$DA_LEGGERE$r"$'\n'
        continue
      fi
      if [ -z "$RIGHE" ] && [ "$SEGRETO" = no ]; then
        # Nessuna chiamata usabile e nessun segreto confrontato: la rotta e' nuda,
        # anche se il nome di una guardia compare da qualche parte nel file.
        NAKED="$NAKED$r"$'\n'
        continue
      fi
      [ -n "$RIGHE" ] || continue

      # L'esito e' usato solo se un operatore precede la chiamata nella STESSA
      # istruzione. Cercare `=` in tutta la riga faceva passare
      # `checkAuth(); const status = 200;`, dove la guardia e' buttata via.
      # L'operatore dev'essere *attaccato* alla chiamata, non solo prima di essa
      # nella riga: `if (featureEnabled) checkAuth();` ha un `if (` che precede ma
      # riguarda altro, e l'esito della guardia resta buttato via.
      # `await guardia(...)` da sola conta: una guardia imperativa che lancia
      # interrompe la richiesta, e segnalarla come "risultato ignorato" era un
      # falso positivo. Una chiamata nuda senza `await`, no.
      # Fra l'operatore e la chiamata ci puo' stare una catena di accessi:
      # `const { data } = await supabase.auth.getUser()` e' l'uso corretto piu'
      # comune di questo progetto, e senza questo pezzo risultava ignorato.
      ENF="(=|return|&&|\|\||\?\?|if[[:space:]]*\()[[:space:]]*!*[[:space:]]*(await[[:space:]]+)?[A-Za-z0-9_.\$]*($CALLS)"
      [ -n "$CALLS_IMP" ] && ENF="$ENF|await[[:space:]]+($CALLS_IMP)"
      printf '%s\n' "$RIGHE" | grep -qE "$ENF" || IGNORATA="$IGNORATA$r"$'\n'
    done <<EOF
$ROUTES
EOF
    IGNORATA=$(printf '%s' "$IGNORATA" | grep -v '^$')
    DA_LEGGERE=$(printf '%s' "$DA_LEGGERE" | grep -v '^$')
    if [ -n "$DA_LEGGERE" ]; then
      flag "difesa da un segreto condiviso, ma il confronto non e' dimostrabile leggendo una riga alla volta — controlla a mano che la chiave venga confrontata davvero:"
      printf '%s\n' "$DA_LEGGERE" | head -5 | sed 's/^/      /'
    fi
    if [ -n "$IGNORATA" ]; then
      flag "guardia chiamata e risultato che sembra IGNORATO — o la rotta risponde comunque, o l'esito finisce in un'espressione che una riga di testo non basta a leggere (una lambda, un .map). Guardala:"
      printf '%s\n' "$IGNORATA" | head -5 | sed 's/^/      /'
    fi
    NAKED=$(printf '%s' "$NAKED" | grep -v '^$')
    if [ -n "$NAKED" ]; then
      # The sharp case. A route with no guard is usually fine: it is public by
      # choice, or the request-scoped client carries the user's identity and RLS
      # decides. But a route with no guard that ALSO builds a service-role client
      # has turned RLS off and put nothing in its place.
      BYPASS=""
      while IFS= read -r r; do
        [ -f "$r" ] || continue
        grep -qE 'SERVICE_ROLE|service_role|createAdminClient|supabaseAdmin' "$r" && BYPASS="$BYPASS$r"$'\n'
      done <<EOF
$NAKED
EOF
      BYPASS=$(printf '%s' "$BYPASS" | grep -v '^$')
      if [ -n "$BYPASS" ]; then
        flag "SENZA GUARDIA E CON LA RLS SCAVALCATA — guarda queste per prime:"
        printf '%s\n' "$BYPASS" | sed 's/^/      >>> /'
      fi
      flag "rotte che non usano nessuna di quelle guardie ($(printf '%s\n' "$NAKED" | wc -l | tr -d ' ') su $NR):"
      printf '%s\n' "$NAKED" | head -10 | sed 's/^/      /'
      ok "pubblica per scelta va bene, e affidarsi alla RLS pure — ma dev'essere scritto"
    elif [ -z "$IGNORATA" ] && [ -z "$DA_LEGGERE" ]; then
      # "nominano", non "passano da": questo controllo legge testo, non esegue il
      # codice, e non sa se la guardia copre ogni ramo della rotta.
      ok "tutte le $NR rotte in ambito nominano una guardia e ne usano l'esito"
    fi
  fi
else
  ok "nessuna rotta API fra i file in ambito"
fi

hr "4. Input che diventa percorso, HTML o query"
TRAV=$(scan -e '(readFile|createReadStream|unlink|readdir|sendFile|join)\(.*(params|query|searchParams|req\.body)' | head -5)
[ -n "$TRAV" ] && { flag "parametro della richiesta dentro un percorso di file (path traversal):"; printf '%s\n' "$TRAV" | cut_value | sed 's/^/      /'; }

XSS=$(scan -e 'dangerousl[y]SetInnerHTML|v-html|\.inner[H]TML[[:space:]]*=' | head -5)
[ -n "$XSS" ] && { flag "HTML iniettato senza passare da React:"; printf '%s\n' "$XSS" | cut_value | sed 's/^/      /'; }

PHPIN=$(scan -e '\$_(GET|POST|REQUEST)\[' | head -5)
[ -n "$PHPIN" ] && { flag "input PHP grezzo (sanitizza e verifica il nonce):"; printf '%s\n' "$PHPIN" | cut_value | sed 's/^/      /'; }

hr "Esito"
if [ "$SIGNALS" -eq 0 ]; then
  echo "  Nessuno dei controlli noti ha segnalato niente su $COUNT file."
  echo "  NON vuol dire sicuro: vuol dire che nessuna di queste trappole e' scattata."
else
  echo "  $SIGNALS segnali. Ognuno e' una domanda a cui rispondere, non una condanna:"
  echo "  o lo correggi, o scrivi nel report perche' li' va bene cosi'."
fi
echo
echo "Quello che questo script NON puo' vedere, perche' legge testo e non esegue"
echo "il codice: se i permessi sono giusti, se un utente vede i dati di un altro,"
echo "se una guardia copre tutti i rami, se un dato pericoloso arriva al punto"
echo "sbagliato passando per due o tre righe (qui si guarda una riga alla volta)."
echo "Per quello serve il ruolo Security in references/quality-gates.md, e a"
echo "rischio HIGH la verifica in croce con un modello diverso."
echo "=== FINE ==="

# Uscita distinguibile: 0 pulito, 2 segnali da rispondere. Uscire sempre 0
# rendeva impossibile usarlo come cancello — e un cancello che dice sempre "vai"
# e' la stessa esortazione che questo script esisteva per sostituire.
[ "$SIGNALS" -eq 0 ] && exit 0
exit 2
