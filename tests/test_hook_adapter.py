import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'bin/codex-hook-adapter.py'
spec = importlib.util.spec_from_file_location('adapter', SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class AdapterTest(unittest.TestCase):
    def test_findings_preserved(self):
        original = {'metrics': {'pv': 2}, 'rewakeSummary': 'review',
                    'decision': 'block', 'reason': 'Security finding',
                    'systemMessage': 'Review required'}
        self.assertEqual(json.loads(adapter.adapt(json.dumps(original))),
                         {k: v for k, v in original.items() if k not in ('metrics', 'rewakeSummary')})

    def test_clean_and_unknown_fields(self):
        self.assertEqual(adapter.adapt(''), '')
        self.assertEqual(json.loads(adapter.adapt('{"metrics": {}}')), {})
        self.assertEqual(json.loads(adapter.adapt('{"futureField": 1}')), {'futureField': 1})
        with self.assertRaises(ValueError):
            adapter.adapt('not json')

    def test_process_channels(self):
        command = [sys.executable, str(SCRIPT), '--', sys.executable, '-c']
        result = subprocess.run(command + ["import sys; print(sys.stdin.read()); print('finding', file=sys.stderr); sys.exit(2)"],
                                input='original output', text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, 'original output\n')
        self.assertEqual(result.stderr, 'finding\n')
        result = subprocess.run(command + ["print('garbage')"], capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'invalid upstream', result.stderr)


if __name__ == '__main__':
    unittest.main()
