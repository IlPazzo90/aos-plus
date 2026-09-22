#!/usr/bin/env python3
"""Guards shared by every open worker run: clean tree, retry ownership, git metadata
fingerprint, streamed invocation with time/cost/step caps, usage parsing.

The main session owns verification. This module owns invocation and the facts
about it: exit code, diff, seconds, usage as the runtime reported it or null.
The runtime command line comes from aos-open-executor.py; nothing here starts a
worker on its own.
"""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from queue import Empty, Queue
from threading import Thread

EXIT_DIRTY = 3
EXIT_NO_RUNTIME = 4
EXIT_PROJECT_CONFIG = 5  # the repo carries its own runtime config, which would reopen the guards
EXIT_CONFLICT = 6   # a retry found dirty changes the current task does not own
EXIT_CAP = 125      # the run was stopped by --max-cost or --max-steps
EXIT_TIMEOUT = 124
XDG_DEFAULT = Path("~/.config").expanduser()


# closure; these are the common clients of each class, not all of them.
DENIED_BASH = (
    # network clients
    "curl", "wget", "nc", "ncat", "socat", "lftp", "ftp", "sftp",
    # remote shells and transfers
    "ssh", "scp", "rsync", "mosh",
    # publishing and version control that leaves the repo
    "git push", "git commit", "git stash", "git fetch", "git pull", "git clone", "git ls-remote",
    "git submodule", "git config", "git update-index", "gh", "npm publish", "pnpm publish", "yarn publish", "twine", "docker push",
    # deploy and cloud CLIs; an entry ending in "*" is a pattern as written
    "vercel", "npx vercel", "npx vercel@*", "npx -y vercel*", "netlify", "npx netlify", "npx netlify-cli",
    "fly", "aws", "gcloud", "kubectl", "wp-deploy",
    # privilege
    "sudo",
)


# The script's own git reads no config the fingerprint does not cover: not the
# user's global or system file (a worker can append to ~/.gitconfig with a shell
# redirect — round 22), and no submodule's (`diff.ignoreSubmodules=dirty` keeps
# `git diff` out of their git dirs).
GIT_ENV = dict(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def git(repo, *args, **kwargs):
    env = dict(os.environ, **GIT_ENV)
    return subprocess.run(["git", "-C", str(repo), "-c", "diff.ignoreSubmodules=dirty", *args],
                          capture_output=True, text=True, env=env, **kwargs)


def xdg_home():
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else XDG_DEFAULT


def ensure_clean(repo, allow_dirty):
    # --untracked-files=all: a user's status.showUntrackedFiles=no would hide a
    # file the record then attributes to the worker (round 17).
    status = git(repo, "status", "--porcelain", "--untracked-files=all", check=True).stdout
    if status.strip() and not allow_dirty:
        print(f"repo sporco: il diff non sarebbe attribuibile ({repo}); --allow-dirty per forzare",
              file=sys.stderr)
        raise SystemExit(EXIT_DIRTY)


def dirty_paths(repo):
    """The set of dirty and untracked paths, as git sees them; None when git cannot report.

    `-z` keeps spaces and renames whole; a rename's old path follows as its own
    record and is the same change, so only the new path is kept.
    """
    status = git(repo, "status", "--porcelain", "-z", "--untracked-files=all")
    if status.returncode != 0:
        return None
    paths = set()
    entries = status.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            index += 1  # the old path follows as its own record
        paths.add(path)
    return paths


RETRY_STATE_SCHEMA = 1


def load_retry_state(path):
    """The state a previous attempt wrote, or None when absent, unreadable or invalid.

    The state records the baseline HEAD and the paths the task owns, so a retry
    can tell worker-owned dirt from an external change. A state file the caller
    never wrote is a first attempt, not an error.
    """
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != RETRY_STATE_SCHEMA:
        return None
    owned = data.get("owned")
    if not isinstance(owned, list) or not all(isinstance(name, str) for name in owned):
        return None
    return {"baseline_head": data.get("baseline_head"),
            "initial_repo_clean": bool(data.get("initial_repo_clean")),
            "owned": set(owned)}


def save_retry_state(path, state):
    """Persist the retry state atomically; a missing parent directory is created."""
    if not path:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": RETRY_STATE_SCHEMA,
            "baseline_head": state["baseline_head"],
            "initial_repo_clean": state["initial_repo_clean"],
            "owned": sorted(state["owned"])}
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def retry_conflict(repo, state):
    """None when the retry may run on the dirty tree; a refusal message when not.

    A retry may only touch what the current task already changed: the baseline
    HEAD must still be HEAD (no external commit) and every dirty path must be one
    a previous attempt produced. Anything else is refused — never overwritten,
    never stashed, never reset.
    """
    if git_head(repo) != state["baseline_head"]:
        return "HEAD è cambiato dopo il primo tentativo (commit esterno): retry negato"
    dirty = dirty_paths(repo)
    if dirty is None:
        return "git status illeggibile: retry negato"
    unexpected = dirty - state["owned"]
    if unexpected:
        preview = ", ".join(sorted(unexpected)[:5])
        return f"modifiche non attribuibili al worker corrente ({preview}): retry negato"
    return None


