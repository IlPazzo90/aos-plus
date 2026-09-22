#!/usr/bin/env python3
"""Read-only local AOS health checks (Python 3.9+).

There is one installation, ~/.claude/skills/aos; the Codex path is a symlink to
it. The doctor checks the link, then the maintained files of the one root.
Frontmatter checks required fields only, not complete YAML syntax.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit


class Doctor:
    def __init__(self):
        self.errors = 0
        self.warnings = 0
        self.bash = shutil.which("bash")

    def fail(self, code, subject, remedy):
        self.errors += 1
        print(f"{code}: {subject} — {remedy}")

    def warn(self, code, subject, remedy):
        # A catalog row whose target moved says the index is old, not that the
        # installation is broken: 40 of them once made every run exit 1, and a gate
        # that always says no stops being read.
        self.warnings += 1
        print(f"AVVISO {code}: {subject} — {remedy}")

    def read(self, root, relative):
        path = root / relative
        try:
            # Never follow a maintained-file symlink outside the installation.
            path.resolve().relative_to(root.resolve())
            return path.read_bytes()
        except (OSError, ValueError, RuntimeError):
            self.fail("MANCANTE", f"{root}: {relative}", "ripristinare il file nella root AOS")
            return None

    def manifest(self, root):
        raw = self.read(root, "bin/aos-install.sh")
        if raw is None:
            return set()
        try:
            matches = re.findall(r'^REQUIRED_FILES="([^"$`]+)"', raw.decode("utf-8"), re.M)
            if len(matches) != 1:
                raise ValueError()
            names = matches[0].split()
            if not names or "bin/aos-install.sh" not in names:
                raise ValueError()
            for name in names:
                if not re.fullmatch(r"[A-Za-z0-9_./-]+", name) or Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError()
            return set(names)
        except (ValueError, UnicodeError):
            self.fail("MANIFEST", str(root), "ripristinare REQUIRED_FILES statico e relativo in bin/aos-install.sh")
            return set()

    def references(self, root, relative, content):
        # Check literal Markdown links and AOS-owned backtick paths, not prose examples.
        candidates = re.findall(r"\[[^\]]*\]\(([^)]+)\)", content)
        candidates += re.findall(r"`((?:references|bin|catalog|evals)/[A-Za-z0-9_./-]+)`", content)
        for candidate in set(candidates):
            candidate = candidate.strip()
            if candidate.startswith("<") and ">" in candidate:
                target = candidate[1:candidate.index(">")]
            else:
                target = candidate.split(" ", 1)[0]
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path or "$" in target:
                continue
            path = Path(unquote(parsed.path))
            if path.is_absolute():
                continue
            base = root if str(path).startswith(("references/", "bin/", "catalog/", "evals/")) else (root / relative).parent
            if not (base / path).exists():
                self.fail("RIFERIMENTO", f"{root}: {relative} -> {target}", "correggere il link o ripristinare la destinazione")

    def check_file(self, root, relative, raw):
        suffix = Path(relative).suffix
        if suffix not in (".md", ".py", ".sh", ".json"):
            return
        try:
            content = raw.decode("utf-8")
            if suffix == ".py":
                # Compile in memory: never import code or create __pycache__.
                compile(content, relative, "exec", dont_inherit=True)
            elif suffix == ".sh" and self.bash:
                env = {k: v for k, v in os.environ.items() if k not in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS") and not k.startswith("BASH_FUNC_")}
                result = subprocess.run([self.bash, "--noprofile", "--norc", "-n"], input=content,
                                        text=True, capture_output=True, timeout=10, env=env)
                if result.returncode:
                    raise ValueError()
            elif suffix == ".json":
                data = json.loads(content)
                if relative == "catalog/index.json":
                    if not isinstance(data, list):
                        raise ValueError()
                    for entry in data:
                        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("name"), str):
                            raise ValueError()
                        target = Path(entry["path"]).expanduser()
                        if not target.is_absolute():
                            target = root / target
                        if not target.is_file():
                            self.warn("CATALOGO", f"{root}: {entry['name']}", "rigenerare l'indice con skill-library.py refresh: destinazione locale assente")
                elif relative == "catalog/core.json" and (not isinstance(data, list) or not all(isinstance(x, str) for x in data)):
                    raise ValueError()
            elif suffix == ".md":
                if Path(relative).name == "SKILL.md":
                    # Required field presence only; use quick_validate for full YAML.
                    frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", content, re.S)
                    if not frontmatter or not all(re.search(r"^" + key + r":\s*\S", frontmatter[1], re.M) for key in ("name", "description")):
                        raise ValueError()
                self.references(root, relative, content)
        except (SyntaxError, ValueError, UnicodeError, OSError, RuntimeError, subprocess.TimeoutExpired):
            # Never echo source text: it may contain a pasted credential.
            self.fail("SINTASSI", f"{root}: {relative}", "correggere sintassi/formato con il validatore locale")

    def link(self, codex_root, claude_root):
        # Two copies were the defect, not their drift: the Codex clone sat on 1.14.0
        # with ten dirty files while the installer copied files around it. A real
        # directory here is reported as a copy, whatever its contents.
        try:
            if codex_root.is_symlink() and codex_root.resolve() == claude_root.resolve() and claude_root.is_dir():
                return
        except (OSError, RuntimeError):
            pass
        if codex_root.is_dir() and not codex_root.is_symlink():
            self.fail("COPIA", str(codex_root), "è una copia, non un link: sostituirla con bash bin/aos-install.sh --host codex --link")
        else:
            self.fail("MANCANTE", str(codex_root), f"creare il link a {claude_root} con bash bin/aos-install.sh --host codex --link")

    TMP_WARN_BYTES = 200 * 1024 * 1024

    BACKUP_WARN_BYTES = 1024 * 1024 * 1024

    def distribution(self, root):
        # Optional: a DISTRIBUTION file naming the public edition's checkout on its
        # first line, then the files the two editions keep byte-identical. "Same
        # intervention" had no check, and 1.17.0 never reached it; a version match
        # alone would not have seen a port that changed the number and not the code.
        marker = root / "DISTRIBUTION"
        if not marker.is_file():
            return
        try:
            lines = [line.strip() for line in marker.read_text().splitlines()
                     if line.strip() and not line.strip().startswith("#")]
            target = Path(lines[0]).expanduser() if lines else None
            here = (root / "VERSION").read_text().strip()
            there = (target / "VERSION").read_text().strip() if target and (target / "VERSION").is_file() else None
        except (OSError, UnicodeError):
            self.warn("DISTRIBUZIONE", str(marker), "file illeggibile: correggerlo o rimuoverlo")
            return
        if target is None or there is None:
            self.warn("DISTRIBUZIONE", str(target or marker), "checkout assente: correggere DISTRIBUTION o clonare la distribuzione")
            return
        if there != here:
            self.warn("DISTRIBUZIONE", f"{target} è a {there}, questa installazione a {here}", "portare le modifiche alla distribuzione nello stesso intervento")
        for relative in lines[1:]:
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                continue
            try:
                same = (root / relative).read_bytes() == (target / relative).read_bytes()
            except OSError:
                same = False
            if not same:
                self.warn("DISTRIBUZIONE", f"{relative} differisce da {target}", "i file elencati in DISTRIBUTION sono identici per costruzione: portare la modifica")

    def backups(self):
        # 8.3 GB sat in ~/.agents/backups on 2026-09-15: eight copies of the same
        # clone, each carrying the same 936 MB of tmp/. Outside the root, so the tmp/
        # check could not see it; size only, deletion is the user's.
        for base in (Path.home() / ".claude/backups", Path.home() / ".agents/backups"):
            if not base.is_dir():
                continue
            total = 0
            for path in base.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink():
                        total += path.stat().st_size
                except OSError:
                    continue
            if total > self.BACKUP_WARN_BYTES:
                self.warn("BACKUP", f"{base} pesa {total // (1024 * 1024)} MB", "backup dell'installer e di aggiorna.sh: verificare cosa è unico e cancellare il resto")

    def tmp_weight(self, root):
        # tmp/ is ignored by Git and nobody counts it: 936 MB sat in one clone's tmp/
        # until the directory was backed up whole. Size, not age, because a single
        # research dump is what fills it.
        tmp = root / "tmp"
        if not tmp.is_dir():
            return
        total = 0
        for path in tmp.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
        if total > self.TMP_WARN_BYTES:
            self.warn("TMP", f"{tmp} pesa {total // (1024 * 1024)} MB", "è ignorata da Git: archiviare o cancellare ciò che non serve")

    def version_consistency(self, root):
        # SKILL.md metadata.version and VERSION name the same release. The
        # frontmatter field was bumped alongside VERSION through 1.22.1 and then
        # left behind by the 2.x releases: two numbers that both claim to be the
        # AOS version. Neither file is authoritative over the other — they must agree.
        try:
            version = (root / "VERSION").read_text().strip()
        except OSError:
            return  # no VERSION: nothing to compare
        try:
            skill = (root / "SKILL.md").read_text()
        except OSError:
            return
        frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", skill, re.S)
        if not frontmatter:
            return
        match = re.search(r"^\s*version:\s*[\"']([^\"']+)[\"']", frontmatter[1], re.M)
        if not match:
            return  # no metadata.version: nothing to compare
        if match.group(1) != version:
            self.warn("VERSIONE", f"SKILL.md metadata.version={match.group(1)} != VERSION={version}",
                      "allineare il frontmatter di SKILL.md a VERSION")

    def provider_privacy(self, root):
        # Report the privacy posture the catalog declares per open model. AOS reads
        # configuration only, never the provider's server-side behaviour:
        # `provider_reported_zdr` is what the Gateway model listing said, and
        # `privacy_status` is the operator's note; neither is a verification.
        try:
            raw = (root / "config/open-models.json").read_text()
        except OSError:
            return  # no routing config: nothing to check
        try:
            routing = json.loads(raw)
        except ValueError:
            self.warn("PRIVACY", "config/open-models.json illeggibile", "correggere il file")
            return
        open_ = routing.get("open")
        if not isinstance(open_, dict):
            return
        models = [m for m in (open_.get("primary"), open_.get("fallback")) if isinstance(m, str) and m.strip()]
        if not models:
            return
        catalog = routing.get("model_catalog")
        catalog = catalog if isinstance(catalog, dict) else {}
        statuses = {}
        for model in models:
            entry = catalog.get(model)
            zdr = entry.get("provider_reported_zdr") if isinstance(entry, dict) else None
            statuses[model] = zdr if isinstance(zdr, str) and zdr.strip() else "unknown"
        print("Privacy provider: " + "; ".join(f"{m} zero_data_retention={statuses[m]}" for m in models))
        for model, status in statuses.items():
            if status == "unknown":
                self.warn("PRIVACY", f"{model}: provider_reported_zdr assente dal catalogo",
                          "registrare provider_reported_zdr dal listino del provider o verificare l'account")

    def operational_status(self, root):
        # Read-only report of the routing/operational policy. Only
        # config/open-models.json and PATH binary presence are inspected; a
        # credential env var names presence, never live authentication, and zero
        # anomalies here never means a live validation passed.
        route = root / "config/open-models.json"
        try:
            raw = route.read_text()
        except OSError:
            return  # absent config: legacy behavior, nothing to report
        try:
            cfg = json.loads(raw)
        except ValueError:
            self.fail("CONFIG", "config/open-models.json illeggibile", "correggere il file")
            return
        if not isinstance(cfg, dict):
            self.fail("CONFIG", "config/open-models.json non è un oggetto", "correggere il file")
            return

        print("Operational status: local health, no live certification")
        host = os.environ.get("AOS_MAIN_HOST")
        print(f"  main_host={host if host else 'unknown'}")

        open_cfg = cfg.get("open")
        open_cfg = open_cfg if isinstance(open_cfg, dict) else {}
        primary = open_cfg.get("primary")
        catalog = cfg.get("model_catalog")
        catalog = catalog if isinstance(catalog, dict) else {}
        entry = catalog.get(primary) if isinstance(primary, str) else {}
        entry = entry if isinstance(entry, dict) else {}
        benchmark = entry.get("benchmark") if isinstance(entry.get("benchmark"), dict) else {}
        source = benchmark.get("source") or open_cfg.get("source") or "no source"
        print(f"  benchmark winner={primary if primary else 'unknown'} "
              f"({benchmark.get('status', 'unknown')}, {source})")
        print(f"  cost class (primary)={entry.get('cost_class', 'unknown')}")
        cost_classes = sorted(set(m.get("cost_class") for m in catalog.values() if isinstance(m, dict) and m.get("cost_class")))
        print(f"  cost classes configured={', '.join(cost_classes) if cost_classes else 'none'}")

        executors = cfg.get("executors")
        executors = executors if isinstance(executors, dict) else {}
        runtime_status = executors.get("runtime_status")
        runtime_status = runtime_status if isinstance(runtime_status, dict) else {}
        viable = []
        for name, binary in (("claude-code", "claude"), ("codex-cli", "codex")):
            status = runtime_status.get(name)
            status = status if isinstance(status, dict) else {}
            installed = shutil.which(binary) is not None
            isolation = status.get("os_isolation", "none")
            verified = status.get("isolation_verified") is True
            evidence = status.get("isolation_evidence")
            evidence_problem = self.isolation_evidence_problem(root, evidence, name, isolation)
            evidence_present = evidence_problem is None
            if status.get("open_execution") is False:
                print(f"  runtime {name}: disabled; reason={status.get('reason') or 'disabled'}; installed={'yes' if installed else 'no'}")
            else:
                print(f"  runtime {name}: enabled by configuration; installed={'yes' if installed else 'no'}; "
                      f"os_isolation={isolation}; isolation "
                      f"{'verified by ' + evidence if verified and evidence_present else 'unverified'}")
                if verified and not evidence_present:
                    self.warn("RUNTIME", f"{name}: isolation_verified senza record di sonda valido ({evidence_problem})",
                              "eseguire bin/aos-isolation.py e indicare il record in isolation_evidence")
                if isolation == "seatbelt" and shutil.which("sandbox-exec") is None:
                    self.warn("RUNTIME", f"{name}: os_isolation=seatbelt ma sandbox-exec assente",
                              "il worker si rifiuta di partire su questo host finché non è disponibile")
                if installed:
                    viable.append(name)
        if viable:
            print("  viable enabled installed runtime=" + ", ".join(viable))
        else:
            self.fail("RUNTIME", "nessuna runtime open abilitata e installata",
                      "abilitare una runtime configurata e installare il relativo binario")

        mid_models = [identity for identity, model in catalog.items()
                      if isinstance(model, dict) and model.get("cost_class") == "MID"]
        mid = bool(mid_models)
        if mid:
            print("  MID candidate configured=" + ", ".join(sorted(mid_models)))
        if catalog and not mid:
            self.warn("MID", "nessun modello MID configurato",
                      "aggiungere un candidato MID con benchmark confrontabile o proseguire con primary/fallback/premium")

        providers = cfg.get("providers")
        providers = providers if isinstance(providers, dict) else {}
        for name, block in providers.items():
            if not isinstance(block, dict):
                continue
            env_name = block.get("api_key_env")
            cred = isinstance(env_name, str) and bool(os.environ.get(env_name))
            print(f"  provider {name}: configured (credential env {'set' if cred else 'not set'}); authentication unknown")

        premium = cfg.get("premium")
        premium = premium if isinstance(premium, dict) else {}
        reviewer = premium.get("reviewer")
        binaries = {"claude": "claude", "codex": "codex"}
        if reviewer:
            installed = isinstance(reviewer, str) and shutil.which(binaries.get(reviewer, reviewer)) is not None
            print(f"  reviewer={reviewer} installed={'presente' if installed else 'assente'}, authenticated=unknown")

        file_tools = [name for name in ("claude-code", "codex-cli")
                      if ((isinstance(executors.get(name), dict) and executors[name].get("file_tools") is True)
                          or (isinstance(runtime_status.get(name), dict)
                              and runtime_status[name].get("file_tools") is True))]
        if file_tools:
            print("  file-tools policy configured for " + ", ".join(file_tools) + "; live security unknown")
        permission_profile = executors.get("permission_profile")
        if isinstance(permission_profile, str) and permission_profile:
            print(f"  permission_profile configured={permission_profile}; live security unknown")

        context_policy = cfg.get("context_policy")
        context_policy = context_policy if isinstance(context_policy, dict) else {}
        defaults = context_policy.get("defaults")
        if isinstance(defaults, dict):
            print(f"  context policy: target={defaults.get('target_context')}, "
                  f"soft={defaults.get('soft_limit')}, hard={defaults.get('hard_limit')}")
            print("  context_budgets=configured")
        else:
            print("  context_budgets=unknown")

        cost_budgets = cfg.get("budgets")
        if not isinstance(cost_budgets, dict) or not cost_budgets or all(value is None for value in cost_budgets.values()):
            print("  cost_budgets=unset")
        else:
            invalid = [name for name, value in cost_budgets.items()
                       if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                                 or value < 0 or value != value or value in (float("inf"), float("-inf")))]
            if invalid:
                self.fail("CONFIG", "budgets contiene valori non validi", "correggere i valori budget (solo numeri finiti non negativi)")
                print("  cost_budgets=invalid")
            else:
                active = [name for name, value in cost_budgets.items() if value is not None]
                print(f"  cost_budgets=configured ({', '.join(active)})")

        learning = cfg.get("learning")
        if isinstance(learning, dict):
            enabled = bool(learning.get("enabled"))
            db = learning.get("database")
            exists = isinstance(db, str) and bool(db) and Path(db).expanduser().is_file()
            print(f"  learning={'enabled' if enabled else 'disabled'}; database={'existing' if exists else 'missing'}")
        else:
            print("  learning=not_configured; database=unknown")

    @staticmethod
    def isolation_evidence_problem(root, evidence, runtime, isolation):
        """None when the probe record proves this runtime's isolation, else why not.

        A file that merely exists proved nothing: any record, of another runtime or
        with leaks, made the line read "isolation verified".
        """
        if not isinstance(evidence, str) or not evidence:
            return "nessun record indicato"
        path = root / evidence
        try:
            path.resolve().relative_to(root.resolve())
        except (OSError, ValueError, RuntimeError):
            return "record fuori dalla root AOS"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return "record assente o illeggibile"
        if not isinstance(data, dict):
            return "record non è un oggetto"
        if data.get("runtime") != runtime:
            return f"record di un'altra runtime ({data.get('runtime')})"
        if data.get("os_isolation") != isolation:
            return f"record con os_isolation={data.get('os_isolation')}, configurato {isolation}"
        if data.get("isolated") is not True:
            return "il record non dichiara isolated=true"
        if data.get("leaks") != []:
            return "il record riporta fughe"
        return None

    def hook_status(self):
        # Read-only schema inspection of host Stop-hook declarations. The command is
        # read only to tell AOS's own hooks from third-party ones (gstack, Impeccable)
        # and is never printed nor executed. AOS ships no Stop hook today: calling a
        # third-party hook "configurato" read as if AOS's were in place.
        for name, path in (("codex", Path.home() / ".codex/hooks.json"),
                           ("claude", Path.home() / ".claude/settings.json")):
            ours = theirs = 0
            try:
                data = json.loads(path.read_text())
                hooks = data.get("hooks") if isinstance(data, dict) else None
                stop = hooks.get("Stop") if isinstance(hooks, dict) else None
                for entry in stop if isinstance(stop, list) else []:
                    inner = entry.get("hooks") if isinstance(entry, dict) else None
                    for hook in inner if isinstance(inner, list) else [entry]:
                        command = hook.get("command") if isinstance(hook, dict) else None
                        if isinstance(command, str) and "/skills/aos/" in command:
                            ours += 1
                        else:
                            theirs += 1
            except (OSError, ValueError):
                pass
            if not ours and not theirs:
                print(f"Hook Stop ({name}): non configurato")
            else:
                aos = f"{ours} hook AOS" if ours else "nessun hook AOS"
                print(f"Hook Stop ({name}): {aos}; {theirs} hook di terzi")
        print("Hook runtime: il protocollo richiede un test live separato per la validazione")

    def run(self, codex_root, claude_root):
        print(f"Python {sys.version.split()[0]}: {sys.executable}; sola verifica locale")
        if sys.version_info < (3, 9):
            self.fail("PREREQUISITO", "Python < 3.9", "usare Python 3.9 o successivo")
        if not self.bash:
            self.fail("PREREQUISITO", "bash assente dal PATH", "rendere disponibile bash per la verifica sintattica")
        self.link(codex_root, claude_root)
        maintained = self.manifest(claude_root)
        for relative in sorted(maintained):
            raw = self.read(claude_root, relative)
            if raw is not None:
                self.check_file(claude_root, relative, raw)
        self.distribution(claude_root)
        self.version_consistency(claude_root)
        self.provider_privacy(claude_root)
        self.tmp_weight(claude_root)
        self.backups()
        self.operational_status(claude_root)
        self.hook_status()
        for root in (codex_root, claude_root):
            router = root.parent / "skill-library"
            try:
                valid = router.is_symlink() and router.resolve() == (root / "catalog/skill-library").resolve() and (router / "SKILL.md").is_file()
            except (OSError, RuntimeError):
                valid = False
            if not valid:
                self.fail("ROUTER", str(router), "ripristinare il link alla root AOS/catalog/skill-library con l'installer")
        runtime = ", ".join(f"{name}={'presente' if shutil.which(name) else 'assente'}" for name in ("rtk", "claude", "codex"))
        print("Open Executor: runtime separato dal modello; harness Claude Code con soli strumenti file, senza shell; l'host esegue i check.")
        print("Frontmatter: presenza campi; sintassi YAML completa non verificata (usare quick_validate).")
        print(f"CLI opzionali (solo PATH): {runtime}. Autenticazione e backend non verificati.")
        print("Esito locale: sola verifica locale,nessuna certificazione live.")
        print(f"{'ERRORE' if self.errors else 'OK'}: {len(maintained)} file mantenuti in {claude_root}, "
              f"{self.errors} anomalie, {self.warnings} avvisi; nessuna modifica.")
        return 1 if self.errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".agents/skills/aos")
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude/skills/aos")
    args = parser.parse_args()
    return Doctor().run(args.codex_root.expanduser(), args.claude_root.expanduser())


if __name__ == "__main__":
    raise SystemExit(main())
