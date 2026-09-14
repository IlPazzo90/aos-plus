"""Behavior checks for local discovery, bounds, and config preservation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('library', ROOT / 'bin/skill-library.py')
library = importlib.util.module_from_spec(spec)
spec.loader.exec_module(library)


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

    def test_no_match_and_missing_file(self):
        self.assertEqual(library.search([], 'missing', 3), ([], 0))
        self.assertIsNone(library.source_path({'path': '/nonexistent/skill/SKILL.md'}))

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

    def test_empty_discovery_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = root / 'snapshot.json'
            snapshot.write_text('{"data": []}')
            with self.assertRaises(ValueError):
                library.refresh(snapshot, root / 'index.json', root / 'config.toml', True)


if __name__ == '__main__':
    unittest.main()