EMPTY_USAGE = {"input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
               "cache_read_tokens": None, "cost_usd": None, "cost_known_usd": None,
               "session_id": None, "steps": 0}


def parse_usage(text):
    """Sum the step_finish events of a --format json stream; anything missing stays null.

    Normalized stream format (step_finish / text / error events, produced by the runtime adapters in aos-open-executor.py):
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


def git_meta_paths(repo):
    """Where git reads what makes it run a command, resolved once, before the run:
    config, info/, hooks/ of the common dir (a linked worktree's own dir has none
    of them — round 19), config.worktree of the worktree dir, every file the
    configs include, nested too (rounds 19-20), and the pointers git follows to
    find those directories — the repo's `.git` file and `<gitdir>/commondir`
    (round 20): a worker that redirects them leaves the watched files intact."""
    dirs = git(repo, "rev-parse", "--git-dir", "--git-common-dir")
    if dirs.returncode != 0:
        return None
    git_dir, common = (Path(repo) / line for line in dirs.stdout.splitlines()[:2])
    top = git(repo, "rev-parse", "--show-toplevel")
    top = Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else Path(repo)
    # The pointer git follows is the toplevel's `.git`, not the subfolder's (round 21).
    globals_ = [Path("~/.gitconfig").expanduser(), xdg_home() / "git" / "config"]
    if os.environ.get("GIT_CONFIG_GLOBAL"):   # the worker's git reads that file instead (round 23)
        globals_.append(Path(os.environ["GIT_CONFIG_GLOBAL"]))
    paths = [top / ".git", git_dir / "commondir", git_dir / "gitdir",
             common / "config", common / "info", common / "hooks", git_dir / "config.worktree",
             # The user's own files, which the script's git no longer reads but the
             # user's next git will: the record says when the worker touched them.
             *globals_]
    # The hooks git actually runs, when core.hooksPath points elsewhere (round 23).
    hooks = git(repo, "config", "--get", "core.hooksPath")
    if hooks.returncode == 0 and hooks.stdout.strip():
        hooks_path = Path(os.path.expanduser(hooks.stdout.strip()))
        paths.append(hooks_path if hooks_path.is_absolute() else top / hooks_path)
    for config in (common / "config", git_dir / "config.worktree", *globals_):
        paths += included_configs(config)
    # Not resolved: a link re-pointed to another file is a change of the link
    # itself (round 22), so the link is what gets read.
    return [Path(os.path.abspath(p)) for p in paths]


def included_configs(config, depth=0):
    """The files a git config file includes, nested as git follows them (depth 10);
    a relative path counts from the including file's directory, quotes stripped."""
    if not config.is_file() or depth >= 10:
        return []
    found = []
    # A trailing backslash continues the value on the next line (round 23).
    text = re.sub(r"\\\n", "", config.read_text(errors="replace"))
    for line in text.splitlines():
        # As git reads it (round 21): the key is case-insensitive, may follow the
        # section header on the same line, and an unquoted `#` or `;` starts a comment.
        m = re.match(r"\s*(?:\[[^\]]*\]\s*)?path\s*=\s*(.*)$", line, re.IGNORECASE)
        if not m:
            continue
        # Quoted stretches keep their text, `#`/`;` outside them start a comment
        # (round 23: `"../extra".gitconfig` is one value).
        value, quoted_parts = "", re.split(r'("(?:[^"\\]|\\.)*")', m.group(1))
        for i, part in enumerate(quoted_parts):
            if i % 2:
                value += re.sub(r"\\(.)", r"\1", part[1:-1])   # `\"` and `\\` as git reads them (round 24)
            else:
                head = re.split(r"[#;]", part, 1)
                value += head[0]
                if len(head) > 1:
                    break
        value = value.strip()
        if not value:
            continue
        target = Path(os.path.expanduser(value))
        target = target if target.is_absolute() else config.parent / target
        found += [target] + included_configs(target, depth + 1)
    return found


