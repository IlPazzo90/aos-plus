"""Read-only installation doctor behavior against a temporary linked installation."""
import json
import os
import shutil
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
        codex, claude = self.roots
        for name, content in files.items():
            target = claude / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        # One installation: the Codex path is a link to it, as for every shared skill.
        codex.parent.mkdir()
        codex.symlink_to(claude)
        for root in self.roots:
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

    def test_backticked_evals_reference_must_exist(self):
        # A README can promise evals/scenarios.json while the installer never ships it.
        for root in self.roots:
            (root / "SKILL.md").write_text(
                "---\nname: aos\ndescription: test\n---\n"
                "Read `references/check.md`. Run `evals/scenarios.json` for repeatable cases.\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("RIFERIMENTO", result.stdout)
        self.assertIn("evals/scenarios.json", result.stdout)

    def test_detects_missing_files(self):
        (self.roots[1] / "references/check.md").unlink()
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("MANCANTE", result.stdout)

    def test_a_real_directory_on_the_codex_path_is_a_copy_even_when_identical(self):
        # The Codex clone sat on 1.14.0 with ten dirty files while the doctor compared
        # hashes: two copies were the defect, and an identical copy is still a copy.
        codex, claude = self.roots
        codex.unlink()
        shutil.copytree(claude, codex)
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("COPIA", result.stdout)
        self.assertIn("--host codex", result.stdout)
        self.assertNotIn("HASH", result.stdout)

    def test_a_missing_or_misdirected_codex_link_is_reported(self):
        codex, claude = self.roots
        codex.unlink()
        self.assertIn("MANCANTE", self.run_doctor().stdout)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        codex.symlink_to(elsewhere)
        result = self.run_doctor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("MANCANTE", result.stdout)

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

    def test_a_stale_catalog_row_is_a_warning_not_a_broken_installation(self):
        # Forty CATALOGO rows once made every run exit 1 while both copies were identical.
        for root in self.roots:
            (root / "catalog/index.json").write_text(json.dumps([{"name": "gone", "path": str(self.base / "gone/SKILL.md")}]))
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AVVISO CATALOGO", result.stdout)
        self.assertIn("OK", result.stdout)

    def test_a_distribution_left_behind_is_a_warning_at_the_source(self):
        # 1.17.0 never reached the public edition: "same intervention" had no check.
        codex, claude = self.roots
        (claude / "VERSION").write_text("1.19.0\n")
        public = self.base / "public"
        public.mkdir()
        (public / "VERSION").write_text("1.16.0\n")
        (claude / "DISTRIBUTION").write_text(str(public) + "\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AVVISO DISTRIBUZIONE", result.stdout)
        self.assertIn("1.16.0", result.stdout)
        (public / "VERSION").write_text("1.19.0\n")
        self.assertNotIn("DISTRIBUZIONE", self.run_doctor().stdout)
        (claude / "DISTRIBUTION").write_text(str(self.base / "nowhere") + "\n")
        self.assertIn("checkout assente", self.run_doctor().stdout)
        # Bytes that are not text are a warning too, not a traceback (found by review).
        (claude / "DISTRIBUTION").write_bytes(b"\xff\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("file illeggibile", result.stdout)

    def test_a_heavy_tmp_is_a_warning_and_a_light_one_is_silent(self):
        # 936 MB of research sat in one clone's ignored tmp/ and nobody had counted it.
        codex, claude = self.roots
        (claude / "tmp").mkdir()
        with (claude / "tmp/dump.bin").open("wb") as stream:
            stream.truncate(250 * 1024 * 1024)  # sparse: size without the bytes
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AVVISO TMP", result.stdout)
        self.assertIn("250 MB", result.stdout)
        (claude / "tmp/dump.bin").write_bytes(b"small")
        self.assertNotIn("AVVISO TMP", self.run_doctor().stdout)

    def test_the_real_manifest_ships_every_test_file(self):
        # Two copies were reported aligned with 61 tests on one side and 56 on the other:
        # tests/ was outside the manifest, so the comparison could not see the drift.
        installer = (DOCTOR.parent / "aos-install.sh").read_text()
        manifest = installer.split('REQUIRED_FILES="', 1)[1].split('"', 1)[0].split()
        present = sorted(str(p.relative_to(DOCTOR.parent.parent)) for p in (DOCTOR.parent.parent / "tests").glob("test_*.py"))
        self.assertEqual(sorted(name for name in manifest if name.startswith("tests/")), present)

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
