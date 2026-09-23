# Changelog

## 3.0.1 — l'hook non lascia tracce e tace sulle notifiche — 2026-09-23

Codex ha riletto l'hook prima che lo approvassi e ha corretto due cose che avevo scritto
male: l'hook non legge solo stdin (legge anche `config/open-models.json` e
`config/adaptive.json`), e gli import potevano creare `__pycache__` accanto agli script.
Ora `aos-prompt-hook.py` imposta `sys.dont_write_bytecode`, decide se tacere prima di
caricare moduli e configurazioni (un «ciao» non apre più nessun file) e non classifica
le notifiche dei task in background che l'host gli passa come prompt: prima ogni
«comando completato» riceveva la sua riga AOS. Il comando registrato in
`hooks.json` non cambia, quindi l'approvazione già data in Codex resta valida.

Verifiche: 665 test, verdi su Python 3.12 e 3.9 (3 skip per `tomllib`); tre test nuovi
(notifiche zitte, nessun modulo caricato su un prompt da ignorare, nessun `__pycache__`
eseguendo l'hook da una copia pulita).

## 3.0.0 — il modello lo sceglie il costo atteso, non il tier — 2026-09-23

**Chiedi un contratto, e DeepSeek non lo scrive.** Prima di oggi una richiesta non di
codice usciva da AOS con una frase («non è lavoro software»), e dentro AOS il modello
lo sceglievano tier e rischio: un T1 LOW andava al worker open qualunque cosa fosse.
Ora `bin/aos-orchestrate.py route` legge la richiesta, la mette in uno o più dei sei
domini (GENERAL, ENGINEERING, LEGAL_COMPLIANCE, BUSINESS_OPERATIONS, RESEARCH,
DATA_ANALYTICS), ne ricava un vettore di capacità e scarta chi non le ha: DeepSeek
dichiara 0,4 di capacità legale, un contratto chiede 1,0, e il router lo rifiuta con
`capability shortfall legal` prima di calcolargli un punteggio. Fra chi resta sceglie
il costo atteso più basso sopra la soglia di successo del rischio:
`iniziale + (1−p)·retry + (1−p)²·escalation`. Un modello economico che sbaglia spesso
perde contro uno più caro che non sbaglia; senza storico vince il più economico che
basta. Ogni termine del punteggio esce nel JSON, così puoi controllare perché.

Cosa entra, e dove sta:
- **Registry.** Ogni voce di `config/open-models.json` ha classe (PREMIUM, HIGH, MID,
  LOW, OPEN), famiglia, modo d'invocazione, strumenti e capacità dichiarate da 0 a 1
  (prior a bassa confidenza, non misure). Entrano `anthropic/haiku` e
  `openai/gpt-5.6-luna` come LOW. HIGH è supportata e vuota: nessun modello verificato.
- **Qwen esce dal pool.** Resta nel catalogo per lo storico, `availability: false`;
  la scala open diventa DeepSeek ×2 → premium. Anche `aos-open-executor.py` lo rifiuta
  se lo chiedi con `--model`.
- **Tutta la conoscenza in `config/adaptive.json`:** parole chiave IT/EN, vettori per
  dominio e tipo di task, skill per dominio, controlli deterministici per dominio,
  tassonomia dei 13 tipi di errore con azione e attribuzione, soglie di scoring,
  exploration e apprendimento. Il codice non nomina un modello (c'è un test che lo
  controlla).
- **Bundle solo quando servono.** Si spezza in requisiti → implementazione → verifica
  congiunta solo una funzione che attraversa un dominio di requisiti e uno di
  produzione («costruisci un modulo GDPR»). «Analizza i KPI» resta un lavoro solo.
- **Reviewer indipendente.** Famiglia opposta all'esecutore (all'host, se l'esecutore è
  open), premium a HIGH, mai un modello OPEN o LOW come revisore finale; se un reviewer
  fallisce, l'escalation resta nella sua famiglia.
- **Escalation per causa.** `escalate --failure-type`: un timeout riprova lo stesso
  modello e non gli viene addebitato; un errore di ragionamento sale di classe, saltando
  HIGH vuota; un'ambiguità architetturale va al planner premium; una violazione di
  sicurezza si ferma e chiede a te.
- **Exploration.** Al massimo 5%, solo LOW, reversibile, con un controllo osservabile
  dichiarato (`--verifiable`) e un task id: deterministica, mai su lavoro critico.
- **Apprendimento.** `aos-learning.py record-outcome | matrix | model-status | kpi |
  recommend`. La matrice dà punteggio, campione, confidenza e ultimo aggiornamento per
  modello, capacità, dominio e tipo di task, con decadimento a 30 giorni; conta solo
  esiti `verified`; un successo con retry, escalation o finding vale meno di uno
  pulito; un errore di contesto o di infrastruttura non abbassa il modello. Stati NEW,
  KEEP, WATCH (deriva), PROMOTE, DEMOTE, mai dopo un solo fallimento. `recommend`
  distingue osservazione, ipotesi e raccomandazione e non applica niente.
- **Contesto per task.** `aos-context.py zone` stringe la zona ottimale con
  complessità, incertezza, volume di evidenze e ragionamento profondo; il limite duro
  non si sposta. `checkpoint when` aspetta il prossimo punto naturale (fine discovery,
  test verdi, fine bundle…) e compatta subito solo oltre il limite duro; `checkpoint
  validate` blocca il rilascio del contesto senza obiettivo, stato, decisioni, lavoro
  aperto, stato della verifica e prossima azione.
- **Hook.** `bin/aos-prompt-hook.py` su `UserPromptSubmit`, da registrare a mano in
  Claude Code e Codex (README, «Prompt hook»): una riga con domini, tipo e skill, 35 ms,
  zero token di modello, sempre exit 0, zitto su saluti, comandi slash e prompt corti;
  `AOS_PROMPT_HOOK=off` lo spegne. Non stampa tier né esecutore: da parole chiave
  sarebbero valori di default travestiti da decisione.

**Come ci si è arrivati.** Piano premium, tre bundle eseguiti in parallelo da un worker
open in tre worktree esterni, poi sei round di review cross-model (il tetto): 21 finding,
20 accettati e corretti con un test ciascuno, uno confutato. Cinque erano correzioni del
round prima, sbagliate o incomplete. Dal secondo round in poi stavano tutti nei KPI per
task: il ledger registra tentativi, i KPI chiedono task, bundle e review, e quella
struttura è entrata a strati (colonna `bundle`, review legate al loro bundle, task
distinti per progetto). Il prossimo lavoro sul ledger dovrebbe modellarla per davvero.

Verifiche: suite di 662 test, verde su Python 3.12 e 3.9 (3 skip preesistenti per
`tomllib`); `aos-security.sh` exit 0; `aos-doctor` pulito salvo la distribuzione;
hook provato con un prompt reale su Claude (il modello ha riportato la riga).
Verdetto della review: verificato con riserve, perché l'ultima correzione (round 6) è
provata dai test e dal comando del reviewer ma non ha avuto un settimo round.

Limiti noti:
- In Codex l'hook gira solo dopo che ne hai approvato l'hash (`/hooks`).
- L'hook classifica anche le notifiche di sistema che l'host inietta come prompt.
- La classe HIGH è vuota finché non c'è un modello verificato da metterci.

## 2.6.0 — audit completo: 40 bug corretti, un T0 non passa più dal worker — 2026-09-22

**La causa scritta nella 2.5.2 era sbagliata.** Il worker open non poteva scrivere
perché il repository stava sotto `~/.claude/skills/`: Claude Code considera «sensitive
file» ogni percorso con un segmento `.claude` e nega la scrittura in qualunque modalità
di permesso. Lo scrub dell'ambiente forza sì `--permission-mode default`, ma con la
lista `allow` delle `--settings` le Edit passano (provato contro un server Anthropic
finto, nessuna chiamata a pagamento). Ora `aos-open-executor.py` rifiuta un repository
sotto `.claude/` o `.codex/` prima di spendere un token e suggerisce un worktree
esterno; il messaggio di negazione nomina strumento e percorso invece di un generico
«permission denied». `--allowedTools` resta aggiunto: non era la causa, ma è quello
che lo scrub chiede.