def git_meta(paths):
    """A snapshot of those files — plain reads, no git: after the run this is what
    decides whether git may run at all. Reproduced (round 18): a worker that writes
    `core.fsmonitor` has its command run by this script's own `git diff`. A
    directory in the list is walked; the repo's `.git` is a marker when it is one."""
    if paths is None:
        return None
    snapshot = {}
    for path in paths:
        if path.name == ".git" and path.is_dir() and not path.is_symlink():
            snapshot[str(path)] = b"<dir>"
            continue
        # A linked directory is walked too: its files are the hooks git runs (round 23).
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        if path.is_symlink():
            snapshot[str(path) + "@"] = os.readlink(path).encode()
        for file in files:
            if file.is_symlink():   # where the link points is part of what git reads (round 22)
                snapshot[str(file) + "@"] = os.readlink(file).encode()
            if file.is_file():
                try:
                    # Content and mode: an executable bit turns a file into a hook (round 22).
                    snapshot[str(file)] = b"%o:" % file.stat().st_mode + file.read_bytes()
                except OSError:   # unreadable: a stable marker, so an unreadable-before file
                    snapshot[str(file)] = b"<unreadable>"   # does not fail every run (round 22)
    return snapshot


def git_head(repo):
    out = git(repo, "rev-parse", "HEAD")
    return out.stdout.strip() if out.returncode == 0 else None


def diff_stat(repo):
    """Tracked changes as `git diff --stat`, then the untracked files: a worker that only
    created a module would otherwise report "(nessuna modifica)" (benchmark, two tasks)."""
    # Against HEAD: a file the worker `git add`ed is neither unstaged nor untracked
    # (reviewers, round 6). A repo without a commit has no HEAD: index plus tree then.
    # Files under ignored paths (.env, dist/, node_modules/) are not listed: the
    # record reads the repo as git does (round 14).
    # -c diff.relative=false: a user setting would hide tracked changes above the cwd (round 16).
    # --stat=1000 --no-color: outside a terminal git folds paths to 80 columns
    # ("…/Panel.test.tsx") and a user's color.ui=always would add escapes (round 16).
    stat_args = ["-c", "diff.relative=false", "diff", "--stat=1000", "--no-color"]
    stat = git(repo, *stat_args, "HEAD")
    if stat.returncode == 0:
        stat = stat.stdout.strip()
    else:
        stat = "\n".join(s for s in (git(repo, *stat_args, "--cached").stdout.strip(),
                                     git(repo, *stat_args).stdout.strip()) if s)
    # From the toplevel: `ls-files` lists below the cwd only, `diff` the whole tree (round 15).
    untracked = git(repo, "ls-files", "-z", "--others", "--exclude-standard", "--full-name", "--", ":/").stdout.split("\0")
    lines = [stat] if stat else []
    lines += [f"?? {f}" for f in untracked if f]
    return "\n".join(lines)


