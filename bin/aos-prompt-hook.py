#!/usr/bin/env python3
"""AOS UserPromptSubmit hook for Claude Code and Codex.

This hook is cosmetic and advisory, so its contract is narrower than the
orchestrator it calls: read at most 64KiB of the hook payload on stdin, classify
the prompt with aos-orchestrate, and emit a single JSON object whose
`hookSpecificOutput` carries a one-line AOS routing hint. It never fails a
delegation: every exit is 0, and a payload it cannot parse, an environment that
asks for silence, or a classification that says nothing useful produces no
output at all. It never talks to a provider, never runs a command and never
reads a secret. Under Claude Code it also stores domains and task type in the
session's status record (aos-status.py), so the status line can show them.
"""

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

# An advisory hook leaves no trace: no __pycache__ next to the modules it imports.
sys.dont_write_bytecode = True

MAX_STDIN_BYTES = 65536
TRANSCRIPT_TAIL_BYTES = 256 * 1024

HOOK_EVENT = "UserPromptSubmit"


def _load_orchestrate():
    spec = importlib.util.spec_from_file_location(
        "aos_orchestrate", Path(__file__).resolve().parent / "aos-orchestrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prompt_text(payload):
    """The user's prompt from a hook payload, whatever key the host used."""
    if not isinstance(payload, dict):
        return None
    for key in ("prompt", "prompt_text", "text", "user_prompt"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def silent_reason(prompt):
    """Why this prompt must produce no output, or None when it may act."""
    if os.environ.get("AOS_PROMPT_HOOK", "").lower() in ("off", "0", "false"):
        return "AOS_PROMPT_HOOK=off"
    if not isinstance(prompt, str) or not prompt.strip():
        return "empty prompt"
    stripped = prompt.strip()
    # Slash commands and shell/escape prefixes are host controls, not work.
    if stripped[:1] in ("/", "!", "$"):
        return "host control prefix"
    if len(stripped.split()) < 3:
        return "too short to classify"
    # Background-task notifications reach the hook as prompts; nobody typed them.
    head = stripped[:400]
    if "<task-notification>" in head or "[SYSTEM NOTIFICATION" in head:
        return "host notification"
    return None


def premium_names():
    """The policy's premium model names: claude_model, codex_model.

    Loaded relative to the script (ROOT/config/open-models.json). Missing file or
    keys return () so the caller stays silent about the session model.
    """
    try:
        data = json.loads((Path(__file__).resolve().parents[1] / "config/open-models.json").read_text())
    except (OSError, ValueError):
        return ()
    if not isinstance(data, dict):
        return ()
    premium = data.get("premium")
    if not isinstance(premium, dict):
        return ()
    claude = premium.get("claude_model")
    codex = premium.get("codex_model")
    if not (isinstance(claude, str) and claude.strip() and isinstance(codex, str) and codex.strip()):
        return ()
    return (claude, codex)


def _model_from_payload(payload):
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return model
    return None


def _model_from_transcript(path):
    """Last model named in a transcript JSONL, scanning lines from the end.

    Reads at most TRANSCRIPT_TAIL_BYTES from the tail of the file. First match
    wins (last line first): a Claude Code assistant line's `message.model`
    (skipping "<synthetic>") or a Codex `turn_context` payload's `model`.
    Any error -> None.
    """
    try:
        # Only a regular file: a FIFO or a device would block open() or read()
        # and hang the prompt the hook annotates.
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = stream.read(TRANSCRIPT_TAIL_BYTES)
    except (OSError, ValueError):
        # A malformed path (a NUL byte raises ValueError) or an unopenable file
        # must never block the hint.
        return None
    lines = tail.split(b"\n")
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue
        model = None
        if line.get("type") == "assistant":
            message = line.get("message")
            if isinstance(message, dict):
                candidate = message.get("model")
                if isinstance(candidate, str) and candidate.strip() and candidate != "<synthetic>":
                    model = candidate
        elif line.get("type") == "turn_context":
            payload = line.get("payload")
            if isinstance(payload, dict):
                candidate = payload.get("model")
                if isinstance(candidate, str) and candidate.strip():
                    model = candidate
        if model:
            return model
    return None


def session_model(payload):
    """The model the session is running, or None when it cannot be read.

    Claude Code payloads do not carry the model, so it falls back to the tail of
    `transcript_path`. Any failure is silent: the hint is never blocked by the
    model probe.
    """
    model = _model_from_payload(payload)
    if model:
        return model
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if isinstance(path, str) and path.strip():
        return _model_from_transcript(path)
    return None


def is_premium(model, names):
    return any(name in model for name in names)


def _routing_hint(module, prompt, registry, payload=None):
    """One line from the keyword classifier: a hint, never a routing decision.

    Tier, risk and executor are not printed: from keywords alone they would be
    defaults dressed up as a decision. The census and `route` decide those.
    """
    adaptive = registry["adaptive"]
    cls = module.classify(prompt, adaptive)
    domains = cls.get("domains") or []
    task_type = cls.get("task_type")
    if domains == ["GENERAL"] and task_type is None:
        return None
    _publish_profile(payload, domains, task_type)
    profile = module.build_profile(text=prompt, adaptive=adaptive)
    skills = []
    for bundle in module.decompose(profile, adaptive):
        for skill in module.select_skills(bundle, profile, adaptive):
            if skill not in skills:
                skills.append(skill)
    line = "AOS · domini: " + "+".join(domains) + " · tipo: " + (task_type or "—")
    if skills:
        line += " · skill: " + ", ".join(skills)
    line += " · per T1+ censimento e route con /aos ($aos in Codex)"
    model = session_model(payload)
    names = premium_names()
    warning = ""
    if model and names and not is_premium(model, names):
        claude, codex = names
        warning = (" · sessione su " + model[:60] + ": a T2+ o rischio HIGH chiedi il cambio "
                   "(/model " + claude[:30] + " · codex -m " + codex[:30] + ")")
    # The warning is kept whole: a long skill list is what gets truncated.
    return line[:420 - len(warning)] + warning


def _publish_profile(payload, domains, task_type):
    """Show the classification in the Claude Code status bar (aos-status.py).

    Only for Claude Code (CLAUDE_PROJECT_DIR is set for its hooks) and only with
    a session id in the payload: Codex has no status line to feed. Cosmetic:
    any failure is swallowed.
    """
    if not isinstance(payload, dict) or not os.environ.get("CLAUDE_PROJECT_DIR"):
        return
    try:
        spec = importlib.util.spec_from_file_location(
            "aos_status", Path(__file__).resolve().parent / "aos-status.py")
        status = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(status)
        requested = payload.get("session_id")
        # session_id() falls back to the environment: publish only the payload's own id.
        if isinstance(requested, str) and status.session_id(requested) == requested:
            status.set_profile(requested, domains, task_type)
    except Exception:
        pass


def decide(payload, registry, module=None):
    """The output object this hook emits for a payload, or None to stay silent."""
    module = module or _load_orchestrate()
    prompt = _prompt_text(payload)
    if silent_reason(prompt) is not None:
        return None
    try:
        hint = _routing_hint(module, prompt, registry, payload)
    except Exception:
        return None
    if not hint:
        return None
    return {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": hint}}


def main(argv=None):
    # An advisory hook must never break the prompt it annotates.
    try:
        return _main()
    except Exception:
        return 0


def _main():
    data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(data) > MAX_STDIN_BYTES:
        # Oversized payload: refuse to act rather than trust a truncated prompt.
        return 0
    if not data:
        return 0
    try:
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except ValueError:
        return 0
    # Decide silence before loading any module or configuration file.
    if silent_reason(_prompt_text(payload)) is not None:
        return 0
    try:
        module = _load_orchestrate()
        registry = module.load_registry()
        output = decide(payload, registry, module=module)
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if output is None:
        return 0
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
