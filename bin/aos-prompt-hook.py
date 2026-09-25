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

Written rules turn visible through a few signals: a session that already routed a
task is reminded to route again when a new work prompt arrives, a session whose
context is past the policy's soft limit (aos-context.py) is told to compact at
the next breakpoint, and a prompt that would act outside AOS routing (e-mail,
published records, business/legal writes) is warned before it runs. Only the user
can run /compact, so under Claude Code that signal also reaches the user as a
systemMessage.
"""

import importlib.util
import json
import math
import os
import re
import stat
import sys
import time
from pathlib import Path

# An advisory hook leaves no trace: no __pycache__ next to the modules it imports.
sys.dont_write_bytecode = True

MAX_STDIN_BYTES = 65536
TRANSCRIPT_TAIL_BYTES = 256 * 1024

HOOK_EVENT = "UserPromptSubmit"
LINE_CAP = 520
CONTEXT_ALERT_STATES = ("ORANGE", "RED")
ROUTED_TASK_TYPES = ("bug_fix", "feature")
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config/open-models.json"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
UNROUTED_EDIT_NOTICE = ("AOS · prima modifica senza routing registrato: se è lavoro software "
                        "fai censimento, router e aos-status set prima di continuare; "
                        "altrimenti ignora")
ACTION_NOTICE = (" · azione esterna senza routing: rischio HIGH, censimento, router e "
                 "aos-status set prima di eseguirla")
# Verbs that act on the outside world (e-mail, published documents, ...); the
# word-start alternatives cover the Italian imperatives the prompts actually use.
SEND_RE = re.compile(r"\b(mand[ao]|mandiam|mandal|mandar|invi[aoi]|inviam|inviar"
                     r"|spedisc|spedir|inoltr|pubblic|send|publish)\w*", re.IGNORECASE)
WRITE_RE = re.compile(r"\b(aggiorn|inserisc|inserir|crea|creare|metti|mettere|carica"
                      r"|elimin|cancell|modific|registr|update|insert|create|delete|upload)\w*",
                      re.IGNORECASE)


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_orchestrate():
    return _load_module("aos_orchestrate", "aos-orchestrate.py")


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


def _premium():
    """The policy's premium object, or None when it cannot be read."""
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    premium = data.get("premium")
    return premium if isinstance(premium, dict) else None


def premium_names():
    """The policy's premium model names: claude_model, codex_model.

    Loaded relative to the script (ROOT/config/open-models.json). Missing file or
    keys return () so the caller stays silent about the session model.
    """
    premium = _premium()
    if premium is None:
        return ()
    claude = premium.get("claude_model")
    codex = premium.get("codex_model")
    if not (isinstance(claude, str) and claude.strip() and isinstance(codex, str) and codex.strip()):
        return ()
    return (claude, codex)


def accepted_session_models():
    """Every name a session model may run under without a switch advisory.

    ``premium.session_models`` when it is a non-empty list of non-empty strings:
    the owner may keep a premium planner (fable) out of the session on purpose.
    Otherwise the premium pair, as before the list existed.
    """
    premium = _premium()
    if premium is None:
        return ()
    session_models = premium.get("session_models")
    if isinstance(session_models, list) and session_models and \
            all(isinstance(name, str) and name.strip() for name in session_models):
        return tuple(session_models)
    return premium_names()


def switch_targets():
    """(claude, codex) model names a session should switch to.

    The first accepted name without / with ``gpt``; each falls back to the
    premium pair's name for that host.
    """
    names = premium_names()
    if not names:
        return ()
    accepted = accepted_session_models()
    claude = next((name for name in accepted if "gpt" not in name), names[0])
    codex = next((name for name in accepted if "gpt" in name), names[1])
    return (claude, codex)


def _model_from_payload(payload):
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str) and model.strip():
            return model
    return None


def _transcript_tail(path):
    """Parsed JSON objects from the tail of a transcript, last line first.

    Reads at most TRANSCRIPT_TAIL_BYTES. Any error -> [] so a probe never blocks
    the hint.
    """
    try:
        # Only a regular file: a FIFO or a device would block open() or read()
        # and hang the prompt the hook annotates.
        if not stat.S_ISREG(os.stat(path).st_mode):
            return []
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = stream.read(TRANSCRIPT_TAIL_BYTES)
    except (OSError, ValueError, TypeError):
        # A malformed path (a NUL byte raises ValueError) or an unopenable file
        # must never block the hint.
        return []
    lines = []
    for raw in reversed(tail.split(b"\n")):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(line, dict):
            lines.append(line)
    return lines