**Il ripiego premium su Claude non scriveva niente e dichiarava successo.**
`aos-entry.py` lanciava `claude -p --permission-mode dontAsk` senza strumenti
pre-approvati: ogni Write negata, `subtype: success`, escalation registrata come
riuscita. Ora passa `--allowedTools Read,Glob,Grep,Edit,Write` e una
`permission_denials` non vuota è un errore.

**Il routing.**
- Un T0 resta sulla sessione host: LOW sul subagent economico, MEDIUM su quello
  intermedio, HIGH sul modello principale. I nomi stanno nella nuova sezione
  `host_subagents` di `config/open-models.json` (Claude `haiku`/`sonnet`, Codex
  `gpt-5.6-luna`/`gpt-5.6-sol`); `aos-router.py --host claude|codex` li risolve.
  Prima un refuso passava da brief, seatbelt e worker esterno.
- La review cross-model vale per ogni T2/T3 a qualsiasi rischio e per nessun T0/T1:
  a T0/T1 HIGH i controlli HIGH si fanno in linea. SKILL.md, quality-gates.md, il
  router e gli scenari dicevano tre cose diverse.
- Il router non promette più `premium_review` quando nessun revisore è disponibile.
- La scala open è primary, fallback, MID, premium e non torna mai indietro: con il
  fallback spento i tentativi 2 e 3 andavano al premium e il 4 di nuovo al MID. Ogni
  tentativo ha il suo posto fisso, un gradino spento cede il turno al successivo, e il
  MID ha un solo posto come nella pipeline (`aos-pipeline.py`), che lo saltava del
  tutto quando mancava il fallback. Il router non ha stato: chi ha già fatto girare il
  MID al posto di un gradino spento lo dice con `--no-open-mid`.
- `--config` inesistente o inutilizzabile è un errore (exit 2), non un `executor=main`
  silenzioso.
- Un override manuale dell'esecutore a T2/T3 non toglie più la review: il revisore
  è la famiglia opposta all'host (`--host`), scelto con le stesse regole di budget e
  disponibilità della pipeline; senza un modello disponibile nessuna review è promessa.

**SKILL.md da 18,8 a 15,3 KB**, caricato a ogni task. Il comando del router, che
mancava del tutto, ora è in §1 con il significato di ogni esito; l'elenco dei campi di
telemetria, la storia delle sonde e i dettagli dei runtime sono passati in
`references/orchestration.md`. Nessuna regola con effetto è stata tolta. Nell'edizione
pubblica `orchestration.md` e `aos-profile.sh` descrivevano ancora OpenCode: ora sono
allineati al codice.

**Sicurezza (`aos-security.sh`).**
- Con un solo file in ambito `grep` non stampa il nome del file, l'oscuramento non
  trovava `:riga:` e **il segreto usciva in chiaro**. Ora `grep -H`, e una riga senza
  forma `file:riga:` viene scartata, mai stampata.
- File con nomi non ASCII saltati (git li quota); file in stage saltati in un
  repository senza commit.
- Chiavi non riconosciute: `sk-proj-…`, `sk-ant-api03-…`, Stripe `sk_live_`, PEM,
  valori senza virgolette nei `.env`; `process.env.KEY || "AKIA…"` era nascosto dal
  filtro sull'ambiente.
- La sezione 4 non cercava `eval`/`exec` con input della richiesta, `os.system`,
  `shell=True`, `pickle.loads`, SQL composto a mano; ora sì, e dice «ok» quando è pulita.
- Le migration si leggono senza commenti né stringhe SQL e con le policy su più righe
  unite. Delle policy segnalate si stampa solo la testa, fino al primo carattere che
  non può stare in un identificatore e al massimo sei parole (`create policy p on t
  using`): cinque round di review hanno trovato cinque modi di far uscire un valore
  dal testo stampato (`$$…$$`, `--` dentro la stringa, `E'…'`, identificatori con
  apici, commenti annidati senza spazi), e un lexer a regex ne avrà sempre un altro.

**Il resto, per file.**
- `aos-status.py`: 20 `usage` in parallelo registravano 12.000 token su 20.000 (niente
  lock, un solo `.tmp` condiviso); `clear` cancellava il file di lock senza prenderlo;
  `--session ../x` scriveva fuori dalla cartella; token negativi accettati.
- `aos-measure.py`: `finish --pipeline` sovrascriveva con `null` i contatori passati a
  mano; `premium_dependency_ratio` non contava review e planning come documentato (ora
  resta nullo finché uno dei due è ignoto; nuovo `--planner-tokens`);
  un `--pipeline` illeggibile falliva dopo aver preso il lock.
- `aos-context.py`: `anthropic/fable`, `sonnet`, `opus` cadevano nei limiti di
  default invece della classe `claude`; un id duplicato con contenuto diverso scartava
  la versione nuova, anche di un criterio di accettazione; traceback su input errato.
- `aos-open-executor.py`: gli step contavano i blocchi di contenuto, non i turni
  (thinking + testo + tool = 3 step); campi `null` negli eventi facevano crollare la
  run dopo averla pagata.
- `aos-pipeline.py`: il MID non veniva mai provato senza fallback;
  `open_fallback_used` vero quando nessun fallback era configurato.
- `aos-bench.py`: `--cap-usd` accettato sui run di riferimento e mai applicato; un
  rifiuto al secondo tentativo cancellava tempo e costo del primo.
- `aos-learning.py`: `reserve_budget(now=…)` falliva con una stringa e con un
  datetime usava due orologi diversi; `report --since` perdeva le righe dello stesso
  secondo (timestamp ora sempre a microsecondi).
- `aos-doctor.py`: «isolation verified» bastava che il file esistesse, ora legge il
  record; gli hook Stop di gstack e Impeccable erano contati come hook AOS.
- `aos-profile.sh`: diceva sana un'installazione con il doctor in errore; `test:unit`
  valeva come script `test`; n8n trovato dentro `tmp/`. `profile-python.py` ignorava i
  test pytest senza `import pytest`.
- `aos-install.sh`: un link Codex rotto passava come «ok». `skill-library.py`: la
  ricerca confrontava anche i percorsi, e `skills` trovava tutto.
- `aos-isolation.py`, `aos-entry.py`: traceback su modello sbagliato; il file del
  report restava orfano in `/tmp`.

Lasciato com'è per scelta: una prenotazione di budget con stima ignota blocca
l'ammissione per un'ora (è il comportamento fail-closed voluto, con il suo test);
il ripiego premium su Codex eredita la sandbox di `~/.codex/config.toml`.

Verifiche: 18 moduli, da 460 a 516 test verdi più 3 saltati su Python 3.9;
`aos-doctor.py` 0 anomalie; scan di sicurezza provato su un repository con segreti
piantati. Gate esterno con Codex, verbale in `docs/verifiche/aos-2-6-0-audit/`.

## 2.5.2 — la barra dice quando il routing è stato ignorato — 2026-09-22

`aos-status.py` pubblicava quello che il chiamante dichiarava, `aos-router.py`
sapeva cosa andava fatto, e niente confrontava le due cose: una sessione che
ignora il routing e lavora sul modello principale era indistinguibile da una che
lo rispetta. Ora `set` interroga il router con lo stesso tier e rischio, salva la
decisione in `routed_executor` e la barra mostra `⚠ open` quando il router
avrebbe delegato a un worker open e il chiamante ha pubblicato altro:

    🤖 T2/LOW · claude-opus-5 · sub ⚠ open

L'avviso tace in ogni altro caso, e in particolare quando il router dice
`premium` e l'esecutore è `main`: la sessione principale gira già su un modello
di fascia premium, quindi quell'avviso si accenderebbe su quasi ogni task HIGH e
insegnerebbe a ignorare il simbolo. Un router che non risponde lascia la barra
esattamente com'era.

Implementazione delegata al modello open configurato via
`bin/aos-open-executor.py`: 40.512 token in ingresso, 23.749 in uscita, 26 passi.
Il worker ha letto il codice e prodotto progetto, patch e test; non ha potuto
scriverli perché l'harness forza `--permission-mode default` quando
`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` è attivo, quindi ogni Edit è stato negato e
l'executor ha riportato `permission denied; do not escalate as a model failure`
invece di ricadere su un modello premium. Le modifiche sono state applicate a
mano dal suo output. Resta da dichiarare `allowedTools` esplicitamente
nell'executor: finché non lo si fa, un worker open può leggere ma non scrivere.

