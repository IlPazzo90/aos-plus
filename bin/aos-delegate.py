#!/usr/bin/env python3
"""Run one opencode task in a repo and print the evidence; never run tests, commit or retry.

The main session owns verification. This script owns invocation and the facts
about it: exit code, diff, seconds, usage as OpenCode reported it or null.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

EXIT_DIRTY = 3
EXIT_NO_OPENCODE = 4
EXIT_CAP = 125      # the run was stopped by --max-cost or --max-steps
EXIT_TIMEOUT = 124
USER_CONFIG = Path("~/.config/opencode/opencode.json").expanduser()


def command(model, brief, repo=None):
    # --auto approves the edits an unattended run needs; --pure keeps third-party
    # plugins out of the measurement; --format json is the only output that carries
    # tokens and cost. --dir names the repo because OpenCode resolves its directory
    # from the PWD variable, not from the process cwd: measured on 2026-09-20, a run
    # launched from the AOS repo with cwd=<worktree> edited the AOS repo.
    cmd = ["opencode", "run", "--pure", "--auto", "--format", "json", "-m", model]
    if repo:
        cmd += ["--dir", str(repo)]
    # "--": a brief that starts with "-" is a message, not an option.
    return cmd + ["--", brief]


# Commands a delegated worker never needs and that would carry data or changes off
# the repo: network, remote shells, publishing, privilege. `--auto` approves every
# permission that is not explicitly denied, so the denies are the whole guard.
# Probed on 2026-09-21 (OpenCode 1.18.30, scratch repo): a denied pattern refuses the
# call even inside `a && curl …`, the later rule wins on the same command, and a
# trailing `"*": "allow"` cancels every deny before it. Not a sandbox: the shell is
# the user's, and `python3 -c "open(…)"` reads what `cat` may not. A denylist has no
# closure; these are the common clients of each class, not all of them.
DENIED_BASH = (
    # network clients
    "curl", "wget", "nc", "ncat", "socat", "lftp", "ftp", "sftp",
    # remote shells and transfers
    "ssh", "scp", "rsync", "mosh",
    # publishing and version control that leaves the repo
    "git push", "git commit", "gh", "npm publish", "pnpm publish", "yarn publish", "twine",
    "docker push",
    # deploy and cloud CLIs
    "vercel", "npx vercel", "netlify", "npx netlify", "fly", "aws", "gcloud", "kubectl", "wp-deploy",
    # privilege
    "sudo",
)


def run_config(user_config=USER_CONFIG):
    """A copy of the user's OpenCode config with the run's guards, for this run only.

    Measured on 2026-09-20: with the ~280 skills under ~/.claude/skills listed in the
    system prompt a one-word reply costs 42,621 input tokens per step; with
    `permission.skill = deny` it costs 7,260. A delegated worker gets its task from
    the brief and its rules from CLAUDE.md; the skill catalog is the main session's.
    The other denies keep an unattended worker inside the repo: no edits outside it,
    no web tools, no subagents, none of DENIED_BASH.
    """
    config = {}
    if user_config.is_file():
        try:
            config = json.loads(user_config.read_text())
        except json.JSONDecodeError:
            config = {}
    permission = config.setdefault("permission", {})
    if not isinstance(permission, dict):
        permission = config["permission"] = {"*": permission}
    bash = permission.pop("bash", None)
    # Our keys leave and come back so they close the map after any user wildcard
    # (reviewer round 4): the later rule wins here too.
    for key in ("skill", "external_directory", "webfetch", "websearch", "task"):
        permission.pop(key, None)
    permission.update({"skill": {"*": "deny"}, "external_directory": "deny", "webfetch": "deny",
                       "websearch": "deny", "task": "deny"})
    # A plain string rule ("deny", "ask", "allow") is the user's rule for every
    # command: it becomes the map's first entry, so nothing they denied is reopened.
    bash = dict(bash) if isinstance(bash, dict) else ({"*": bash} if isinstance(bash, str) else {})
    for name in DENIED_BASH:
        bash.pop(name, None)
        bash.pop(name + " *", None)
    # Ours last: the later matching rule wins.
    bash.update({p: "deny" for name in DENIED_BASH for p in (name, name + " *")})
    permission["bash"] = bash
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", prefix="aos-delegate-", delete=False)
    with handle:
        json.dump(config, handle)
    return handle.name


def ensure_clean(repo, allow_dirty):
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    if status.strip() and not allow_dirty:
        print(f"repo sporco: il diff non sarebbe attribuibile ({repo}); --allow-dirty per forzare",
              file=sys.stderr)
        raise SystemExit(EXIT_DIRTY)


EMPTY_USAGE = {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
               "cache_read_tokens": None, "cost_usd": None, "cost_known_usd": None,
               "session_id": None, "steps": 0}


def parse_usage(text):
    """Sum the step_finish events of a --format json stream; anything missing stays null.

    Format observed on OpenCode 1.18.30 (evals/opencode-run-sample.jsonl):
    one JSON object per line, `sessionID` on every event, `part.tokens` and
    `part.cost` on `step_finish`. A null here is "not reported", never zero.
    """
    usage = dict(EMPTY_USAGE)
    totals = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0}
    # A field absent from even one step makes its total unknown: a default of 0 would
    # be an estimate, and 0.0 $ is the most misleading estimate there is.
    missing = set()
    steps_with_cost = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if usage["session_id"] is None and isinstance(event.get("sessionID"), str):
            usage["session_id"] = event["sessionID"]
        if event.get("type") != "step_finish":
            continue
        part = event.get("part") or {}
        tokens = part.get("tokens") or {}
        cache = tokens.get("cache") or {}
        fields = {"input_tokens": tokens.get("input"), "output_tokens": tokens.get("output"),
                  "reasoning_tokens": tokens.get("reasoning"), "cache_read_tokens": cache.get("read"),
                  "cost_usd": part.get("cost")}
        parsed = {}
        for key, value in fields.items():
            if value is None:
                missing.add(key)
                continue
            try:
                parsed[key] = float(value) if key == "cost_usd" else int(value)
            except (TypeError, ValueError):
                missing.add(key)
        # Validate the whole event before touching the totals: no partial contributions.
        for key, value in parsed.items():
            totals[key] += value
        steps_with_cost += "cost_usd" in parsed
        usage["steps"] += 1
    if usage["steps"]:
        usage.update({k: (None if k in missing else v) for k, v in totals.items()})
        # What the provider did report, for a spend cap: null only when no step said.
        usage["cost_known_usd"] = totals["cost_usd"] if steps_with_cost else None
    return usage


def parse_reply(text):
    """Concatenate the assistant text events, for the record and for debugging a refusal."""
    parts = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "text":
            parts.append((event.get("part") or {}).get("text", ""))
    return "".join(parts)


def parse_error(text):
    """The provider's own error event, if any: a run that produced no diff needs to say why."""
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "error":
            err = event.get("error") or {}
            return ((err.get("data") or {}).get("message")) or err.get("name") or json.dumps(err)[:500]
    return None


