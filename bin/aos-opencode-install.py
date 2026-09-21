#!/usr/bin/env python3
"""Install the AOS OpenCode entry without copying its source files."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
try:
    import tomllib
except ImportError:  # Python < 3.11; only --import-skills needs TOML.
    tomllib = None
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "opencode" / "aos-bridge.mjs"
POLICY = Path(__file__).resolve().parents[1] / "config" / "open-models.json"
MCP_EXCLUDED = {"node_repl", "computer-use"}
INSTRUCTIONS = """# AOS OpenCode host instructions

- Keep the user's working directory; read applicable AGENTS.md, CLAUDE.md, and area CONTEXT.md files.
- AOS is always entered through the installed OpenCode plugin. Do not invoke a second classifier.
- Claude host tools are available only through the AOS bridge.
- Codex app capabilities are unavailable in this host; do not claim or simulate them.
"""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def paths(home):
    root = Path(home) / ".config" / "opencode"
    config = root / ("opencode.jsonc" if (root / "opencode.jsonc").exists() else "opencode.json")
    return root, config, root / "plugins" / "aos-entry.mjs", root / "aos-instructions.md", root / ".aos-install-state.json"


def loader():
    source = SOURCE.as_uri()
    return (f'import {{ tool }} from "@opencode-ai/plugin";\n'
            f'import {{ createHooks }} from "{source}";\n\n'
            'export const AosPlugin = async ({ client, directory }) =>\n'
            '  createHooks({ client, directory }, tool);\n').encode()


def atomic_write(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".aos-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def skip_space(text, pos):
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
        elif text.startswith("//", pos):
            pos = text.find("\n", pos)
            if pos < 0:
                return len(text)
        elif text.startswith("/*", pos):
            end = text.find("*/", pos + 2)
            if end < 0:
                raise ValueError("commento JSONC non terminato")
            pos = end + 2
        else:
            return pos
    return pos


def string_end(text, pos):
    pos += 1
    while pos < len(text):
        if text[pos] == "\\":
            pos += 2
        elif text[pos] == '"':
            return pos + 1
        else:
            pos += 1
    raise ValueError("stringa JSONC non terminata")


def matching(text, pos, opening, closing):
    depth = 0
    while pos < len(text):
        if text[pos] == '"':
            pos = string_end(text, pos)
            continue
        if text.startswith("//", pos) or text.startswith("/*", pos):
            pos = skip_space(text, pos)
            continue
        if text[pos] == opening:
            depth += 1
        elif text[pos] == closing:
            depth -= 1
            if not depth:
                return pos
        pos += 1
    raise ValueError(f"{closing} JSONC mancante")


def clean_jsonc(text):
    output = []
    pos = 0
    while pos < len(text):
        if text[pos] == '"':
            end = string_end(text, pos)
            output.append(text[pos:end])
            pos = end
        elif text.startswith("//", pos):
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end
        elif text.startswith("/*", pos):
            end = text.find("*/", pos + 2)
            if end < 0:
                raise ValueError("commento JSONC non terminato")
            pos = end + 2
        else:
            output.append(text[pos])
            pos += 1
    stripped = "".join(output)
    output = []
    pos = 0
    while pos < len(stripped):
        if stripped[pos] == '"':
            end = string_end(stripped, pos)
            output.append(stripped[pos:end])
            pos = end
        elif stripped[pos] == "," and stripped[skip_space(stripped, pos + 1):skip_space(stripped, pos + 1) + 1] in ("}", "]"):
            pos += 1
        else:
            output.append(stripped[pos])
            pos += 1
    return "".join(output)


def top_property(text, wanted):
    start = skip_space(text, 0)
    end = matching(text, start, "{", "}")
    pos = start + 1
    while pos < end:
        pos = skip_space(text, pos)
        if pos < end and text[pos] == ",":
            pos += 1
            continue
        if pos >= end or text[pos] != '"':
            break
        key_end = string_end(text, pos)
        key = json.loads(text[pos:key_end])
        colon = skip_space(text, key_end)
        value = skip_space(text, colon + 1)
        if value < end and text[value] in "[{":
            value_end = matching(text, value, text[value], "]" if text[value] == "[" else "}") + 1
        elif value < end and text[value] == '"':
            value_end = string_end(text, value)
        else:
            value_end = value
            while value_end < end and text[value_end] not in ",}":
                if text.startswith("//", value_end):
                    newline = text.find("\n", value_end + 2)
                    value_end = end if newline < 0 else newline + 1
                elif text.startswith("/*", value_end):
                    close = text.find("*/", value_end + 2)
                    if close < 0:
                        raise ValueError("commento JSONC non terminato")
                    value_end = close + 2
                else:
                    value_end += 1
        if key == wanted:
            return value, value_end
        pos = value_end
    return None


def set_top(raw, key, value, preserve=True):
    text = raw.decode()
    found = top_property(text, key)
    encoded = json.dumps(value, indent=2)
    if found:
        if preserve:
            return raw
        return (text[:found[0]] + encoded + text[found[1]:]).encode()
    start = skip_space(text, 0)
    parsed = json.loads(clean_jsonc(text))
    if not isinstance(parsed, dict):
        raise ValueError("la configurazione deve essere un oggetto")
    comma = "," if parsed else ""
    addition = "\n  " + json.dumps(key) + ": " + encoded + comma + "\n"
    return (text[:start + 1] + addition + text[start + 1:]).encode()


def add_instruction(raw, instruction):
    raw = raw or b"{}\n"
    values = json.loads(clean_jsonc(raw.decode())).get("instructions", [])
    if not isinstance(values, list):
        raise ValueError("instructions deve essere un array")
    if instruction in values:
        return raw
    return set_top(raw, "instructions", [*values, instruction], preserve=False)


def desired(home):
    root, config, entry, instructions, state = paths(home)
    reference = str(instructions)
    original = config.read_bytes() if config.exists() else b"{}\n"
    configured = add_instruction(original, reference)
    plugins = json.loads(clean_jsonc(configured.decode())).get('plugin', [])
    if not isinstance(plugins, list):
        raise RuntimeError('plugin OpenCode deve essere un array')
    # OpenCode 1.18 scans only *.ts and *.js; .mjs must be registered explicitly.
    if entry.as_uri() not in plugins:
        configured = set_top(configured, 'plugin', [*plugins, entry.as_uri()], preserve=False)
    configured = set_top(configured, "share", "disabled")
    primary = json.loads(POLICY.read_text())["open"]["primary"]
    configured = set_top(configured, "small_model", primary)
    parsed = json.loads(clean_jsonc(configured.decode()))
    permission = parsed.get("permission")
    if isinstance(permission, str):
        permission = {"*": permission, "aos_critical": "deny" if permission == "deny" else "ask"}
        configured = set_top(configured, "permission", permission, preserve=False)
    elif permission is None:
        configured = set_top(configured, "permission", {"aos_critical": "ask"})
    elif isinstance(permission, dict):
        if "aos_critical" not in permission:
            text = configured.decode()
            start, end = top_property(text, "permission")
            updated = set_top(text[start:end].encode(), "aos_critical",
                              "deny" if permission.get("*") == "deny" else "ask")
            configured = text[:start].encode() + updated + text[end:].encode()
    else:
        raise ValueError("permission OpenCode non valido")
    return {entry: loader(), instructions: INSTRUCTIONS.encode(), config: configured}, state


def active_claude_plugins(home):
    home = Path(home)
    manifest = home / '.claude/plugins/installed_plugins.json'
    settings = home / '.claude/settings.json'
    if not manifest.is_file() or not settings.is_file():
        return []
    enabled = json.loads(settings.read_text()).get('enabledPlugins', {})
    raw = json.loads(manifest.read_text())
    plugins = raw.get('plugins', raw)
    result = []
    for name, items in plugins.items():
        if not enabled.get(name):
            continue
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get('installPath', item.get('path'))
            if not value:
                continue
            path = Path(value).expanduser()
            result.append(path if path.is_absolute() else home / path)
    return result


def mcp_variables(value, plugin_root=None):
    if isinstance(value, dict):
        return {key: mcp_variables(item, plugin_root) for key, item in value.items()}
    if isinstance(value, list):
        return [mcp_variables(item, plugin_root) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match):
        name = match.group(1)
        if name == "CLAUDE_PLUGIN_ROOT" and plugin_root is not None:
            return str(plugin_root)
        if name.startswith("CLAUDE_"):
            raise RuntimeError("variabile MCP legata al runtime Claude non portabile: " + name)
        return "{env:" + name + "}"

    result = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    if "${" in result:
        raise RuntimeError("interpolazione MCP non supportata; configurare esplicitamente il server")
    return result


def imported_mcps(home):
    source = Path(home) / ".claude.json"
    if not source.is_file():
        raise RuntimeError(f"configurazione Claude assente: {source}")
    servers = mcp_variables(json.loads(source.read_text()).get("mcpServers", {}))
    for base in active_claude_plugins(home):
        manifest = base / '.mcp.json'
        if not manifest.is_file():
            continue
        extra = json.loads(manifest.read_text())
        for name, server in extra.get('mcpServers', extra).items():
            server = mcp_variables(server, base)
            if name in servers and servers[name] != server:
                raise RuntimeError('conflitto fra fonti MCP: ' + name)
            servers[name] = server
    result = {}
    for name, server in servers.items():
        if name in MCP_EXCLUDED:
            continue
        if isinstance(server, dict) and server.get('type') in ('http', 'sse') and isinstance(server.get('url'), str):
            item = {'type': 'remote', 'url': server['url']}
            if server.get('headers'):
                item['headers'] = server['headers']
            if server.get('disabled'):
                item['enabled'] = False
            result[name] = item
            continue
        if not isinstance(server, dict) or not isinstance(server.get("command"), str):
            raise RuntimeError(f"MCP locale non supportato: {name}")
        args = server.get("args", [])
        env = server.get("env", {})
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args) or not isinstance(env, dict):
            raise RuntimeError(f"MCP locale non valido: {name}")
        environment = {}
        for key, value in env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise RuntimeError(f"ambiente MCP non valido: {name}")
            environment[key] = value
        item = {"type": "local", "command": [server["command"], *args]}
        if environment:
            item["environment"] = environment
        if server.get("disabled") is True:
            item["enabled"] = False
        result[name] = item
    return result


def skill_name(directory):
    try:
        head = (directory / "SKILL.md").read_text()[:4096]
    except OSError:
        return None
    match = re.search(r"(?m)^name:\s*[\"']?([^\n\"']+)", head)
    return match.group(1).strip() if match else None


def imported_skills(home, snapshot=None):
    home = Path(home)
    candidates = []
    if snapshot:
        data = json.loads(Path(snapshot).read_text())
        for group in data['data']:
            for skill in group.get('skills', []):
                if skill.get('enabled', True) and skill.get('path'):
                    path = Path(skill['path']).expanduser()
                    if path.is_file() and path.name == 'SKILL.md':
                        candidates.append(path.parent)
    for base in active_claude_plugins(home):
        candidates.extend(p.parent for p in base.glob("skills/*/SKILL.md"))
        if (base / "SKILL.md").is_file():
            candidates.append(base)
    codex = home / ".codex/config.toml"
    if codex.exists():
        if tomllib is None:
            raise RuntimeError("--import-skills richiede Python 3.11+ per leggere config.toml")
        data = tomllib.loads(codex.read_text())
        entries = data.get("skills", {}).get("config", [])
        if isinstance(entries, dict):
            entries = [entries]
        for item in entries:
            if item.get("enabled", True) and item.get("path"):
                directory = Path(item["path"]).expanduser()
                if not directory.is_absolute():
                    directory = home / directory
                if directory.name == "SKILL.md":
                    directory = directory.parent
                if (directory / "SKILL.md").is_file():
                    candidates.append(directory)
    known_paths, known_names = set(), set()
    for root in (home / ".agents/skills", home / ".claude/skills", home / ".config/opencode/skills"):
        if root.is_dir():
            for skill in root.glob("*/SKILL.md"):
                known_paths.add(str(skill.parent.resolve()))
                name = skill_name(skill.parent)
                if name:
                    known_names.add(name)
    result = []
    for candidate in candidates:
        real = str(candidate.resolve())
        name = skill_name(candidate)
        if not name or real in known_paths or name in known_names:
            continue
        known_paths.add(real)
        known_names.add(name)
        result.append(real)
    return result


def vscode_settings(home):
    return Path(home) / "Library" / "Application Support" / "Code" / "User" / "settings.json"


def configure_vscode(path):
    raw = path.read_bytes() if path.exists() else b"{}\n"
    current = json.loads(clean_jsonc(raw.decode()))
    profiles = current.get("terminal.integrated.profiles.osx", {})
    if not isinstance(profiles, dict):
        raise RuntimeError("terminal.integrated.profiles.osx non è un oggetto")
    profile = {"path": "/bin/zsh", "args": ["-l", "-c", "opencode; exec /bin/zsh -l"],
               "overrideName": True, "icon": "terminal"}
    existing = profiles.get("OpenCode")
    if existing is not None and existing != profile:
        raise RuntimeError("conflitto con il profilo VS Code OpenCode esistente")
    return set_top(raw, "terminal.integrated.profiles.osx", {**profiles, "OpenCode": profile}, preserve=False)


def dependency(root):
    return root / "node_modules" / "@opencode-ai" / "plugin" / "package.json"


def install(home, import_mcp=False, import_skills=False, vscode=False, skills_snapshot=None):
    root, config, entry, instructions, state_path = paths(home)
    if not SOURCE.is_file():
        raise RuntimeError(f"sorgente canonica assente: {SOURCE}")
    if not dependency(root).is_file():
        raise RuntimeError(f"dipendenza assente: {dependency(root).parent}; installarla separatamente (nessun download eseguito)")
    wanted, _ = desired(home)
    if import_mcp:
        current = json.loads(clean_jsonc(wanted[config].decode()))
        existing = current.get("mcp", {})
        incoming = imported_mcps(home)
        conflicts = sorted(name for name in incoming if name in existing and existing[name] != incoming[name])
        if conflicts:
            raise RuntimeError("conflitto MCP esistente: " + ", ".join(conflicts))
        wanted[config] = set_top(wanted[config], "mcp", {**existing, **incoming}, preserve=False)
    if import_skills:
        current = json.loads(clean_jsonc(wanted[config].decode()))
        skills = current.get("skills", {})
        if not isinstance(skills, dict) or not isinstance(skills.get("paths", []), list):
            raise RuntimeError("skills.paths OpenCode non valido")
        paths_value = list(skills.get("paths", []))
        seen = {str(Path(item).expanduser().resolve()) for item in paths_value if isinstance(item, str)}
        for item in imported_skills(home, skills_snapshot):
            if item not in seen:
                paths_value.append(item)
                seen.add(item)
        wanted[config] = set_top(wanted[config], "skills", {**skills, "paths": paths_value}, preserve=False)
    if vscode:
        settings = vscode_settings(home)
        wanted[settings] = configure_vscode(settings)
    if state_path.exists():
        state = json.loads(state_path.read_text())
        records = {Path(record["path"]): record for record in state["files"]}
        conflicts = [str(path) for path, record in records.items()
                     if not path.is_file() or digest(path.read_bytes()) != record["installed_sha256"]]
        if conflicts:
            raise RuntimeError("installazione AOS modificata o incompleta: " + ", ".join(conflicts))
        new_paths = [path for path in wanted if path not in records]
        if new_paths:
            backup = Path(state["files"][0]["backup"]).parent if state["files"][0].get("backup") else state_path.parent / ".aos-backups" / digest(os.urandom(32))[:16]
            backup.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(backup, 0o700)
            for path in new_paths:
                existed = path.exists()
                backup_file = backup / (digest(str(path).encode())[:8] + "-" + path.name)
                if existed:
                    atomic_write(backup_file, path.read_bytes(), 0o600)
                record = {"path": str(path), "existed": existed,
                          "mode": stat.S_IMODE(path.stat().st_mode) if existed else None,
                          "backup": str(backup_file) if existed else None,
                          "installed_sha256": digest(wanted[path])}
                state["files"].append(record)
                records[path] = record
        if all(path.is_file() and path.read_bytes() == data for path, data in wanted.items()):
            print("AOS OpenCode è già installato")
            return
        previous = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else (None, None)
                    for path in wanted}
        try:
            for path, data in wanted.items():
                record = records[path]
                mode = 0o600 if import_mcp and path == config else (previous[path][1] or 0o600)
                atomic_write(path, data, mode)
                record["installed_sha256"] = digest(data)
            atomic_write(state_path, json.dumps(state, indent=2).encode() + b"\n", 0o600)
        except Exception:
            for path, (data, mode) in previous.items():
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data, mode)
            raise
        print("AOS OpenCode aggiornato")
        return
    backup = root / ".aos-backups" / digest(os.urandom(32))[:16]
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    os.chmod(backup, 0o700)
    records = []
    for path, data in wanted.items():
        existed = path.exists()
        old_mode = stat.S_IMODE(path.stat().st_mode) if existed else None
        backup_file = backup / path.name
        if existed:
            atomic_write(backup_file, path.read_bytes(), 0o600)
        records.append({"path": str(path), "existed": existed, "mode": old_mode,
                        "backup": str(backup_file) if existed else None, "installed_sha256": digest(data)})
    atomic_write(state_path, json.dumps({"files": records}, indent=2).encode() + b"\n", 0o600)
    written = []
    try:
        for record, (path, data) in zip(records, wanted.items()):
            mode = 0o600 if import_mcp and path == config else (record["mode"] if record["mode"] is not None else 0o600)
            atomic_write(path, data, mode)
            written.append(record)
    except Exception:
        for record in reversed(written):
            path = Path(record["path"])
            if record["existed"]:
                atomic_write(path, Path(record["backup"]).read_bytes(), record["mode"])
            elif path.exists():
                path.unlink()
        state_path.unlink(missing_ok=True)
        raise
    print("AOS OpenCode installato")


def check(home):
    root, _, _, _, state_path = paths(home)
    errors = []
    if not dependency(root).is_file():
        errors.append("dipendenza @opencode-ai/plugin assente")
    if not SOURCE.is_file():
        errors.append("sorgente canonica AOS assente")
    if not state_path.is_file():
        errors.append("stato installazione assente")
    else:
        try:
            state = json.loads(state_path.read_text())
            for record in state["files"]:
                path = Path(record["path"])
                if not path.is_file() or digest(path.read_bytes()) != record["installed_sha256"]:
                    errors.append(f"file mancante o diverso: {path}")
            entry = paths(home)[2]
            if entry.is_file() and entry.read_bytes() != loader():
                errors.append("loader non riferisce la sorgente canonica corrente")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            errors.append(f"stato installazione non valido: {error}")
    if errors:
        raise RuntimeError("; ".join(errors))
    print("AOS OpenCode: OK")


def rollback(home):
    _, _, _, _, state_path = paths(home)
    if not state_path.is_file():
        raise RuntimeError("nessuna installazione AOS da ripristinare")
    state = json.loads(state_path.read_text())
    conflicts = [r["path"] for r in state["files"] if not Path(r["path"]).is_file()
                 or digest(Path(r["path"]).read_bytes()) != r["installed_sha256"]]
    missing_backups = [r["backup"] for r in state["files"] if r["existed"] and
                       (not r.get("backup") or not Path(r["backup"]).is_file())]
    if conflicts:
        raise RuntimeError("rollback rifiutato: file modificato dopo l'installazione: " + ", ".join(conflicts))
    if missing_backups:
        raise RuntimeError("rollback rifiutato: backup mancante")
    for record in state["files"]:
        path = Path(record["path"])
        if record["existed"]:
            atomic_write(path, Path(record["backup"]).read_bytes(), record["mode"])
        else:
            path.unlink()
    state_path.unlink()
    print("Rollback AOS OpenCode completato")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "check", "rollback"))
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--import-mcp", action="store_true", help="import local stdio MCP servers from ~/.claude.json")
    parser.add_argument("--import-skills", action="store_true", help="reference active Claude and Codex plugin skills")
    parser.add_argument("--vscode", action="store_true", help="add an OpenCode VS Code terminal profile")
    parser.add_argument('--skills-snapshot', type=Path, help='Codex skills/list JSON snapshot, with --import-skills')
    args = parser.parse_args(argv)
    try:
        if (args.import_mcp or args.import_skills or args.vscode) and args.action != "install":
            raise RuntimeError("le opzioni di import sono valide solo con install")
        if args.action == "install":
            if args.skills_snapshot and not args.import_skills:
                raise RuntimeError('--skills-snapshot richiede --import-skills')
            install(Path(args.home).resolve(), args.import_mcp, args.import_skills, args.vscode, args.skills_snapshot)
        else:
            {"check": check, "rollback": rollback}[args.action](Path(args.home).resolve())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Errore: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
