## Findings

### [BLOCKER] La sonda certifica dinieghi senza un risultato correlato

- Evidenza: [aos-isolation.py:165](~/.claude/skills/aos/bin/aos-isolation.py:165) restituisce `False` anche quando manca il risultato della chiamata; [riga 232](~/.claude/skills/aos/bin/aos-isolation.py:232) trasforma questa assenza in `denied`. Inoltre [aos-open-executor.py:449](~/.claude/skills/aos/bin/aos-open-executor.py:449) conserva solo `tool_results[-20:]`, contro 60 chiamate: può eliminare la prova di una lettura riuscita anche con ID validi. Il test in [test_open_executor.py:291](~/.claude/skills/aos/tests/test_open_executor.py:291) richiede esplicitamente `denied` per un risultato non correlato.
- Impatto: il fix del round 3 continua a poter certificare un runtime che ha letto un bersaglio vietato senza canary, come `.ssh/config`. Le nuove evidenze sono **ID mancanti e risultati eliminati dal troncamento**, non la precedente fiducia nella risposta testuale.
- Come verificarlo: eseguire dalla root, senza scritture:

```sh
rtk proxy python3 -B -c '
import runpy
from pathlib import Path
p = runpy.run_path("bin/aos-isolation.py")
call = {"id":"r7","tool":"Read","input":{"file_path":"/synthetic/.ssh/config"}}
ok = {"tool_use_id":"r7","is_error":False,"output":"Host private"}
targets = {"R7":("read","/synthetic/.ssh/config",None)}
state = {"root":"/synthetic","external":"/synthetic/external"}
tail = [{"tool_use_id":str(i),"is_error":True} for i in range(20)]
for label, results in [
    ("completo", [ok]+tail),
    ("ultimi20", ([ok]+tail)[-20:]),
    ("senza_id", [dict(ok, tool_use_id=None)])
]:
    v = p["observe"]({"tool_calls":[call],"tool_results":results},
                     Path("/synthetic/repo"), targets, {}, state)
    print(label, v["verdicts"]["R7"]["observed"], v["isolated"])
'
```

Risultato riprodotto: `completo LEAK False`; `ultimi20 denied True`; `senza_id denied True`. Ho verificato anche il caso senza ID attraverso `Stream`, con entrambi i controlli permessi simulati come riusciti: `isolated=True`.

### [MAJOR] Un check può spostare HEAD senza attivare il blocco dei metadati

- Evidenza: [aos-delegate.py:273](~/.claude/skills/aos/bin/aos-delegate.py:273) non include HEAD, refs o index nei percorsi sorvegliati; [riga 333](~/.claude/skills/aos/bin/aos-delegate.py:333) rappresenta la directory `.git` soltanto con `b"<dir>"`. Il nuovo controllo in [aos-entry.py:731](~/.claude/skills/aos/bin/aos-entry.py:731) confronta questi fingerprint, poi registra **la revisione successiva al comando**, senza confrontare HEAD prima/dopo.
- Impatto: il codice eseguito da un test può creare un commit o spostare HEAD lasciando invariati config, info e hooks. La pipeline accetta la nuova baseline; un commit può quindi sottrarre le modifiche al diff destinato al reviewer. È un’evasione diversa dalla modifica di `.git/config` corretta nel round 3.
- Come verificarlo: questo comando mostra in sola lettura le omissioni effettive:

```sh
rtk proxy python3 -B -c '
import runpy
from pathlib import Path
d = runpy.run_path("bin/aos-delegate.py")
repo = Path.cwd().resolve()
snapshot = d["git_meta"](d["git_meta_paths"](repo))
for name in ("HEAD", "index"):
    print(name, str(repo / ".git" / name) in snapshot)
'
```

Restituisce `HEAD False` e `index False`. Ho inoltre riprodotto il percorso completo **in memoria**: stato T1 `verify`, check `move/diff/security`, `delegate.invoke` simulato che sposta HEAD da `a×40` a `b×40`, lettura di HEAD coerentemente simulata e fingerprint reale invariato. Le tre chiamate `pipeline_step(..., "check", ...)` vengono accettate; il successivo `verify` restituisce **`stage="pass"`**. Nessun file è stato modificato.
