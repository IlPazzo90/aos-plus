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
