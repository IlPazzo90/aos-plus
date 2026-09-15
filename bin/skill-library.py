#!/usr/bin/env python3
"""Search original skill sources and maintain an opt-in Codex metadata catalog."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BEGIN = '# BEGIN AOS ON-DEMAND SKILLS\n'
END = '# END AOS ON-DEMAND SKILLS\n'


def read_index(path):
    return json.loads(path.read_text())


def source_path(entry):
    path = Path(entry['path']).expanduser()
    if path.is_file():
        return path
    # Never silently choose among cached versions. Refresh from discovery instead.
    return None


def search(entries, query, limit):
    words = query.casefold().split()
    found = []
    for entry in entries:
        name = entry['name'].casefold()
        hay = ' '.join((name, entry['description'], entry['path'])).casefold()
        if all(word in hay for word in words):
            score = 0 if name == query.casefold() else 1 if name.split(':')[-1] == query.casefold() else 2
            found.append((score, not entry.get('preferred', False), name, entry['path'], entry))
    found.sort(key=lambda item: item[:4])
    return [item[-1] for item in found[:limit]], len(found)


def refresh(snapshot, index, config, apply):
    import tomllib
    data = json.loads(snapshot.read_text())['data']
    if not data or any(row.get('errors') for row in data):
        raise ValueError('Discovery empty or contains errors; no files changed')
    # A fixture under a collection's test tree is discovered like a skill and is not one.
    rows = {s['path']: s for row in data for s in row['skills'] if '/test/fixtures/' not in s['path']}
    if not rows:
        raise ValueError('Empty skill inventory; no files changed')
    core = set(read_index(ROOT / 'catalog/core.json'))
    old = {e['path']: e for e in read_index(index)} if index.exists() else {}
    entries = []
    for s in rows.values():
        # Keep initial discovery preference across our own disable/reload cycle.
        preferred = old.get(s['path'], {}).get('preferred', s.get('enabled', False))
        entries.append({k: s.get(k, '') for k in ('name', 'path', 'description')} | {'preferred': preferred})
    text = config.read_text()
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError('Malformed managed block; no files changed')
    base = re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END), '', text, flags=re.S)
    existing = {e['path']: e for e in tomllib.loads(base).get('skills', {}).get('config', [])}
    hidden = sorted(p for p, s in rows.items() if s['name'] not in core and p not in existing)
    block = BEGIN + ''.join('\n[[skills.config]]\npath = ' + json.dumps(p) + '\nenabled = false\n' for p in hidden) + END
    candidate = base.rstrip() + '\n\n' + block
    tomllib.loads(candidate)
    print(json.dumps({'indexed': len(entries), 'managed_exclusions': len(hidden), 'apply': apply}))
    if not apply:
        return
    # Preserve unrelated config bytes and save a complete backup before mutation.
    if candidate != text:
        backup = config.with_name(config.name + '.backup-lazy-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        shutil.copy2(config, backup)
        config.write_text(candidate)
        print('Backup: ' + str(backup))
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(json.dumps(sorted(entries, key=lambda e: (e['name'], e['path'])), ensure_ascii=False, indent=2) + '\n')


def discover(cwd, timeout):
    """Ask a local `codex app-server` for its skill inventory; return the skills/list result.

    The snapshot used to be produced by hand from a twenty-line scratch script, which
    is the reason the catalog stayed eighty skills behind: a step nobody can run from
    the tool itself is a step that runs when somebody remembers.
    """
    import queue
    import threading
    import time
    codex = shutil.which('codex')
    if codex is None:
        raise ValueError('codex not on PATH; pass a skills/list snapshot instead of --discover')
    process = subprocess.Popen([codex, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    try:
        def send(message):
            process.stdin.write(json.dumps(message) + '\n')
            process.stdin.flush()
        send({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
              'params': {'clientInfo': {'name': 'aos-skill-library', 'version': '1.0'}}})
        send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
        send({'jsonrpc': '2.0', 'id': 2, 'method': 'skills/list',
              'params': {'cwds': [str(cwd)], 'forceReload': True, 'includeDisabled': True}})
        # readline() blocks: a server that stays open and silent would have made the
        # deadline below decorative. A reader thread feeds a queue and the wait has a bound.
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in iter(process.stdout.readline, '')] + [lines.put(None)],
                         daemon=True).start()
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            # An error on either request is a failed handshake, not noise: a server that
            # refuses initialize and then answers skills/list is not one to trust.
            if message.get('id') in (1, 2) and 'error' in message:
                error = message['error'] if isinstance(message['error'], dict) else {}
                which = 'initialize' if message['id'] == 1 else 'skills/list'
                raise ValueError('codex %s failed: %s' % (which, str(error.get('message', ''))[:200]))
            if message.get('id') == 2:
                result = message.get('result')
                if not isinstance(result, dict) or 'data' not in result:
                    raise ValueError('codex skills/list returned no data')
                return result
        raise ValueError('codex app-server gave no skills/list result within %ss' % timeout)
    finally:
        process.kill()
        process.wait()


def ensure_refresh_runtime():
    """Search works on system Python; TOML maintenance requires stdlib tomllib."""
    try:
        import tomllib
        return
    except ImportError:
        pass
    candidates = set()
    for directory in os.get_exec_path():
        for candidate in Path(directory).glob('python3.*'):
            if re.fullmatch(r'python3\.\d+', candidate.name) and os.access(candidate, os.X_OK):
                candidates.add(candidate.resolve())
    for candidate in sorted(candidates, key=lambda p: p.name, reverse=True):
        try:
            check = subprocess.run([str(candidate), '-c', 'import tomllib'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if check.returncode == 0:
            os.execv(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise ValueError('Refresh requires Python 3.11+ with tomllib on PATH; search remains available')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, default=ROOT / 'catalog/index.json')
    sub = parser.add_subparsers(dest='command', required=True)
    find = sub.add_parser('search')
    find.add_argument('query')
    find.add_argument('--limit', type=int, default=5)
    update = sub.add_parser('refresh', help='Use a Codex skills/list JSON result, or --discover to ask codex app-server; dry run by default')
    update.add_argument('snapshot', type=Path, nargs='?')
    update.add_argument('--discover', action='store_true', help='run codex app-server and save its skills/list result under tmp/')
    update.add_argument('--discover-cwd', type=Path, default=Path.home())
    update.add_argument('--timeout', type=int, default=120)
    update.add_argument('--config', type=Path, default=Path.home() / '.codex/config.toml')
    update.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'refresh':
            if bool(args.snapshot) == bool(args.discover):
                parser.error('refresh takes exactly one of: a snapshot file, or --discover')
            ensure_refresh_runtime()
            if args.discover:
                result = discover(args.discover_cwd, args.timeout)
                # tmp/ is ignored by Git; the snapshot is evidence for this refresh, not a source.
                snapshot = ROOT / 'tmp' / ('skills-list-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_text(json.dumps(result))
                print('Snapshot: ' + str(snapshot))
                args.snapshot = snapshot
            refresh(args.snapshot, args.index, args.config, args.apply)
            return 0
        if not args.query.strip() or not 1 <= args.limit <= 20:
            parser.error('query must be nonempty; limit must be 1..20')
        matches, count = search(read_index(args.index), args.query, args.limit)
        print(f'{count} matches; showing {len(matches)}')
        for entry in matches:
            path = source_path(entry)
            # Bound descriptions independently from the full on-disk searchable index.
            desc = ' '.join(entry['description'].split())[:240]
            print(f"{entry['name']}\n  {entry['path']}\n  {'OK' if path else 'MISSING — refresh discovery; do not guess'} | {desc}")
        return 0 if matches else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('skill-library: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
