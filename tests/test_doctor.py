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

    def test_a_file_the_editions_keep_identical_is_compared_not_just_the_version(self):
        # Same version number, different code: the number alone would have said fine.
        codex, claude = self.roots
        public = self.base / "public"
        (public / "bin").mkdir(parents=True)
        (claude / "VERSION").write_text("1.0\n")
        (public / "VERSION").write_text("1.0\n")
        (public / "bin/check.py").write_text((claude / "bin/check.py").read_text())
        (claude / "DISTRIBUTION").write_text(f"{public}\n# identical by construction\nbin/check.py\n")
        self.assertNotIn("DISTRIBUZIONE", self.run_doctor().stdout)
        (public / "bin/check.py").write_text("print('ported wrong')\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bin/check.py differisce", result.stdout)
        (claude / "DISTRIBUTION").write_text(f"{public}\n../escape\n")
        self.assertNotIn("differisce", self.run_doctor().stdout)
        # An indented comment is a comment, not a path (found by review).
        (claude / "DISTRIBUTION").write_text(f"{public}\n  # indented comment\nbin/check.py\n")
        (public / "bin/check.py").write_text((claude / "bin/check.py").read_text())
        self.assertNotIn("DISTRIBUZIONE", self.run_doctor().stdout)

    def test_heavy_backup_directories_are_a_warning(self):
        # 8.3 GB of backups outside the root, invisible to the tmp/ check.
        home = self.base / "home"
        (home / ".agents/backups/aos-old").mkdir(parents=True)
        with (home / ".agents/backups/aos-old/dump.bin").open("wb") as stream:
            stream.truncate(1100 * 1024 * 1024)
        result = self.run_doctor(dict(os.environ, HOME=str(home)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AVVISO BACKUP", result.stdout)
        self.assertIn("1100 MB", result.stdout)
        (home / ".agents/backups/aos-old/dump.bin").write_bytes(b"small")
        self.assertNotIn("AVVISO BACKUP", self.run_doctor(dict(os.environ, HOME=str(home))).stdout)

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
        present = sorted(str(p.relative_to(DOCTOR.parent.parent)) for pattern in ("test_*.py", "*.test.mjs")
                         for p in (DOCTOR.parent.parent / "tests").glob(pattern))
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

    def test_version_drift_is_a_warning_not_a_failure(self):
        # metadata.version was bumped alongside VERSION through 1.22.1 and then
        # left behind by the 2.x releases: the two must agree, as a warning.
        codex, claude = self.roots
        (claude / "VERSION").write_text("2.1.2\n")
        (claude / "SKILL.md").write_text(
            "---\nname: aos\nmetadata:\n  version: \"1.22.1\"\ndescription: test\n---\n")
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AVVISO VERSIONE", result.stdout)
        self.assertIn("1.22.1", result.stdout)
        self.assertIn("2.1.2", result.stdout)
        (claude / "SKILL.md").write_text(
            "---\nname: aos\nmetadata:\n  version: \"2.1.2\"\ndescription: test\n---\n")
        self.assertNotIn("VERSIONE", self.run_doctor().stdout)

    def test_provider_privacy_reports_the_catalog_declaration(self):
        # The catalog carries what the provider listing said about retention; the
        # doctor reports it and never reads a secret or claims a verification.
        codex, claude = self.roots
        (claude / "config").mkdir()
        (claude / "config/open-models.json").write_text(json.dumps({
            "schema": 1,
            "open": {"primary": "vercel/deepseek/deepseek-v4-pro-0813",
                     "fallback": "vercel/alibaba/qwen3-coder-next"},
            "model_catalog": {"vercel/deepseek/deepseek-v4-pro-0813": {"provider_reported_zdr": "some"},
                              "vercel/alibaba/qwen3-coder-next": {"provider_reported_zdr": "all"}}}))
        result = self.run_doctor(dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Privacy provider:", result.stdout)
        self.assertIn("zero_data_retention=some", result.stdout)
        self.assertIn("zero_data_retention=all", result.stdout)
        self.assertNotIn("AVVISO PRIVACY", result.stdout)

    def test_provider_privacy_warns_when_the_catalog_says_nothing(self):
        codex, claude = self.roots
        (claude / "config").mkdir()
        (claude / "config/open-models.json").write_text(json.dumps({
            "schema": 1,
            "open": {"primary": "vercel/deepseek/deepseek-v4-pro-0813"},
            "model_catalog": {"vercel/deepseek/deepseek-v4-pro-0813": {}}}))
        result = self.run_doctor(dict(os.environ))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("zero_data_retention=unknown", result.stdout)
        self.assertIn("AVVISO PRIVACY", result.stdout)

    def _write_models_config(self, with_budgets=True, budgets_value=None, with_mid=False, with_invalid_budget=False):
        codex, claude = self.roots
        (claude / "config").mkdir(exist_ok=True)
        cfg = {
            "open": {"primary": "vercel/deepseek/deepseek-v4-pro-0813",
                     "fallback": "vercel/alibaba/qwen3-coder-next",
                     "source": "benchmark 2026-09-20"},
            "model_catalog": {
                "vercel/deepseek/deepseek-v4-pro-0813": {
                    "cost_class": "CHEAP",
                    "roles": ["executor", "fixer"],
                    "benchmark": {"available": True, "status": "winner", "source": "comparable benchmark"}}},
            "executors": {"runtime_status": {"codex-cli": {
                "open_execution": False, "reason": "restrictive rule loading unverified"}},
                "codex-cli": {"file_tools": True}, "claude-code": {"file_tools": True}},
            "providers": {"vercel": {"api_key_env": "AOS_OPEN_API_KEY"}},
            "premium": {"reviewer": "claude"},
            "context_policy": {"defaults": {"target_context": 80000, "soft_limit": 120000, "hard_limit": 180000}}}
        if budgets_value is not None:
            cfg["budgets"] = budgets_value
        elif with_budgets and not with_invalid_budget:
            cfg["budgets"] = {"cheap": 1000, "mid": 5000}
        elif with_invalid_budget:
            cfg["budgets"] = {"cheap": float('nan')}
        if with_mid:
            cfg["model_catalog"]["vercel/alibaba/qwen3-coder-next"] = {
                "cost_class": "MID",
                "roles": ["primary"],
                "benchmark": {"available": True, "status": "runnerup", "source": "comparable benchmark"}}
        (claude / "config/open-models.json").write_text(json.dumps(cfg))
        home = self.base / "home"
        home.mkdir()
        xdg = self.base / "xdg"
        xdg.mkdir(parents=True, exist_ok=True)
        return dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(xdg))

    def test_operational_status_reports_policy_without_certifying_live(self):
        env = self._write_models_config()
        result = self.run_doctor(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local health, no live certification", result.stdout)
        self.assertIn("benchmark winner=vercel/deepseek/deepseek-v4-pro-0813", result.stdout)
        self.assertIn("cost class (primary)=CHEAP", result.stdout)
        self.assertIn("cost_budgets=configured", result.stdout)
        self.assertIn("authentication unknown", result.stdout)

    def test_operational_status_budgets_unset(self):
        codex, claude = self.roots
        (claude / "config").mkdir(exist_ok=True)
        (claude / "config/open-models.json").write_text(json.dumps({
            "open": {"primary": "vercel/deepseek/deepseek-v4-pro-0813"},
            "model_catalog": {"vercel/deepseek/deepseek-v4-pro-0813": {"cost_class": "CHEAP"}},
            "context_policy": {"defaults": {"target_context": 80000}},
            "budgets": {"cheap": None, "mid": None}}))
        result = self.run_doctor()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cost_budgets=unset", result.stdout)

    def test_operational_status_budgets_finite(self):
        env = self._write_models_config(with_budgets=True, budgets_value={"cheap": 1000, "mid": 5000}, with_mid=True)
        result = self.run_doctor(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cost_budgets=configured (cheap, mid)", result.stdout)
        self.assertIn("cost classes configured=CHEAP, MID", result.stdout)

    def test_operational_status_budgets_invalid_nan(self):
        env = self._write_models_config(with_budgets=True, budgets_value={"cheap": float('nan')})
        result = self.run_doctor(env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CONFIG", result.stdout)
        self.assertIn("cost_budgets=invalid", result.stdout)

    def test_main_host_env_else_unknown(self):
        env = self._write_models_config()
        env["AOS_MAIN_HOST"] = "claude-code"
        self.assertIn("main_host=claude-code", self.run_doctor(env).stdout)
        env.pop("AOS_MAIN_HOST")
        self.assertIn("main_host=unknown", self.run_doctor(env).stdout)

    def test_codex_disabled_is_informational_when_another_runtime_exists(self):
        env = self._write_models_config()
        result = self.run_doctor(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runtime codex-cli: disabled; reason=restrictive rule loading unverified", result.stdout)
        self.assertIn("viable enabled installed runtime=", result.stdout)

    def test_every_disabled_runtime_is_reported_and_none_viable_fails(self):
        env = self._write_models_config()
        cfg_path = self.roots[1] / "config/open-models.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["executors"]["runtime_status"] = {
            "claude-code": {"open_execution": False, "reason": "host policy"},
            "codex-cli": {"open_execution": False, "reason": "native rules unverified"},
        }
        cfg_path.write_text(json.dumps(cfg))
        result = self.run_doctor(env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("runtime claude-code: disabled; reason=host policy", result.stdout)
        self.assertIn("runtime codex-cli: disabled; reason=native rules unverified", result.stdout)
        self.assertIn("RUNTIME", result.stdout)

    def test_enabled_installed_runtime_is_reported_without_live_security_claim(self):
        env = self._write_models_config()
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text("#!/bin/sh\n")
        claude.chmod(0o755)
        env["PATH"] = str(bin_dir) + os.pathsep + "/bin"
        result = self.run_doctor(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("runtime claude-code: enabled by configuration; installed=yes; os_isolation=none; isolation unverified", result.stdout)
        self.assertIn("viable enabled installed runtime=claude-code", result.stdout)

    def test_missing_mid_candidate_is_a_warning(self):
        env = self._write_models_config()
        result = self.run_doctor(env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AVVISO MID", result.stdout)

    def test_malformed_model_config_is_an_error(self):
        codex, claude = self.roots
        (claude / "config").mkdir(exist_ok=True)
        (claude / "config/open-models.json").write_text("{invalid")
        result = self.run_doctor(dict(os.environ, HOME=str(self.base / "home")))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CONFIG", result.stdout)

    def test_hook_status_reports_declarations_without_executing(self):
        home = self.base / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex/hooks.json").write_text(json.dumps({
            "hooks": {"Stop": [{"matcher": "x",
                                "hooks": [{"type": "command", "command": "touch SHOULD_NOT_RUN"}]}]}}))
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text(json.dumps({"permissions": {"deny": []}}))
        result = self.run_doctor(dict(os.environ, HOME=str(home)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # A third-party Stop hook is not AOS's: "configurato" read as if it were.
        self.assertIn("Hook Stop (codex): nessun hook AOS; 1 hook di terzi", result.stdout)
        self.assertNotIn("Hook Stop (codex): configurato", result.stdout)
        self.assertIn("Hook Stop (claude): non configurato", result.stdout)
        self.assertNotIn("SHOULD_NOT_RUN", result.stdout + result.stderr)
        self.assertFalse((self.base / "SHOULD_NOT_RUN").exists())


    def test_an_aos_stop_hook_is_told_apart_from_third_party_ones(self):
        home = self.base / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "python3 ~/.claude/skills/aos/bin/stop.py"},
                       {"type": "command", "command": "~/.claude/skills/gstack/hooks/stop"}]}]}}))
        result = self.run_doctor(dict(os.environ, HOME=str(home)))
        self.assertIn("Hook Stop (claude): 1 hook AOS; 1 hook di terzi", result.stdout)
        self.assertNotIn("gstack", result.stdout)

    def _isolation_case(self, record):
        shutil.rmtree(self.base / "home", ignore_errors=True)
        env = self._write_models_config()
        claude = self.roots[1]
        cfg_path = claude / "config/open-models.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["executors"]["runtime_status"]["claude-code"] = {
            "open_execution": True, "os_isolation": "seatbelt", "isolation_verified": True,
            "isolation_evidence": "docs/probe.json"}
        cfg_path.write_text(json.dumps(cfg))
        (claude / "docs").mkdir(exist_ok=True)
        (claude / "docs/probe.json").unlink(missing_ok=True)
        if record is not None:
            (claude / "docs/probe.json").write_text(record if isinstance(record, str) else json.dumps(record))
        return self.run_doctor(env).stdout

    def test_isolation_is_verified_only_by_a_matching_clean_probe_record(self):
        good = {"runtime": "claude-code", "os_isolation": "seatbelt", "isolated": True, "leaks": []}
        self.assertIn("isolation verified by docs/probe.json", self._isolation_case(good))
        for label, record in [("other runtime", dict(good, runtime="codex-cli")),
                              ("other isolation", dict(good, os_isolation="none")),
                              ("not isolated", dict(good, isolated=False)),
                              ("leaks", dict(good, leaks=["R1"])),
                              ("not json", "{}garbage"),
                              ("missing", None)]:
            with self.subTest(case=label):
                out = self._isolation_case(record)
                self.assertNotIn("isolation verified by", out)
                self.assertIn("isolation unverified", out)
                self.assertIn("AVVISO RUNTIME", out)

    def test_isolation_evidence_outside_the_root_does_not_count(self):
        outside = self.base / "outside.json"
        outside.write_text(json.dumps({"runtime": "claude-code", "os_isolation": "seatbelt",
                                       "isolated": True, "leaks": []}))
        env = self._write_models_config()
        cfg_path = self.roots[1] / "config/open-models.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["executors"]["runtime_status"]["claude-code"] = {
            "open_execution": True, "os_isolation": "seatbelt", "isolation_verified": True,
            "isolation_evidence": "../../outside.json"}
        cfg_path.write_text(json.dumps(cfg))
        out = self.run_doctor(env).stdout
        self.assertIn("isolation unverified", out)


if __name__ == "__main__":
    unittest.main()