def invoke(cmd, cwd, timeout, max_cost=None, max_steps=None, env=None, event_adapter=None):
    """The one call that leaves the machine, read as it streams.

    A timeout is a result (124), not a crash. The cost and step caps are enforced
    here, on the provider's own numbers, because a worker that loops costs real
    money before anyone reads the record: the first benchmark task ran 140 steps
    and 6.5M input tokens for a diff of zero lines.
    """
    started = time.monotonic()
    # Its own session: a runtime that forks a server leaves the grandchild holding
    # stderr open past the timeout if only the parent is killed (reviewer's repro:
    # 3 s on a 0.2 s timeout, and a worker still running).
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
            if event_adapter is not None:
                line = event_adapter(line)
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


def run(repo, model, brief, timeout, allow_dirty, max_cost=None, max_steps=None, state_file=None, runner=None):
    """Run one bounded worker attempt through `runner` and return the evidence record."""
    repo = Path(repo).resolve()
    state = load_retry_state(state_file)
    if state is not None:
        # A retry of the same task: the tree may be dirty from a previous attempt.
        # Only worker-owned dirt may stay; anything else is refused, never overwritten.
        conflict = retry_conflict(repo, state)
        if conflict:
            print(conflict, file=sys.stderr)
            raise SystemExit(EXIT_CONFLICT)
        initial_repo_clean = state["initial_repo_clean"]
        dirty_owned_by_current_run = True
        retry_dirty_policy = "owned"
    else:
        # First attempt: pre-existing dirt is the user's, so it is refused unless
        # the caller explicitly overrides with --allow-dirty (the blunt escape hatch).
        ensure_clean(repo, allow_dirty)
        pre_dirty = dirty_paths(repo) if allow_dirty else set()
        state = {"baseline_head": git_head(repo),
                 "initial_repo_clean": not allow_dirty and not pre_dirty,
                 "owned": set(pre_dirty)}
        initial_repo_clean = state["initial_repo_clean"]
        dirty_owned_by_current_run = False
        retry_dirty_policy = "allow-dirty" if allow_dirty else "clean"
    started = time.monotonic()
    head_before = git_head(repo)
    meta_paths = git_meta_paths(repo)
    meta_before = git_meta(meta_paths)
    if runner is None:
        raise ValueError('a runtime runner is required; the executor builds it')
    exit_code, out, err = runner(repo, model, brief, timeout, max_cost, max_steps)
    # No git of ours runs on a repo whose .git config, attributes or hooks the
    # worker changed: it would run the worker's command (round 18).
    try:
        tampered = git_meta(meta_paths) != meta_before
    except Exception:   # a snapshot that cannot be taken is not a clean one (round 21)
        tampered = True
    dirty = None if tampered else dirty_paths(repo)
    if dirty is not None:
        state["owned"] |= dirty
    if state_file:
        save_retry_state(state_file, state)
    return {
        "model": model,
        "repo": str(repo),
        "exit_code": exit_code,
        "seconds": round(time.monotonic() - started, 3),
        "diff_stat": None if tampered else diff_stat(repo),
        # A worker that moved HEAD (commit through a wrapper, reset) leaves a diff
        # against the new HEAD that says nothing (round 10): the record says so.
        "head_before": head_before,
        "head_after": None if tampered else git_head(repo),
        "git_meta_changed": tampered,
        "usage": parse_usage(out),
        "reply": parse_reply(out)[-4000:],
        "error": ("il worker ha modificato .git (config, info o hooks): nessun git eseguito, "
                  "controlla il repo a mano prima di qualsiasi comando git" if tampered else None)
                 or parse_error(out) or ({EXIT_CAP: "tetto di costo o di step raggiunto",
                                          EXIT_TIMEOUT: "timeout"}.get(exit_code)),
        "stderr_tail": err[-4000:],
        "initial_repo_clean": initial_repo_clean,
        "dirty_owned_by_current_run": dirty_owned_by_current_run,
        "dirty_conflict_detected": False,
        "retry_dirty_policy": retry_dirty_policy,
    }

