> Stato aggiornato: sospeso su richiesta. Vedi [HANDOFF](HANDOFF-2026-09-22.md): rilievo HIGH e test rosso aperti; nessun rilascio. Il testo sotto descrive la fase precedente.

# Premium plan / open execute: stato di verifica

Lavoro recuperato da un tentativo interrotto, su autorizzazione esplicita. La
versione di partenza committata instradava T3 direttamente al premium; T2 ammessi
usavano già open, senza separazione eseguibile di planner, reviewer e fixer.

La nuova pipeline separa i ruoli e conserva la scelta dei modelli in
`config/open-models.json`. DeepSeek resta il benchmark winner; Qwen il fallback.
Non sono state modificate guardie, denylist, Security Gate o installazione Verify Agent.

| Tier | Planner | Executor | Reviewer | Fixer |
|---|---|---|---|---|
| T0 | nessuno | open | deterministico | open |
| T1 | nessuno | open primary | deterministico | open |
| T2 ammesso | premium read-only | open primary | premium della famiglia opposta | open |
| T3 ammesso | premium, architettura e sotto-task | open per sotto-task | review premium finale della famiglia opposta | open |

HIGH conserva l'eccezione open T2 con controllo osservabile; negli altri casi
resta la policy premium esistente e la review obbligatoria. CRITICAL conserva
l'approvazione umana, anche con classificazioni passate in minuscolo.

Il piano contiene obiettivo, scope, file, step, criteri di accettazione, rischi,
vincoli di sicurezza, test, architettura, dipendenze, incertezze, esclusioni e
sotto-task. I comandi effettivi richiedono i permessi dell'host; il piano non è
un'autorizzazione. Planner e reviewer riusano gli adattatori read-only di Verify Agent.

Il reviewer riceve evidenze e diff; i finding strutturati non sono accettati sulla
fiducia. Ogni finding richiede check di riproduzione o controprova sulla revisione
corrente. Quelli confermati tornano all'open fixer; quelli refutati non producono
correzioni. Nessun PASS da review vuota, interrotta o non disponibile.

`premium_execution.default=false`. La policy attuale concede due tentativi al
primary, poi due al fallback; esauriti entrambi abilita l'escalation premium.
Rifiuti di permesso, conflitti su modifiche preesistenti e alterazioni dei metadati
Git bloccano il lavoro: non sono una ragione per aggirare il rifiuto col premium.
Restano le eccezioni esplicite di rischio, capacità del runtime e override.

Telemetria: identità e token per planner/executor/reviewer/fixer, esecuzione
premium e motivazione, finding totali/confermati/refutati, contatori open e
rapporti dei token. `role_events` conserva le singole chiamate e usage nativo.
Contatori assenti e risparmio premium non misurabile rimangono null. Non si
attribuisce un costo a una quota in abbonamento; confronti causali tra planner e
soglie storiche automatiche richiedono dati ulteriori.

Compatibilità: Claude/Codex usano gli stessi contratti; OpenCode ha lo strumento
aos_pipeline. Modelli assenti mantengono il routing legacy dove disponibile. Una
review cross-model richiesta ma indisponibile resta incompleta; non si dichiara
indipendente una review della stessa famiglia. OpenCode senza abbonamenti premium
può eseguire T0/T1; T2/T3 della nuova pipeline restano bloccati al ruolo mancante.
Questa limitazione impedisce di dichiarare copertura completa di ogni ambiente.

Verifiche definitive e stato Git: da completare dopo il secondo worker open.
I mini E2E usano provider simulati e processi Git/check reali; non costituiscono
una prova live dell'intera catena Codex/DeepSeek/Claude. Il lavoro operativo del
recupero usa invece davvero il primary open tramite aos-delegate.

Review indipendente: Claude ha risposto quota settimanale esaurita. Il gate non
è passato. Verbale ed evidenze in REVIEW-LOG.md; tracce grezze escluse da Git.
