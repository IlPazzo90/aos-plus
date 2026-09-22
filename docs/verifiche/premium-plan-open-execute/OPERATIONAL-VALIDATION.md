# Validazione operativa — 22 settembre 2026

Stato: **NOT READY FOR MAIN**. Branch: `upgrade/runtime-independent-open-execution`.
Questo verbale distingue codice verificato, prove live e gate ancora aperti.

## Architettura osservata

Host Claude Code o Codex; identità host separata da modello e runtime di ogni ruolo.
Il catalogo descrive CHEAP/MID/PREMIUM, capability, runtime, prezzi e provenienza dei
benchmark. T0/T1: open e check deterministici. T2 definito: planner MID, open executor,
reviewer MID dell'altra famiglia, open fixer. T3/HIGH: soglie superiori e review
obbligatoria; CRITICAL richiede approvazione come prima. Una preferenza planner è
esplicita, mai dedotta dal nome dell'host. Planner/reviewer ricevono CLI model override
reale e strumenti read-only. Verifica e arbitrato restano al coordinatore.

Vincitore storico invariato: DeepSeek v4-pro-0813; fallback Qwen3-coder-next.
Candidati MID planning/review: GPT-5.6-sol e Sonnet. I loro score sono soglie di
policy dichiarate, non benchmark di qualità inventati. Non c'è un MID executor
promosso: la scala salta quel livello con assenza esplicita di candidato.

## Runtime e sicurezza

- Claude Code open: abilitato con Read/Glob/Grep/Edit/Write, Bash assente. Modifica
  reale `answer()=41 → 42`, check host superato. Prove negative: lettura `.env` e
  symlink esterno negate dai tool nativi. Non è una certificazione di ogni possibile
  attacco al runtime; manca un isolamento OS completo verificato.
- OpenCode open: prova negativa fallita. Export nativo conferma che entrambi i read
  sono terminati e hanno restituito i canary sintetici. Config deny caricata ma
  insufficiente. Escluso da selezione automatica e override esplicito; una nuova
  prova dopo la modifica conferma il rifiuto prima del worker.
- Codex CLI open: resta escluso dopo la precedente prova fallita delle regole
  restrittive. Codex host e ruoli subscription non sono rimossi.
- Delegate e Security Gate originali invariati. L'uso diretto del vecchio delegate
  e il turno nativo del plugin OpenCode non sono certificati da questa validazione.
  Nessun aumento di privilegi per ottenere una prova verde.

## Prove live

Codex MID ha prodotto un piano strutturato valido. Claude Sonnet/Fable avevano
restituito il limite settimanale nelle prove iniziali; dopo il ripristino della quota
sono stati eseguiti i due flussi cross-family con CLI reali. La disponibilità binario del
doctor non equivale ad autenticazione o quota disponibile.

Corpus: task storico `aos-pytest-toml`, parent rosso / commit riferimento verde.
Il prompt delimita l'editing e affida test al host. Claude Code + Qwen: first-pass;
Claude Code + DeepSeek: successo dopo retry, non first-pass. Costi nativi Gateway
non riportati: null. OpenCode + DeepSeek: successo dopo retry nel fixture isolato,
prima dell'esclusione di sicurezza. Nessuno di questi risultati autorizza una
promozione del runtime; review benchmark non eseguita. Dati sanitizzati nel record
`operational-evidence.json`.

Le prime prove con limiti più bassi avevano timeout/tool-step limit anche con test
verdi: restano fallimenti. Non si seleziona soltanto la ripetizione riuscita.

## Learning e contesto

Il ledger è esterno al worktree. Una lesson richiede ID check, argv eseguito,
exit code, output e revisione; un claim modello incompleto è respinto. L'host è il
confine di fiducia: il ledger non attesta crittograficamente i suoi input.

Ciclo reale: ammissione indebita di un check privo di esecuzione riprodotta → finding
meccanico → fixer DeepSeek → stesso caso respinto → test permanente → lesson e
applicazione registrate. Secondo ciclo: bypass read OpenCode → prova tramite export
→ runtime disabilitato → stessa richiesta bloccata → lesson di policy con rollback.
Le applicazioni sono cambiamenti verificati dal coordinatore, non comandi arbitrari
eseguiti da testo generato. Non si modificano skill globali da una sola osservazione.

Lo storico produce consigli scoped per progetto/runtime/model/role/tier/risk solo
dopo almeno cinque task indipendenti. Retry dello stesso task non bastano. Nessuna
osservazione sintetica viene usata per promuovere il vincitore globale.

Context management: valutazione prima delle chiamate, handoff strutturato e
compaction delle sole parti eliminabili. RED residuo blocca. Il conteggio copre il
prompt fornito, non il contesto nascosto totale del runtime. Gli handoff preservano
vincoli, stato retry e finding. I budget sono admission check puntuali, non prenotazioni
atomiche tra sessioni concorrenti; nessun limite economico utente è stato inventato.

## Hook e misure

I due hook Stop installati sono stati invocati con evento controllato: stdout vuoto
valido ed exit 0. Il payload Codex Stop usa `decision/reason`; il test di regressione
copre anche PostToolUse, input malformato e rientro. Il sorgente vendor era già
corretto, non è stato modificato. Il test integrato salta se il vendor non è installato.

