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
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('aos_open_delegate', ROOT / 'bin/aos-delegate.py')
delegate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delegate)
RUNTIMES = {'opencode': 'opencode', 'codex-cli': 'codex', 'claude-code': 'claude'}
CAPABILITIES = {name: dict(read_repo=True, write_repo=True, shell=name != 'claude-code',
                         structured_output=True, provider_override=True, model_override=True,
                         sandbox=name != 'opencode', network=False, subagent=False,
                         timeout=True, usage_reporting=True, json_output=True, tool_calling=True)
                for name in RUNTIMES}


def policy():
    return json.loads((ROOT / 'config/open-models.json').read_text())


def resolve(config=None, slot='primary', runtime=None, model=None, provider=None, required_capabilities=()):
    config = policy() if config is None else config
    settings = config.get('executors', {})
    identity = model or config.get('open', {}).get(slot)
    if not isinstance(identity, str) or '/' not in identity:
        raise ValueError('open model requires provider/model identity')
    prefix, model_id = identity.split('/', 1)
    provider = provider or prefix
    if runtime is None:
        ordered = [settings.get('default_runtime', 'opencode')] + settings.get('runtime_order', list(RUNTIMES))
        runtime = next((r for r in ordered if r in RUNTIMES and shutil.which(RUNTIMES[r])
                        and all(CAPABILITIES[r].get(c) for c in required_capabilities)), None)
    if runtime not in RUNTIMES:
        raise ValueError('no compatible open runtime installed or invalid runtime')
    if any(not CAPABILITIES[runtime].get(c) for c in required_capabilities):
        raise ValueError('open runtime lacks a required capability')
    return dict(role='executor', runtime=runtime, provider=provider, model=model_id,
                model_ref=f'{provider}/{model_id}', runtime_model_pair=f'{runtime}|{provider}|{model_id}',
                capabilities=CAPABILITIES[runtime].copy())



@lru_cache(maxsize=None)
def check_runtime(runtime):
    if runtime == 'opencode':
        return
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
    if not key and configured.get('opencode_credentials'):
        key = delegate.user_config().get('provider', {}).get(provider, {}).get('options', {}).get('apiKey')
        if isinstance(key, str):
            key = delegate.expand_env(key)
            key = re.sub(r'\{file:([^{}]+)\}', lambda m: Path(m[1]).expanduser().read_text().strip(), key)
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
    cmd += ['--enable', 'skip_host_skill_discovery']
    for feature in ('apps', 'plugins', 'hooks', 'multi_agent', 'browser_use', 'browser_use_external',
                    'computer_use', 'image_generation', 'in_app_browser', 'in_app_local_automation',
                    'remote_control', 'skill_search', 'skill_mcp_dependency_install', 'view_image', 'shell_snapshot'):
        cmd += ['--disable', feature]
    return cmd + ['--', brief]


def codex_rules(roots=None):
    """Narrow inherited approvals: native allow rules can bypass the sandbox.

    Never evaluate Starlark or expose user rule arguments inside the workspace.
    Dynamic policy cannot be safely translated and blocks this adapter.
    """
    patterns = [shlex.split(x.split('*')[0].rstrip()) for x in delegate.DENIED_BASH]
    patterns.append(['npx'])
    roots = roots if roots is not None else [Path.home() / '.codex/rules', Path('/etc/codex/rules')]
    for root in roots:
        for path in sorted(Path(root).glob('*.rules')):
            try:
                tree = ast.parse(path.read_text())
                for node in tree.body:
                    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                            and isinstance(node.value.func, ast.Name) and node.value.func.id == 'prefix_rule'):
                        raise ValueError('unsupported policy expression')
                    values = {k.arg: ast.literal_eval(k.value) for k in node.value.keywords}
                    if node.value.args or 'pattern' not in values:
                        raise ValueError('unsupported policy arguments')
                    if values.get('decision', 'allow') == 'allow':
                        pattern = values['pattern']
                        if not isinstance(pattern, list) or not pattern:
                            raise ValueError('invalid policy pattern')
                        patterns.append(pattern)
            except (OSError, SyntaxError, ValueError, TypeError):
                raise ValueError('inherited Codex rules cannot be safely restricted') from None
    return '\n'.join('prefix_rule(pattern=' + json.dumps(p) + ', decision="forbidden")' for p in patterns)


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


