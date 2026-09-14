"""Read-only installation doctor behavior against temporary host roots."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

DOCTOR = Path(__file__).resolve().parents[1] / "bin/aos-doctor.py"


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.roots = [self.base / host / "aos" for host in ("codex", "claude")]
        files = {
            "SKILL.md": "---\nname: aos\ndescription: test\n---\nRead `references/check.md`.\n",
            "references/check.md": "Checks.\n",
            "bin/check.sh": "#!/bin/bash\nprintf ok\n",
            "bin/check.py": "print('ok')\n",
            "catalog/index.json": "[]\n",
            "catalog/core.json": "[]\n",
            "catalog/skill-library/SKILL.md": "---\nname: skill-library\ndescription: router\n---\n",
        }
        files["bin/aos-install.sh"] = 'REQUIRED_FILES="' + "\n".join([*files, "bin/aos-install.sh"]) + '"\n'
        for root in self.roots:
            for name, content in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            (root.parent / "skill-library").symlink_to(root / "catalog/skill-library")

    def run_doctor(self, env=None):
        result = subprocess.run([sys.executable, str(DOCTOR), "--codex-root", str(self.roots[0]),
                                 "--claude-root", str(self.roots[1])],
                                capture_output=True, text=True, env=env)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_healthy_pair_is_read_only(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob("*") if p.is_file()}
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_detects_missing_and_different_files(self):
        (self.roots[0] / "references/check.md").unlink()
        (self.roots[1] / "bin/check.py").write_text("print('different')\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("MANCANTE", result.stdout)
        self.assertIn("HASH", result.stdout)

    def test_detects_syntax_even_when_copies_match(self):
        for root in self.roots:
            (root / "bin/check.py").write_text("def broken(\n")
            (root / "bin/check.sh").write_text("if then\n")
            (root / "catalog/core.json").write_text("{invalid")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        for name in ("bin/check.py", "bin/check.sh", "catalog/core.json"):
            self.assertIn("SINTASSI", result.stdout)
            self.assertIn(name, result.stdout)

    def test_detects_reference_router_and_catalog_targets(self):
        for root in self.roots:
            (root / "references/check.md").write_text("See [guide](missing.md).\n")
            (root / "catalog/index.json").write_text(json.dumps([{"name": "gone", "path": str(self.base / "gone/SKILL.md")}]))
        (self.roots[0].parent / "skill-library").unlink()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        for code in ("RIFERIMENTO", "ROUTER", "CATALOGO"):
            self.assertIn(code, result.stdout)

    def test_does_not_execute_installer_scripts_or_bash_env(self):
        marker = self.base / "executed"
        for root in self.roots:
            with (root / "bin/aos-install.sh").open("a") as stream:
                stream.write("touch " + str(marker) + "\n")
        hook = self.base / "hook"
        hook.write_text("touch " + str(marker) + "\n")
        env = dict(os.environ, BASH_ENV=str(hook))
        result = self.run_doctor(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_rejects_manifest_escape_without_disclosing_contents(self):
        secret = self.base / "secret"
        secret.write_text("do-not-print-this-secret")
        for root in self.roots:
            (root / "bin/aos-install.sh").write_text('REQUIRED_FILES="../../secret"\n')
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("MANIFEST", result.stdout)
        self.assertNotIn("do-not-print-this-secret", result.stdout + result.stderr)

    def test_markdown_angle_link_preserves_spaces(self):
        for root in self.roots:
            (root / "references/my guide.md").write_text("Guide.\n")
            (root / "references/check.md").write_text('[Guide](<my guide.md>) and [titled](<my guide.md> "Title")\n')
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_frontmatter_limit_is_explicit_and_no_ollama_check(self):
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sintassi YAML completa non verificata", result.stdout)
        self.assertNotIn("ollama", result.stdout.lower())

    def test_rejects_broken_skill_frontmatter(self):
        for root in self.roots:
            (root / "SKILL.md").write_text("---\nname: aos\n---\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("SINTASSI", result.stdout)

    def test_rejects_maintained_symlink_outside_root(self):
        secret = self.base / "private"
        secret.write_text("do-not-disclose")
        target = self.roots[0] / "bin/check.py"
        target.unlink()
        target.symlink_to(secret)
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("MANCANTE", result.stdout)
        self.assertNotIn("do-not-disclose", result.stdout + result.stderr)

    def test_missing_bash_is_unhealthy(self):
        result = self.run_doctor(dict(os.environ, PATH=""))
        self.assertEqual(result.returncode, 1)
        self.assertIn("PREREQUISITO", result.stdout)


if __name__ == "__main__":
    unittest.main()
