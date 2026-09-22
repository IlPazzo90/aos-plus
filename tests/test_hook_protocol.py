"""Optional installed-host regressions; no provider calls or user config writes."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


class InstalledStopHookTests(unittest.TestCase):
    def test_codex_stop_output_and_reentry(self):
        library = Path.home() / '.claude/skills/impeccable/scripts/hook-lib.mjs'
        if not library.is_file() or not shutil.which('node'):
            self.skipTest('optional Impeccable hook or Node unavailable')
        script = """
const {payload, runStopHook} = await import(process.argv[1]);
const results = [];
for (const input of ['malformed', JSON.stringify({hook_event_name:'Stop',
    session_id:'aos-test', turn_id:'test', stop_hook_active:true})]) {
  const result = await runStopHook({stdinJson:input,
    env:{IMPECCABLE_HOOK_HARNESS:'codex'},cwd:process.cwd()});
  results.push({code:result.exitCode,stdout:result.stdout});
}
console.log(JSON.stringify({stop:JSON.parse(payload('fix','Stop','codex')),
    empty:payload('','Stop','codex'),
    post:JSON.parse(payload('context','PostToolUse','codex')),results}));
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script, library.as_uri()],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        data = json.loads(result.stdout)
        self.assertEqual(data['stop'], {'decision': 'block', 'reason': 'fix'})
        self.assertEqual(data['empty'], '')
        self.assertEqual(data['post']['hookSpecificOutput']['hookEventName'], 'PostToolUse')
        for row in data['results']:
            self.assertEqual(row, {'code': 0, 'stdout': ''})
