#!/usr/bin/env python3
"""Read runner evidence without importing or executing project code."""
import ast
import os
from pathlib import Path
import re
import shlex
import sys


def read(path):
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def main():
    interpreter = sys.argv[1]
    if not interpreter:
        return
    pytest_evidence = []
    # Options that decide what pytest collects. Reading them is not emulating
    # collection: it only says the found test files may not be the ones that run.
    narrowing = re.compile(r"(?m)^\s*(addopts|testpaths|norecursedirs|collect_ignore)\s*[=:]")
    collection_limited = False
    if Path("pytest.ini").is_file():
        pytest_evidence.append("pytest.ini")
        collection_limited = collection_limited or bool(narrowing.search(read(Path("pytest.ini"))))
    for name in ("pyproject.toml", "setup.cfg", "tox.ini"):
        text = read(Path(name))
        if re.search(r"(?m)^\s*\[(?:tool\.pytest(?:\.ini_options)?|pytest|tool:pytest)\]", text):
            pytest_evidence.append(name)
            collection_limited = collection_limited or bool(narrowing.search(text))
    for path in Path(".").glob("requirements*.txt"):
        if re.search(r"(?mi)^\s*pytest(?:\s|[<>=!~;\[]|$)", read(path)):
            pytest_evidence.append(str(path))
    try:
        import tomllib
        data = tomllib.loads(read(Path("pyproject.toml")))
        project = data.get("project", {})
        dependencies = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            dependencies.extend(group)
        for group in data.get("dependency-groups", {}).values():
            dependencies.extend(group)
        if any(isinstance(dep, str) and re.match(r"(?i)^pytest(?:\s|[<>=!~;\[]|$)", dep) for dep in dependencies):
            pytest_evidence.append("pyproject.toml dependencies")
    except (ImportError, ValueError, TypeError, AttributeError):
        # Python < 3.11 has no stdlib TOML parser; other evidence still applies.
        pass
    from_test_files = False
    unittest_roots = set()
    excluded ={"node_modules", ".git", "vendor", ".next", "dist", "build", ".venv", "venv", "__pycache__"}
    for directory, dirs, files in os.walk(".", followlinks=False):
        relative = Path(directory)
        dirs[:] = [d for d in dirs if d not in excluded and not (relative / d).is_symlink()]
        if len(relative.parts) >= 4:
            dirs[:] = []
        for name in files:
            if not (name.startswith("test_") or name.endswith("_test.py")) or not name.endswith(".py"):
                continue
            path = relative / name
            if path.is_symlink():
                continue
            try:
                tree = ast.parse(read(path))
            except (SyntaxError, ValueError):
                continue
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            if "pytest" in imports:
                pytest_evidence.append(str(path))
                from_test_files = True
            if "unittest" in imports:
                unittest_roots.add((str(relative), "test*.py" if name.startswith("test_") else "*_test.py"))
    command = shlex.quote(interpreter)
    # Config and dependencies prove a runner is configured; only test files prove
    # there is Python to run. The caller needs the difference to decide whether the
    # minimum bar still applies, so report it on a line it can strip.
    proven = "files" if (from_test_files or unittest_roots) else ("config" if pytest_evidence else "none")
    if proven == "files" and pytest_evidence and collection_limited:
        # Found test files do not prove this command runs them: addopts, testpaths
        # and friends can exclude exactly what was found.
        proven = "config"
    print("PYTHON_TESTS_EVIDENCE=" + proven)
    if pytest_evidence:
        print("  {} -m pytest   (evidenza: {}; disponibilita pytest non verificata)".format(command, ", ".join(pytest_evidence[:3])))
        if collection_limited:
            print("    la configurazione pytest limita la raccolta (addopts, testpaths,")
            print("    norecursedirs o collect_ignore): i test trovati potrebbero non essere eseguiti")
    elif unittest_roots:
        # Run discovery at each evidenced directory: Python does not recurse
        # into nested non-package directories on all supported versions.
        for root, pattern in sorted(unittest_roots):
            print("  {} -m unittest discover -s {} -p {} -v   (import unittest nei test)".format(command, shlex.quote(root), shlex.quote(pattern)))


if __name__ == "__main__":
    main()
