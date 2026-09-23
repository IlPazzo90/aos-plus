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
import sys
from pathlib import Path

# An advisory hook leaves no trace: no __pycache__ next to the modules it imports.
sys.dont_write_bytecode = True

MAX_STDIN_BYTES = 65536

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
    return line[:300]


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
