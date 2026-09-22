## [MINOR] Il dedup dei candidati rifiutati fonde ancora azioni contraddittorie

**File**: `bin/aos-learning.py:324-343` (`_reject`), confrontare con `bin/aos-learning.py:244-263`.

**Cosa succede**: la correzione "dedup merged contradictory actions" è stata applicata solo al percorso approvato, dove la chiave include `lesson_type`, `confidence` e `recommended_action`. Nel percorso rifiutato la chiave resta `source_task + affected_component + evidence + scope(+project)`. Due candidati con la stessa evidenza ma `recommended_action` opposte finiscono nella stessa riga rifiutata: la seconda azione viene persa e il `count` sale.

**Riproduttore**:
1. Inviare un candidato scope `global` con `project` valorizzato (rifiuto `global_with_project`), `recommended_action="pin model A"`.
2. Inviare un secondo candidato identico salvo `recommended_action="drop model A"`.
3. Il secondo ritorna `{"status":"rejected","count":2}`; `report()` mostra un `repeated_pattern` con count 2 la cui `recommended_action` è solo la prima.

**Contratto violato**: il dedup non deve fondere azioni contraddittorie. L'impatto è limitato: le righe rifiutate non alimentano `routing_advice` né `apply_lesson`, quindi non c'è steering; il danno è solo sul report e sui pattern ripetuti.

---

Verifiche mirate sulle correzioni dichiarate, entro il limite di 8 letture:

- **Solo evidenza verificata dall'host guida il routing**: `routing_advice` filtra `verification_status == "verified"` (`aos-learning.py:496`); righe legacy con `NULL` e righe `pending`/`not_scored` sono escluse. Il confronto testuale su `error == "pending mandatory review"` (riga 504) resta ma ora è ridondante e conservativo, non un vettore di steering. Ha retto.
- **Global richiede approvazione esplicita o evidenza indipendente**: `apply_lesson` accetta solo `approved is True`, rifiuta `low` confidence, esige almeno 3 `source_task` distinti e 3 identità di evidenza distinte (`aos-learning.py:732-750`). Evidenza copiata non passa. Ha retto. Non ho letto `_evidence_identity`, quindi la robustezza della canonicalizzazione resta non verificata da me.
- **Nessuna mutazione automatica di policy o esecuzione di comandi**: `apply_lesson` inserisce solo una riga in `applications` con `rollback_identifier` fornito dall'host; `routing_advice` è documentato e implementato come solo consiglio. Ha retto.
- **Nessun fallback a premium quando manca il modello di ruolo**: `_role_model` solleva errore se esiste un `model_catalog` (`aos-entry.py:205-217`); il fallback ai `model_refs` è confinato al formato legacy senza catalogo. Ha retto.
- **Ruolo fuori budget non procede in silenzio**: `_require_routing_budget_models` solleva errore se planner/reviewer manca in una pipeline o se `choose_model` con budget non conferma il modello scelto (`aos-entry.py:220-234`). Ha retto.
- **Review HIGH obbligatoria non eliminabile dal budget**: lo stadio `review` fallisce chiuso se il reviewer manca o coincide col planner (`aos-entry.py:719-720`); il budget viene verificato prima dell'esecuzione, non usato per saltare lo stadio. Ha retto.
- **Learning disabilitato con budget attivo**: `_budget` solleva errore se serve accounting e il learning è disabilitato o manca il database (`aos-entry.py:486-489`). Ha retto sulla base del grep; non ho letto `_learning_configuration` per intero.
- **Offset `since`**: ora viene parsato come ISO con timezone, normalizzato a UTC `Z` e solo poi confrontato (`aos-learning.py:612-619`). Ha retto, con una riserva: il confronto resta stringa contro `created_at`, quindi dipende dal fatto che `_now()` usi lo stesso formato e la stessa precisione. Non l'ho verificato.
- **Aggregazione token da input/output del ledger**: non verificata entro il limite di letture. `aggregate` (righe 560-590) somma solo `cost`; se la correzione vive in `aos-operations.py` non l'ho vista.

Nessun blocker rilevato nel perimetro letto. I blocker documentati (cap monetario nativo, isolamento OS nativo, Codex/OpenCode open disabilitati) non sono presentati come risolti nel codice esaminato. Questo è un checkpoint di sviluppo, non una certificazione di deploy.
