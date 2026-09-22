"""Behavior checks for local discovery, bounds, and config preservation."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('library', ROOT / 'bin/skill-library.py')
library = importlib.util.module_from_spec(spec)
spec.loader.exec_module(library)

# refresh() needs stdlib tomllib. The CLI re-execs into a 3.11+ interpreter via
# ensure_refresh_runtime(); these tests call refresh() directly, so they skip
# instead of erroring when the running interpreter predates 3.11.
try:
    import tomllib  # noqa: F401
    HAS_TOMLLIB = True
except ImportError:
    HAS_TOMLLIB = False

NEEDS_TOMLLIB = unittest.skipUnless(HAS_TOMLLIB, 'refresh requires stdlib tomllib (Python 3.11+)')


class LibraryTests(unittest.TestCase):
    def test_exact_name_and_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / 'SKILL.md'
            skill.write_text('---\nname: cli-anything-blender\ndescription: Blender helper\n---\n')
            index = root / 'index.json'
            index.write_text(json.dumps([
                {'name': 'cli-anything-blender', 'path': str(skill), 'description': 'Blender helper'},
                {'name': 'render-helper', 'path': str(skill), 'description': 'Blender rendering'}
            ]))
            entries = library.read_index(index)
            matches, _ = library.search(entries, 'cli-anything-blender', 3)
            self.assertEqual(matches[0]['name'], 'cli-anything-blender')
            matches, total = library.search(entries, 'blender', 1)
            self.assertEqual(total, 2)
            self.assertEqual(len(matches), 1)
            self.assertEqual(library.source_path(matches[0]), skill)

    def test_search_ignores_the_file_path(self):
        entries = [{'name': 'alpha', 'description': 'first', 'path': '/home/u/.claude/skills/alpha/SKILL.md'},
                   {'name': 'beta', 'description': 'manage skills', 'path': '/home/u/.claude/skills/beta/SKILL.md'}]
        matches, total = library.search(entries, 'skills', 5)
        self.assertEqual([m['name'] for m in matches], ['beta'])
        self.assertEqual(total, 1)
        self.assertEqual(library.search(entries, 'claude', 5), ([], 0))

    def test_no_match_and_missing_file(self):
        self.assertEqual(library.search([], 'missing', 3), ([], 0))
        self.assertIsNone(library.source_path({'path': '/nonexistent/skill/SKILL.md'}))

    @NEEDS_TOMLLIB
    def test_config_preservation_refresh_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, index, snapshot = (root / n for n in ('config.toml', 'index.json', 'snapshot.json'))
            base = '# untouched\nmodel = "example"\n[hooks]\nenabled = true\n\n[[skills.config]]\npath = "/manual/SKILL.md"\nenabled = false\n'
            config.write_text(base)
            snapshot.write_text(json.dumps({'data': [{'skills': [{'name': 'specialist', 'path': '/plugin/1/SKILL.md', 'description': 'test', 'enabled': True}]}]}))
            library.refresh(snapshot, index, config, False)
            self.assertEqual(config.read_text(), base)
            self.assertFalse(index.exists())
            library.refresh(snapshot, index, config, True)
            first = config.read_text()
            self.assertTrue(first.startswith(base))
            library.refresh(snapshot, index, config, True)
            self.assertEqual(config.read_text(), first)
            self.assertEqual(len(list(root.glob('*.backup-*'))), 1)
            snapshot.write_text(snapshot.read_text().replace('/plugin/1/', '/plugin/2/'))
            library.refresh(snapshot, index, config, True)
            self.assertIn('/plugin/2/', config.read_text())
            self.assertNotIn('/plugin/1/', config.read_text())
            self.assertIn('/manual/SKILL.md', config.read_text())
            self.assertEqual(library.read_index(index)[0]['path'], '/plugin/2/SKILL.md')

    @NEEDS_TOMLLIB
    def test_a_test_fixture_discovered_as_a_skill_is_not_indexed_nor_hidden(self):
        # gstack ships fixture SKILL.md files under test/fixtures; discovery lists them
        # and the catalog carried two of them ("alpha", "beta") as skills.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, index, snapshot = (root / n for n in ('config.toml', 'index.json', 'snapshot.json'))
            config.write_text('model = "example"\n')
            snapshot.write_text(json.dumps({'data': [{'skills': [
                {'name': 'alpha', 'path': '/gstack/test/fixtures/tree-a/alpha/SKILL.md', 'description': 'fixture', 'enabled': True},
                {'name': 'real', 'path': '/plugin/real/SKILL.md', 'description': 'skill', 'enabled': True}]}]}))
            library.refresh(snapshot, index, config, True)
            names = [e['name'] for e in library.read_index(index)]
            self.assertEqual(names, ['real'])
            self.assertNotIn('/test/fixtures/', config.read_text())

    def test_cli_limit_and_missing_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / 'index.json'
            index.write_text(json.dumps([{'name': 'missing', 'path': '/nonexistent/SKILL.md', 'description': 'x' * 10000}]))
            cmd = [sys.executable, str(ROOT / 'bin/skill-library.py'), '--index', str(index), 'search', 'missing']
            result = subprocess.run(cmd + ['--limit', '1'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn('MISSING', result.stdout)
            self.assertLess(len(result.stdout), 500)
            result = subprocess.run(cmd + ['--limit', '0'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)

    def test_discover_drives_a_local_codex_app_server_and_returns_its_inventory(self):
        # The snapshot used to come from a scratch script run by hand, which is why the
        # catalog stayed eighty skills behind. A fake `codex` on PATH answers the same
        # JSON-RPC the real app-server does; no network, no real Codex.
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'codex'
            fake.write_text('#!/usr/bin/env python3\nimport json, sys\n'
                            'assert sys.argv[1:] == ["app-server"]\n'
                            'print("not json, ignored")\n'
                            'for line in sys.stdin:\n'
                            '    m = json.loads(line)\n'
                            '    if m.get("method") == "initialize":\n'
                            '        print(json.dumps({"id": 1, "result": {}}), flush=True)\n'
                            '    if m.get("method") == "skills/list":\n'
                            '        p = m["params"]; assert p["forceReload"] and p["includeDisabled"] and p["cwds"] == ["/somewhere"]\n'
                            '        print(json.dumps({"jsonrpc": "2.0", "method": "note", "params": {}}), flush=True)\n'
                            '        print(json.dumps({"id": 2, "result": {"data": [{"skills": [{"name": "x", "path": "/x/SKILL.md", "description": "d", "enabled": True}]}]}}), flush=True)\n'
                            '        break\n')
            fake.chmod(0o755)
            with mock.patch.dict('os.environ', {'PATH': tmp + ':' + os.environ['PATH']}):
                result = library.discover(Path('/somewhere'), 10)
            self.assertEqual(result['data'][0]['skills'][0]['name'], 'x')
            fake.write_text('#!/usr/bin/env python3\nimport json, sys\n'
                            'for line in sys.stdin:\n'
                            '    if json.loads(line).get("method") == "skills/list":\n'
                            '        print(json.dumps({"id": 2, "error": {"code": 1, "message": "boom"}}), flush=True); break\n')
            with mock.patch.dict('os.environ', {'PATH': tmp + ':' + os.environ['PATH']}):
                with self.assertRaises(ValueError) as failure:
                    library.discover(Path('/somewhere'), 10)
            self.assertIn('boom', str(failure.exception))
            with mock.patch.dict('os.environ', {'PATH': tmp + '/nowhere'}):
                with self.assertRaises(ValueError):
                    library.discover(Path('/somewhere'), 10)

    def test_an_error_on_initialize_fails_the_discovery_even_if_skills_list_answers(self):
        # The first version read `error` only on the skills/list reply: a server that
        # refused the handshake and still produced an inventory was accepted.
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'codex'
            fake.write_text('#!/usr/bin/env python3\nimport json, sys\n'
                            'for line in sys.stdin:\n'
                            '    m = json.loads(line)\n'
                            '    if m.get("method") == "initialize":\n'
                            '        print(json.dumps({"id": 1, "error": {"message": "initialize failed"}}), flush=True)\n'
                            '    if m.get("method") == "skills/list":\n'
                            '        print(json.dumps({"id": 2, "result": {"data": [{"skills": []}]}}), flush=True); break\n')
            fake.chmod(0o755)
            with mock.patch.dict('os.environ', {'PATH': tmp + ':' + os.environ['PATH']}):
                with self.assertRaises(ValueError) as failure:
                    library.discover(Path('/somewhere'), 10)
            self.assertIn('initialize failed', str(failure.exception))

    def test_a_silent_app_server_is_abandoned_at_the_timeout(self):
        # readline() blocks; the first version checked the deadline only between lines,
        # so a server that stayed open and quiet hung the refresh past any --timeout.
        import time
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'codex'
            fake.write_text('#!/usr/bin/env python3\nimport sys\nfor line in sys.stdin:\n    pass\n')
            fake.chmod(0o755)
            started = time.monotonic()
            with mock.patch.dict('os.environ', {'PATH': tmp + ':' + os.environ['PATH']}):
                with self.assertRaises(ValueError) as failure:
                    library.discover(Path('/somewhere'), 1)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIn('within 1s', str(failure.exception))

    def test_refresh_cli_takes_a_snapshot_or_discover_not_both_nor_neither(self):
        cmd = [sys.executable, str(ROOT / 'bin/skill-library.py'), 'refresh']
        for extra in ([], ['snap.json', '--discover']):
            result = subprocess.run(cmd + extra, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, extra)
            self.assertIn('exactly one', result.stderr)

    @NEEDS_TOMLLIB
    def test_empty_discovery_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / 'snapshot.json'
            snapshot.write_text('{"data": []}')
            with self.assertRaises(ValueError):
                library.refresh(snapshot, root / 'index.json', root / 'config.toml', True)


if __name__ == '__main__':
    unittest.main()