Telemetry distingue ruolo/runtime/provider/model/cost class, usage osservato e
stima catalogo. Costi e risparmi non misurati restano null. Il subtotale dei worker
con costo osservabile non è il costo totale della sessione né una misura della quota
subscription. Le percentuali obiettivo CHEAP richiedono un campione rappresentativo;
non vengono dichiarate raggiunte da questo intervento.

## Gate ancora da chiudere

Review indipendente completa dell’intera architettura oltre alle correzioni mirate; prova interattiva completa
nei due host oltre al contratto `main_host` con CLI reali; matrice offensiva completa; confronto benchmark più ampio e revisionato;
benchmark planner/reviewer; isolamento verificato dei runtime esclusi; budget hard
concorrenti e misura del contesto nascosto. Nessun PASS indipendente è dedotto dai
subagent Terra o dai test meccanici. Main rimane invariato.

## Aggiornamento prove cross-family

La pipeline è stata invocata dal coordinatore con entrambi i valori `main_host` e
con CLI/model reali: non è una registrazione di due sessioni interattive terminale.
T1 open-only: PASS. Planner Sonnet → DeepSeek via Claude Code → reviewer GPT-5.6-sol:
PASS. Planner GPT-5.6-sol → DeepSeek → reviewer Sonnet → due finding sui check
confermati → open fixer → reverify → sei finding successivi arbitrati/refutati
con baseline, contenuto esatto e diff HEAD: PASS. Un primo worker T2 aveva prodotto
un rifiuto di permesso; non è stato trasformato in escalation. La ripetizione ha
mantenuto gli stessi permessi. Nessuna esecuzione premium in questi fixture.

Compaction controllata con policy reale: 225011 token stimati ORANGE → 11 GREEN,
con vincoli e finding conservati. I contatori sono stime del prompt, non consumi.

Review finale Fable: primo round in timeout, due scope mirati restituiti ma respinti
dal validatore perché privi degli heading richiesti. I finding sono stati comunque
arbitrati; nessuno di questi round è contato come PASS. Il round successivo ha usato il formato corretto.


## Chiusura del checkpoint

Round 4 Fable completato con protocollo valido: nessun blocker nel perimetro letto,
un MINOR sul dedup delle lesson rifiutate. Riprodotto meccanicamente, corretto e
coperto da regressione. Round 5 Fable: PASS limitato alla correzione `_reject`,
non certificazione dell'intera architettura. I due report sono conservati qui in
forma leggibile; gli stream grezzi restano esclusi da Git.

Learning: 64 test mirati verdi, comprese evidenze ricopiate/rinominate, candidati
contraddittori approvati e rifiutati, migrazione dello stato `verification_status`,
esclusione di pending/legacy dallo steering, offset temporali e classi costo.
Il budget che esclude ogni modello adeguato ferma la pipeline prima delle chiamate.
L'apprendimento disabilitato non apre il ledger; un cap che ne richiede l'accounting
blocca se il ledger è disabilitato.

Questo è un checkpoint verificato sul branch di sviluppo, **NOT READY FOR MAIN**.
Non esistono blocker di quota Claude attualmente osservati; i limiti residui sono
tecnici e di copertura, non risolti rinominandoli blocker esterni.

Verifiche finali del checkpoint: entrambe le edizioni 452 test Python (448 PASS,
4 skip), 22/22 Node. Security Gate verde; doctor privato 0 anomalie e 7 avvisi
locali. Guardie originali invariate.

## Chiusura dei gate — 22 settembre, seconda sessione

