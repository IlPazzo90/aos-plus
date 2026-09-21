# OpenCode come ingresso quotidiano

Apri il terminale nella cartella del progetto e avvia `opencode` oppure
`opencode --mini`. OpenCode lavora direttamente sui file della cartella corrente:
non crea una copia del progetto. Anche i CLI premium ricevono quella directory.
Con più agenti contemporanei, assegna file distinti o usa worktree.

## AOS e scelta del modello

Il loader globale richiama `opencode/aos-bridge.mjs` dalla sorgente AOS. Prima di
ogni messaggio, un classificatore senza strumenti valuta compito, rischio, dominio
e capacità richieste. `bin/aos-router.py` applica `config/open-models.json`.

- I turni open usano il modello scelto da AOS nel runtime nativo OpenCode, con
  skill e MCP disponibili. Il modello selezionato manualmente nell'interfaccia
  non prevale su questa scelta.
- I turni premium chiamano `aos_execute`: Claude Code o Codex CLI, con modello
  definito dalla policy e abbonamento del rispettivo CLI. Il coordinatore non può
  eseguire il lavoro con altri strumenti. Non vengono attivati bypass dei permessi.
- Se una skill rivela una capacità di un altro host, `aos_handoff` riclassifica
  il compito e trasferisce anche lo stato del lavoro. Non concede nuove autorizzazioni.
- AOS può scomporre il lavoro e scegliere esecutori distinti per sottocompiti.
  La delega non sostituisce le verifiche previste da AOS.
- CRITICAL richiede il controllo di approvazione del runtime. Plan non lancia
  esecutori premium. Errori di classificazione fermano il turno; un'esecuzione
  interrotta non viene ripetuta automaticamente.

La riga `AOS · tier/rischio · esecutore` nella risposta indica la destinazione
effettiva. L'etichetta nativa del modello nella TUI può restare sulla selezione
manuale. Gli export della sessione conservano il modello di ogni messaggio.

## Compatibilità

Le skill sono istruzioni, gli MCP sono strumenti e gli hook sono codice del loro
runtime. Queste tre cose richiedono verifiche diverse.

| Componente | Comportamento in OpenCode |
|---|---|
| Skill in `~/.agents/skills` e `~/.claude/skills` | Scoperta nativa; la fonte resta la stessa |
| Skill dei plugin attivi | Riferimenti ai percorsi esistenti con `--import-skills`; snapshot `skills/list` opzionale per plugin nativi Codex; nessuna copia |
| MCP stdio/HTTP di Claude e dei plugin attivi | Conversione locale con `--import-mcp`, mantenendo configurazione e credenziali fuori Git |
| Plugin/hook specifici di Claude | Restano nel runtime Claude; handoff quando necessari |
| Strumenti esclusivi dell'app Codex | Non disponibili nel CLI: richiesta fermata con indicazione del limite |
| CLI locali usati dalle skill | Utilizzabili tramite shell se installati e consentiti dai permessi |

Il catalogo comune non garantisce che qualsiasi modello interpreti ogni skill
allo stesso modo. Una skill che nomina uno strumento assente non lo rende
disponibile. AOS deve usare la capacità reale o dichiarare il limite.

Gli import fanno riferimento ai plugin attivi al momento dell'installazione.
Dopo un aggiornamento che sposta le directory dei plugin, verifica i riferimenti
e aggiorna l'installazione. Non importare indiscriminatamente tutti i cache.

## Installazione e ripristino

La dipendenza `@opencode-ai/plugin` deve essere già installata nella configurazione
OpenCode. L'installer non scarica pacchetti. Usa Python 3.11+ per leggere anche la
configurazione TOML di Codex con `--import-skills`.

```sh
python3 bin/aos-opencode-install.py install --import-mcp --import-skills --vscode
python3 bin/aos-opencode-install.py check
python3 bin/aos-opencode-install.py rollback
```

L'installer conserva backup privati e verifica che i file non siano stati cambiati
prima del rollback. Se rileva modifiche successive, si ferma per non sovrascriverle.
Chiudi e riapri OpenCode dopo installazione o rollback. I test si eseguono con una
home temporanea, senza modificare le configurazioni reali di Claude e Codex.

`--pure` disabilita intenzionalmente i plugin. È riservato a worker e classificatore
isolati: non usarlo per l'ingresso quotidiano. Un utente che disabilita il loader
o avvia OpenCode con una configurazione diversa esce da questa installazione AOS.
Il plugin è un controllo di instradamento, non una sandbox del sistema operativo.

Controlli locali: `opencode debug skill`, `opencode mcp list`, export della sessione,
`node --test tests/opencode-entry.test.mjs` e suite Python AOS. I protocolli specifici
dei singoli MCP richiedono login e dipendenze funzionanti: un riferimento importato
non è una prova di connessione.

Lo snapshot completo dei plugin Codex si ottiene tramite la funzione `discover` di
`bin/skill-library.py` (API locale `skills/list`); passalo con
`--skills-snapshot <file.json> --import-skills`. Le skill marcate disabilitate nello
snapshot non vengono aggiunte. Una skill già presente nei roots condivisi resta
comunque autoscoperta da OpenCode: le preferenze di visibilità dei due host sono
separate. L'import non esegue gli hook Claude/Codex nel runtime OpenCode.

Gli MCP OAuth richiedono un login per il client OpenCode (`opencode mcp auth <nome>`).
L'import non copia i token OAuth di Claude. Se il login diretto manca, il compito
può passare al runtime già autenticato tramite AOS; non dichiarare una connessione
OpenCode riuscita finché `opencode mcp list` non la conferma.

### Permessi e contesto lungo

`aos_execute` rispetta la policy OpenCode, compreso `*`; con il default `allow`
il passaggio al CLI è automatico, come richiesto dal routing. Per richiedere una
conferma a ogni passaggio, impostare `permission.aos_execute` a `ask`. Il CLI
mantiene i propri permessi; i compiti CRITICAL richiedono anche `aos_critical`.
Codex eredita sandbox e approvazioni dalla propria configurazione: AOS non forza
`workspace-write` né abilita la rete. Se la configurazione Codex vieta la rete,
anche il lavoro delegato resta senza rete.

Il routing riparte dall’ultimo riepilogo di compattazione disponibile e conserva
tutti i messaggi utente successivi. Senza riepilogo conserva tutti i messaggi utente.
Quando lo storico supera 140.000 caratteri, conserva le evidenze recenti degli
strumenti e segnala quelle omesse; il modello deve verificare i file prima di
ripetere operazioni. Se i soli messaggi utente superano il limite, richiede una
compattazione esplicita per non perdere vincoli o autorizzazioni.