Verifiche: 7 test nuovi in `tests/test_status.py` (26 nel modulo); 18 moduli verdi
uno per uno; `aos-doctor.py` senza anomalie.

## 2.5.1 — il router da riga di comando leggeva la politica giusta — 2026-09-22

`bin/aos-router.py --tier T1 --risk LOW` rispondeva `executor=main`, «not eligible
for open», per ogni tier e ogni rischio: `--config` aveva default `None` e
`load_config(None)` restituisce una config vuota, che il router interpreta come
«nessun modello open configurato». Il percorso di produzione era sano
(`aos-entry.py` passa `config/open-models.json`), ma chi verificava il routing da
riga di comando leggeva una decisione di routing dove c'era solo un file mancante.

Ora il default è `DEFAULT_CONFIG`, risolto dal file dello script e non dal cwd, così
la risposta è la stessa da qualsiasi directory. `--config` esplicito continua a
vincere, anche quando punta a una politica inutilizzabile.

Verifiche: 3 test nuovi in `tests/test_router.py` (default senza argomenti, default
da una directory estranea, `--config` esplicito che vince); 18 moduli verdi uno per
uno; `aos-doctor.py` senza anomalie.

## 2.5.0 — il routing si vede nella barra di stato — 2026-09-22

`decide()` è una funzione pura e l'executor open riporta token, non denaro: fuori
dalla conversazione nessuno sapeva su quale modello stesse girando il task. Nuovo
`bin/aos-status.py`: `set` pubblica tier, rischio e catena planner→executor→reviewer
in un record per sessione, `usage` somma i token che il worker open ha bruciato
davvero. La chiave è l'identificativo di sessione dell'host, quindi due sessioni sullo
stesso repository non si sovrascrivono; senza identificativo ogni comando è un no-op.

Il costo è una **stima** dai prezzi di `config/open-models.json`, mai la fattura:
l'harness nativa non riporta il billing del gateway (`aos-open-executor.py` rifiuta
`--max-cost` per questo). La stima applica il moltiplicatore `peak_pricing` in base
all'ora UTC e prezza i token di cache con `input_cache_read`. Executor premium: nessuna
cifra, sta nell'abbonamento.

`aos-open-executor.py` pubblica i token solo dalla sua `main()`, non da `run()`: una
sessione di benchmark o un chiamante di libreria non sporcano la barra dell'utente.
La pubblicazione non può far fallire una delega — errore ingoiato per disegno, con
due test che lo provano (directory non scrivibile, sessione assente).

Verifiche: 19 test nuovi in `tests/test_status.py` (prezzi off-peak e peak, finestra a
cavallo di mezzanotte, cache più economica dell'input fresco, modello fuori catalogo
senza cifra, record corrotto, record scaduto, accumulo fra run, riclassificazione che
non azzera i token); i test dell'open executor verdi dopo la modifica.

## 2.4.0 — un solo harness open, verificato con una sonda d'attacco — 2026-09-22

Cinque gate di rilascio su sette chiusi; i due di benchmark non eseguiti per scelta,
con la ragione scritta nel verbale
[OPERATIONAL-VALIDATION.md](docs/verifiche/premium-plan-open-execute/OPERATIONAL-VALIDATION.md).
Host principale Claude Code, reviewer esterno Codex, sei round fino al tetto: 14 finding,
tutti nuovi, tutti riprodotti meccanicamente prima della correzione, nessuno confutato,
nessun round degradato. Verdetto VERIFICATO CON RISERVE in REVIEW-LOG.md — la riserva
principale è che le correzioni dell'ultimo round non hanno avuto un round indipendente.

**Un solo contenitore per i modelli open: Claude Code.** OpenCode è stato tolto dal
progetto (plugin d'ingresso, installer, runner, permission map; config utente ripulita
col rollback) dopo che la sonda ne aveva dimostrato le fughe. Codex non fa da
contenitore: modifica i file attraverso il tool shell, la cui policy dei comandi non
tiene la prova d'attacco, e senza shell resta senza strumenti file (sonda: 0 chiamate).
Codex resta host, planner, reviewer cross-family ed escalation premium. Il worker open
ha quindi Read/Glob/Grep/Edit/Write e nient'altro; l'host esegue ogni check. `aos-delegate.py` resta la guardia
comune (albero pulito, proprietà del retry, impronta dei metadati git, timeout di
gruppo, tetto di step); la credenziale del provider arriva da `api_key_env` o
`api_key_file`, letta dall'host e mai nel repo o negli argomenti.

**Isolamento verificato.** `bin/aos-isolation.py`: fixture usa e getta, 13 bersagli
vietati + 2 controlli permessi (+ 2 comandi per gli harness con shell), verdetto
meccanico sulle chiamate agli strumenti e sugli effetti su disco; un runtime escluso
gira solo dentro la fixture marcata. Profilo seatbelt macOS attorno al worker
(`seatbelt_profile`): Claude Code 13/13 negati con e senza; Codex nega file e rete ma
esegue `npx` con la shell accesa → shell spenta, riabilitazione solo con sonda verde.
Produzione non può scegliere `os_isolation`; `isolation_verified` vale solo se lo
strato applicato è quello verificato.

**Budget concorrenti.** `reserve_budget`: ammissione e prenotazione in una sola
transazione `BEGIN IMMEDIATE`; due sessioni contro un cap che ne ammette una: una
ammessa, una rifiutata (test a due processi). Un outcome libera solo la prenotazione
che nomina; scadenza dopo un'ora.

**Contesto nascosto.** Per ogni chiamata di ruolo: stima del prompt e
`observed_input_tokens` (input + cache); `hidden_context_tokens` è la differenza,
null se non riportato. Misurato: reviewer 2 596 stimati → 200 958 osservati.

**Pipeline.** `json_reply` estrae l'oggetto da una risposta con una riga di prosa e
rifiuta una spiegazione con un esempio dentro; `attacked` deve essere una lista non
vuota di stringhe; un riferimento di modello assente dal catalogo blocca invece di
raggiungere il CLI; un catalogo senza i modelli open configurati blocca invece di
andare in premium; il fixer ha 25 step (misurato: 60 step per zero righe).