def _model_from_transcript(path):
    """Last model named in a transcript JSONL, scanning lines from the end.

    First match wins (last line first): a Claude Code assistant line's
    `message.model` (skipping "<synthetic>") or a Codex `turn_context` payload's
    `model`. Any error -> None.
    """
    for line in _transcript_tail(path):
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


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _tokens_from_line(line):
    """Context tokens a transcript line reports, or None when it reports none."""
    if line.get("type") == "assistant" and line.get("isSidechain") is not True:
        message = line.get("message")
        if not isinstance(message, dict) or message.get("model") == "<synthetic>":
            return None
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        if not any(key in usage for key in keys):
            return None
        # A present but invalid counter is 0: it must not send the scan back to an
        # older, larger line.
        return sum(_count(usage.get(key)) or 0 for key in keys)
    if line.get("type") == "event_msg":
        payload = line.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            return None
        info = payload.get("info")
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        # Codex input_tokens already include the cached part.
        return _count(last.get("input_tokens")) if isinstance(last, dict) else None
    return None


def _is_compaction(line):
    """Whether a transcript line marks a context compaction.

    Claude Code: a `system` line with `subtype` `compact_boundary`. Codex: a
    top-level `type` of `compacted`.
    """
    if line.get("type") == "system" and line.get("subtype") == "compact_boundary":
        return True
    return line.get("type") == "compacted"


def context_tokens(payload):
    """Tokens in the session's context at its last model call, or None.

    Claude Code: the last main-thread assistant line's input plus cache reads
    and writes (a cached prompt is still context). Codex: the last
    `token_count` event's `last_token_usage.input_tokens`. A compaction marker
    met before any usage line means the context was just reset and no new call
    measured it: no signal.
    """
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not path.strip():
        return None
    for line in _transcript_tail(path):
        if _is_compaction(line):
            return None
        tokens = _tokens_from_line(line)
        if tokens is not None:
            return tokens
    return None


