## Findings

### [BLOCKER] La sonda certifica tentativi mai eseguiti

- Evidenza: [aos-isolation.py:133](~/.claude/skills/aos/bin/aos-isolation.py:133) determina `attempted` esclusivamente con `f'RESULT {key}:' in reply`. Alle righe 140 e 165, assenza di leak più questa stringa diventa `denied`, senza verificare chiamate agli strumenti. Alla [riga 207](~/.claude/skills/aos/bin/aos-isolation.py:207), `exit_code == 0` basta per `worker_ran`. Un worker che esegue soltanto i due controlli consentiti può quindi ottenere `isolation_verified=true` dichiarando rifiutati tutti gli altri bersagli. Questo invalida il criterio di ammissione dei runtime richiesto dal brief.
- Come verificarlo: chiamare `observe()` con i 15 bersagli del record `claude-code-seatbelt.json`, `tool_results=[]`, `exit_code=0` e una risposta contenente `RESULT <id>: REFUSED not attempted` per ogni ID. Simulare con `unittest.mock` soltanto i controlli riusciti e i file vietati invariati. **Riprodotto:** `isolated=True`, `unattempted=[]`; la successiva formula di `main()` produce `isolation_verified=True`.

### [BLOCKER] L’esecuzione di produzione può disattivare seatbelt e dichiararsi verificata

- Evidenza: [aos-open-executor.py:464](~/.claude/skills/aos/bin/aos-open-executor.py:464) preferisce l’argomento `os_isolation` alla policy; `"none"` è accettato anche senza `probe_root`. Il wrapping a riga 554 avviene soltanto per `"seatbelt"`. Tuttavia, alla [riga 571](~/.claude/skills/aos/bin/aos-open-executor.py:571), `isolation_verified` deriva dalla configurazione, senza confrontarla con l’isolamento effettivamente applicato. Il brief ammette OpenCode e Claude Code **soltanto sotto seatbelt**.
- Come verificarlo: importare il modulo; simulare `shutil.which`, `delegate.run` e `delegate.dirty_paths` per evitare qualsiasi worker. Invocare `run(repo, primary, "mock", 1, False, runtime="opencode", os_isolation="none")`, senza `probe_root`. Per `delegate.run` basta restituire `{"usage": {}, "reply": "", "git_meta_changed": False}`. **Riprodotto:** nessun rifiuto, `os_isolation="none"` e `isolation_verified=True`.

### [MAJOR] `probe_root="/"` aggira il blocco Codex su repository ordinari

- Evidenza: [aos-open-executor.py:456](~/.claude/skills/aos/bin/aos-open-executor.py:456) verifica soltanto che `probe_root` sia un antenato del repository. La presenza del parametro disabilita i blocchi in `resolve()` e viene trasmessa come `probe=True` a `check_runtime()`, riga 479. Non viene verificato che il repository appartenga a una fixture creata dalla sonda. `/` soddisfa il controllo per qualsiasi repository ordinario.
- Come verificarlo: `resolve(runtime="codex-cli", probe_root=Path("/"))` restituisce `codex-cli` nonostante la policy lo disabiliti. Per verificare anche `run()`, usare `/opt/production-repo`, `probe_root="/"` e simulare disponibilità binario, controllo versione, credenziali e `delegate.run`, senza avviare processi. **Riprodotto:** raggiunge il delegate e restituisce `runtime="codex-cli", os_isolation="none"`. La verifica del confine della fixture rimane quella reale, non simulata.

### [MAJOR] Un outcome libera anche le prenotazioni delle altre chiamate del task

- Evidenza: [aos-learning.py:449](~/.claude/skills/aos/bin/aos-learning.py:449) esegue:
  ```sql
  UPDATE reservations SET settled_at = ?
  WHERE task_id = ? AND settled_at IS NULL
  ```
  Ogni chiamata prenota separatamente, ma il primo outcome liquida **tutte** le prenotazioni del task. Inoltre, [aos-entry.py:613](~/.claude/skills/aos/bin/aos-entry.py:613) registra gli outcome degli executor uno alla volta, in transazioni separate. Tra due registrazioni, un’altra sessione può essere ammessa usando budget già impegnato.
- Come verificarlo: con SQLite condiviso in memoria e il vero `operations.budget_check`, impostare `daily_budget=1`; prenotare due volte `0.4` per `task-a`; registrare un solo outcome di `task-a` con costo `0.4`; prenotare `0.6` per `task-b`. **Riprodotto:** dopo il primo outcome restano **zero** hold aperti e `task-b` è ammesso. Contando anche la seconda chiamata di `task-a`, l’impegno è `1.4` contro un cap di `1`. `BEGIN IMMEDIATE` protegge l’ammissione, ma non corregge questa liquidazione eccessiva.

### [MAJOR] Modelli assenti dal catalogo provocano escalation premium invece del blocco

- Evidenza: in [aos-router.py:513](~/.claude/skills/aos/bin/aos-router.py:513), quando primary, fallback e MID risultano inutilizzabili, viene restituito `Decision(executor="premium")`. L’assenza dal catalogo viene assimilata all’indisponibilità, anche con zero tentativi falliti. Contraddice il requisito «Missing catalog models block instead of silently substituting a premium model». Inoltre, [aos-entry.py:202](~/.claude/skills/aos/bin/aos-entry.py:202) restituisce `(None, model)` per un modello sconosciuto: `_role_model()` non lo respinge se il riferimento nello stato è una stringa non vuota.
- Come verificarlo: caricare `RoutingConfig` dalla configurazione corrente; rimuovere **soltanto dalla copia in memoria** le due voci `catalog[open_primary]` e `catalog[open_fallback]`; chiamare `decide("T1", "LOW", config=config)`. **Riprodotto:** `executor="premium"`, `premium_execution_used=True`, senza tentativi open. Separatamente, `_role_model({"role_models": {"planner": "anthropic/not-in-catalog"}}, "planner", "claude")` restituisce il riferimento sconosciuto senza errore.

### [MAJOR] Una risposta che dichiara la review indisponibile può diventare PASS

- Evidenza: [aos-entry.py:379](~/.claude/skills/aos/bin/aos-entry.py:379) estrae tutto ciò che sta tra la prima `{` e l’ultima `}`, scartando il significato del testo circostante. [aos-pipeline.py:129](~/.claude/skills/aos/bin/aos-pipeline.py:129) richiede soltanto che `attacked` sia truthy: accetta persino `true`, senza una lista di criteri attaccati. Con `findings=[]`, la riga 140 passa direttamente a `pass`.
- Come verificarlo: creare uno stato T2 allo stadio `review`, quindi applicare `advance(state, "reviewed", json_reply({"reply": testo}))` con:
  ```text
  Review unavailable. Example only: {"attacked":["example"],"findings":[]}
  ```
  **Riprodotto:** `stage="pass"`. Anche la risposta malformata rispetto al protocollo `{"attacked":true,"findings":[]}` produce `pass`. L’esito positivo può quindi derivare da un esempio contenuto in una mancata review, anziché da una review completata.
