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


    # Fake credentials are assembled at run time so this file never holds one.
    FAKE = {
        "openai.js": 'const k = "' + "sk-" + "proj-" + "Ab1" * 10 + '";\n',
        "anthropic.js": 'const k = "' + "sk-" + "ant-api03-" + "Zq9" * 10 + '";\n',
        "stripe.js": 'const k = "' + "sk_" + "live_" + "Xy7" * 8 + '";\n',
        "key.pem": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIE\n",
        ".env.local": "STRIPE_SECRET" + "_KEY=" + "Qw3" * 8 + "\n",
        "fallback.js": "const id = process.env.AWS_ID || '" + "AKIA" + "Q" * 16 + "';\n",
    }

    def scan(self, files, commit=None, stage=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
        if commit:
            for name, content in commit.items():
                (repo / name).write_text(content)
            subprocess.run(git + ["add", "."], check=True)
            subprocess.run(git + ["commit", "-qm", "c"], check=True)
        for name, content in (stage or {}).items():
            (repo / name).write_text(content)
            subprocess.run(git + ["add", name], check=True)
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return subprocess.run(["/bin/bash", str(SCRIPT), str(repo)], capture_output=True, text=True, timeout=60).stdout

    def test_a_passphrase_with_spaces_is_still_a_credential(self):
        out = self.scan({"a.js": 'const databasePassword = "correct horse battery staple";\n',
                         "b.js": "x\n"})
        self.assertIn("a.js:1:", out)
        self.assertNotIn("battery staple", out)

    def test_a_dollar_quoted_secret_in_a_policy_is_not_printed(self):
        key = "sk-proj-" + "Ab1" * 10
        out = self.scan({"supabase/migrations/001.sql":
                         "create policy p on t using\n  (true AND token = $$" + key + "$$);\n"})
        self.assertIn("USING true", out)
        self.assertNotIn(key, out)
        # A `--` inside the dollar-quoted body is not a comment.
        out = self.scan({"supabase/migrations/002.sql":
                         "create policy q on t using (true or token = $$" + key + "--$$);\n"})
        self.assertNotIn(key, out)
        # An escaped quote inside an E'' string does not end it.
        out = self.scan({"supabase/migrations/003.sql":
                         "create policy e on t using (true or token = E'prefix\\'" + key + "');\n"})
        self.assertIn("003.sql", out)
        self.assertNotIn(key, out)
        # A quote inside a double-quoted identifier does not open a string.
        out = self.scan({"supabase/migrations/004.sql":
                         "create policy d on t using\n  (true or \"a'b\" = 'correct horse battery staple');\n"})
        self.assertIn("004.sql", out)
        self.assertNotIn("battery staple", out)
        # Nested comments defeat a regex lexer: the output never reaches a value.
        out = self.scan({"supabase/migrations/005.sql":
                         "create policy n on t using (true /* outer /* inner */ ' */ "
                         "or password='correct horse battery staple');\n"})
        self.assertIn("005.sql: create policy n on t using", out)
        self.assertNotIn("battery staple", out)
        # No spaces: the whole expression would fit in one "word".
        out = self.scan({"supabase/migrations/006.sql":
                         "create policy m on t using(true/* outer /* inner */'*/"
                         "or(password='ReviewDummySecret123'));\n"})
        self.assertIn("006.sql: create policy m on t using", out)
        self.assertNotIn("ReviewDummySecret123", out)

    def test_a_single_file_in_scope_never_prints_the_secret(self):
        # With one file grep printed no file name, the redaction keyed on `:line:`
        # matched nothing and the key went to the terminal in clear.
        # A format the scanner always knew: the leak came from the output shape alone.
        key = "AKIA" + "QZ7" * 5 + "Q"
        out = self.scan({"config.js": 'const k = "' + key + '";\n'})
        self.assertIn("1 file", out)
        self.assertIn("credenziali in chiaro", out)
        self.assertIn("config.js:1:", out)
        self.assertNotIn(key, out)

    def test_a_non_ascii_file_name_is_scanned(self):
        # git quoted "citta\303\240.js" and `[ -f ]` skipped the file.
        out = self.scan({"città.js": "const id = '" + "AKIA" + "Q" * 16 + "';\n", "b.js": "x\n"})
        self.assertIn("2 file", out)
        self.assertIn("città.js:1:", out)

    def test_modern_key_formats_env_files_and_env_fallbacks_are_caught(self):
        out = self.scan(dict(self.FAKE))
        for name in self.FAKE:
            with self.subTest(file=name):
                self.assertIn(name + ":1:", out)
        for content in self.FAKE.values():
            self.assertNotIn(content.split("\n")[0][-12:], out)

    def test_a_label_containing_the_word_password_is_not_a_credential(self):
        out = self.scan({"form.js": 'const label_password = "Inserisci la password";\n', "b.js": "x\n"})
        self.assertIn("nessun formato di credenziale noto", out)

    def test_a_repository_without_commits_scans_staged_files(self):
        # No HEAD: `git diff HEAD` failed and the staged file fell out of scope.
        out = self.scan({"untracked.js": "x\n"}, stage={"staged.js": "y\n"})
        self.assertIn("2 file", out)

    def test_a_file_named_like_the_scanner_elsewhere_is_still_scanned(self):
        # The self-exclusion compares the basename first; the full path must still decide.
        out = self.scan({"sub/aos-security.sh": "PASSWORD='" + "Qw3" * 8 + "'\n", "b.js": "x\n"})
        self.assertIn("sub/aos-security.sh:1:", out)

    def test_execution_and_query_built_from_the_request_are_flagged(self):
        cases = {
            "e.js": "const r = eval(req.body.expr);\n",
            "s.py": "subprocess.run(cmd, shell=True)\n",
            "p.py": "obj = pickle.loads(data)\n",
            "q.js": "db.query(`SELECT * FROM users WHERE id = ${req.query.id}`);\n",
            "c.js": 'db.query("DELETE FROM items WHERE id = " + req.params.id);\n',
        }
        out = self.scan(cases)
        for name in cases:
            with self.subTest(file=name):
                self.assertIn(name + ":1:", out)
        self.assertNotIn("nessun percorso, HTML, comando o query", out)

    def test_a_clean_input_section_says_it_ran(self):
        out = self.scan({"a.js": "const re = /x/; re.exec(value);\n", "b.js": "x\n"})
        self.assertIn("nessun percorso, HTML, comando o query", out)

    def test_policies_are_read_without_comments_and_across_lines(self):
        head = "create table t (id int);\nalter table t enable row level security;\n"
        out = self.scan({"supabase/migrations/1_a.sql": head + "-- create policy p on t using (true);\n"})
        self.assertNotIn("USING true", out)
        out = self.scan({"supabase/migrations/1_a.sql": head + "create policy p on t\n  using (\n    true\n  );\n"})
        self.assertIn("USING true", out)
        self.assertIn("supabase/migrations/1_a.sql:", out)


if __name__ == "__main__":
    unittest.main()
