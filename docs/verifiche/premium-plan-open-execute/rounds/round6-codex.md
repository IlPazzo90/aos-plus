## Findings

### [BLOCKER] La sonda ignora scritture riuscite su percorsi assoluti equivalenti

- Evidenza: [bin/aos-isolation.py:202](~/.claude/skills/aos/bin/aos-isolation.py:202) normalizza il percorso dello strumento solo se relativo: `if repo is not None and not Path(used).is_absolute()`. Una chiamata su `/synthetic/./written-outside.txt` non viene correlata al bersaglio `/synthetic/written-outside.txt`. Se una precedente chiamata canonica è negata e la scrittura successiva viene rimossa, [observe():269](~/.claude/skills/aos/bin/aos-isolation.py:269) restituisce `denied` nonostante il risultato runtime di successo.
- Come verificarlo: passare a `observe()` due chiamate `Write`, con ID distinti: percorso canonico → `is_error=True`; percorso assoluto con `/./` → `is_error=False`. Simulare il file finale assente. Riprodotto in memoria anche sull’intera matrice, con gli altri bersagli negati e i controlli riusciti: **`isolated=true`, 13 `denied`, entrambi i controlli `allowed`, `leaks=[]`, `unknown=[]`**. È un bypass nuovo del fix del round 5; non dimostra una fuga live di Seatbelt.

### [MAJOR] Collisione della revisione: un check resta valido dopo la creazione di codice mai verificato

- Evidenza: [bin/aos-entry.py:391](~/.claude/skills/aos/bin/aos-entry.py:391) serializza i file nuovi con `'NEW FILE ' + name + '\n' + path.read_text()`, poi li concatena senza delimitazione non ambigua. Due alberi diversi producono lo stesso artefatto e quindi lo stesso SHA-256. Il confronto in [pipeline_step():755](~/.claude/skills/aos/bin/aos-entry.py:755) accetta così un check che ha cambiato il worktree.
- Come verificarlo: mantenere HEAD, indice e diff dei file tracciati invariati. Confrontare `snapshot()` per questi due insiemi di file **untracked**, con contenuti esatti:

  ```python
  prima = {"a.txt": "harmless\nNEW FILE b.py\nvalue = 0\n"}
  dopo  = {"a.txt": "harmless", "b.py": "value = 0\n"}
  ```

  Entrambi producono `NEW FILE a.txt\nharmless\nNEW FILE b.py\nvalue = 0\n` e la stessa revisione. Riprodotto anche dentro `pipeline_step(..., "check", ...)`, simulando in memoria un comando che verifica l’assenza di `b.py`, passa allo stato `dopo` e termina con exit 0: **il check viene registrato**, benché `b.py` ora esista. Questo aggira il vincolo di verifica sulla revisione corrente senza modificare HEAD, metadati Git o flag dell’indice.