**Bench.** `--planners` (piano MID/premium nel brief dell'esecutore) e
`--review-replay` (reviewer candidati sui diff registrati). Nessuna promozione di
modelli o runtime.

**Prove live.** Host Claude Code: T2 completo, finding MAJOR vero confermato →
fixer → PASS. Host Codex via `codex exec`: piano ed esecuzione, poi 402 del Gateway
(budget team esaurito, alzato dall'utente); dal sandbox di Codex i ruoli premium
annidati richiedono l'escalation.

Review Codex, sei round: 2+4, 1+2, 1+3, 1+1, 1+2, 1+1 fra BLOCKER e MAJOR. Il filo che
li lega è uno solo — **una prova è un'osservazione, non un resoconto**. La sonda contava
il testo del proprio report come un tentativo; giudicava una lettura senza canarino sulla
parola del worker; trasformava in diniego l'assenza di un risultato correlato e, per le
scritture, l'assenza di un effetto finale su disco; ignorava una grafia assoluta
equivalente dello stesso bersaglio. Dall'altro lato la catena verifica → review: una
frase breve rimetteva in PASS una review dichiarata non eseguita, un check poteva
cambiare `.git`, spostare HEAD, nascondere un file tracciato con un flag d'indice o
riscrivere il codice dopo averlo verificato, e due worktree diversi potevano condividere
una sola revisione. Ogni finding è stato riprodotto prima della correzione e coperto da
regressione. Verifica finale: 436 test Python, Security Gate verde, doctor 0 anomalie,
sonda seatbelt 13/13 negati con 0 `unknown`.

Benchmark: resta autorevole quello del 2026-09-20 (DeepSeek v4-pro-0813 vincitore,
Qwen3-coder-next fallback). La corsa del 22/09 è stata fermata a 3 task su 20: nessuna
decisione di questo rilascio dipendeva dal suo esito.

## 2.4.0 WIP — pipeline operativa e learning verificato — 2026-09-22

Recuperati i worker DeepSeek/Qwen con interventi MID Terra dopo failure documentati.
Selezione dei modelli per ruolo indipendente dal main host; T2 definito usa MID,
T3/HIGH richiede capacità superiori. I modelli selezionati arrivano ai CLI reali.
Aggiunti ledger SQLite host-owned, lesson con check completi, report filtrabili,
storico scoped, context handoff e controlli budget. Costi mancanti restano null.

Prova offensiva OpenCode: lettura di `.env` sintetico e symlink esterno consentita
nonostante i deny. Runtime escluso fail-closed; Claude Code file-tools è il default
open. Codex CLI open resta escluso. Nessuna modifica al delegate o Security Gate.
Hook Stop compatibile verificato e coperto da regressione. Benchmark live su task
storico reale, senza promozione del vincitore e senza considerare i timeout successi.
Risultati, test finali e limiti nel verbale OPERATIONAL-VALIDATION.md. Nessun merge,
nessuna nuova release stabile; versione conservata a 2.4.0 WIP.


Review Fable mirata completata: finding corretti, PASS sull’ultima correzione.
Verifica finale: 452 test Python (4 skip) e 22 Node per edizione, Security Gate verde.

## 2.4.0 WIP — runtime indipendente — 2026-09-22

Il catalogo modelli registra provider, runtime compatibili, classe di costo,
capacità, contesto, prezzi e disponibilità delle evidenze. Il router sceglie il
modello meno costoso sufficiente al ruolo e al budget. La scala esecutiva usa
primary economico, fallback economico, MID configurato e premium.

`codex-cli` è escluso dall'esecuzione open perché la prova negativa del layer di
regole restrittivo non ha bloccato un comando vietato. Codex resta host e reviewer
premium. Il benchmark richiede un worker concluso con successo oltre ai test verdi.

## Snapshot WIP sospeso — 2026-09-22

Salvato il recupero sul branch di sviluppo, senza rilascio o merge in main.
Passaggio a Claude: `docs/verifiche/premium-plan-open-execute/HANDOFF-2026-09-22.md`.
Ultima suite completa precedente: 283 Python e 22 Node pass. Stato finale:
11 test Open Executor pass; benchmark 33 pass e una regressione intenzionalmente
rossa sul timeout. Prova live: policy Codex tramite symlink non applicata al
comando vietato; rilievo HIGH aperto. Review premium e benchmark incompleti.
Gli adapter nativi restano sperimentali e non approvati per uso operativo.

## 2.4.0 — pipeline premium plan / open execute (in verifica) — 2026-09-21

T2/T3 separano planner premium read-only, executor e fixer open, verifica
meccanica e reviewer premium della famiglia opposta. Il router conserva i modelli
del benchmark e concede l'esecuzione premium dopo esaurimento dei tentativi open,
o per le eccezioni già previste da rischio, capacità e override.

Aggiunti stato della pipeline, contratti JSON, arbitrato dei finding con evidenze,
telemetria per ruolo e scenari sintetici. Guardie del worker, Security Gate e
installazione Verify Agent invariati. Ripristinata l'approvazione CRITICAL anche
per classificazioni in minuscolo. Rilascio non concluso: verifiche e verbale in
`docs/verifiche/premium-plan-open-execute/`; review Claude indisponibile per quota.
Ripristino: revert dei soli commit di questo intervento, quando pubblicati.

Estensione del 22 settembre: Open Executor separa host, runtime, provider e modello.
Adapter Codex CLI e Claude Code con provider open; OpenCode resta opzionale.
Codex usa sandbox e denylist native; Claude open usa solo file tools perché il
wrapper Bash non è compatibile con il profilo restrittivo provato. Il delegate
aggiunge due punti di adattamento conservando i controlli di proprietà e integrità.
Benchmark per runtime/provider/modello, telemetria per ruolo e test dei due host.
La verifica indipendente premium resta bloccata dalla quota; nessuna promozione
stabile né cambio del benchmark winner da questo lavoro in verifica.



## Hotfix permessi Codex — 2026-09-21

Il bridge premium eredita sandbox e approvazioni dalla configurazione Codex invece
di forzare `workspace-write`, che in alcuni ambienti disabilitava la rete. Nessun
bypass aggiunto e nessuna modifica alla configurazione utente o al comando Claude.
Verificati test di regressione passati, controllo di sicurezza senza rilievi.
Ripristino: revert del commit.

## 2.3.0-public — 2026-09-21

Ingresso OpenCode AOS con routing per turno, bridge premium Claude/Codex, skill e
MCP dalle fonti esistenti, installer reversibile e profilo VS Code. Verificati
231 test Python, 19 Node ed E2E interattivi. Rilievi della review Claude corretti;
revisione indipendente finale incompleta per quota esaurita. Il proprietario ha
autorizzato esplicitamente il rilascio con questo limite.


## 2.2.1-public — 2026-09-21

**Context budget per ogni famiglia di modello.** Aggiunte a `context_policy.model_classes`
le policy per i modelli premium che prima cadevano sui default: `claude` (technical
1M, target 120k/soft 200k/hard 320k), `codex` e `gpt` (technical 400k, target
100k/soft 160k/hard 250k). `codex` e `gpt` sono la stessa famiglia (Codex gira su
GPT): due chiavi per coprire entrambi i nomi. Ogni modello che AOS instrada —
DeepSeek, Qwen, Claude e Codex — ha ora un budget dedicato, non il fallback.

## 2.2.0-public — 2026-09-21

**Context budget per modello.** AOS non riempie più la finestra di contesto fino al
limite tecnico del modello: un context manager nuovo, configurabile e
provider/model-aware classeifica GREEN/YELLOW/ORANGE/RED e decide quando compattare
o passare il testimone. Nessuna modifica a routing, benchmark winner, escalation o
security policy.

- **`bin/aos-context.py` (nuovo)** — policy (technical/target/soft/hard), lettura o
  stima del contesto (`measured`/`estimated`, mai confusi), classificazione, azione
  raccomandata, compaction strutturale e handoff. Il limite tecnico è una soglia
  riportata nei ratio, mai un target operativo.
- **`config/open-models.json`** — nuova sezione `context_policy` con
  `defaults`, `model_classes` (deepseek, qwen) e `models` (i due open). Fallback
  model → model_class → default, con `model_context_policy_source` registrato.
- **Compaction strutturale** — rimuove solo ruoli espliciti (log risolti, output
  verbosi, ipotesi superate, traceback spiegati, snapshot obsoleti, …); un ruolo
  sconosciuto è sempre conservato: la riduzione del contesto non tocca mai
  acceptance criteria, finding di sicurezza aperti, vincoli o decisioni di sicurezza.
- **Handoff** — `build_handoff` produce lo stato strutturato per nuova sessione,
  subtask o modello (task, acceptance, decisioni, file, test, issue aperte,
  vincoli, retry, executor, finding, escalation pendente).
- **Telemetria** — `aos-measure.py` accetta `--context-budget` (file JSON) e lo
  registra come campo annidato; il manager espone `context_tokens`,
  `context_tokens_source`, stato, ratio di utilizzo e token prima/dopo compaction.
- **Documentazione** — `references/context-budget.md`; riferimenti in SKILL.md e
  `references/orchestration.md`.

Test: 174 → 190 passati (16 context + 1 measure), 3 skip.

## 2.1.2-public — 2026-09-21

**Manutenzione mirata dopo il test end-to-end.** Tre correzioni, nessuna modifica a
routing, benchmark winner, provider open, verify-agent, escalation, permission model,
denylist, telemetria o policy di sicurezza.

- **Version drift** — `SKILL.md` `metadata.version` era fermo a `1.22.1` mentre
  `VERSION` era a `2.1.1`: due numeri che dichiaravano entrambi la versione AOS.
  Allineati a `2.1.2`. Il doctor ora avvisa (`AVVISO VERSIONE`) quando i due
  divergono, così il drift non può ripresentarsi in silenzio.
- **Retry su repo dirty** — `aos-delegate.py` distingue ora il repo sporco *prima*
  del task (rifiutato, exit 3, modifiche utente preservate) dal repo sporco *a causa*
  del worker corrente. Con `--state-file` (stesso path su entrambi i tentativi) il
  delegate registra HEAD iniziale e i path posseduti dal task; il retry è consentito
  solo se HEAD è invariato e ogni file dirty è attribuibile al worker. Modifiche
  esterne o commit esterni → conflitto (exit 6), mai overwrite/stash/reset. Nuovi
  campi telemetria: `initial_repo_clean`, `dirty_owned_by_current_run`,
  `dirty_conflict_detected`, `retry_dirty_policy`.
- **zeroDataRetention** — era già configurato a livello di modello
  (`models.<model>.options.zeroDataRetention: true`) nella config utente OpenCode; il
  pre-check del test lo cercava al livello sbagliato (provider). Il doctor ora legge
  il livello corretto e riporta `configured`/`not_configured`/`unknown` per ogni
  modello open; `verified` (lato provider) e `unsupported` (schema senza la chiave)
  non vengono mai inventati da un check locale in sola lettura.

Test: 174 passati, 3 skip (erano 166/3); aggiunti 5 test delegate sul retry e 3 test
doctor su version drift e provider privacy.

## 2.1.1-public — 2026-09-21

**AOS è il router anche nell'edizione pubblica.** Portata ad AOS Plus la modifica 2.1.0
del repository privato: il modello manuale della sessione OpenCode non decide più
l'esecutore — è un fallback runtime che esegue solo su override esplicito o dove la
policy riserva premium (pianificazione T3, arbitrato, report finale, HIGH/CRITICAL). Il
router sceglie l'esecutore da tier, rischio, complessità, incertezza, impatto di
sicurezza, capability richieste, vincitore benchmark configurato e storico di retry.

- **`bin/aos-router.py` (nuovo)** — decisione pura `decide()`: T0/T1 e T2 LOW/MEDIUM →
  esecutore open (vincitore benchmark); T2/HIGH → open solo con observable check e
  review premium obbligatoria; T3 → premium per pianificazione/review, open sui
  sottotask; CRITICAL intatto (main + approvazione); retry → fallback open → escalation
  premium; override esplicito → main. Config assente/invalida → comportamento legacy.
- **`config/open-models.json` (nuovo)** — policy esecutiva: `open.primary` DeepSeek v4
  pro (vincitore benchmark), `open.fallback` Qwen, `premium.reviewer=claude`,
  `premium.escalation_executor=codex`. Nomi in config, mai nel codice.
- **`bin/aos-measure.py` (esteso)** — telemetria di routing: executor reale
  (`main_executor_runtime/model/provider`, `routed_by_aos`, `manual_model_override`),
  token open/premium, escalation e ratio (`workload_open_ratio`,
  `premium_dependency_ratio`).
- **Premium sugli abbonamenti già pagati** — l'esecutore e il reviewer premium sono le
  sessioni Claude Code e Codex CLI coperte dagli abbonamenti attivi, mai una chiave API
  a consumo separata; l'open è l'unico percorso a consumo e il default per il lavoro
  eleggibile. Dichiarato in `premium.billing` e in `references/orchestration.md`.
- **Test** — `tests/test_router.py` (16 casi) porta la suite dell'edizione pubblica a
  166 passati (3 skip), come il repository privato.
- **Non toccato** — permission manager, denylist, guardie anti-injection, verify-agent,
  cross-model review, policy HIGH/CRITICAL: routing, non privilegi.

## 2.0.1-public (second review cycle) — 2026-09-21

The user asked for a new check by both reviewers, Codex and Claude, in parallel:
nineteen rounds, 40 MAJOR accepted, each with a test and, where semantics were in
doubt, a measure. What changed since the first 2.0.1 entry:

- The run's config is a **whitelist** copy of the user's config (providers and a
  reworked permission map, `share: disabled`, the run's model as `small_model` too —
  OpenCode titled every session with a model the user never chose), served as the
  only layer from a temporary XDG root that links the rest of `~/.config` in; every
  `OPENCODE_*` config variable is dropped; a repo with its own OpenCode config is
  refused. Measured with `opencode debug config` and real runs: a global wildcard,
  a repo config, an agent-level permission and a `tools` map each reopened the guards
  before.
- The permission map is rebuilt as OpenCode reads it: later rule wins, at every
  level, every key that names `bash` by glob or `{env:}`, user patterns for denied
  commands removed, ours last.
- The record's own `git` reads no global or system config and never runs on a repo
  whose `.git` config, attributes, hooks, includes or pointers the worker changed
  (reproduced: a `core.fsmonitor` written by the worker ran in the record's git);
  the bench takes the same fingerprint around its test command, clones
  `node_modules` instead of linking it, and stops on tampering — on the retry, on
  resume and on an exception mid-run.
- `diff_stat` lists staged and untracked files from the toplevel, unfolded,
  uncolored, with HEAD before and after.

Closed by the user's choice after round 24 with reserves: the class "file written
by the worker, executed by our git" has no closure by fingerprint — the real one is
a process sandbox, kept as backlog. Suite 153 OK.

## 2.0.1-public — 2026-09-21

Audit of the open runtime before adoption, done by running it: four OpenCode runs on a
throwaway repo. With `--auto` and a per-run config that denied only skills, the worker
ran `curl`, `git commit` and read a secrets file with `cat`. `run_config` now denies
`external_directory`, `webfetch`, `websearch`, `task` and `DENIED_BASH` (network
clients, remote shells, publishing, deploy CLIs, `sudo`). Three measured facts shape
the rule: a denied pattern refuses the call even inside `a && curl …`; on the same
entry the later rule wins; a trailing `"*": "allow"` cancels every deny before it — so
our entries close both the `permission` map and the `bash` map, and a user's string
rule becomes the map's first `"*"` entry. A guard, not a sandbox: an interpreter reads
what `cat` may not, and the routing rule says so.

`diff_stat` now lists untracked files (a worker that only created a module reported
no change) and the brief follows `--` (a brief starting with `-` was an option).

Cross-model review (Codex, 5 rounds): 4 MAJOR and 1 MINOR accepted — the string
`bash: "deny"` lost, `sftp`/`ftp` then other clients missing, the permission-level
wildcard — PASS at round 5. Suite 143 OK.

## 2.0.0-public — 2026-09-21

**Open runtime.** OpenCode as a third host (it already reads the skills and the
`CLAUDE.md` files) and `bin/aos-delegate.py` for bounded T0/T1 work with an observable
check: one `opencode run --pure --auto --format json` in the repo, usage summed from
the `step_finish` events or null, a per-run config that denies every skill (42,621 →
7,260 input tokens per step, measured), cost and step caps on the stream, `PWD` and
`--dir` naming the repo because OpenCode resolves its directory from `PWD`, not the
process cwd. Routing rule in `references/orchestration.md` §Model routing; model names
stay in the user's instructions.

**Benchmark harness.** `bin/aos-bench.py` replays real commits in throwaway worktrees
against each model: the commit's test files are checked out before the run and before
every test, one retry with the failure output, a Codex reviewer with a JSON schema,
untracked files staged before diff and review, records with diff and finding titles,
`--check` to prove every task (fails on the parent, passes on the commit). The
maintainer's first run: three defects in the harness before any model was measured,
then 20 tasks × 3 runners; the winner is written in the maintainer's instructions,
not here.

`aos-profile.sh` reports OpenCode. Cross-model review, six rounds: 15 findings, all
accepted, eight of them born from earlier fixes in two functions — the delegate's
`invoke()` (kill the whole process group, always) and the bench's `run_task()` (the
spend cap between attempts and on the known part; untracked files staged with `-z`;
the work read against `HEAD` without the restored tests). 45 new tests.

## 1.22.1-public — 2026-09-19

**Prose routing.** SKILL.md §4, row "Prose a person will read": a structure skill runs
**before** drafting, `humanizer` after. StoryScope (arXiv 2604.03136) measures why: with
every stylistic feature removed, narrative choices alone separate human from AI fiction
at 93.2% macro-F1, and surface editing leaves that at 93.9%. A gate that only runs after
the draft cannot see what was decided before the first sentence. The structure skill
named in the row is the maintainer's; any equivalent fits.

## 1.22.0-public — 2026-09-15

**Model routing.** SKILL.md §3 and `references/orchestration.md` §Model routing: the main
session runs on the strongest model the user configured and keeps classification, T2/T3
work, verification, arbitration, the final report and anything HIGH/CRITICAL; bounded
T0/T1 work at risk ≤ MEDIUM may go to a subagent on the host's working model (Claude
`Agent` with `model`; Codex `spawn_agent` or `[agents].default_subagent_model`). When the
main session is on a weaker model and the task is T2+ or HIGH+, AOS says so and asks for
the switch before the first change. Model names never live in AOS: they belong in the
user's global instructions. The cross-model reviewer is the strongest model of the other
family; SKILL.md no longer names a specific Claude model for it.

No new tests: these are instructions. Checks: 98 tests green on 3.12 and 3.9.

## 1.21.0-public — 2026-09-15

**A verdict nobody asks for.** `judge` existed since 1.18.0 and had never been called:
no record ever carried the user's verdict: the early ones said `accepted` in the author's
hand, the later ones stayed `delivered`, which is also the author's word. Now
`aos-measure.py start` prints the sibling records that were finished as
`delivered`/`partial` and never judged, one complete `judge` command each (relative to the
cwd when possible); JSON files that are not records, including a shaped one whose
`finished_at` is not a string, are skipped, not reported. SKILL.md adds one line: when the notice
appears, ask the user for that line and record it. Three tests.

**Where self-audits stop.** This edition's source went through five audit-and-fix cycles in
one day, and the last ones found bookkeeping of earlier fixes rather than defects. The next
audit of AOS on itself waits for use: at least five measured T2 records on real projects.

Checks: `python3 -m unittest discover tests` green on 3.12 and 3.9 (98 tests); the
cross-model gate ran on the reserve model, so the verdict is verified with reservations.

## 1.20.0-public — 2026-09-15

**Backups nobody weighs.** One install had 8.3 GB in `~/.agents/backups`: eight copies of
the same clone, one per install of the day, each carrying the same 936 MB of `tmp/`. The
doctor warns (`AVVISO BACKUP`) above 1 GB in either host's backup directory, which sits
outside the root where the `tmp/` check could not see it; the profile repeats it.
Deletion stays the user's.

**`DISTRIBUTION` compares files, not only the version.** After the checkout path, the
file may list the files the two editions keep byte-identical; the doctor compares them
and warns on the one that differs. A version match alone would not have seen a port that
changed the number and not the code.

**The open measurement record.** `quality-gates.md` §3 gains a fourth precondition: the
brief states once that the record is open and closes with `finish` before the commit, so
a reviewer does not file `outcome: null` as a MAJOR every round.

Tests: 95 on Python 3.12 and 3.9 (3 skipped for `tomllib`).

## 1.19.0-public — 2026-09-15

Two warnings the doctor did not have, both from things nobody counted.

**A derived edition left behind.** "Same intervention" had no check, and one release of
the source never reached this edition. The doctor reads an optional `DISTRIBUTION` file
naming a checkout and warns (`AVVISO DISTRIBUZIONE`, exit 0) when that checkout's
`VERSION` differs or the checkout is missing; `aos-profile.sh` repeats the warning at
the start of work. This edition ships no such file.

**`tmp/` nobody weighs.** Ignored by Git, it held 936 MB of research on one install,
found only because a backup copied it whole. `AVVISO TMP` above 200 MB, from the doctor
and the profile.

Tests: 93 on Python 3.12 and 3.9 (3 skipped for `tomllib`).

## 1.18.0-public — 2026-09-15

The six findings of the second audit, the one written at the end of 1.17.0. Four are
code with a test that reproduces the defect; two are declared without a remedy, with the
reason.

**Two copies were the defect, not their drift.** The Codex installation had stayed on
1.14.0 with ten dirty files while the installer copied files and the doctor compared
hashes. The whole apparatus — `HASH`, manifests compared across hosts, "a commit is not
an install" — policed a duplication that `~/.agents/skills` already avoids for every
other shared skill: with a link. `~/.agents/skills/aos` is now a symlink to
`~/.claude/skills/aos`, created or repaired by `aos-install.sh --host codex --link`,
which backs up a real directory found there and replaces it; `--from` is refused for
Codex and `--uninstall` removes only the link, refusing a real directory; a stray regular file on that path is moved to the backup directory and replaced by the link. The doctor checks the link and the files of
one root: a real directory on the Codex path is `COPIA`, identical or not, because it is
a Codex reading an older AOS without either session seeing it. `aos-profile.sh` says so
in one line. Tests: `test_install.py` (link, backup of the copy, `--from` refused,
`--link` Codex-only, uninstall that leaves the installation), `test_doctor.py` (`COPIA` on
an identical copy, missing or misdirected link).

**The catalog refresh had no way to start.** The `skills/list` snapshot had to be
produced by hand, which is why one measured install stayed eighty skills behind.
`skill-library.py refresh --discover` starts `codex app-server`, sends `initialize`,
`initialized` and `skills/list` with `forceReload` and `includeDisabled`, saves the
snapshot under `tmp/` and continues as before; a ready snapshot is still accepted as the
argument, exactly one of the two. Tested with a fake `codex` on PATH answering the same
JSON-RPC, no network; a server that stays open and silent is abandoned at the timeout, and an error on either request fails the discovery.

**The user's verdict was written by the author.** Every record measured so far said
`accepted`, written minutes before the user had read the result. `finish --outcome` is
now `delivered | partial | blocked`, what was handed over; `judge --verdict
accepted|rejected` runs later, once, and keeps in `delivered_as` what `finish` had
written. A `blocked` task has nothing to judge.

**Instructions have no tests** and will not get them: a test that looks for words proves
the words are there. The scenarios remain, to be walked on every change to `SKILL.md`;
this one touches only the measurement sentence in §5, which none of the three scenarios
crosses.

**Fixed cost**: `SKILL.md` is at 1,990 words and is the only file loaded every session.
No cut: the measure that matters is per task, in `docs/misure/`.

Also ported from the private 1.17.0 that this edition had skipped: `tests/` joined the
installer manifest and a test checks that every `tests/test_*.py` is listed; a stale
catalog row is a warning, not a failed doctor; the installer removes from the target
what left the manifest, orphan tests included; `aos-security.sh` never reports its own
lines and scans a subdirectory with paths it can open (`--relative`); `aos-measure.py
finish` says when no work is observed after `start` (`work_observed_after_start`), names
a stale lock and the remedy, and parses `git status -z` as records so a rename or an
arrow inside a file name is read correctly; the security scan follows risk, not tier
(`SKILL.md` §5, `risk-and-tiers.md`); the learning line extends to T1 with destinations
per host (`output-contract.md`) and a Definition of Done box; the profiler lists `docs/`
as documentation and does not read `supabase` inside a test tree as a stack;
`bin/codex-hook-adapter.py` and its test are gone, no hook used them.

Checks: 91 tests on Python 3.12, 91 on 3.9 (3 skipped for `tomllib`), `bash -n`,
`py_compile`, shellcheck, the mechanical security pass, and the public-sanity scan.

## 1.16.0-public — 2026-09-15

**AOS did not measure itself, and it had already said so.** An earlier revision closed
by naming its own next investment: measure a sample of real completed tasks. The tool to
do it, `bin/aos-measure.py`, already existed and was complete — locking, a versioned
schema, mandatory provenance on any provider counter, a record that cannot be completed
twice. The only instruction that named it lived in `references/token-efficiency.md`, a
file loaded **only on request**, and read "use only for a requested comparison". Records
produced in the meantime: **zero**. The tool was never the missing part. The trigger was.

Measurement now lives in the core (`SKILL.md` §5) and applies to **every T2/T3 task**,
not only to a requested comparison. The record goes to `docs/misure/` and is committed
with the work, for the same reason review logs are: an ignored directory is where
evidence goes to disappear. The Definition of Done in `quality-gates.md` checks it.

`--outcome` gains **`blocked`**. Work stopped by a missing capability, an exhausted quota
or a declared gate produced nothing to accept in part; filing it as `partial` was the
only option available and it made the record lie. `start` now also creates the record's
directory — `docs/misure/` will not exist the first time, and that must not become the
convenient excuse for closing without a measurement.

Same defect, same remedy in `output-contract.md`: the learning step said "after
**significant** work". Significance is judged by the author, about their own work, at the
moment they most want to be finished. It is now unconditional at T2/T3 and admits an
explicit empty result — an outcome, not a skipped step. The same trap is documented one
ecosystem over: gstack's skill instructions carry issue #2402, where 43 of 44 learnings
arrived only from an explicit command because "if you discovered" read as optional.

**Drift between the two host copies stops being invisible.** `aos-doctor.py` has always
compared the Claude and Codex installations file by file, but it only ran when somebody
thought to ask — and somebody who has just edited one host does not think to ask. Now
`aos-profile.sh`, which runs at the start of the work anyway, prints the loaded version
and the result of that comparison, reusing aos-doctor rather than inventing a weaker
check. It reports only the drift codes: catalog anomalies are a separate, known condition,
and printing forty of them there would bury the one line that matters.

**The design gate believed a false success.** The viewport rule said "when the driven
browser cannot resize below its own width". Measured: `resize_window` answers
`Successfully resized window ... to 375x812` while `window.innerWidth` stays **1920**;
repeated at 600×800 after a three-second wait, same declared success, same 1920. The
danger is not a resize that refuses, it is one that says yes and does nothing — a
responsive check would have reported "verified at 375px" having measured the desktop.
The rule now requires reading `window.innerWidth` back and comparing it to the target; if
it diverges, the check did not run.

**A deploy claim was corrected.** The profile said a git push does not deploy. With a
Git integration active, every push to the production branch goes live on its own —
observed on a Next.js project whose deployments carried the `…-git-main-…` alias with no
`vercel` command ever issued. An instruction that denies a release is worse than no
instruction: it gets intermediate states committed to `main` in the belief that they stay
local.

Checks: 61 tests (5 new, one per introduced behaviour), `bash -n`, `py_compile`, the
mechanical security pass, and both host installations confirmed identical by
`aos-doctor.py`.

**Two limits found by using it once**, written down here rather than discovered in six
months. The first real record's `elapsed_seconds` does not cover the work: the rule
requiring it was written during that work, so `start` ran after the implementation was
finished. That circumstance will not repeat, but nothing in the code forces `start` to
actually sit at the beginning. The second matters more: **the outcome is written by the
author before the user has spoken.** `accepted` there means "delivered without
rejection", not "accepted", and the record is immutable by construction — it cannot be
completed twice. As long as the author closes that axis, it measures their own opinion of
their own work, which is the judgement this release set out to replace. The remedy is not
in this version: either the record stays open until the user's next turn, or the outcome
is written when the user answers.

## 1.15.1-public — 2026-09-15

**A spent reviewer account is not a finished review.** Until now a quota running out
mid-gate simply ended the verification: nothing distinguished "the reviewer found
nothing" from "the reviewer's account is empty", and both arrived as a failed round.
The round is now retried once on a reserve model when the provider's own usage-limit
wording is present — only on that wording, because retrying a crashed or silent round on
a weaker model is how a broken round turns into a weak PASS.

The weighting is asymmetric, and that is the part worth keeping:

- a **finding** from the reserve model counts in full, because the arbiter confirms every
  finding mechanically anyway, so who found it does not matter;
- a **PASS** from it does not close the gate, because the absence of findings is exactly
  what depends on the strength of whoever looked.

A gate whose only PASS came from the reserve is VERIFICATO CON RISERVE, never VERIFICATO,
and the verdict names the model. The earlier rounds are not re-run on the reserve to
reach convergence: that manufactures a PASS.


## 1.15.0-public — 2026-09-15

An audit across four axes — design, token cost, context organization, orchestration —
with the measurement kept next to each finding. The first two corrections come from
running the design gate against a real product rather than from reasoning about it.

**Design.** The §5 gate never said to check **both themes**. On the first product it was
run against, all five contrast failures found existed **only in the light theme**, which
had never once been measured. A target also has **two axes**: a min-height utility passed
a height-only check while the element was 11px wide, twice, on two different elements.
The 16px input rule now says `any-pointer`, not `pointer` — `pointer` describes the
primary pointer, so a tablet with a keyboard attached reports fine and keeps the small
text it is being typed into with a thumb.

**New section: how to measure.** A measurement that finds nothing and a measurement that
*cannot* find anything produce the same zero. It is the empty-round trap from
`quality-gates.md` §3 one floor down, and it is answered the same way: plant the defect
the probe is meant to catch, confirm it screams, then remove it. Four traps documented
with their countermeasure — an element's rect is not its touch target, computed colours
come back in `oklch`/`lab` rather than `rgb()`, a rule nested in `@layer` escapes a
non-recursive CSSOM walk, and the tool's viewport is not the user's. When the driven
browser cannot resize below its own width or apply page zoom, the 375px and 200% boxes
are not ticked: the substitution is declared.

**Ponytail** was one routing line behind a trigger nobody fires against themselves — "the
solution looks larger than the problem" — and absent from the file about spending less.
It is now framed with the other two savers, because they work on three different
surfaces: Caveman shortens what goes to the user, RTK what the tools send back, ponytail
what lands in the repository. Only the third saves anything permanently. Its trigger is
mechanical now: before building at T2/T3, and whenever the answer adds a dependency, an
abstraction or a configuration option.

**Stale catalog.** A catalog hit is a claim about a path, not proof the skill is there:
one measured install had 20 of 399 entries pointing into a plugin cache that no longer
existed, while every one of those skills was installed and reachable elsewhere. A dead
path means the index is old — never that the capability is missing. The index is not
repaired by hand: it is generated from a runtime snapshot, and fixing rows manually
leaves it diverging from the next regeneration. Regenerate, or leave it stale and say so.

**A commit is not an install.** Editing one host's copy leaves the other on the old
version, and the drift is invisible from inside either session. After changing AOS,
install for the other host in the same intervention and confirm with `aos-doctor.py`.

**Review logs, and how to count them.** The gate said to move `BRIEF.md` and
`REVIEW-LOG.md` out of `tmp/`, and nothing checked. Counting what sits under `tmp/` turned
out to be the wrong count — the working copy stays next to the one that was preserved. Of
73 directories across four repositories, **71 were already saved and 2 were not**. The
Definition of Done box now compares the slugs instead of checking for emptiness.

**An update is an install.** §7 checked a capability once, at install, and a skill that
updated afterwards shipped new scripts under the trust the old ones earned. Now: not a
re-audit every time, but a checksum list compared against the copy already checked, which
reopens the question only when the executables changed.

**CTO role** added to the multi-role table with its limit written down: T3 only, or when
a choice locks in a dependency, a vendor or a data model. Its output is the single
next-investment advisory, not a fourth opinion on the diff.


## 1.14.0-public — 2026-09-15

**Reviewed across four adversarial rounds before this line was written.** Ten findings,
ten confirmed mechanically, none refuted. Three of them were contradictions inside the
new file itself, and five more were inside the fixes for the first three — which is
where the worst ones always are. What survives is listed below; what it got wrong is
listed with it, because a gate that hides its own corrections teaches nothing.

- The gate first demanded a direction written **before the first component**. No
  existing surface can satisfy that, which would have made the role unusable on
  everything already built. Adopting it on existing work now means writing down the
  direction the shipped result already implies, and naming what departs from it.
- The default direction allowed a near-black canvas and then prescribed near-black
  numerals — 1.06:1 against its own 3:1 floor. Foreground is now stated relative to
  the canvas.
- Verification said "in a browser", which a native screen cannot satisfy; a web
  replica verifies the replica. The medium now picks the check: browser, simulator,
  or the export at final size. That fix then had to be made three times, because it
  was written into two files and not into the checklist that T2/T3 work is measured
  against.
- The gate demanded one accent with no exception while the same file told you to adopt
  an existing brand's palette. A two-accent brand could satisfy the direction or the
  gate, never both. Both counts are now relative to the direction — and the reviewer
  then attacked that relativization and could not empty the gate: contrast, focus and
  reduced-motion stay absolute whatever a direction declares. An unexplained second
  accent still fails, and semantic colours never counted as accents to begin with.

### What the role is

A pile of design skills was installed and none of them said *when* it enters a job,
or what makes the result acceptable. `references/design.md` adds the missing role.

- **The UI/UX role** owns the direction and the acceptance bar, not the pixels of
  every commit. It asks the one question the other roles do not: would someone who
  designs for a living read this as designed, or as assembled? The gap between the
  two is rarely talent — it is that nobody wrote the direction down before the first
  component existed, so every later decision was taken locally and the sum is generic.
- **Seven stages, each with an artifact someone else can read**: plan review,
  direction, tokens, library choice, variants, motion spec, review. A stage with no
  artifact did not run; "I kept it in mind" is not an output. This is the ICM stage
  contract applied to design, with the installed skills mapped to the stage where they
  help rather than listed together.
- **Four of those skills declare `disable-model-invocation: true`** in their
  frontmatter — an agent cannot invoke them, only the user can. Suggest them and say
  so; reporting a stage as complete through a skill that never ran is a false claim.
  Read the frontmatter of anything you plan to route to before promising it.
- **The gate is checked against the rendered result where it runs, not the source.**
  Contrast ratios, 44px touch targets, ≥16px text in any field the user types into
  (below that mobile Safari zooms the page on focus), `prefers-reduced-motion`,
  visible focus, the viewports or OS text sizes for the medium, and the
  empty/loading/error states. Reading the CSS tells you what you wrote, not what shipped.
- **Costs are named from the line that reads the key, not from the line that mentions
  it.** One design skill reads four separate image-generation API keys — `os.environ.get`
  in its generator scripts, an operational read. The image-direction skills name no
  backend of their own and will use whatever the host provides. So no asset-generating
  stage runs unattended, and none runs in a loop: the first unattended run spends real
  money. A fifth key was claimed here in the first draft and the reviewer removed it —
  its only occurrences were in a test that *unsets* the variable, and the script that
  would read it is not shipped. A grep hit is not a read.
- Designer row in `references/quality-gates.md` §4, next to UX/Product, which asks a
  different question, plus the matching DoD box.

## 1.13.0-public — 2026-09-15

- Routing gains three rows: reach for an anti-over-engineering pass when the solution
  looks larger than the problem, ask a documentation service for library and framework
  APIs instead of trusting a remembered signature, and run prose through an AI-tell
  pass before publishing. Discovery of everything else stays with `skill-library`.
- **Installed capability is executable configuration** (SKILL.md §3, detail in
  `references/quality-gates.md` §7). A skill, hook or MCP server added from outside is
  not documentation: it ships scripts, may require paid API keys and may send data off
  the machine, with the agent's own permissions. The check has four points — what it
  executes, what it asks for, where it sends, what it can reach — and comes from
  measuring a real install: 213 `.mjs`, 42 `.py` and 2 `.sh` inside folders that read
  as plain Markdown, eleven distinct API keys expected across image, music and speech
  generation, a deploy helper that uploads the project directory to a third-party
  endpoint, and analytics that post to a third-party product-analytics host with the
  project key embedded in the skill. The check says to follow symlinks: an installer
  that writes one copy and links the rest makes an unqualified `find` report zero
  scripts on the path the agent actually loads.
- The guard was then taken apart twice by a reviewer and rebuilt: it reads the staged
  blob rather than the working tree, handles git-quoted filenames (`-z`), includes
  type changes in the filter, checks the commit message from a `commit-msg` hook
  instead of reading the previous one from `pre-commit`, blanks only its own exact
  pattern assignment rather than every similar line, and exits non-zero when it cannot
  read the index — before, a scan that failed printed a clean result.
- A pre-commit check now refuses to publish content that names the maintainer, their
  machine or their clients: this distribution is assembled by copying files out of a
  private repository, and that copy is where sanitization gets skipped. Enable it in a
  fresh clone with `git config core.hooksPath .githooks`. The private terms live in an
  ignored `.public-sanity-terms`, never in the script — a guard that lists the names it
  hides publishes them itself, which is what its first version did.
- Compaction is chosen, not suffered (§3): compact at a breakpoint you pick, not
  mid-implementation and not at the automatic threshold, where the paths and partial
  state still in play are exactly what gets dropped.


## 1.12.2-public — 2026-09-15

- `aos-profile.sh`: the Python minimum bar no longer disappears just because the project
  declares some test command. `make test`, `npm run test` or a loose script dropped the
  `py_compile` guidance even when none of them exercises the Python in the project. The
  condition is now the absence of a *Python* runner; when a command exists but is not
  proven to cover the Python, the profile says so instead of going quiet.
- `aos-profile.sh`: recognizing an n8n workflow file says nothing about whether that
  workflow is active on the instance. The line asserted HIGH from detection alone; it now
  reports what it recognized and makes the consequence conditional on activation. The risk
  signal stays.
- `evals/scenarios.json` is installed and verified (`REQUIRED_FILES`, 22 files): the README
  promised it while the installer never shipped it. `aos-doctor.py` checked backticked
  paths only under `references/`, `bin/` and `catalog/`, so it could not see the broken
  promise; `evals/` is now covered, with a test.
- `references/quality-gates.md`: reviewer capability is back among the gate preconditions.
  A reviewer counts only if it is at least comparable to the model that produced the work.
- A `[pytest]` section, or pytest among the dependencies, still removed the minimum bar:
  `profile-python.py` printed a pytest command from configuration alone. It now states on
  a dedicated line whether the evidence comes from **test files** or only from
  configuration, and the bar stays up in the second case.
- The n8n line no longer presumes the effects either: a workflow of Manual Trigger and Set
  publishes nothing, so the text reports what was recognized and defers to reading the nodes.
- A found test file does not prove the proposed command runs it either: `addopts`,
  `testpaths`, `norecursedirs` and `collect_ignore` can exclude exactly what was found.
  When the pytest configuration narrows collection, the profile says so and keeps the
  minimum bar. This reads the options; it does not emulate collection.
- `collect_ignore` was named in that check but lives in `conftest.py`, which was never
  read; and the narrowing options were searched across the whole file, so an unrelated
  section with its own `testpaths` produced a false warning. Both fixed: conftest files
  are read for `collect_ignore`, and the options count only inside the pytest section.
- Those options are now read with real parsers instead of line patterns: `tomllib` for
  `pyproject.toml`, `configparser` for the ini files, with the previous scan kept as a
  fallback when a file cannot be parsed. A `[`-looking line inside a multiline TOML
  string is not a table, and `collect_ignore` written with a type annotation is still
  an exclusion.
- Two residual ways past that check: picking one candidate section per file meant an
  empty `[pytest]` could shadow the `[tool:pytest]` that setup.cfg actually uses, and
  the header regex gated the TOML parser, so a quoted table name was never parsed at
  all. Any candidate section now counts, and `pyproject.toml` goes to the parser first.
- Structural change, after five rounds each found another option missing from the list:
  pytest evidence no longer removes the minimum bar at all. What pytest collects depends
  on options this reader cannot enumerate — `python_files`, markers, a conftest, a plugin
  — and settling it needs pytest itself, which the reader never runs. Absence of a
  recognized restriction is not proof. Only a unittest discovery command, which names the
  files it will run, still counts as proven. The recognized restrictions now only change
  how the doubt is worded.
- Known and inherited: backticked reference checking cannot tell an illustrative path from a
  promise. This already applied to `references/`, `bin/` and `catalog/`; `evals/` joins the
  same class.
- Validation: 48 tests (were 43) OK under Python 3.13, OK with 2 skipped under Python 3.9.6.
  Six of the seven new tests fail against the code they were written for, checked before
  each fix; the seventh is a guard for behavior that already held.

## 1.12.1-public — 2026-09-15

- The two tests that call `refresh()` directly now skip, instead of erroring, when the running interpreter has no stdlib `tomllib`. The CLI path is unaffected: `ensure_refresh_runtime()` re-execs into an installed Python 3.11+. The 1.12.0 entry recorded "43 tests passed" without naming the interpreter.
- `test_process_channels` no longer inherits the runner's stdin. `codex-hook-adapter.py` reads stdin to EOF by design; the second subprocess call passed no `input=`, so the whole suite blocked whenever it was started with an open stdin. Reproduced deterministically: killed at the timeout with stdin open, 0.057s with stdin closed.
- README states the interpreter requirement as a skip condition rather than a hard prerequisite.
- ICM guidance unchanged; its documented limits were re-checked against arXiv 2603.16021v2 §4.6, §5.2 and §6, and the stage folder naming against upstream `_core/CONVENTIONS.md`.
- Validation: 43 tests OK under Python 3.13; 43 OK with 2 skipped under Python 3.9.6. `catalog/index.json` still ships empty; no workstation paths, inventories or review transcripts are included.

## 1.12.0-public — 2026-09-14

- Initial public distribution with fresh history, generic examples and no workstation inventory or internal audit records.
- Includes ICM context guidance, scope/risk classification, verification helpers and existing regression tests.
- Deployment hints defer to project tooling and environment; no assumed private infrastructure.
- Optional integrations remain separately installed and subject to their own instructions.
- Made the skill-search test independent of any workstation catalog using temporary fixtures.
- Validation: 43 tests passed, isolated two-host installation/doctor passed, skill and secret scans passed. Independent review unavailable; owner explicitly authorized public release as aos-plus.
- Published package name: aos-plus; installed skill name remains aos.
