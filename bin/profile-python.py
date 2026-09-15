#!/usr/bin/env python3
"""Read runner evidence without importing or executing project code."""
import ast
import configparser
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


NARROWING = ("addopts", "testpaths", "norecursedirs")


def narrows_collection(name, text, fallback):
    """True when the file's pytest section carries an option that limits collection.

    Parsed, not pattern-matched across the file: a `[` inside a multiline TOML string
    is not a new table, and an unrelated section's testpaths is not pytest's. Reading
    these options is not emulating collection — it only says the test files found may
    not be the ones that run. `fallback` is used when the file cannot be parsed.
    """
    if name == "pyproject.toml":
        try:
            import tomllib
            options = tomllib.loads(text).get("tool", {}).get("pytest", {}).get("ini_options")
        except (ImportError, ValueError, TypeError, AttributeError):
            return fallback(text)
        if options is None:
            return False
        return any(key in options for key in NARROWING)
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        # An ini file with no section header at all: the whole file is the section.
        return fallback(text)
    for candidate in ("pytest", "tool:pytest"):
        if parser.has_section(candidate):
            return any(parser.has_option(candidate, key) for key in NARROWING)
    return False


def main():
    interpreter = sys.argv[1]
    if not interpreter:
        return
    pytest_evidence = []
    narrowing = re.compile(r"(?m)^\s*(" + "|".join(NARROWING) + r")\s*[=:]")
    fallback = lambda text: bool(narrowing.search(text))
    header = re.compile(r"(?m)^\s*\[(?:tool\.pytest(?:\.ini_options)?|pytest|tool:pytest)\]")
    collection_limited = False
    if Path("pytest.ini").is_file():
        pytest_evidence.append("pytest.ini")
        collection_limited |= narrows_collection("pytest.ini", read(Path("pytest.ini")), fallback)
    for name in ("pyproject.toml", "setup.cfg", "tox.ini"):
        text = read(Path(name))
        if header.search(text):
            pytest_evidence.append(name)
            collection_limited |= narrows_collection(name, text, fallback)
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
            # collect_ignore lives in conftest.py, not in the ini files above.
            # `collect_ignore: list[str] = [...]` is the same exclusion, annotated.
            if name == "conftest.py" and re.search(r"(?m)^\s*collect_ignore(_glob)?\s*(:[^=\n]*)?=", read(relative / name)):
                collection_limited = True
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
        # Found test files do not prove this command runs them: addopts, testpaths,
        # norecursedirs or a conftest collect_ignore can exclude exactly what was found.
        proven = "limited"
    print("PYTHON_TESTS_EVIDENCE=" + proven)
    if pytest_evidence:
        print("  {} -m pytest   (evidenza: {}; disponibilita pytest non verificata)".format(command, ", ".join(pytest_evidence[:3])))
        if collection_limited:
            print("    la configurazione pytest limita la raccolta (addopts, testpaths,")
            print("    norecursedirs o collect_ignore in conftest.py): i test trovati")
            print("    potrebbero non essere quelli eseguiti")
    elif unittest_roots:
        # Run discovery at each evidenced directory: Python does not recurse
        # into nested non-package directories on all supported versions.
        for root, pattern in sorted(unittest_roots):
            print("  {} -m unittest discover -s {} -p {} -v   (import unittest nei test)".format(command, shlex.quote(root), shlex.quote(pattern)))


if __name__ == "__main__":
    main()
