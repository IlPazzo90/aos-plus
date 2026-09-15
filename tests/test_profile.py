"""Integration checks for evidence-based project profiling."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROFILE = Path(__file__).resolve().parents[1] / "bin/aos-profile.sh"


class ProfileTests(unittest.TestCase):
    def profile(self, files, venv=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            if venv:
                binary = root / ".venv/bin/python"
                binary.parent.mkdir(parents=True)
                binary.symlink_to(sys.executable)
            result = subprocess.run(["/bin/bash", str(PROFILE), tmp], text=True,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "executed").exists(), "Profiler executed project code")
            return result.stdout

    def test_unittest_detected_without_executing_tests(self):
        output = self.profile({"tests/test_app.py": "import unittest\nopen('executed', 'w').close()\nclass Example(unittest.TestCase): pass\n"})
        self.assertIn("-m unittest discover -s tests", output)
        self.assertNotIn("-m pytest", output)
        self.assertNotIn("Nessun test python", output)

    def test_empty_test_folder_does_not_imply_pytest(self):
        output = self.profile({"tests/placeholder": "", "app.py": ""})
        self.assertNotIn("-m pytest", output)

    def test_pytest_configuration_and_local_interpreter(self):
        output = self.profile({"pytest.ini": "[pytest]\n", "app.py": ""}, venv=True)
        self.assertIn(".venv/bin/python -m pytest", output)
        self.assertIn("interprete", output.lower())

    def test_pytest_import_evidence(self):
        output = self.profile({"tests/test_app.py": "import pytest\n"})
        self.assertIn("-m pytest", output)

    def test_tox_alone_does_not_imply_pytest(self):
        output = self.profile({"tox.ini": "[tox]\n", "app.py": ""})
        self.assertNotIn("-m pytest", output)

    def test_make_test_does_not_claim_python_has_no_tests(self):
        output = self.profile({"Makefile": "test:\n\techo checked\n", "app.py": ""})
        self.assertIn("make test", output)
        self.assertNotIn("Nessun test python", output)

    def test_non_python_test_command_keeps_the_python_minimum_bar(self):
        # A declared test command is not evidence that it exercises the Python here.
        for name, files in [("make", {"Makefile": "test:\n\techo checked\n", "app.py": ""}),
                            ("npm", {"package.json": json.dumps({"scripts": {"test": "jest"}}), "app.py": ""}),
                            ("scripts", {"scripts/verifica-deploy.sh": "echo ok\n", "app.py": ""})]:
            with self.subTest(runner=name):
                output = self.profile(files)
                self.assertIn("py_compile", output)

    def test_pytest_config_alone_keeps_the_python_minimum_bar(self):
        # pytest.ini proves a configuration, not that any Python test exists to run.
        output = self.profile({"pytest.ini": "[pytest]\n",
                               "package.json": json.dumps({"scripts": {"test": "jest"}}),
                               "app.py": ""})
        self.assertIn("-m pytest", output)
        self.assertIn("py_compile", output)

    def test_python_runner_replaces_the_minimum_bar(self):
        output = self.profile({"tests/test_app.py": "import unittest\nclass Example(unittest.TestCase): pass\n"})
        self.assertIn("-m unittest discover -s tests", output)
        self.assertNotIn("py_compile", output)

    def test_lockfiles_select_matching_commands(self):
        for lock, manager in [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lock", "bun"), ("bun.lockb", "bun")]:
            with self.subTest(lock=lock):
                output = self.profile({lock: "", "package.json": json.dumps({"scripts": {"test": "node test.js", "build": "build-tool"}})})
                self.assertIn(manager + " run build", output)
                self.assertNotIn("  npm run", output)

    def test_package_manager_declaration_overrides_stale_lock(self):
        output = self.profile({"yarn.lock": "", "package.json": json.dumps({"packageManager": "pnpm@9.0.0", "scripts": {"test": "node test.js"}})})
        self.assertIn("pnpm run test", output)

    def test_declared_pytest_dependency(self):
        output = self.profile({"requirements-dev.txt": "pytest>=8\n", "app.py": ""})
        self.assertIn("-m pytest", output)

    def test_pyproject_optional_pytest_dependency(self):
        output = self.profile({"pyproject.toml": '[project.optional-dependencies]\ntest = ["pytest>=8"]\n'})
        self.assertIn("-m pytest", output)

    def test_nested_unittest_suffix_has_matching_discovery_pattern(self):
        output = self.profile({"src/checks/widget_test.py": "from unittest import TestCase\n"})
        self.assertIn("-m unittest discover -s src/checks -p '*_test.py'", output)

    def test_broken_virtualenv_falls_back_to_available_interpreter(self):
        output = self.profile({".venv/bin/python": "not executable", "tests/test_app.py": "import unittest\n"})
        self.assertIn("-m unittest discover", output)
        self.assertNotIn("./.venv/bin/python -m", output)

    def test_symlink_launcher_finds_runner_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_app.py").write_text("import unittest\n")
            launcher = root / "aos-profile"
            launcher.symlink_to(PROFILE)
            result = subprocess.run(["/bin/bash", str(launcher), tmp], text=True,
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("-m unittest discover", result.stdout)

    def test_risk_signals_do_not_assign_risk_from_stack(self):
        output = self.profile({"plugin.php": "<?php add_action('init', 'example');", "supabase/config.toml": ""})
        self.assertIn("WordPress", output)
        self.assertIn("Supabase", output)
        self.assertNotIn("rischio >= HIGH", output)
        self.assertNotIn("ogni modifica di schema o delete e' CRITICAL", output)

    def test_n8n_workflow_is_reported_without_asserting_severity(self):
        # Recognizing a workflow file says nothing about whether it is active.
        output = self.profile({"Editorial Flow.json": json.dumps({"nodes": [], "connections": {}})})
        self.assertIn("workflow n8n", output)
        self.assertIn("Editorial Flow.json", output)
        self.assertNotIn("HIGH", output)
        # Nor may it assert what the nodes do: a Manual Trigger and a Set publish nothing.
        self.assertNotIn("pubblica contenuti o manda messaggi veri", output)
        self.assertIn("leggi i nodi", output)


if __name__ == "__main__":
    unittest.main()
