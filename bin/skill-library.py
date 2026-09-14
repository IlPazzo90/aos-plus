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
    rows = {s['path']: s for row in data for s in row['skills']}
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
    update = sub.add_parser('refresh', help='Use a fresh Codex skills/list JSON result; dry run by default')
    update.add_argument('snapshot', type=Path)
    update.add_argument('--config', type=Path, default=Path.home() / '.codex/config.toml')
    update.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'refresh':
            ensure_refresh_runtime()
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
