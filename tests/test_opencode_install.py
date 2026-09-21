import importlib.util
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "bin" / "aos-opencode-install.py"
SPEC = importlib.util.spec_from_file_location("aos_opencode_install", SCRIPT)
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class OpenCodeInstallerTests(unittest.TestCase):
    def test_scalar_before_inline_comment_does_not_hide_later_property(self):
        raw = b'{"autoupdate": true // on, keep\n, "permission": {"*":"allow"}}'
        found = INSTALLER.top_property(raw.decode(), "permission")
        self.assertIsNotNone(found)
        self.assertEqual(json.loads(INSTALLER.clean_jsonc(raw.decode()[found[0]:found[1]])), {"*": "allow"})

    def test_active_claude_plugins_normalizes_manifest_shapes_and_relative_paths(self):
        claude = self.home / ".claude"
        (claude / "plugins").mkdir(parents=True)
        (claude / "plugins/installed_plugins.json").write_text(json.dumps({"plugins": {
            "dict@market": {"path": "plugins/dict"},
            "list@market": [{"installPath": "plugins/list"}],
            "off@market": {"path": "plugins/off"},
        }}))
        (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {
            "dict@market": True, "list@market": True, "off@market": False,
        }}))
        self.assertEqual(INSTALLER.active_claude_plugins(self.home),
                         [self.home / "plugins/dict", self.home / "plugins/list"])

    def test_update_can_add_previously_absent_vscode_settings(self):
        INSTALLER.install(self.home)
        settings = INSTALLER.vscode_settings(self.home)
        self.assertFalse(settings.exists())
        INSTALLER.install(self.home, vscode=True)
        self.assertTrue(settings.is_file())
        INSTALLER.rollback(self.home)
        self.assertFalse(settings.exists())

    def test_permission_comments_survive_insertion_and_rerun(self):
        config = self.root / 'opencode.jsonc'
        config.write_text('{"permission": {// retain this note\n"*": "allow"}}')
        INSTALLER.install(self.home)
        self.assertIn('// retain this note', config.read_text())
        INSTALLER.install(self.home)
        self.assertIn('// retain this note', config.read_text())

    def test_mcp_variables_are_converted_in_all_fields_without_reading_secrets(self):
        plugin = self.home / 'plugin'
        plugin.mkdir()
        (plugin / '.mcp.json').write_text(json.dumps({'mcpServers': {'d': {
            'command': '${BIN}', 'args': ['run', '--cwd', '${CLAUDE_PLUGIN_ROOT}', 'start'],
            'env': {'CONFIG': '${CLAUDE_PLUGIN_ROOT}/config', 'TOKEN': 'prefix-${T}'}}}}))
        registry = self.home / '.claude/plugins/installed_plugins.json'
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({'plugins': {'test@market': [{'installPath': str(plugin)}]}}))
        (self.home / '.claude/settings.json').write_text(json.dumps({'enabledPlugins': {'test@market': True}}))
        (self.home / '.claude.json').write_text(json.dumps({'mcpServers': {'r': {
            'type': 'http', 'url': 'https://${HOST}/mcp', 'headers': {'Authorization': 'Bearer ${T}'}}}}))
        result = INSTALLER.imported_mcps(self.home)
        self.assertEqual(result['d']['command'], ['{env:BIN}', 'run', '--cwd', str(plugin), 'start'])
        self.assertEqual(result['d']['environment'], {'CONFIG': str(plugin) + '/config', 'TOKEN': 'prefix-{env:T}'})
        self.assertEqual(result['r']['headers']['Authorization'], 'Bearer {env:T}')
        self.assertEqual(result['r']['url'], 'https://{env:HOST}/mcp')

    def test_mjs_loader_is_explicitly_registered_for_opencode_discovery(self):
        wanted, _ = INSTALLER.desired(self.home)
        _, config, entry, _, _ = INSTALLER.paths(self.home)
        parsed = json.loads(INSTALLER.clean_jsonc(wanted[config].decode()))
        self.assertIn(entry.as_uri(), parsed.get('plugin', []))

    def test_adding_fields_preserves_url_strings_and_trailing_commas(self):
        raw = b'{"endpoint":"https://example.invalid",}\n'
        result = INSTALLER.set_top(raw, 'new', 'value')
        self.assertEqual(json.loads(INSTALLER.clean_jsonc(result.decode())),
                         {'endpoint': 'https://example.invalid', 'new': 'value'})

    def test_runtime_snapshot_imports_enabled_skills_missing_from_config(self):
        skill = self.home / 'plugin/skills/example'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: example\ndescription: test\n---\n')
        snapshot = self.home / 'skills.json'
        snapshot.write_text(json.dumps({'data': [{'skills': [{'path': str(skill / 'SKILL.md'), 'enabled': True},
                                                              {'path': '/missing/SKILL.md', 'enabled': False}]}]}))
        self.assertEqual(INSTALLER.imported_skills(self.home, snapshot), [str(skill.resolve())])

    def test_active_plugin_mcp_servers_are_ported_but_disabled_plugins_are_not(self):
        plugin = self.home / 'plugin'
        plugin.mkdir()
        (plugin / '.mcp.json').write_text(json.dumps({'context7': {'command': 'npx', 'args': ['test']},
                                                   'remote': {'type': 'http', 'url': 'https://example.invalid/mcp'}}))
        registry = self.home / '.claude/plugins/installed_plugins.json'
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({'plugins': {'test@market': [{'installPath': str(plugin)}]}}))
        (self.home / '.claude/settings.json').write_text(json.dumps({'enabledPlugins': {'test@market': True}}))
        (self.home / '.claude.json').write_text('{"mcpServers":{}}')
        result = INSTALLER.imported_mcps(self.home)
        self.assertEqual(result['context7']['command'], ['npx', 'test'])
        self.assertEqual(result['remote'], {'type': 'remote', 'url': 'https://example.invalid/mcp'})
        (self.home / '.claude/settings.json').write_text(json.dumps({'enabledPlugins': {'test@market': False}}))
        self.assertEqual(INSTALLER.imported_mcps(self.home), {})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.root = self.home / ".config/opencode"
        package = self.root / "node_modules/@opencode-ai/plugin/package.json"
        package.parent.mkdir(parents=True)
        package.write_text("{}")

    def tearDown(self):
        self.temp.cleanup()

    def test_install_is_idempotent_and_loader_references_canonical_source(self):
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        state = (self.root / ".aos-install-state.json").read_bytes()
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        self.assertEqual((self.root / ".aos-install-state.json").read_bytes(), state)
        loader = (self.root / "plugins/aos-entry.mjs").read_text()
        self.assertIn('@opencode-ai/plugin', loader)
        self.assertIn(INSTALLER.SOURCE.as_uri(), loader)
        self.assertIn('export const AosPlugin', loader)
        self.assertEqual(loader.count("export "), 1)
        self.assertNotIn((INSTALLER.SOURCE.parent / "aos-plugin.mjs").read_text(), loader)
        self.assertEqual(INSTALLER.main(["check", "--home", str(self.home)]), 0)

    def test_generated_loader_is_loadable_by_node(self):
        package_dir = self.root / "node_modules/@opencode-ai/plugin"
        (package_dir / "package.json").write_text(json.dumps({"type": "module", "exports": "./index.mjs"}))
        (package_dir / "index.mjs").write_text(
            "const string = () => ({describe: () => ({})});\n"
            "export const tool = Object.assign((definition) => definition, {schema: {string}});\n")
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        entry = self.root / "plugins/aos-entry.mjs"
        probe = ("const m = await import(" + json.dumps(entry.as_uri()) + ");"
                 "const hooks = await m.AosPlugin({client:{},directory:'/tmp'});"
                 "if (Object.keys(m).join() !== 'AosPlugin' || !hooks.tool?.aos_execute) process.exit(2);")
        result = subprocess.run(["node", "--input-type=module", "--eval", probe],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_preserves_jsonc_and_permissions_then_rolls_back(self):
        config = self.root / "opencode.jsonc"
        config.parent.mkdir(parents=True, exist_ok=True)
        original = b'{// keep me\n"theme":"dark",\n}\n'
        config.write_bytes(original)
        config.chmod(0o640)
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        self.assertIn("// keep me", config.read_text())
        self.assertIn("aos-instructions.md", config.read_text())
        self.assertNotIn(",,", config.read_text())
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o640)
        backup_root = self.root / ".aos-backups"
        self.assertEqual(stat.S_IMODE(backup_root.stat().st_mode), 0o700)
        backup = next(backup_root.iterdir()) / "opencode.jsonc"
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(INSTALLER.main(["rollback", "--home", str(self.home)]), 0)
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o640)
        self.assertFalse((self.root / "plugins/aos-entry.mjs").exists())

    def test_rollback_refuses_changed_installed_file(self):
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        entry = self.root / "plugins/aos-entry.mjs"
        entry.write_text(entry.read_text() + "// local change\n")
        self.assertEqual(INSTALLER.main(["rollback", "--home", str(self.home)]), 1)
        self.assertIn("local change", entry.read_text())
        self.assertTrue((self.root / ".aos-install-state.json").exists())

    def test_missing_dependency_fails_without_creating_installation(self):
        (self.root / "node_modules/@opencode-ai/plugin/package.json").unlink()
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 1)
        self.assertFalse((self.root / "plugins/aos-entry.mjs").exists())

    def test_appends_to_existing_instruction_array(self):
        config = self.root / "opencode.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"instructions": ["existing.md"], "theme": "dark"}))
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        loaded = json.loads(config.read_text())
        self.assertEqual(loaded["instructions"][0], "existing.md")
        self.assertEqual(loaded["theme"], "dark")
        self.assertEqual(len(loaded["instructions"]), 2)
        self.assertEqual(loaded["permission"]["aos_critical"], "ask")

    def test_permission_preserves_deny_and_other_entries(self):
        config = self.root / "opencode.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"permission": {"*": "allow", "bash": "deny", "aos_critical": "deny"}}))
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        permission = json.loads(config.read_text())["permission"]
        self.assertEqual(permission, {"*": "allow", "bash": "deny", "aos_critical": "deny"})

    def test_string_permission_becomes_map_without_widening_deny(self):
        config = self.root / "opencode.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('{"permission":"deny"}')
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        self.assertEqual(json.loads(config.read_text())["permission"], {"*": "deny", "aos_critical": "deny"})

    def test_appends_to_jsonc_array_with_trailing_comma(self):
        config = self.root / "opencode.jsonc"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('{"instructions": ["existing.md",],}\n')
        self.assertEqual(INSTALLER.main(["install", "--home", str(self.home)]), 0)
        self.assertNotIn(",,", config.read_text())
        self.assertIn("aos-instructions.md", config.read_text())

    def test_jsonc_cleanup_does_not_change_comma_brace_inside_strings(self):
        parsed = json.loads(INSTALLER.clean_jsonc('{"value": ",}", "array": [1,],}'))
        self.assertEqual(parsed, {"value": ",}", "array": [1]})

    def test_imports_local_mcp_without_exposing_or_overwriting_values(self):
        fixture_value = "fixture-value"
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "search": {"command": "npx", "args": ["-y", "server"],
                       "env": {"TOKEN": fixture_value, "FROM_ENV": "${SEARCH_TOKEN}"}, "disabled": True},
            "node_repl": {"command": "ignored"},
            "computer-use": {"command": "ignored"}
        }}))
        self.assertEqual(INSTALLER.main(["install", "--import-mcp", "--home", str(self.home)]), 0)
        config = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(config["share"], "disabled")
        self.assertEqual(config["small_model"], json.loads(INSTALLER.POLICY.read_text())["open"]["primary"])
        self.assertEqual(config["mcp"]["search"]["command"], ["npx", "-y", "server"])
        self.assertEqual(config["mcp"]["search"]["environment"]["TOKEN"], fixture_value)
        self.assertEqual(config["mcp"]["search"]["environment"]["FROM_ENV"], "{env:SEARCH_TOKEN}")
        self.assertFalse(config["mcp"]["search"]["enabled"])
        self.assertNotIn("node_repl", config["mcp"])
        self.assertEqual(stat.S_IMODE((self.root / "opencode.json").stat().st_mode), 0o600)

    def test_mcp_conflict_refuses_install_without_modifying_config(self):
        config = self.root / "opencode.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"mcp": {"search": {"type": "remote"}}}).encode()
        config.write_bytes(original)
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "search": {"command": "npx", "args": ["server"]}
        }}))
        self.assertEqual(INSTALLER.main(["install", "--import-mcp", "--home", str(self.home)]), 1)
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse((self.root / ".aos-install-state.json").exists())

    @unittest.skipIf(INSTALLER.tomllib is None, "Skill config import requires Python 3.11+")
    def test_imports_only_active_unique_plugin_skills_and_updates_on_rerun(self):
        plugin = self.home / "plugins/demo/1.0.0"
        skill = plugin / "skills/new-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: new-skill\n---\n")
        duplicate = plugin / "skills/duplicate"
        duplicate.mkdir()
        (duplicate / "SKILL.md").write_text("---\nname: already-there\n---\n")
        existing = self.home / ".agents/skills/already"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("---\nname: already-there\n---\n")
        claude = self.home / ".claude"
        (claude / "plugins").mkdir(parents=True)
        (claude / "plugins/installed_plugins.json").write_text(json.dumps({
            "plugins": {"demo@source": [{"installPath": str(plugin)}]}}))
        (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"demo@source": True}}))
        codex_skill = self.home / "codex-extra"
        codex_skill.mkdir()
        (codex_skill / "SKILL.md").write_text("---\nname: codex-extra\n---\n")
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text('[[skills.config]]\npath = "' + str(codex_skill / "SKILL.md") + '"\nenabled = true\n')
        self.assertEqual(INSTALLER.main(["install", "--import-skills", "--home", str(self.home)]), 0)
        config = json.loads((self.root / "opencode.json").read_text())
        self.assertEqual(set(config["skills"]["paths"]), {str(skill.resolve()), str(codex_skill.resolve())})
        later = plugin / "skills/later"
        later.mkdir()
        (later / "SKILL.md").write_text("---\nname: later\n---\n")
        self.assertEqual(INSTALLER.main(["install", "--import-skills", "--home", str(self.home)]), 0)
        config = json.loads((self.root / "opencode.json").read_text())
        self.assertIn(str(later.resolve()), config["skills"]["paths"])

    def test_vscode_profile_preserves_existing_profiles_and_default(self):
        settings = INSTALLER.vscode_settings(self.home)
        settings.parent.mkdir(parents=True)
        settings.write_text('{// keep\n"terminal.integrated.defaultProfile.osx":"zsh",\n'
                            '"terminal.integrated.profiles.osx":{"Claude Code":{"path":"claude"},'
                            '"Codex":{"path":"codex"}},}\n')
        self.assertEqual(INSTALLER.main(["install", "--vscode", "--home", str(self.home)]), 0)
        data = json.loads(INSTALLER.clean_jsonc(settings.read_text()))
        self.assertEqual(data["terminal.integrated.defaultProfile.osx"], "zsh")
        self.assertEqual(set(data["terminal.integrated.profiles.osx"]), {"Claude Code", "Codex", "OpenCode"})
        profile = data["terminal.integrated.profiles.osx"]["OpenCode"]
        self.assertEqual(profile["path"], "/bin/zsh")
        self.assertEqual(profile["args"], ["-l", "-c", "opencode; exec /bin/zsh -l"])
        self.assertTrue(profile["overrideName"])
        self.assertEqual(profile["icon"], "terminal")


if __name__ == "__main__":
    unittest.main()