class Stream:
    """Normalize native events for the existing timeout/step monitor, retaining usage."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.usage = dict(delegate.EMPTY_USAGE)
        self.reply = ''
        self.error = None
        self.tool_results = []

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
                    self.tool_results.append({k: item.get(k) for k in ('command', 'exit_code', 'aggregated_output')})
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
    """No-repository, no-write open inference for classification without OpenCode."""
    identity = resolve(model=model, runtime=runtime)
    runtime = identity['runtime']
    if runtime == 'opencode':
        raise ValueError('OpenCode classification uses the existing guarded classifier')
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
            cmd[-2:-2] = ['--skip-git-repo-check', '--disable', 'shell_tool',
                          '-c', 'permissions.aos-open=' + toml(readonly)]
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
        permission_profile='GREEN'):
    if role not in ('executor', 'fixer'):
        raise ValueError('write-capable open execution is reserved for executor/fixer roles')
    if permission_profile != 'GREEN':
        raise ValueError('only GREEN open execution is implemented; no automatic privilege increase')
    identity = resolve(model=model, runtime=runtime, provider=provider)
    runtime = identity['runtime']
    repo = Path(repo).resolve()
    if not shutil.which(RUNTIMES[runtime]):
        raise ValueError('requested open runtime unavailable')
    stream = None
    runner = None
    if runtime != 'opencode':
        if max_cost is not None:
            raise ValueError('runtime does not report gateway cost; use explicit time/step bounds')
        custom = set(delegate.user_config().get('permission', {})) - {'aos_critical'}
        if custom:
            raise ValueError('custom OpenCode permissions need an explicit equivalent profile before changing runtime')
        check_runtime(runtime)
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
            rules = None
            runtime_tmp = None
            rules_tmp = None
            try:
                if runtime == 'codex-cli':
                    rules = workdir / '.codex/rules'
                    rules.mkdir(parents=True, exist_ok=False)
                    (rules.parent / 'config.toml').write_text('# AOS task-scoped rule layer.\n')
                    # Native deny rules retain user/managed rules. Wildcard commands
                    # narrow to their command prefix rather than weakening a deny.
                    # Rule arguments may contain private paths or credentials.
                    # The harness reads this external file; workspace tools cannot.
                    rules_tmp = tempfile.TemporaryDirectory(prefix='aos-private-rules-')
                    private_rules = Path(rules_tmp.name) / 'aos-open.rules'
                    private_rules.write_text(codex_rules())
                    private_rules.chmod(0o600)
                    (rules / 'aos-open.rules').symlink_to(private_rules)
                    env['AOS_OPEN_API_KEY'] = key
                    cmd = codex_command(workdir, identity['model'], prompt, endpoint)
                else:
                    runtime_tmp = tempfile.TemporaryDirectory(prefix='aos-claude-runtime-')
                    env.update(ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_API_KEY='', ANTHROPIC_BASE_URL=endpoint,
                               CLAUDE_CODE_TMPDIR=runtime_tmp.name, CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1')
                    cmd = claude_command(workdir, identity['model'], prompt, Path(runtime_tmp.name).resolve())
                code, _, err = delegate.invoke(cmd, workdir, seconds, None, steps, env, event_adapter=stream)
                return code, stream.normalized().replace(key, '[REDACTED]'), err.replace(key, '[REDACTED]')
            finally:
                if runtime_tmp:
                    runtime_tmp.cleanup()
                if rules:
                    (rules / 'aos-open.rules').unlink(missing_ok=True)
                    rules.rmdir()
                    (rules.parent / 'config.toml').unlink()
                    rules.parent.rmdir()
                if rules_tmp:
                    rules_tmp.cleanup()

    result = delegate.run(repo, identity['model_ref'], brief, timeout, allow_dirty,
                          max_cost=max_cost, max_steps=max_steps, state_file=state_file, runner=runner)
    result.update(role=role, runtime=runtime, provider=identity['provider'],
                  executor_model=identity['model'], task_id=task_id,
                  runtime_model_pair=identity['runtime_model_pair'], permission_profile=permission_profile,
                  result=result.get('reply', ''), files_changed=sorted(delegate.dirty_paths(repo) or [])
                  if not result.get('git_meta_changed') else None, tests=[])
    if stream:
        result['usage'] = stream.usage
        result['tool_results'] = json.loads(json.dumps(stream.tool_results[-20:]).replace(key, '[REDACTED]'))
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