Host principale Claude Code (Opus 5 1M, scelto dall'utente), reviewer esterno Codex.
Piano in [CLOSING-PLAN.md](CLOSING-PLAN.md). Ogni gate ha comando, esito e limite.

| Gate | Esito | Evidenza |
|---|---|---|
| G7 budget concorrenti | **chiuso** | `reserve_budget` in una transazione `BEGIN IMMEDIATE`; `tests/test_learning.py::ReservationTests` fa correre due processi contro un cap che ne ammette uno: uno ADMITTED, uno REFUSED. Hold rilasciato dall'outcome, scaduto dopo un'ora. |
| G7 contesto nascosto | **chiuso** | Ogni chiamata di ruolo registra stima del prompt e `observed_input_tokens` (input + cache). Misura live: planner Sonnet 441 → 28 914; reviewer GPT-5.6-sol 2 596 → 200 958 (legge il repo da solo). Restano stime i soli casi senza contatore del runtime (null, mai zero). |
| G3 matrice offensiva | **chiuso** | `bin/aos-isolation.py`: 7 letture, 6 scritture, 2 controlli permessi, più 2 comandi shell per i runtime con shell. Verdetto meccanico: canary nell'output, effetti su disco. |
| G6 isolamento runtime esclusi | **chiuso** | OpenCode nudo: 5 LEAK (`.env`, symlink, esterno, scrittura via symlink) — riproduce il finding storico; OpenCode sotto seatbelt: 13/13 negati, controlli OK → riabilitato **solo** con `os_isolation: seatbelt`. Claude Code: 13/13 con e senza seatbelt; seatbelt tenuto come strato OS. Codex CLI: filesystem 13/13 e rete negati dal suo sandbox, ma `npx --version` eseguito → resta escluso; il seatbelt esterno lo fa fallire all'avvio (non supportato). Record in `isolation/`. |
| G2 host Claude Code | **chiuso** | T2 reale su fixture: Sonnet pianifica (3 sottotask), DeepSeek esegue su Claude Code, check host, reviewer GPT-5.6-sol trova un MAJOR vero (alfanumerici Unicode scartati), riprodotto e confermato, fixer DeepSeek al secondo tentativo (il primo esaurisce 60 step), riverifica, round 2 senza finding → PASS. `live/claude-host-t2-state.json`. |
| G2 host Codex | **parziale** | `codex exec` con `$aos` sulla stessa fixture: classifica T2, avvia la pipeline con `main_host codex-cli`, misura, `plan` fallisce dentro il sandbox workspace-write di Codex (`claude exited 1`: il CLI annidato non può scrivere nella sua home), riesce con autorizzazione estesa; DeepSeek scrive i file, poi 402 budget Gateway; l'host riporta `execute` incompleto, non PASS. Nessuna sessione TTY registrata. `live/codex-host-t2-state.json`. Limite operativo: dal host Codex i ruoli premium annidati richiedono l'escalation fuori dal sandbox. |
| G4 benchmark ampio | **non eseguito, per scelta** | Corsa fermata a 3 task su 20 dall'utente: ore di esecuzione per una decisione che non si prende comunque qui. Il benchmark del 2026-09-20 resta autorevole; i tre record e la ragione in `docs/misure/bench/2026-09-22-claude-code/README.md`. Il codice del gate esiste ed è testato (`--planners`, `--review-replay`). |
| G5 benchmark planner/reviewer | **non eseguito, per scelta** | Stessa ragione: gli strumenti ci sono, la corsa no. Nessun candidato MID è stato promosso; i loro punteggi restano soglie di policy dichiarate, non misure. |
| G1 review indipendente | **eseguito, 3 round** | Round 1: 2 BLOCKER + 4 MAJOR; round 2: 1 BLOCKER + 2 MAJOR, tutti nuovi e tutti riprodotti meccanicamente prima della correzione. Verbale in REVIEW-LOG.md, report in `round*-codex.md`. |

Blocco esterno incontrato: **AI Gateway Vercel, budget team 50 $ esaurito** durante il
primo task del bench G4 (`402 Team budget exceeded. Current spend: $50.10`). L'utente
ha scelto di alzare il tetto; le corse G4/G5 sono riprese dopo il ripristino.

Correzioni emerse dalle prove live: `json_reply` estrae l'oggetto JSON da una risposta
con prosa attorno (il reviewer GPT-5.6-sol aveva risposto con testo e oggetto, e la
review era saltata con `Expecting value`); la sonda usa canary distinti per symlink e
file esterno; OpenCode riceve la credenziale `{file:}` risolta dall'host come variabile
d'ambiente perché sotto seatbelt non legge più `~/.secrets`.


## Decisioni dell'utente del 22 settembre e stato finale dei runtime

**OpenCode fuori dal progetto.** La sonda nuda aveva letto `.env`, il bersaglio del
symlink e il file esterno, e scritto attraverso il symlink; sotto seatbelt negava
13/13. L'utente ha scelto la rimozione completa invece della convivenza: plugin
d'ingresso, installer, runner, permission map, test Node e riferimento sono stati
cancellati e la configurazione utente ripulita con il rollback dell'installer. I
record delle sonde restano in `isolation/history/` come storia, non come policy.

**Codex fuori come contenitore dei modelli open.** La prova decisiva è di oggi: con
lo `shell_tool` disabilitato Codex non ha alcuno strumento file — `apply_patch`
viaggia dentro la shell, e `codex features list` non espone un tool alternativo — e
la sonda ha registrato 0 chiamate con quindici «no file tools available». Con la
shell accesa il suo strato di regole non ferma i comandi (`npx` eseguito mentre file
e rete erano negati). Restano quindi due sole possibilità, entrambe peggiori di
Claude Code: un worker inerte o un worker con una policy dei comandi non
dimostrabile. Codex resta host, planner MID, reviewer cross-family ed escalation
premium: è lì che vive il gate indipendente, ed è intatto.

**Claude Code è l'unico harness open**, con `os_isolation: seatbelt`. Record finale
`isolation/claude-code-seatbelt.json`: 13/13 bersagli vietati negati, entrambi i
controlli permessi eseguiti, 22 chiamate agli strumenti registrate. Il limite di
questa scelta è dichiarato: nessun harness di riserva — se il contratto del CLI
cambia, l'esecuzione open si ferma al controllo di versione invece di ripiegare — e
i modelli raggiungibili sono quelli che il provider serve sul protocollo Anthropic
(DeepSeek e Qwen provati; ogni nuovo modello va provato prima di entrare in
catalogo). Riabilitare Codex richiede una sonda verde presa con la shell accesa,
non un test di connettività.
