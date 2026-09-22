# Chiusura dei gate — piano del 22 settembre 2026

Tier T3, rischio HIGH. Host principale Claude Code (Opus 5 1M, scelta esplicita
dell'utente per il contesto); reviewer esterno Codex. Scope scelto dall'utente:
**tutti i sette gate** del verbale OPERATIONAL-VALIDATION.md, poi release 2.4.0
su `main` di entrambe le edizioni.

## Esito atteso

Pipeline a ruoli utilizzabile da Claude Code e Codex con evidenza per ogni gate;
`main` allineato in entrambe le edizioni; nessuna riduzione di guardie, denylist o
Security Gate; nessun runtime riabilitato senza prova negativa superata.

## Ordine di lavoro (ogni voce è un task T2)

1. **G7 budget concorrenti + contesto nascosto.** Prenotazione atomica in SQLite
   (`BEGIN IMMEDIATE`) nel ledger host: tabella `reservations`, somma outcome +
   prenotazioni aperte + stima nella stessa transazione, settle alla registrazione
   dell'outcome. Test con due processi contro un cap che ne ammette uno.
   Contesto: accanto alla stima del prompt registrare l'`input_tokens` osservato
   del runtime e la differenza (`hidden_context_tokens`), null se non riportato.
2. **G3 + G6 sonda di isolamento.** `bin/aos-isolation.py`: fixture usa e getta
   (repo git, `.env`, `*.pem`, directory esterna con canary e symlink nel repo,
   `.git/config`), brief che chiede al worker di leggere/scrivere quei bersagli;
   verifica meccanica di reply, tool_results e filesystem. Matrice per runtime.
   Le sonde possono invocare un runtime escluso **solo** dentro la fixture della
   sonda; la policy di produzione non cambia. Per OpenCode e Codex tentativo di
   isolamento OS con `sandbox-exec` (seatbelt, last-match-wins verificato):
   deny HOME, allow repo + toolchain + tmp del run, deny segreti nel repo, deny
   scrittura `.git`. Riabilitazione solo con matrice verde; altrimenti resta
   l'esclusione con evidenza fresca.
3. **G4 + G5 estensione bench.** `aos-bench.py`: `--review-replay` (reviewer MID
   sui diff registrati, confronto con i finding del reviewer premium) e
   `--planners` (piano MID/premium nel brief dell'esecutore open, first-pass a
   confronto con il baseline senza piano). Corsa G4: 20 task × claude-code ×
   {DeepSeek, Qwen} con review premium; tetto tempo/step, costo da usage
   osservato × prezzo di catalogo, etichettato stima.
4. **G2 prova nei due host.** Claude Code: questa sessione esegue la pipeline
   T2 su fixture (planner Sonnet → DeepSeek → reviewer GPT-5.6-sol). Codex:
   `codex exec` con `$aos` sulla stessa fixture. Limite dichiarato: nessuna
   sessione TTY registrata.
5. **G5 corsa** su sottoinsieme (≥6 task): reviewer Sonnet e GPT-5.6-sol sui diff
   di G4; planner Sonnet e GPT-5.6-sol con esecutore DeepSeek. Nessuna promozione
   automatica: il catalogo cambia solo se i numeri lo giustificano e lo dico.
6. **G1 review indipendente** Codex via verify-agent sull'intero diff `main..HEAD`,
   max 6 round, finding arbitrati meccanicamente, fix agli open worker dove
   ammesso.
7. **Release.** VERSION 2.4.0 finale, CHANGELOG, DISTRIBUTION uguale byte a byte
   nell'edizione pubblica, docs sanificate, suite Python/Node in entrambe,
   Security Gate, doctor, misura `finish`, merge ff su `main`, push, hash remoti.

## Vincoli

Nessun segreto in Git; sonde con canary sintetici; nessun `--allow-dirty`;
nessuna modifica a hook vendor o permessi globali; i worker non approvano lesson
né cambiano policy. Ogni runtime resta escluso finché la sua matrice non è verde.

## Accettazione

Ogni gate ha una riga in OPERATIONAL-VALIDATION.md con comando, esito e limite;
suite verdi; review Codex PASS o finding residui dichiarati; `main` == branch in
entrambe le edizioni, remoto verificato.

## Rollback

Revert dei commit di chiusura; `main` non si tocca prima dell'ultimo passo.
