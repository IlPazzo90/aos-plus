# Risk, Tiers & Autonomy

Read when the risk is HIGH/CRITICAL, or when it is unclear whether to proceed alone.

## Classify risk by blast radius, not by effort

The question is never "how hard is this?" It is **"if I am wrong, what breaks, who
notices, and can I undo it?"**

| | Blast radius | Reversible? | Level |
|---|---|---|---|
| Local file, no users yet — or a content-only edit on a live site that moves no URL, heading, structured data, auth or stored record, previous version saved | One component | Yes, instantly | **LOW** |
| Shared module or several components, without a HIGH trigger | Several components | Yes | **MEDIUM** |
| Auth, payments, personal data, public API, live site content, DNS, production deploy, schema change | Users, money, or SEO | Yes, but slowly and visibly | **HIGH** |
| Data deletion, irreversible migration, DNS cutover, credential rotation, anything with no undo | Everything downstream | **No** | **CRITICAL** |

Risk is a property of the *change*, not of the task. A one-word edit to a live H1 on
a production website is HIGH: it is small, easy, and it moves SEO.

### Examples requiring contextual assessment

- Live WordPress deployments: validate syntax, preserve backups and verify health.
- DNS changes, database schema changes and data deletion: assess reversibility and blast radius.
- Home automation controlling heating, charging, irrigation or gates: validate configuration and retain recovery access.
- Production automation, messages and ad spend: respect the specific authorization.
- Credentials and private data: never expose them in reports, commits or artifacts.

## Verification depth by risk

| Risk | Required before "done" |
|------|------------------------|
| LOW | Observe the change works. Run whatever check the project has. |
| MEDIUM | The above + regression check on what the change touched + the obvious edge cases |
| HIGH | The above + explicit plan stated before starting + **security pass** (`bin/aos-security.sh` run and every signal answered, *plus* the Security role in `quality-gates.md` §4 answered in prose: where is the trust boundary and what crosses it unchecked) + **a stated rollback path** + review from at least the two most relevant roles + **at tier ≥ T2, the cross-model gate** (`verify-agent`, conditions in `quality-gates.md` §3) and its log committed to `docs/verifiche/` |
| CRITICAL | The above + **stop before executing.** State exactly what will happen, what cannot be undone, and what the recovery is. Wait for explicit approval. |

A HIGH-risk task with no rollback path is not ready to execute. Find one first.
At T0/T1 answer the applicable HIGH security and role questions inline; do not load
the entire quality-gates workflow or add a red-team loop. A short report may link
the detailed evidence. Risk verification is not reduced by the tier.

## Tier and risk are independent

| | LOW | HIGH |
|---|---|---|
| **T0** | Fix a typo in software docs → just do it | One-line auth flag → HIGH checks; existing authorization persists |
| **T3** | Build a local prototype → full process, no gate | Migrate the production schema → full process **and** a gate |

## Autonomy — proceed without asking

Exploration and relevant reads inside the agreed scope. Creating and editing files.
Local refactoring needed by the task. Writing and running tests. Lint, format,
typecheck, build. Debugging. Documenting what changed. Fixing defects found in your
own work. Reversible experiments in a scratch directory.

## Autonomy — stop and ask

- A **business** decision: pricing, positioning, what to promise a customer.
- A requirement so ambiguous that the alternatives produce **different products**.
- Scope growing meaningfully beyond what was asked.
- A UX or product choice not derivable from existing patterns.
- Anything **destructive or irreversible**: delete, drop, truncate, force-push,
  overwrite without backup, `rm -rf`.
- Production migrations, DNS, billing, auth, secrets or external actions **when
  the concrete action is not already authorized**, or the host requires confirmation
  at action time. A risk label does not cancel existing authorization.
- Publishing, sending, posting or spending beyond the approved destination,
  purpose or limit. Authorization for one action does not authorize unrelated ones.
- The loop bounds in `SKILL.md` being reached.

## Do not stop for

A minor detail with an obvious, reversible default. Choose it, proceed, and record it
under Decisions/Assumptions in the report. Asking four clarifying questions before
starting a two-file change is a failure mode, not thoroughness.

## Three strikes → escalate to root cause

Three substantive attempts at the same symptom, still failing, means the *model of the
problem* is wrong. Stop editing. Then:

1. State plainly what was tried and what each attempt actually produced.
2. Re-derive from evidence: what is genuinely known versus assumed. Test the assumption
   that seemed too obvious to check.
3. Widen: is the cause upstream of where the symptom appears? Cache, build step,
   environment, a second copy of the file, a stale deploy?
4. If the architecture is the cause, say so and re-plan — do not keep patching around it.
5. Still stuck: stop and report. What is red, the evidence, the likely cause, what was
   tried, and the recommended next move. **A truthful stop beats a fabricated finish.**
