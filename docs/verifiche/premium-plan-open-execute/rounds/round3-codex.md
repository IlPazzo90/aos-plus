## Findings

### [BLOCKER] La sonda considera negata una lettura riuscita

- **Evidenza:** [aos-isolation.py:207](~/.claude/skills/aos/bin/aos-isolation.py:207): per R7, privo di canary, `leaked = claimed_ok`; segue `observed = ... 'denied' if attempted`. Il risultato dello strumento non viene controllato. Una vera chiamata `Read` che restituisce contenuto SSH con `is_error=False` viene classificata come negata se il worker scrive «REFUSED». È evidenza nuova rispetto ai round precedenti: il tentativo esiste e usa il campo percorso corretto, ma **la lettura riesce**. Gli altri dodici bersagli e i controlli non impediscono questo falso verde.
- **Come verificarlo:** dalla root, senza leggere segreti o modificare file:

```sh
rtk python3 -B -c '
import runpy
from pathlib import Path
p=runpy.run_path("bin/aos-isolation.py")
r={
 "result":"RESULT R7: REFUSED",
 "tool_calls":[{"tool":"Read","input":{"file_path":"/synthetic/.ssh/config"}}],
 "tool_results":[{"is_error":False,"output":"Host production\n User deploy"}]
}
v=p["observe"](r,Path("/synthetic/repo"),
 {"R7":("read","/synthetic/.ssh/config",None)}, {},
 {"root":"/synthetic","external":"/synthetic/external"})
print(v)
'
```

**Risultato riprodotto:** `isolated=True`, `leaks=[]`, R7 `observed='denied'`, nonostante il contenuto restituito dal tool. Anche un `Grep` riuscito senza corrispondenze sulla `.env` viene conteggiato come diniego.

### [MAJOR] I check possono alterare `.git` e arrivare a PASS

- **Evidenza:** [aos-entry.py:727](~/.claude/skills/aos/bin/aos-entry.py:727) esegue `delegate.invoke(...)` e subito `snapshot(directory)`, senza fingerprint dei metadati. [snapshot:373](~/.claude/skills/aos/bin/aos-entry.py:373) invoca Git e identifica la revisione usando HEAD e diff, escludendo configurazione e hook. Un test che esegue codice prodotto dal worker può quindi modificare `.git/config`: la modifica non blocca il check e Git viene richiamato sui metadati alterati. Il benchmark riconosce e protegge proprio questo caso in [aos-bench.py:479](~/.claude/skills/aos/bin/aos-bench.py:479); la pipeline no. Viola il requisito che le modifiche ai metadati siano blocker.
- **Come verificarlo:** riproduzione con I/O simulato esclusivamente in memoria; esegue le funzioni reali `check`, `snapshot` e `verify`:

```sh
rtk python3 -B -c '
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
e=runpy.run_path("bin/aos-entry.py")
d=e["delegate"]; repo=Path("/synthetic/repo")
meta={"config":"original"}
def git(*a,**kw):
 return SimpleNamespace(stdout="HEAD123" if a[1]=="rev-parse" else "",returncode=0)
def check(*a,**kw):
 meta["config"]="modified"; return 0,"green",""
with patch.object(d,"git",side_effect=git), \
     patch.object(d,"invoke",side_effect=check), \
     patch.object(d,"git_meta") as guard:
 revision=e["snapshot"](repo)[0]
 s=e["pipeline"].start("T1","LOW",None,None,"open","fallback",2)
 s.update(directory=str(repo),learning_enabled=False,stage="verify",
  plan={"subtasks":[{"id":"one"}],"tests":["unit"]},
  checks=[{"id":x,"revision":revision,"stage":"verify","exit_code":0}
          for x in ("diff","security")])
 s=e["pipeline_step"](s,"check",
  {"id":"unit","argv":["python3","-m","unittest"]},repo)
 s=e["pipeline_step"](s,"verify",{},repo)
 print(meta,s["stage"],"fingerprint calls:",guard.call_count)
'
```

**Risultato riprodotto:** `{'config': 'modified'} pass fingerprint calls: 0`.

### [MAJOR] L’override del provider aggira il controllo del catalogo

- **Evidenza:** [aos-open-executor.py:74](~/.claude/skills/aos/bin/aos-open-executor.py:74) valida `catalog.get(identity)`, ma [riga 86](~/.claude/skills/aos/bin/aos-open-executor.py:86) restituisce un’identità diversa: `model_ref=f'{provider}/{model_id}'`. Con `model='anthropic/fable', provider='vercel'` viene ammesso `vercel/fable`, assente dal catalogo. Ho verificato anche che `run()` passa questa identità a `delegate.run`, sostituendo con mock credenziali e invocazione. Il fix del round 2 protegge il riferimento iniziale, non quello effettivamente eseguito.
- **Come verificarlo:**

```sh
rtk python3 -B -c '
import runpy
e=runpy.run_path("bin/aos-open-executor.py")
s=e["resolve"](model="anthropic/fable",
 provider="vercel",runtime="claude-code")
print(s["model_ref"],s["model_ref"] in e["policy"]()["model_catalog"])
'
```

**Risultato riprodotto:** `vercel/fable False`, senza rifiuto.

### [MAJOR] L’installazione omette lo script della sonda

- **Evidenza:** `bin/aos-isolation.py` manca da `REQUIRED_FILES` in [aos-install.sh:50](~/.claude/skills/aos/bin/aos-install.sh:50). L’installazione copia **soltanto** quei file, nel ciclo alla [riga 283](~/.claude/skills/aos/bin/aos-install.sh:283). Una nuova installazione risulta integra per `verify()`, ma non può eseguire la sonda richiesta dal brief. Inoltre installa `tests/test_open_executor.py`, il cui `IsolationProbeTests.setUp()` importa proprio lo script mancante.
- **Come verificarlo:** estrarre il manifest realmente usato dall’installer:

```sh
rtk python3 -B -c '
import re
from pathlib import Path
text=Path("bin/aos-install.sh").read_text()
files=re.search(r"^REQUIRED_FILES=\"([^\"]+)\"",text,re.M).group(1).split()
for name in ("bin/aos-isolation.py","tests/test_open_executor.py"):
 print(name,name in files)
'
```

**Risultato riprodotto:** sonda `False`, test che la importano `True`.
