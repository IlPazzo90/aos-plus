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


def toml_pytest_options(text):
    """The [tool.pytest.ini_options] table, or None when it cannot be read.

    Parsed rather than pattern-matched: a `[`-looking line inside a multiline string
    is not a table, and a quoted key like ["tool".pytest.ini_options] is still that
    table. None means "unknown", never "no options".
    """
    try:
        import tomllib
        table = tomllib.loads(text).get("tool", {}).get("pytest", {}).get("ini_options")
    except (ImportError, ValueError, TypeError, AttributeError):
        return None
    return table if isinstance(table, dict) else None


def narrows_collection(text, fallback):
    """True when a pytest section carries an option that limits collection.

    Any candidate section counts. pytest reads `[tool:pytest]` from setup.cfg and
    `[pytest]` from pytest.ini and tox.ini, and picking one of them per file only
    risks reading the section pytest ignores: a doubt raised on the wrong section
    keeps the minimum bar, missing the right one removes it.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        # An ini file with no section header at all: the whole file is the section.
        return fallback(text)
    return any(parser.has_option(candidate, key)
               for candidate in ("pytest", "tool:pytest") if parser.has_section(candidate)
               for key in NARROWING)


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
        collection_limited |= narrows_collection(read(Path("pytest.ini")), fallback)
    # pyproject.toml is asked of the TOML parser first: the header regex cannot see a
    # quoted table name, and a config it cannot read is not a config without options.
    text = read(Path("pyproject.toml"))
    options = toml_pytest_options(text)
    if options is not None or header.search(text):
        pytest_evidence.append("pyproject.toml")
        collection_limited |= (any(key in options for key in NARROWING)
                               if options is not None else fallback(text))
    for name in ("setup.cfg", "tox.ini"):
        text = read(Path(name))
        if header.search(text):
            pytest_evidence.append(name)
            collection_limited |= narrows_collection(text, fallback)
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
    # Only a unittest discovery command names the files it will run, so only that
    # proves the Python is covered. What pytest collects depends on options this
    # reader cannot enumerate — python_files, markers, a conftest, a plugin — and
    # settling that needs pytest itself, which this reader never runs. Five review
    # rounds each found another option missing from the list; the list was the wrong
    # answer. Absence of a recognized restriction is not proof, so pytest evidence
    # never removes the minimum bar: it only changes how the doubt is phrased.
    if pytest_evidence:
        proven = "limited" if collection_limited else ("unverified" if (from_test_files or unittest_roots) else "config")
    elif unittest_roots:
        proven = "files"
    else:
        proven = "none"
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
