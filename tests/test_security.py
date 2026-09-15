"""The mechanical security pass must not report its own source."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/aos-security.sh"


class SecurityTests(unittest.TestCase):
    def test_the_scanner_never_flags_itself(self):
        # The script contains the very patterns it hunts. On the AOS repository it
        # produced three fixed signals per commit, all pointing at its own lines.
        result = subprocess.run(["/bin/bash", str(SCRIPT), str(ROOT)], capture_output=True, text=True, timeout=60)
        self.assertNotIn("aos-security.sh:", result.stdout)

    def test_self_exclusion_survives_a_relative_script_path_and_a_scanned_directory(self):
        # SELF is resolved before the cd into the scanned directory, or a relative
        # script path would be looked up in the wrong place and the exclusion silently lost.
        result = subprocess.run(["/bin/bash", "bin/aos-security.sh", str(ROOT)], cwd=ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertNotIn("aos-security.sh:", result.stdout)
        self.assertIn("=== FINE ===", result.stdout)

    def test_a_subdirectory_of_a_repository_is_scanned_with_paths_it_can_open(self):
        # `git diff --name-only` lists paths from the repository root; scanning bin/ from
        # inside bin/ found none of them and declared "0 file" over a directory of changes.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "sub").mkdir()
            (repo / "sub/x.js").write_text("const a = 1;\n")
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"], check=True)
            (repo / "sub/x.js").write_text("const a = 2;\n")
            result = subprocess.run(["/bin/bash", str(SCRIPT), str(repo / "sub")], capture_output=True, text=True, timeout=60)
            self.assertIn("1 file", result.stdout)


if __name__ == "__main__":
    unittest.main()
