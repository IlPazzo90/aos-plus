#!/usr/bin/env python3
"""Read-only local AOS health checks (Python 3.9+).

Frontmatter checks required fields only, not complete YAML syntax.
"""
import argparse
import hashlib
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
        self.bash = shutil.which("bash")

    def fail(self, code, subject, remedy):
        self.errors += 1
        print(f"{code}: {subject} — {remedy}")

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
        candidates += re.findall(r"`((?:references|bin|catalog)/[A-Za-z0-9_./-]+)`", content)
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
            base = root if str(path).startswith(("references/", "bin/", "catalog/")) else (root / relative).parent
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
                            self.fail("CATALOGO", f"{root}: {entry['name']}", "aggiornare il catalogo: destinazione locale assente")
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

    def run(self, roots):
        print(f"Python {sys.version.split()[0]}: {sys.executable}; sola verifica locale")
        if sys.version_info < (3, 9):
            self.fail("PREREQUISITO", "Python < 3.9", "usare Python 3.9 o successivo")
        if not self.bash:
            self.fail("PREREQUISITO", "bash assente dal PATH", "rendere disponibile bash per la verifica sintattica")
        manifests = [self.manifest(root) for root in roots]
        if manifests[0] != manifests[1]:
            self.fail("MANIFEST", "elenchi diversi fra host", "sincronizzare le versioni con l'installer AOS")
        maintained = set.union(*manifests)
        hashes = []
        for root in roots:
            digest = {}
            for relative in sorted(maintained):
                raw = self.read(root, relative)
                if raw is not None:
                    digest[relative] = hashlib.sha256(raw).hexdigest()
                    self.check_file(root, relative, raw)
            hashes.append(digest)
            router = root.parent / "skill-library"
            try:
                valid = router.is_symlink() and router.resolve() == (root / "catalog/skill-library").resolve() and (router / "SKILL.md").is_file()
            except (OSError, RuntimeError):
                valid = False
            if not valid:
                self.fail("ROUTER", str(router), "ripristinare il link alla root AOS/catalog/skill-library con l'installer")
        for relative in sorted(hashes[0].keys() & hashes[1].keys()):
            if hashes[0][relative] != hashes[1][relative]:
                self.fail("HASH", relative, "confrontare le due copie e sincronizzare dalla sorgente autorevole")
        runtime = ", ".join(f"{name}={'presente' if shutil.which(name) else 'assente'}" for name in ("rtk", "claude", "codex"))
        print("Frontmatter: presenza campi; sintassi YAML completa non verificata (usare quick_validate).")
        print(f"CLI opzionali (solo PATH): {runtime}. Autenticazione e backend non verificati.")
        print(f"{'ERRORE' if self.errors else 'OK'}: {len(maintained)} file mantenuti, {self.errors} anomalie; nessuna modifica.")
        return 1 if self.errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".agents/skills/aos")
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude/skills/aos")
    args = parser.parse_args()
    return Doctor().run([args.codex_root.expanduser(), args.claude_root.expanduser()])


if __name__ == "__main__":
    raise SystemExit(main())