def diff_stat(repo):
    """Tracked changes as `git diff --stat`, then the untracked files: a worker that only
    created a module would otherwise report "(nessuna modifica)" (benchmark, two tasks)."""
    stat = subprocess.run(["git", "-C", str(repo), "diff", "--stat"],
                          capture_output=True, text=True).stdout.strip()
    untracked = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", "--others", "--exclude-standard"],
                               capture_output=True, text=True).stdout.split("\0")
    lines = [stat] if stat else []
    lines += [f"?? {f}" for f in untracked if f]
    return "\n".join(lines)


def invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None):
    """The one call that leaves the machine, read as it streams.

    A timeout is a result (124), not a crash. The cost and step caps are enforced
    here, on the provider's own numbers, because a worker that loops costs real
    money before anyone reads the record: the first benchmark task ran 140 steps
    and 6.5M input tokens for a diff of zero lines.
    """
    started = time.monotonic()
    # Its own session: opencode forks a server, and killing only the parent left the
    # grandchild holding stderr open past the timeout (reviewer's repro: 3 s on a 0.2 s
    # timeout, and a worker still running).
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    # A worker that goes silent would block a plain readline past the timeout; the
    # reader threads turn both streams into buffers the loop can wait on with a deadline.
    queue = Queue()
    err_chunks = []

    def reader():
        for line in proc.stdout:
            queue.put(line)
        queue.put(None)

    def err_reader():
        for line in proc.stderr:
            err_chunks.append(line)
    Thread(target=reader, daemon=True).start()
    err_thread = Thread(target=err_reader, daemon=True)
    err_thread.start()
    lines = []
    cost, steps, code = 0.0, 0, None
    try:
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                code = EXIT_TIMEOUT
                break
            try:
                line = queue.get(timeout=min(remaining, 1.0))
            except Empty:
                continue
            if line is None:
                break
            lines.append(line)
            if line.startswith("{") and '"step_finish"' in line:
                try:
                    part = json.loads(line).get("part") or {}
                    cost += float(part.get("cost") or 0)
                    steps += 1
                except (ValueError, TypeError):
                    pass
            if (max_cost is not None and cost > max_cost) or (max_steps is not None and steps >= max_steps):
                code = EXIT_CAP
                break
        if code is None:
            code = proc.wait(timeout=max(1, timeout - (time.monotonic() - started)))
    except subprocess.TimeoutExpired:
        code = EXIT_TIMEOUT
    finally:
        # Always, whatever the exit: the leader may be gone while its descendants hold
        # the pipes (reviewer rounds 2 and 3: grandchild alive after 124, then after 0
        # with only stderr open). A run is over when nothing of it is left running.
        kill_group(proc)
    err_thread.join(timeout=2)
    return code, "".join(lines), "".join(err_chunks)


