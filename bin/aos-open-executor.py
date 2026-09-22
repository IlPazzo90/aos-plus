#!/usr/bin/env python3
"""Open execution independent of the main host; no premium model substitution."""
import argparse
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import subprocess
from functools import lru_cache
from fnmatch import fnmatchcase
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('aos_open_delegate', ROOT / 'bin/aos-delegate.py')
delegate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delegate)
RUNTIMES = {'claude-code': 'claude', 'codex-cli': 'codex'}
OPEN_EXECUTION_RUNTIMES = ('claude-code', 'codex-cli')
# Claude Code is the open harness: file tools only, no shell, the host runs every
# check. The Codex adapter stays here but the policy keeps it out of open execution:
# Codex edits through its shell tool, and that tool's command policy did not hold a
# negative probe (2026-09-22: `npx` ran while files and network were denied), while
# disabling the shell leaves it with no file tools at all. Codex remains the premium
# host, planner, reviewer and escalation; re-enabling it as a worker needs a green
# probe taken with the shell on.
CAPABILITIES = {name: dict(read_repo=True, write_repo=True, shell=name == 'codex-cli',
                         structured_output=True, provider_override=True, model_override=True,
                         sandbox=True, network=False, subagent=False,
                         timeout=True, usage_reporting=True, json_output=True, tool_calling=True)
                for name in RUNTIMES}


def policy():
    return json.loads((ROOT / 'config/open-models.json').read_text())


def runtime_block(config, runtime):
    executors = config.get('executors', {})
    status = executors.get('runtime_status', {}) if isinstance(executors, dict) else {}
    entry = status.get(runtime, {}) if isinstance(status, dict) else {}
    if isinstance(entry, dict) and entry.get('open_execution') is False:
        return str(entry.get('reason') or 'disabled by runtime policy')
    return None


def resolve(config=None, slot='primary', runtime=None, model=None, provider=None, required_capabilities=(), probe_root=None):
    config = policy() if config is None else config
    settings = config.get('executors', {})
    identity = model or config.get('open', {}).get(slot)
    if not isinstance(identity, str) or '/' not in identity:
        raise ValueError('open model requires provider/model identity')
    prefix, model_id = identity.split('/', 1)
    provider = provider or prefix
    if runtime is None:
        ordered = [settings.get('default_runtime', 'claude-code')] + settings.get('runtime_order', list(RUNTIMES))
        runtime = next((r for r in ordered if r in OPEN_EXECUTION_RUNTIMES and shutil.which(RUNTIMES[r])
                        and not runtime_block(config, r)
                        and all(CAPABILITIES[r].get(c) for c in required_capabilities)), None)
    if runtime not in RUNTIMES:
        raise ValueError('no compatible open runtime installed or invalid runtime')
    if probe_root is None:
        # The isolation probe (aos-isolation.py) is the only caller allowed to run a
        # disabled runtime, and only inside its own disposable fixture.
        blocked = runtime_block(config, runtime)
        if blocked:
            raise ValueError(f'{runtime} open execution is disabled: {blocked}')
    catalog = config.get('model_catalog') if isinstance(config.get('model_catalog'), dict) else None
    # The identity that runs is `provider/model`, which a provider override can move
    # away from the one asked for (round 3: model=anthropic/fable provider=vercel ran
    # as vercel/fable). The catalog must know the identity that actually runs.
    identity = f'{provider}/{model_id}'
    entry = catalog.get(identity) if catalog else None
    if catalog and entry is None and probe_root is None:
        # Round 2 finding: the router blocked unknown models, this path did not, so
        # `--model vercel/missing` reached the harness. With a catalog, an unknown
        # reference is a broken routing decision, not a model to run.
        raise ValueError('open model missing from catalog: ' + identity)
    if isinstance(entry, dict) and runtime not in entry.get('compatible_runtimes', ()) and probe_root is None:
        # The probe tests a runtime the catalog may have dropped after a failed probe.
        raise ValueError('selected model is not compatible with the requested runtime')
    if any(not CAPABILITIES[runtime].get(c) for c in required_capabilities):
        raise ValueError('open runtime lacks a required capability')
    return dict(role='executor', runtime=runtime, provider=provider, model=model_id,
                model_ref=f'{provider}/{model_id}', runtime_model_pair=f'{runtime}|{provider}|{model_id}',
                capabilities=CAPABILITIES[runtime].copy())



