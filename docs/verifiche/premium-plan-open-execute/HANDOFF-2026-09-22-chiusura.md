# Ripresa — chiusura dei gate, 22 settembre 2026 (seconda sessione)

> **Chiuso il 22/09/2026.** Il lavoro descritto qui è stato completato nella stessa
> sessione: round 5 e 6 arbitrati, verdetto VERIFICATO CON RISERVE in
> [REVIEW-LOG.md](REVIEW-LOG.md), 2.4.0 rilasciata su `main` nelle due edizioni.
> Resta un documento di continuità: le azioni elencate sotto sono fatte.

Stato: **lavoro completo sul branch, non ancora su `main`.**
Branch `upgrade/runtime-independent-open-execution`, ~20 commit avanti a `main`,
tutto committato. Edizione pubblica `~/Progetti/Strumenti/aos-plus` allineata fino
ai fix del round 3 (il round 4 va ancora portato).

## Deciso dall'utente in questa sessione

- **OpenCode fuori dal progetto.** Plugin d'ingresso, installer, runner, permission
  map, test Node e riferimento cancellati; `~/.config/opencode` ripulita col rollback
  dell'installer. I suoi record di sonda restano in `isolation/history/` come storia.
- **Codex fuori come harness open.** `apply_patch` viaggia dentro il tool shell:
  senza shell è inerte (sonda: 0 chiamate), con shell la sua policy dei comandi non
  regge (`npx` eseguito). Resta host, planner MID, reviewer cross-family, escalation.
- **Claude Code è l'unico harness open**, con profilo seatbelt macOS.
- **Benchmark del 22/09 fermato** a 3 task su 20: resta autorevole quello del 20/09.
  Gate G4 e G5 segnati come non eseguiti per scelta, con la ragione nel verbale.
- **Sessione principale su Opus 5 1M** (non Fable) per scelta esplicita dell'utente.

## Verifica indipendente

Reviewer Codex via `verify-agent/scripts/review.py --caller claude`, prompt e report
in `tmp/verify/close-release-gates/` (ignorata da git; i report si copiano qui a
chiusura). Quattro round chiusi, **12 finding confermati, nessuno refutato**:

- round 1: 2 BLOCKER + 4 MAJOR;
- round 2: 1 BLOCKER + 2 MAJOR;
- round 3: 1 BLOCKER + 3 MAJOR;
- round 4: 1 BLOCKER + 1 MAJOR.

Ogni finding riprodotto meccanicamente prima della correzione e coperto da
regressione. Il dettaglio con le riproduzioni è in REVIEW-LOG.md.

**Round 5 lanciato** con `reviewer-prompt-round5.md`; tetto a sei round.

## Verifiche correnti

431 test Python OK, Security Gate exit 0, doctor 0 anomalie, sonda
`isolation/claude-code-seatbelt.json`: 13/13 negati, 0 unknown, 17 chiamate
correlate, entrambi i controlli permessi eseguiti.

## Prossime azioni, in ordine

1. Arbitrare il round 5; se PASS o solo riserve dichiarate, chiudere il loop.
2. Copiare i report dei round in `docs/verifiche/premium-plan-open-execute/` e
   aggiornare REVIEW-LOG.md con il verdetto finale.
3. Portare i fix del round 4 (e 5) nell'edizione pubblica con
   `<scratchpad>/sync-public.py`, poi `bin/public-sanity.sh`, suite e doctor.
   **`docs/misure` non si pubblica**: nomina i repository dei clienti.
4. `aos-measure.py finish --record docs/misure/2026-09-22-close-release-gates.json`
   con `--outcome delivered` e i contatori dei ruoli.
5. Merge ff su `main` e push in entrambe le edizioni; verificare gli hash remoti.
6. Chiedere all'utente i verdetti `judge` dei record di misura ancora senza.

## Limiti dichiarati

Nessun harness open di riserva: se il contratto del CLI Claude cambia, l'esecuzione
open si ferma al controllo di versione. I modelli raggiungibili sono quelli serviti
sul protocollo Anthropic (DeepSeek e Qwen provati). La sonda vincola i bersagli
provati su questo host, non ogni attacco possibile.
