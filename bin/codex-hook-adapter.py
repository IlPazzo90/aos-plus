#!/usr/bin/env python3
"""Run a hook, removing only Claude telemetry fields unsupported by Codex."""
import json
import subprocess
import sys


def adapt(output):
    if not output.strip():
        return output
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise ValueError('Hook output must be a JSON object')
    # Findings, decisions, context and error channels remain intact.
    payload.pop('metrics', None)
    payload.pop('rewakeSummary', None)
    return json.dumps(payload) + '\n'


def main():
    command = sys.argv[1:]
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        print('codex-hook-adapter: missing command', file=sys.stderr)
        return 1
    try:
        child = subprocess.run(command, input=sys.stdin.buffer.read(), stdout=subprocess.PIPE)
    except OSError as error:
        print('codex-hook-adapter: ' + str(error), file=sys.stderr)
        return 1
    if child.returncode != 0:
        sys.stdout.buffer.write(child.stdout)
        return child.returncode if child.returncode > 0 else 128 - child.returncode
    try:
        sys.stdout.write(adapt(child.stdout.decode('utf-8')))
    except (ValueError, UnicodeError) as error:
        # Do not hide malformed output or turn a failed check into success.
        print('codex-hook-adapter: invalid upstream output: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
