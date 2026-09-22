# Verified learning and operational evidence

The host owns verification and the learning database. Workers cannot admit lessons,
approve global changes, alter routing policy or read the database through the repo.
The default database is `~/.local/state/aos/learning.sqlite3`; it is local state, never
part of the public distribution or a repository upload.

`bin/aos-learning.py` stores lessons, rejected candidates, measured outcomes and
application records. Admission requires a scoped lesson and references to complete
host checks: ID, argv, integer exit code, output and artifact revision. A nonzero
exit can prove a failure. A model statement or a finding without those checks does
not qualify. The database preserves the check snapshots after the task ends.

The API's checks are trusted host input. It is not an attestation service and cannot
prove that an arbitrary caller actually ran the commands it supplies. Never expose
lesson admission or global approval as a worker tool.

```sh
python3 bin/aos-learning.py report --database ~/.local/state/aos/learning.sqlite3
python3 bin/aos-learning.py history --database ~/.local/state/aos/learning.sqlite3
```

Project lessons remain scoped to their project. Applying a global lesson requires
explicit host approval or at least three independent source tasks with matching
component and recommended action. Low-confidence lessons cannot auto-apply. An
application records its action, host evidence and rollback reference; it does not
execute arbitrary code or rewrite a global skill itself. The coordinator must make
and verify the actual change before recording it as applied.

Outcomes distinguish worker health from test results. A timeout, crash or reported
worker error cannot become a success because project tests happen to pass. Historical
cost per success includes failed attempts; unknown cost remains unknown. Repeated
observations inform a project-scoped routing change only after sufficient evidence;
they never silently replace the benchmark winner.

`bin/aos-operations.py` reuses `aos-context.py` for structural compaction and creates
a handoff retaining unresolved work, findings, constraints and escalation state.
Prompt-based token counts are estimates of the supplied prompt, not measurements of
the runtime's system prompt, caches or entire active context. Protected content is
never truncated to force a request under the hard limit: remaining RED context blocks
the call and requires decomposition.

Cost budgets support task, daily, monthly and premium limits. Unknown cost under an
active cap blocks a call; required review cannot be removed to fit the budget.
The helper is a point-in-time check, not a concurrent spending reservation service.
Native subscription quota and marginal dollar cost remain unknown unless reported
by the provider. Published Gateway token prices are estimates for planning; peak,
regional and provider-specific pricing can differ from the base price.

Release readiness requires live evidence in addition to these helpers and tests:
both host pipelines, required cross-family review, negative runtime security probes,
healthy benchmark workers, and a verified lesson application followed by a repeat of
the original case. A local doctor result alone does not certify those gates.

Gli outcome senza `verification_status=verified` restano nel ledger per audit/costi,
ma non influenzano i consigli di routing. Le righe legacy hanno stato sconosciuto.
La soglia globale richiede fonti task ed evidenze eseguite distinte; cambiare solo
l'ID di un check non crea evidenza indipendente. Il ledger registra applicazioni
verificate dall'host, non esegue azioni arbitrarie contenute nelle lesson.