@lru_cache(maxsize=None)
def check_runtime(runtime, probe=False):
    if not probe:
        blocked = runtime_block(policy(), runtime)
        if blocked:
            raise ValueError(f'{runtime} open execution is disabled: {blocked}')
    completed = subprocess.run([RUNTIMES[runtime], '--version'], text=True, capture_output=True, timeout=10)
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', completed.stdout)
    minimum = {'codex-cli': (0, 155, 1), 'claude-code': (2, 1, 278)}[runtime]
    if completed.returncode or not match or tuple(map(int, match.groups())) < minimum:
        raise ValueError('runtime version lacks the verified restrictive CLI contract')


def provider_config(provider, runtime):
    configured = policy().get('providers', {}).get(provider, {})
    endpoints = {'codex-cli': configured.get('responses_url'),
                 'claude-code': configured.get('anthropic_url')}
    endpoint = endpoints.get(runtime)
    if (not isinstance(endpoint, str) or urlparse(endpoint).scheme != 'https' or not urlparse(endpoint).netloc
            or urlparse(endpoint).username or urlparse(endpoint).query or urlparse(endpoint).fragment):
        raise ValueError('provider needs an HTTPS endpoint for this runtime')
    key_name = configured.get('api_key_env', 'AOS_OPEN_API_KEY')
    key = os.environ.get(key_name)
    key_file = configured.get('api_key_file')
    if not key and isinstance(key_file, str) and key_file.strip():
        # The host reads the credential file; the worker process receives the value
        # through its environment and a worker without a shell cannot read it back.
        path = Path(key_file).expanduser()
        if path.is_file():
            key = path.read_text().strip()
    if not isinstance(key, str) or not key.strip() or '{' in key:
        raise ValueError('open provider credential unavailable')
    return endpoint, key


def codex_permissions(repo):
    return {'extends': ':workspace', 'filesystem': {
        ':root': 'deny', ':minimal': 'read', ':tmpdir': 'deny', ':slash_tmp': 'deny',
        '/opt/homebrew': 'read', '/Library/Developer/CommandLineTools': 'read', '/Library/Frameworks': 'read',
        ':workspace_roots': {'.': 'write', '.git': 'read', '.codex': 'read', '.claude': 'read',
                             '**/.env*': 'deny', '**/*.pem': 'deny', '**/*.key': 'deny'}},
        'network': {'enabled': False}}