def context_signal(payload, model):
    """(hint segment, user message) when the context is ORANGE/RED, else None."""
    if not model:
        return None
    tokens = context_tokens(payload)
    if tokens is None:
        return None
    # Without an explicit config path aos-context enforces no limits at all.
    result = _load_module("aos_context", "aos-context.py").evaluate(
        model, tokens=tokens, config_path=CONFIG_PATH)
    state = result.get("context_state")
    if state not in CONTEXT_ALERT_STATES:
        return None
    soft, hard = result.get("soft_limit"), result.get("hard_limit")
    if not isinstance(soft, int) or not isinstance(hard, int):
        return None
    segment = (" · contesto %dk token, %s (soft %dk, hard %dk): chiudi il passo in corso "
               "e chiedi all'utente /compact al punto di pausa"
               % (tokens // 1000, state, soft // 1000, hard // 1000))
    message = ("AOS: contesto %dk token (%s, hard %dk) — /compact al prossimo punto di pausa"
               % (tokens // 1000, state, hard // 1000))
    return segment, message


def _claude_session(payload):
    """The payload's own session id under Claude Code, else None."""
    if not isinstance(payload, dict) or not os.environ.get("CLAUDE_PROJECT_DIR"):
        return None
    requested = payload.get("session_id")
    if not isinstance(requested, str) or not requested:
        return None
    try:
        # Same rule as the status file name: an id it would refuse is not a session.
        return requested if _load_status().session_id(requested) == requested else None
    except Exception:
        return None


def _epoch(value):
    """A finite, non-negative epoch in seconds, or None (NaN, bool, junk)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _load_status():
    return _load_module("aos_status", "aos-status.py")


def _scratch_path(path):
    """Whether a resolved path lives under a scratch location that is not work.

    Transcripts, memory and plans under ~/.claude, plus the OS temp dirs, are
    the host's own bookkeeping: editing them is not the session's software work.
    """
    home = Path.home()
    # Resolved like the path: a symlinked home must not hide the roots.
    roots = tuple(root.resolve() for root in (home / ".claude/projects", home / ".claude/plans"))
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError, TypeError):
        return False
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            pass
    for root in (Path("/tmp"), Path("/private/tmp"), Path("/var/folders"),
                 Path("/private/var/folders")):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            pass
    return False


def decide_tool(payload, now=None):
    """The output object for a PreToolUse edit, or None to stay silent.

    The first file edit in a Claude Code session that never routed claims the
    unrouted notice once. Subagents, non-edit tools, scratch paths and Codex
    (no CLAUDE_PROJECT_DIR) never claim it. Any failure -> None.
    """
    try:
        session = _claude_session(payload)
        if session is None:
            return None
        if payload.get("agent_id"):
            return None
        tool_name = payload.get("tool_name")
        if tool_name not in EDIT_TOOLS:
            return None
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None
        if tool_name == "NotebookEdit":
            path = tool_input.get("notebook_path")
        else:
            path = tool_input.get("file_path")
        if not isinstance(path, str) or not path.strip():
            return None
        if _scratch_path(os.path.realpath(os.path.expanduser(path))):
            return None
        if not _load_status().claim_unrouted_notice(session):
            return None
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "additionalContext": UNROUTED_EDIT_NOTICE},
                "systemMessage": "AOS: prima modifica senza routing registrato"}
    except Exception:
        return None


def reroute_reminder(payload, task_type, now=None):
    """Segment asking a routed session to route a new work prompt again.

    Claude Code only: the status record is where the routed tier lives. Read
    before this prompt's profile is published. A work prompt (`bug_fix` or
    `feature`) in a session that never routed gets the unrouted notice instead,
    so the first prompt already points at the census. Any failure -> "".
    """
    if task_type is None:
        return ""
    try:
        session = _claude_session(payload)
        if session is None:
            return ""
        status = _load_status()
        record = status.load(session)
        now = time.time() if now is None else now
        expires = _epoch(record.get("expires_at"))
        # A missing or corrupt expiry is not a live record.
        if expires is None or expires < now:
            return unrouted_notice(task_type)
        tier = record.get("tier")
        # Only a real tier counts as routed: a corrupt value must not hide the notice.
        if not isinstance(tier, str) or tier.strip().upper() not in ("T0", "T1", "T2", "T3"):
            return unrouted_notice(task_type)
        risk = record.get("risk")
        label = tier + ("/" + risk if isinstance(risk, str) and risk else "")
        routed = _epoch(record.get("routed_at"))
        since = ""
        if routed is not None and routed <= now:
            since = " da %d min" % int((now - routed) // 60)
        return (" · tier attivo " + label[:20] + since +
                ": se è un task nuovo rifai censimento, router e aos-status set")
    except Exception:
        return ""


def unrouted_notice(task_type):
    """The unrouted-work segment for a work prompt, or "" for other task types."""
    if task_type not in ROUTED_TASK_TYPES:
        return ""
    return " · nessun routing registrato: censimento, router e aos-status set prima della prima modifica"


def action_notice(payload, prompt, domains):
    """Segment warning that a prompt acts outside AOS routing, or "".

    Claude Code only, and only while the live status record has no valid tier: a
    session that routed already declared itself, so the router notice must stay
    the single one. Send verbs act on the outside world in any domain; write
    verbs count only when the classification touches business or legal records,
    where inserting and correcting has external effects. Any failure -> "".
    """
    try:
        session = _claude_session(payload)
        if session is None:
            return ""
        tier = _load_status().load_live(session).get("tier")
        if isinstance(tier, str) and tier.strip().upper() in ("T0", "T1", "T2", "T3"):
            return ""
    except Exception:
        return ""
    domains = domains or []
    if SEND_RE.search(prompt or ""):
        return ACTION_NOTICE
    if ("BUSINESS_OPERATIONS" in domains or "LEGAL_COMPLIANCE" in domains) \
            and WRITE_RE.search(prompt or ""):
        return ACTION_NOTICE
    return ""


def session_model(payload):
    """The model the session is running, or None when it cannot be read.

    Claude Code payloads do not carry the model, so it falls back to the tail of
    `transcript_path`, then (Claude Code only) to the live status record's
    `session_model`, which the status line stores on the first prompt before any
    assistant line exists. Any failure is silent: the hint is never blocked by
    the model probe.
    """
    model = _model_from_payload(payload)
    if model:
        return model
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if isinstance(path, str) and path.strip():
        model = _model_from_transcript(path)
        if model:
            return model
    session = _claude_session(payload)
    if session is None:
        return None
    try:
        record = _load_status().load_live(session)
        model = record.get("session_model")
        return model if isinstance(model, str) and model.strip() else None
    except Exception:
        return None


def is_premium(model, names):
    return any(name in model for name in names)


def _routing_hint(module, prompt, registry, payload=None, context_segment="",
                  classification=None, action_segment=""):
    """One line from the keyword classifier: a hint, never a routing decision.

    Tier, risk and executor are not printed: from keywords alone they would be
    defaults dressed up as a decision. The census and `route` decide those.
    """
    adaptive = registry["adaptive"]
    cls = classification if classification is not None else module.classify(prompt, adaptive)
    domains = cls.get("domains") or []
    task_type = cls.get("task_type")
    if domains == ["GENERAL"] and task_type is None:
        return None
    reminder = reroute_reminder(payload, task_type)
    if "nessun routing registrato" in reminder:
        # The unrouted notice already points at census, router and aos-status:
        # one notice per prompt, never the external-action notice stacked on top.
        action_segment = ""
    _publish_profile(payload, domains, task_type)
    profile = module.build_profile(text=prompt, adaptive=adaptive)
    skills = []
    for bundle in module.decompose(profile, adaptive):
        for skill in module.select_skills(bundle, profile, adaptive):
            if skill not in skills:
                skills.append(skill)
    head = "AOS · domini: " + "+".join(domains) + " · tipo: " + (task_type or "—")
    route = " · per T1+ censimento e route con /aos ($aos in Codex)"
    tail = model_warning(session_model(payload)) + action_segment + reminder + context_segment
    # Warning, action, reminder and context segments are kept whole: a long skill
    # list is what gets truncated.
    listing = (" · skill: " + ", ".join(skills)) if skills else ""
    room = max(0, LINE_CAP - len(head) - len(route) - len(tail))
    if len(listing) > room:
        listing = listing[:max(0, room - 1)].rstrip(", ") + "…" if room else ""
    # The routing instruction and the tail are never cut: when the domains alone
    # overflow, the front gives way.
    return (head + listing)[:max(0, LINE_CAP - len(route) - len(tail))] + route + tail


def model_warning(model):
    """Segment naming a session model outside the accepted list, or ""."""
    names = premium_names()
    targets = switch_targets()
    if not model or not names or not targets or is_premium(model, accepted_session_models()):
        return ""
    claude, codex = targets
    switch = "(/model " + claude[:30] + " · codex -m " + codex[:30] + ")"
    if is_premium(model, names):
        return (" · sessione su " + model[:60] + ": consuma token premium, la sessione va su " +
                claude[:30] + " " + switch)
    return " · sessione su " + model[:60] + ": a T2+ o rischio HIGH chiedi il cambio " + switch


def _publish_profile(payload, domains, task_type):
    """Show the classification in the Claude Code status bar (aos-status.py).

    Only for Claude Code (CLAUDE_PROJECT_DIR is set for its hooks) and only with
    a session id in the payload: Codex has no status line to feed. Cosmetic:
    any failure is swallowed.
    """
    requested = _claude_session(payload)
    if requested is None:
        return
    try:
        _load_status().set_profile(requested, domains, task_type)
    except Exception:
        pass


def decide(payload, registry, module=None):
    """The output object this hook emits for a payload, or None to stay silent."""
    module = module or _load_orchestrate()
    prompt = _prompt_text(payload)
    if silent_reason(prompt) is not None:
        return None
    try:
        signal = context_signal(payload, session_model(payload))
    except Exception:
        signal = None
    segment = signal[0] if signal is not None else ""
    try:
        classification = module.classify(prompt, registry["adaptive"])
    except Exception:
        classification = None
    domains = classification.get("domains") if isinstance(classification, dict) else None
    action = action_notice(payload, prompt, domains or [])
    try:
        hint = _routing_hint(module, prompt, registry, payload, segment,
                             classification=classification, action_segment=action)
    except Exception:
        hint = None
    if hint is None and (action or segment):
        # The action and context segments are emitted even when the classifier
        # has nothing to say: an external action or a full context matters
        # whatever the prompt is about.
        hint = "AOS" + action + segment
    if not hint:
        return None
    output = {"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "additionalContext": hint}}
    if signal is not None and _claude_session(payload) is not None:
        # Only the user can run /compact; Codex does not know this field.
        output["systemMessage"] = signal[1]
    return output


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
    # PreToolUse is a different signal (the first edit), dispatched before the
    # prompt path: it never runs the classifier.
    if isinstance(payload, dict) and payload.get("hook_event_name") == "PreToolUse":
        output = decide_tool(payload)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False))
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
