"""The installer removes what left the manifest and touches nothing else."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "bin/aos-install.sh"


class InstallTests(unittest.TestCase):
    def test_a_file_dropped_from_the_manifest_is_removed_and_unrelated_files_survive(self):
        # 1.17.0 dropped bin/codex-hook-adapter.py; copying alone left it alive on the
        # other host, invisible to a doctor that compares maintained files only.
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / ".claude/skills/aos"
            (target / "bin").mkdir(parents=True)
            (target / "logs").mkdir()
            (target / "bin/aos-install.sh").write_text('REQUIRED_FILES="SKILL.md\nbin/stale.py"\n')
            (target / "bin/stale.py").write_text("print('old')\n")
            (target / "SKILL.md").write_text("old\n")
            (target / "logs/run.log").write_text("keep me\n")
            # A test the previous manifest never named (tests/ was outside it before
            # 1.17.0) importing a module that is about to be removed: keeping it breaks
            # discovery on the upgraded host.
            (target / "tests").mkdir()
            (target / "tests/test_stale.py").write_text("import stale\n")
            result = subprocess.run(["/bin/bash", str(INSTALLER), "--host", "claude", "--from", str(ROOT)],
                                    capture_output=True, text=True, env=dict(os.environ, HOME=home), timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((target / "bin/stale.py").exists(), "stale maintained file survived the install")
            self.assertTrue((target / "logs/run.log").exists(), "unmanaged file was removed")
            self.assertFalse((target / "tests/test_stale.py").exists(), "orphan test survived the install")
            self.assertTrue((target / "tests/test_install.py").exists())
            self.assertEqual((target / "VERSION").read_text(), (ROOT / "VERSION").read_text())
            self.assertIn("Rimuovo (uscito dal manifesto): bin/stale.py", result.stdout)

    def test_the_prune_never_removes_a_file_still_in_the_manifest(self):
        # The first version matched names against a newline-separated list and would
        # have removed every maintained file but the ones on the manifest's first line.
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / ".claude/skills/aos"
            (target / "bin").mkdir(parents=True)
            manifest = INSTALLER.read_text().split('REQUIRED_FILES="', 1)[1].split('"', 1)[0]
            (target / "bin/aos-install.sh").write_text('REQUIRED_FILES="' + manifest + '"\n')
            result = subprocess.run(["/bin/bash", str(INSTALLER), "--host", "claude", "--from", str(ROOT), "-n"],
                                    capture_output=True, text=True, env=dict(os.environ, HOME=home), timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Rimuovo", result.stdout)


    def install(self, home, *args):
        return subprocess.run(["/bin/bash", str(INSTALLER), *args], capture_output=True, text=True,
                              env=dict(os.environ, HOME=home), timeout=60)

    def test_the_codex_host_is_a_link_and_a_copy_found_there_is_backed_up_and_replaced(self):
        # The Codex clone sat on 1.14.0 with ten dirty files while the installer copied
        # files around it. One installation, linked: nothing left to drift.
        with tempfile.TemporaryDirectory() as home:
            claude = Path(home) / ".claude/skills/aos"
            self.assertEqual(self.install(home, "--host", "claude", "--from", str(ROOT)).returncode, 0)
            codex = Path(home) / ".agents/skills/aos"
            codex.mkdir(parents=True)
            (codex / "VERSION").write_text("1.14.0\n")
            (codex / "local-note.txt").write_text("keep a copy of me\n")
            self.assertNotEqual(self.install(home, "--host", "codex").returncode, 0, "verify must see the copy")
            result = self.install(home, "--host", "codex", "--link")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(codex.is_symlink())
            self.assertEqual(codex.resolve(), claude.resolve())
            self.assertEqual((codex / "VERSION").read_text(), (ROOT / "VERSION").read_text())
            backups = list((Path(home) / ".agents/backups").glob("aos-1.14.0-*"))
            self.assertEqual(len(backups), 1, "the copy was not backed up before removal")
            self.assertEqual((backups[0] / "local-note.txt").read_text(), "keep a copy of me\n")
            router = Path(home) / ".agents/skills/skill-library"
            self.assertTrue(router.is_symlink())
            self.assertTrue((router / "SKILL.md").is_file())
            # Idempotent, and verify sees the link.
            self.assertEqual(self.install(home, "--host", "codex", "--link").returncode, 0)
            self.assertEqual(self.install(home, "--host", "codex", "--from", str(ROOT)).returncode, 2, "--from must be refused for the link")
            self.assertEqual(self.install(home, "--host", "claude", "--link").returncode, 2, "--link is Codex-only")
            self.assertEqual(self.install(home, "--host", "codex", "--link", "--dry-run").returncode, 0)
            verify = self.install(home, "--host", "codex")
            self.assertIn("ok   " + str(codex), verify.stdout)
            self.assertEqual(self.install(home, "--host", "claude").returncode, 0)

    def test_a_dry_run_of_link_leaves_the_tree_byte_for_byte_untouched(self):
        # The review claimed the dry run removed and created before checking DRY; every
        # mutation goes through run(), which only prints. Settle it with a listing.
        with tempfile.TemporaryDirectory() as home:
            claude = Path(home) / ".claude/skills/aos"
            self.assertEqual(self.install(home, "--host", "claude", "--from", str(ROOT)).returncode, 0)
            codex = Path(home) / ".agents/skills/aos"
            codex.mkdir(parents=True)
            (codex / "sentinel").write_text("keep\n")
            def listing():
                return sorted((str(p.relative_to(home)), p.is_symlink(), p.stat().st_mtime_ns if p.is_file() else None)
                              for p in Path(home).rglob("*") if not str(p).startswith(str(claude)))
            before = listing()
            result = self.install(home, "--host", "codex", "--link", "--dry-run")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[dry-run]", result.stdout)
            self.assertEqual(listing(), before)
            self.assertFalse(codex.is_symlink())
            self.assertFalse((Path(home) / ".agents/backups").exists())

    def test_link_repairs_a_regular_file_found_on_the_codex_path(self):
        # "Creates or repairs the link": a stray file there made ln fail and left it.
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.install(home, "--host", "claude", "--from", str(ROOT)).returncode, 0)
            codex = Path(home) / ".agents/skills/aos"
            codex.parent.mkdir(parents=True)
            codex.write_text("stray\n")
            result = self.install(home, "--host", "codex", "--link")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(codex.is_symlink())
            moved = list((Path(home) / ".agents/backups").glob("aos-file-*"))
            self.assertEqual([m.read_text() for m in moved], ["stray\n"])

    def test_uninstalling_the_codex_host_refuses_a_real_directory(self):
        # "Removes only the link" means only the link: a real directory there may be a
        # clone with uncommitted work, and the first version backed it up and deleted it.
        with tempfile.TemporaryDirectory() as home:
            codex = Path(home) / ".agents/skills/aos"
            codex.mkdir(parents=True)
            (codex / "sentinel").write_text("keep\n")
            result = self.install(home, "--host", "codex", "--uninstall")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--link", result.stdout)
            self.assertEqual((codex / "sentinel").read_text(), "keep\n")
            self.assertFalse((Path(home) / ".agents/backups").exists())

    def test_uninstalling_the_codex_host_removes_the_link_and_keeps_the_installation(self):
        with tempfile.TemporaryDirectory() as home:
            claude = Path(home) / ".claude/skills/aos"
            self.assertEqual(self.install(home, "--host", "claude", "--from", str(ROOT)).returncode, 0)
            self.assertEqual(self.install(home, "--host", "codex", "--link").returncode, 0)
            result = self.install(home, "--host", "codex", "--uninstall")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            codex = Path(home) / ".agents/skills/aos"
            self.assertFalse(codex.exists() or codex.is_symlink())
            self.assertFalse((Path(home) / ".agents/skills/skill-library").is_symlink())
            self.assertTrue((claude / "SKILL.md").is_file(), "uninstalling the link removed the installation")
            self.assertNotEqual(self.install(home, "--host", "codex").returncode, 0, "verify must fail without the link")


    def test_a_dangling_codex_link_with_no_installation_fails_verification(self):
        # Both `cd` failed and "" = "" passed the link check.
        with tempfile.TemporaryDirectory() as home:
            codex = Path(home) / ".agents/skills/aos"
            codex.parent.mkdir(parents=True)
            codex.symlink_to(Path(home) / ".claude/skills/aos")
            result = self.install(home, "--host", "codex")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("ok   " + str(codex), result.stdout)

    def test_uninstalling_the_claude_host_removes_the_codex_link_that_pointed_at_it(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.install(home, "--host", "claude", "--from", str(ROOT)).returncode, 0)
            self.assertEqual(self.install(home, "--host", "codex", "--link").returncode, 0)
            result = self.install(home, "--host", "claude", "--uninstall")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            skills = Path(home) / ".agents/skills"
            self.assertFalse((skills / "aos").is_symlink(), "dangling Codex link left behind")
            self.assertFalse((skills / "skill-library").is_symlink(), "dangling Codex router left behind")

    def test_uninstalling_the_claude_host_never_touches_a_real_codex_directory(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.install(home, "--host", "claude", "--from", str(ROOT)).returncode, 0)
            codex = Path(home) / ".agents/skills/aos"
            codex.mkdir(parents=True)
            (codex / "sentinel").write_text("keep\n")
            self.assertEqual(self.install(home, "--host", "claude", "--uninstall").returncode, 0)
            self.assertEqual((codex / "sentinel").read_text(), "keep\n")


if __name__ == "__main__":
    unittest.main()