def toml(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
    return json.dumps(value)


def codex_command(repo, model, brief, endpoint):
    values = {'model_provider': 'aos_open', 'model_providers.aos_open': {
        'name': 'AOS Open Executor', 'base_url': endpoint, 'env_key': 'AOS_OPEN_API_KEY',
        'wire_api': 'responses', 'request_max_retries': 0, 'stream_max_retries': 0},
        'approval_policy': 'never', 'default_permissions': 'aos-open',
        'permissions.aos-open': codex_permissions(repo), 'shell_environment_policy.inherit': 'none',
        'shell_environment_policy.set': {'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
        'mcp_servers': {}, 'web_search': 'disabled', 'project_doc_max_bytes': 0,
        'model_reasoning_effort': 'low', 'projects': {str(repo): {'trust_level': 'trusted'}}}
    cmd = ['codex', 'exec', '--ignore-user-config', '--ephemeral', '--json', '-C', str(repo), '-m', model]
    for key, value in values.items():
        cmd += ['-c', key + '=' + toml(value)]
    # Codex delivers apply_patch through the shell tool: disabling it leaves the
    # worker with no file tools (probe 2026-09-22: 0 tool calls). The command policy
    # this relies on is exactly what the policy blocks until a probe proves it.
    cmd += ['--enable', 'skip_host_skill_discovery']
    for feature in ('apps', 'plugins', 'hooks', 'multi_agent', 'browser_use', 'browser_use_external',
                    'computer_use', 'image_generation', 'in_app_browser', 'in_app_local_automation',
                    'remote_control', 'skill_search', 'skill_mcp_dependency_install', 'view_image', 'shell_snapshot'):
        cmd += ['--disable', feature]
    return cmd + ['--', brief]


def claude_command(repo, model, brief, runtime_tmp=None):
    # Claude's strict sandbox currently fails its cwd bookkeeping on this host.
    # Keep Bash unavailable; deterministic verification belongs to the host.
    denies = ['Bash'] + ['Bash(' + pattern + ' *)' for pattern in delegate.DENIED_BASH]
    denies += ['Read(**/.env*)', 'Read(**/*.pem)', 'Read(**/*.key)',
               'Edit(.git/**)', 'Write(.git/**)', 'Edit(.claude/**)', 'Write(.claude/**)',
               'Edit(.codex/**)', 'Write(.codex/**)']
    user = Path.home() / '.claude/settings.json'
    if user.is_file():
        stored = json.loads(user.read_text()).get('permissions', {})
        denies += stored.get('deny', []) + stored.get('ask', [])
    settings = {'permissions': {'allow': ['Read', 'Glob', 'Grep', 'Edit', 'Write'],
                               'deny': denies, 'blockReadsOutsideWorkingDirectories': True},
                'sandbox': {'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False,
                            'autoAllowBashIfSandboxed': False, 'excludedCommands': [],
                            'filesystem': {'denyWrite': [str(repo / x) for x in ('.git', '.claude', '.codex')],
                                           'denyRead': ['/', str(repo / '.env'), str(repo / '.env.*'),
                                                        str(repo / '**/.env*'), str(repo / '**/*.pem'), str(repo / '**/*.key')],
                                           'allowRead': [str(repo), '/bin', '/usr', '/System', '/Library',
                                                         '/opt/homebrew', '/dev', '/private/etc'] + ([str(runtime_tmp)] if runtime_tmp else []),
                                           'allowWrite': [str(runtime_tmp)] if runtime_tmp else []},
                            'network': {'allowedDomains': [], 'allowLocalBinding': False, 'allowAllUnixSockets': False},
                            'credentials': {'envVars': [{'name': n, 'mode': 'deny'} for n in
                                                        ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', 'AOS_OPEN_API_KEY')]}}}
    return ['claude', '-p', '--safe-mode', '--restricted', '--disable-slash-commands',
            '--tools', 'Read,Glob,Grep,Edit,Write', '--permission-mode', 'dontAsk',
            '--no-session-persistence', '--output-format', 'stream-json', '--verbose',
            '--effort', 'low', '--model', model, '--settings', json.dumps(settings), '--', brief]


def os_isolation_for(config, runtime):
    """Configured OS-level isolation for a runtime: 'seatbelt' or 'none'."""
    status = config.get('executors', {}).get('runtime_status', {}).get(runtime, {})
    value = status.get('os_isolation', 'none') if isinstance(status, dict) else 'none'
    if value not in ('none', 'seatbelt'):
        raise ValueError('unsupported os_isolation: ' + str(value))
    return value


PROBE_MARKER = '.aos-isolation-probe'


def probe_fixture(root):
    """The disposable fixture root the isolation probe created, or a refusal.

    Only a directory under the temporary tree that carries the probe's marker file
    counts: `/` or a project directory can never unlock a disabled runtime.
    """
    root = Path(root).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if temp not in root.parents or not (root / PROBE_MARKER).is_file():
        raise ValueError('probe_root is not a fixture created by the isolation probe')
    return root


def _sb_path(path):
    return json.dumps(str(Path(path).resolve()))


def seatbelt_profile(repo, read_write=(), read_only=()):
    """macOS sandbox profile: last matching rule wins, so broad denies come first.

    Everything outside the user's home and temporary trees stays readable (the
    toolchain lives there); the home, every temp tree and every volume are denied;
    then the repo and the run's own directories are re-allowed; then the secrets
    inside the repo and the repository/runtime metadata are denied again. Symlinks
    resolve to their target before evaluation, so a link out of the repo is denied.
    """
    repo = Path(repo).resolve()
    escaped = re.escape(str(repo))
    lines = ['(version 1)', '(allow default)',
             '(deny file-read* file-write* (subpath ' + _sb_path(Path.home()) + '))',
             # Resolved names only: a rule on the `/tmp` symlink itself shadows every
             # later allow below `/private/tmp` on this macOS.
             '(deny file-read* file-write* (subpath "/Users") (subpath "/private/var/folders")'
             ' (subpath "/private/tmp") (subpath "/Volumes"))',
             # `(subpath "/")` cannot be re-allowed below it on this macOS; the regex can.
             '(deny file-write* (regex "^/"))',
             '(allow file-write* (subpath "/dev"))']
    for path in read_only:
        lines.append('(allow file-read* (subpath ' + _sb_path(path) + '))')
    for path in (repo, *read_write):
        lines.append('(allow file-read* file-write* (subpath ' + _sb_path(path) + '))')
    lines.append('(deny file-read* file-write* (regex "^' + escaped + '/(.*/)?\\.env") (regex "^' + escaped
                 + '/.*\\.pem$") (regex "^' + escaped + '/.*\\.key$"))')
    lines.append('(deny file-write* (subpath ' + _sb_path(repo / '.git') + ') (subpath ' + _sb_path(repo / '.claude')
                 + ') (subpath ' + _sb_path(repo / '.codex') + '))')
    return '\n'.join(lines) + '\n'


def seatbelt_wrap(cmd, profile):
    if not shutil.which('sandbox-exec'):
        raise ValueError('seatbelt isolation requested but sandbox-exec is unavailable on this host')
    return ['sandbox-exec', '-p', profile] + list(cmd)


class Stream:
    """Normalize native events for the existing timeout/step monitor, retaining usage."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.usage = dict(delegate.EMPTY_USAGE)
        self.reply = ''
        self.error = None
        self.tool_results = []
        self.tool_calls = []

    def __call__(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            return ''
        if not isinstance(event, dict):
            return ''
        step = False
        if self.runtime == 'codex-cli':
            kind, item = event.get('type'), event.get('item', {})
            if not isinstance(item, dict):
                item = {}
            if kind == 'item.completed':
                step = item.get('type') in ('command_execution', 'file_change', 'mcp_tool_call')
                if item.get('type') == 'command_execution':
                    self.tool_results.append({'tool_use_id': item.get('id'), 'is_error': item.get('exit_code') != 0,
                                              **{k: item.get(k) for k in ('command', 'exit_code', 'aggregated_output')}})
                    self.tool_calls.append({'id': item.get('id'), 'tool': 'shell',
                                            'input': {'command': item.get('command')}})
                if item.get('type') == 'file_change':
                    self.tool_calls.append({'id': item.get('id'), 'tool': 'file_change',
                                            'input': {'changes': item.get('changes')}})
                    self.tool_results.append({'tool_use_id': item.get('id'),
                                              'is_error': item.get('status') not in (None, 'completed')})
                if item.get('type') == 'agent_message':
                    self.reply = item.get('text', '')
            if kind == 'turn.completed':
                usage = event.get('usage', {})
                self.usage.update(input_tokens=usage.get('input_tokens'), output_tokens=usage.get('output_tokens'),
                                  cache_read_tokens=usage.get('cached_input_tokens'))
            if kind in ('error', 'turn.failed'):
                self.error = str(event.get('message') or event.get('error'))
        else:
            if event.get('type') == 'user':
                for part in event.get('message', {}).get('content', []):
                    if isinstance(part, dict) and part.get('type') == 'tool_result':
                        self.tool_results.append({'tool_use_id': part.get('tool_use_id'),
                                                  'is_error': part.get('is_error', False),
                                                  'output': part.get('content')})
            if event.get('type') == 'assistant':
                step = True
                for part in event.get('message', {}).get('content', []):
                    if isinstance(part, dict) and part.get('type') == 'tool_use':
                        self.tool_calls.append({'id': part.get('id'), 'tool': part.get('name'),
                                                'input': part.get('input')})
            if event.get('type') == 'result':
                usage = event.get('usage', {})
                self.usage.update(input_tokens=usage.get('input_tokens'), output_tokens=usage.get('output_tokens'),
                                  cache_read_tokens=usage.get('cache_read_input_tokens'))
                # Claude reports an estimate even for unknown custom-model prices.
                # Only gateway billing or an explicit known basis can establish cost.
                self.reply = event.get('result', '')
                if event.get('is_error'):
                    self.error = str(event.get('errors') or self.reply)
                if event.get('permission_denials'):
                    self.error = 'permission denied; do not escalate as a model failure'
        if step:
            self.usage['steps'] += 1
            return json.dumps({'type': 'step_finish', 'part': {}}) + '\n'
        return ''

    def normalized(self):
        u = self.usage
        events = [{'type': 'step_finish', 'part': {'tokens': {
            'input': u['input_tokens'], 'output': u['output_tokens'],
            'reasoning': u['reasoning_tokens'], 'cache': {'read': u['cache_read_tokens']}},
            'cost': u['cost_usd']}}, {'type': 'text', 'part': {'text': self.reply}}]
        if self.error:
            events.append({'type': 'error', 'error': {'data': {'message': self.error}}})
        return '\n'.join(json.dumps(e) for e in events)


def infer(prompt, model, runtime=None):
    """No-repository, no-write open inference (classification) on an enabled harness."""
    identity = resolve(model=model, runtime=runtime)
    runtime = identity['runtime']
    check_runtime(runtime)
    endpoint, key = provider_config(identity['provider'], runtime)
    stream = Stream(runtime)
    with tempfile.TemporaryDirectory(prefix='aos-open-infer-') as directory:
        repo = Path(directory).resolve()
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG') if k in os.environ}
        if runtime == 'codex-cli':
            cmd = codex_command(repo, identity['model'], prompt, endpoint)
            readonly = codex_permissions(repo)
            readonly['extends'] = ':read-only'
            readonly['filesystem'][':workspace_roots']['.'] = 'read'
            cmd[-2:-2] = ['--skip-git-repo-check', '-c', 'permissions.aos-open=' + toml(readonly)]
            env['AOS_OPEN_API_KEY'] = key
        else:
            cmd = claude_command(repo, identity['model'], prompt)
            cmd[cmd.index('--tools') + 1] = ''
            env.update(ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_API_KEY='', ANTHROPIC_BASE_URL=endpoint)
        code, _, err = delegate.invoke(cmd, repo, 90, max_steps=3, env=env, event_adapter=stream)
    return dict(exit_code=code, reply=stream.reply.replace(key, '[REDACTED]'), usage=stream.usage,
                error=stream.error.replace(key, '[REDACTED]') if stream.error else None, runtime=runtime, stderr_tail=err.replace(key, '[REDACTED]')[-1000:])


def run(repo, model, brief, timeout, allow_dirty, max_cost=None, max_steps=60,
        state_file=None, runtime=None, provider=None, role='executor', task_id=None,
        permission_profile='GREEN', probe_root=None, os_isolation=None):
    if role not in ('executor', 'fixer'):
        raise ValueError('write-capable open execution is reserved for executor/fixer roles')
    if permission_profile != 'GREEN':
        raise ValueError('only GREEN open execution is implemented; no automatic privilege increase')
    repo = Path(repo).resolve()
    if probe_root is not None:
        probe_root = probe_fixture(probe_root)
        if probe_root not in repo.parents:
            raise ValueError('a probe may only run inside its own disposable fixture')
    elif os_isolation is not None:
        # Production always applies the policy's isolation; only a probe may vary it.
        raise ValueError('os_isolation can be chosen only by the isolation probe')
    identity = resolve(model=model, runtime=runtime, provider=provider, probe_root=probe_root)
    runtime = identity['runtime']
    if not shutil.which(RUNTIMES[runtime]):
        raise ValueError('requested open runtime unavailable')
    configured_isolation = os_isolation_for(policy(), runtime)
    isolation = os_isolation if os_isolation is not None else configured_isolation
    if isolation not in ('none', 'seatbelt'):
        raise ValueError('unsupported os_isolation: ' + str(isolation))
    if isolation == 'seatbelt' and runtime == 'codex-cli':
        # Codex refuses to start inside an outer seatbelt (probe 2026-09-22: exit 1 in
        # 6 s, "Operation not permitted"); its own workspace sandbox is its OS layer.
        raise ValueError('seatbelt isolation is not supported for codex-cli')
    if max_cost is not None:
        raise ValueError('native harnesses do not report gateway cost; use explicit time/step bounds')
    check_runtime(runtime, probe=probe_root is not None)
    endpoint, key = provider_config(identity['provider'], runtime)
    # No project runtime configuration can merge after the task guard.
    for base in (repo, *repo.parents):
        if base == Path.home():
            break
        if any((base / name).exists() for name in ('.codex', '.claude')):
            raise ValueError('project/ancestor runtime configuration requires isolation before delegation')
    stream = Stream(runtime)

    def runner(workdir, unused_model, prompt, seconds, cost, steps):
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL') if k in os.environ}
        runtime_tmp = None
        try:
            if runtime == 'codex-cli':
                env['AOS_OPEN_API_KEY'] = key
                cmd = codex_command(workdir, identity['model'], prompt, endpoint)
            else:
                runtime_tmp = tempfile.TemporaryDirectory(prefix='aos-claude-runtime-')
                env.update(ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_API_KEY='', ANTHROPIC_BASE_URL=endpoint,
                           CLAUDE_CODE_TMPDIR=runtime_tmp.name, CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1')
                cmd = claude_command(workdir, identity['model'], prompt, Path(runtime_tmp.name).resolve())
                if isolation == 'seatbelt':
                    env['TMPDIR'] = runtime_tmp.name
                    cmd = seatbelt_wrap(cmd, seatbelt_profile(
                        workdir, read_write=[runtime_tmp.name], read_only=[]))
            code, _, err = delegate.invoke(cmd, workdir, seconds, None, steps, env, event_adapter=stream)
            return code, stream.normalized().replace(key, '[REDACTED]'), err.replace(key, '[REDACTED]')
        finally:
            if runtime_tmp:
                runtime_tmp.cleanup()


    result = delegate.run(repo, identity['model_ref'], brief, timeout, allow_dirty,
                          max_cost=max_cost, max_steps=max_steps, state_file=state_file, runner=runner)
    catalog = policy().get('model_catalog', {}).get(identity['model_ref'], {})
    result.update(role=role, runtime=runtime, provider=identity['provider'],
                  executor_model=identity['model'], task_id=task_id,
                  runtime_model_pair=identity['runtime_model_pair'], permission_profile=permission_profile,
                  cost_class=catalog.get('cost_class'), file_tools_isolation=True,
                  isolation_level='file_tools_policy+seatbelt' if isolation == 'seatbelt' else 'file_tools_policy',
                  os_isolation=isolation,
                  isolation_verified=(probe_root is None and isolation == configured_isolation
                                      and bool(policy().get('executors', {}).get('runtime_status', {})
                                               .get(runtime, {}).get('isolation_verified'))),
                  result=result.get('reply', ''), files_changed=sorted(delegate.dirty_paths(repo) or [])
                  if not result.get('git_meta_changed') else None, tests=[])
    result['usage'] = stream.usage
    result['tool_results'] = json.loads(json.dumps(stream.tool_results[-20:]).replace(key, '[REDACTED]'))
    result['tool_calls'] = json.loads(json.dumps(stream.tool_calls[-60:]).replace(key, '[REDACTED]'))
    result['cost'] = result['usage'].get('cost_usd')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--repo', required=True)
    p.add_argument('--brief', required=True)
    p.add_argument('--runtime', choices=RUNTIMES)
    p.add_argument('--slot', choices=('primary', 'fallback'), default='primary')
    p.add_argument('--model')
    p.add_argument('--task-id')
    p.add_argument('--timeout', type=int, default=900)
    p.add_argument('--state-file', type=Path)
    args = p.parse_args()
    try:
        selection = resolve(slot=args.slot, runtime=args.runtime, model=args.model)
        result = run(args.repo, selection['model_ref'], Path(args.brief).read_text(), args.timeout, False,
                     runtime=selection['runtime'], state_file=args.state_file, task_id=args.task_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(result['exit_code'] != 0 or bool(result['error']))
    except SystemExit as refusal:
        print(json.dumps({'error': 'delegate refused the task', 'blocked': True,
                          'exit_code': refusal.code, 'runtime': args.runtime}))
        return refusal.code
    except (ValueError, OSError) as error:
        print(json.dumps({'error': str(error), 'blocked': True}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
