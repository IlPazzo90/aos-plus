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

    def test_pytest_collection_limits_keep_the_python_minimum_bar(self):
        # A real test file does not prove this command runs it: addopts can exclude it.
        output = self.profile({"pytest.ini": "[pytest]\naddopts = --ignore=tests\n",
                               "tests/test_app.py": "import pytest\ndef test_app(): assert True\n",
                               "app.py": ""})
        self.assertIn("-m pytest", output)
        self.assertIn("limita la raccolta", output)
        self.assertIn("py_compile", output)

    def test_conftest_collect_ignore_keeps_the_python_minimum_bar(self):
        # collect_ignore lives in conftest.py, not in the ini files.
        output = self.profile({"pytest.ini": "[pytest]\n",
                               "conftest.py": 'collect_ignore = ["tests/test_app.py"]\n',
                               "tests/test_app.py": "import pytest\ndef test_app(): assert True\n",
                               "app.py": ""})
        self.assertIn("limita la raccolta", output)
        self.assertIn("py_compile", output)

    def test_annotated_collect_ignore_keeps_the_python_minimum_bar(self):
        # The same exclusion, written with a type annotation.
        output = self.profile({"pytest.ini": "[pytest]\n",
                               "conftest.py": 'collect_ignore: list[str] = ["tests/test_app.py"]\n',
                               "tests/test_app.py": "import pytest\ndef test_app(): assert True\n",
                               "app.py": ""})
        self.assertIn("limita la raccolta", output)
        self.assertIn("py_compile", output)

    def test_multiline_toml_value_does_not_hide_addopts(self):
        # A bracketed line inside a multiline string is not a new TOML table.
        output = self.profile({"pyproject.toml": '[tool.pytest.ini_options]\n'
                                                 'pythonpath = """\n[src]\n"""\n'
                                                 'addopts = "--ignore=tests"\n\n'
                                                 '[project]\nname = "example"\nversion = "0.1"\n',
                               "tests/test_app.py": "import pytest\ndef test_app(): assert True\n",
                               "app.py": ""})
        self.assertIn("limita la raccolta", output)
        self.assertIn("py_compile", output)

    def test_setup_cfg_tool_pytest_section_is_not_shadowed(self):
        # pytest reads [tool:pytest] from setup.cfg; an empty [pytest] must not hide it.
        output = self.profile({"setup.cfg": "[tool:pytest]\naddopts = --ignore=tests\n[pytest]\n",
                               "tests/test_app.py": "import pytest\ndef test_app(): assert True\n",
                               "app.py": ""})
        self.assertIn("limita la raccolta", output)
        self.assertIn("py_compile", output)

    def test_quoted_toml_table_is_still_the_pytest_table(self):
        # ["tool".pytest.ini_options] is the same table, spelled differently.
        output = self.profile({"pyproject.toml": '["tool".pytest.ini_options]\naddopts = "--ignore=tests"\n',
                               "tests/test_app.py": "import pytest\ndef test_app(): assert True\n",
                               "app.py": ""})
        self.assertIn("limita la raccolta", output)
        self.assertIn("py_compile", output)

    def test_unrelated_section_is_not_a_pytest_restriction(self):
        # testpaths under [unrelated] says nothing about what pytest collects.
        output = self.profile({"setup.cfg": "[tool:pytest]\n[unrelated]\ntestpaths = ignored\n",
                               "tests/test_app.py": "import pytest\ndef test_app(): assert True\n",
                               "app.py": ""})
        self.assertIn("-m pytest", output)
        self.assertNotIn("limita la raccolta", output)
        # The bar stays either way for pytest; what must not appear is the claim that
        # this configuration restricts collection.
        self.assertIn("nessuno ha verificato", output)

    def test_pytest_never_removes_the_minimum_bar(self):
        # What pytest collects depends on options this reader cannot enumerate, so a
        # pytest project keeps the bar whatever the configuration says.
        for name, files in [
            ("python_files", {"pyproject.toml": '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n'}),
            ("clean", {"pytest.ini": "[pytest]\n"}),
        ]:
            with self.subTest(config=name):
                output = self.profile({**files, "app.py": "",
                                       "tests/test_app.py": "import pytest\ndef test_app(): assert True\n"})
                self.assertIn("-m pytest", output)
                self.assertIn("py_compile", output)

    def test_python_runner_replaces_the_minimum_bar(self):
        output = self.profile({"tests/test_app.py": "import unittest\nclass Example(unittest.TestCase): pass\n"})
        self.assertIn("-m unittest discover -s tests", output)
        self.assertNotIn("py_compile", output)

    def test_the_profile_states_which_aos_is_loaded_and_whether_the_hosts_agree(self):
        # The two host copies drift in silence; aos-doctor could always see it, but it
        # only ran when someone thought to ask. Assert the block and its two claims,
        # never the verdict: whether they are aligned right now is machine state.
        output = self.profile({"app.py": ""})
        self.assertIn("--- AOS ---", output)
        self.assertIn("versione caricata:", output)
        self.assertRegex(output, r"copie Claude/Codex: (allineate|DIVERGONO|non confrontate)")

    def test_the_profile_never_promises_that_a_git_push_cannot_deploy(self):
        # It used to say the push does NOT deploy. With Vercel's Git integration every
        # push to the production branch goes live, so that sentence sent intermediate
        # states to production while the agent believed they stayed local.
        # Anchored to the invariant, not to one wording: the line must point at the Git
        # integration instead of denying it. The public distribution phrases it its own
        # way, and a test that pinned this copy's sentence would fail there for no reason.
        output = self.profile({"vercel.json": "{}", "package.json": json.dumps({"scripts": {}})})
        self.assertIn("vercel", output.lower())
        self.assertNotIn("NON rilascia", output)
        self.assertIn("ntegrazione Git", output)

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
