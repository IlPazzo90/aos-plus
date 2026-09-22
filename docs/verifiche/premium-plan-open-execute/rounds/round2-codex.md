## Findings

### [BLOCKER] La sonda conta il contenuto del report come tentativo sui bersagli vietati

- Evidenza: [aos-isolation.py:147](~/.claude/skills/aos/bin/aos-isolation.py:147) cerca il percorso nell’intero `json.dumps(call.get('input'))`, senza verificare nome del tool, operazione o campo del percorso. Una `Write` su `probe-report.txt` che menziona `.env` conta quindi come lettura di `.env`. [Riga 199](~/.claude/skills/aos/bin/aos-isolation.py:199) converte l’assenza di effetti in `denied`; riga 203 può certificare l’isolamento.
- Nuova evidenza rispetto al round 1: bastano **due chiamate reali ai controlli permessi**, con i percorsi vietati nel contenuto del report. Ho riprodotto `isolated=True`, 13 bersagli marcati `denied`, `unattempted=[]`, simulando in memoria soltanto gli effetti dei controlli. Nessuna operazione sui bersagli vietati.
- Come verificarlo, senza scritture:

```sh
rtk python3 -B -c '
import runpy
m = runpy.run_path("bin/aos-isolation.py")
calls = [{"tool": "Write", "input": {
    "file_path": "probe-report.txt",
    "content": "Non ho tentato .env né .git/config"
}}]
f = m["attempted_by_tool"]
print(f(calls, "read", ".env"))
print(f(calls, "write", ".git/config"))
'
```

Restituisce `True`, `True`; entrambi devono essere `False`. La correzione non dimostra ancora che le operazioni vietate siano state tentate.

### [MAJOR] Una review esplicitamente non eseguita diventa ancora PASS

- Evidenza: [aos-entry.py:357](~/.claude/skills/aos/bin/aos-entry.py:357) respinge la prosa soltanto oltre 200 caratteri. Sotto quella soglia elimina anche una dichiarazione esplicita di mancata review. [aos-pipeline.py:130](~/.claude/skills/aos/bin/aos-pipeline.py:130) accetta qualsiasi stringa non vuota in `attacked`; riga 142 passa direttamente a `pass` con finding vuoti.
- Nuova evidenza rispetto al round 1: il payload seguente soddisfa **entrambi i nuovi controlli** — prosa breve e lista di stringhe — pur dichiarando che le letture sono state negate.
- Come verificarlo:

```sh
rtk python3 -B -c '
import runpy
e = runpy.run_path("bin/aos-entry.py")
p = e["pipeline"]
reply = """I could not review the diff because reads were denied. Example only:
{"attacked":["not performed: reads denied"],"findings":[]}"""
state = p.start("T2", "MEDIUM", "claude", "codex", "open/x", "open/y", 2)
state["stage"] = "review"
print(p.advance(state, "reviewed", e["json_reply"]({"reply": reply}))["stage"])
'
```

Risultato osservato: `pass`. Viola il gate di review obbligatoria.

### [MAJOR] Il resolver dell’esecutore open accetta modelli assenti dal catalogo

- Evidenza: [aos-open-executor.py:73](~/.claude/skills/aos/bin/aos-open-executor.py:73) verifica la compatibilità soltanto quando `entry` è un dizionario. Se il modello manca, restituisce comunque un’identità utilizzabile. `run()` usa questo resolver alla [riga 371](~/.claude/skills/aos/bin/aos-open-executor.py:371), senza applicare `_catalog_model()` dell’entry premium.
- Nuova evidenza rispetto al round 1: il blocco aggiunto al router non copre il percorso open diretto, incluso `--model`. Un modello sconosciuto supera la selezione anziché bloccare prima dell’invocazione.
- Come verificarlo:

```sh
rtk python3 -B -c '
import runpy
m = runpy.run_path("bin/aos-open-executor.py")
unknown = "vercel/missing-model"
assert unknown not in m["policy"]()["model_catalog"]
print(m["resolve"](model=unknown, runtime="claude-code")["model_ref"])
'
```

Risultato osservato: `vercel/missing-model`, senza eccezione. Il contratto richiede che i modelli assenti dal catalogo blocchino.