def kill_group(proc):
    """Kill the worker and everything it spawned; the group id is the leader's pid."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session: pgid == pid
    except (ProcessLookupError, PermissionError, OSError):
        if proc.poll() is None:
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run(repo, model, brief, timeout, allow_dirty, max_cost=None, max_steps=None):
    repo = Path(repo).resolve()
    ensure_clean(repo, allow_dirty)
    started = time.monotonic()
    config = run_config()
    env = dict(os.environ, OPENCODE_CONFIG=config, PWD=str(repo))
    try:
        exit_code, out, err = invoke(command(model, brief, repo), str(repo), timeout, max_cost, max_steps, env)
    finally:
        os.unlink(config)
    return {
        "model": model,
        "repo": str(repo),
        "exit_code": exit_code,
        "seconds": round(time.monotonic() - started, 3),
        "diff_stat": diff_stat(repo),
        "usage": parse_usage(out),
        "reply": parse_reply(out)[-4000:],
        "error": parse_error(out) or ({EXIT_CAP: "tetto di costo o di step raggiunto",
                                       EXIT_TIMEOUT: "timeout"}.get(exit_code)),
        "stderr_tail": err[-4000:],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--model", required=True, help="provider/model, e.g. vercel/…")
    parser.add_argument("--brief", required=True, help="file path, or - for stdin")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--max-cost", type=float, default=1.0, help="USD, as the provider reports it; 0 = no cap")
    parser.add_argument("--max-steps", type=int, default=60, help="0 = no cap")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    brief = sys.stdin.read() if args.brief == "-" else Path(args.brief).read_text()
    if not shutil.which("opencode"):
        payload = {"error": "opencode non trovato", "model": args.model}
        print(json.dumps(payload) if args.json else payload["error"])
        return EXIT_NO_OPENCODE
    result = run(args.repo, args.model, brief, args.timeout, args.allow_dirty,
                 max_cost=args.max_cost or None, max_steps=args.max_steps or None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"exit {result['exit_code']} · {result['seconds']}s · {result['model']}")
        print(result["diff_stat"] or "(nessuna modifica)")
        print("usage:", json.dumps(result["usage"]))
        if result["error"]:
            print("error:", result["error"])
    return 0 if result["exit_code"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
