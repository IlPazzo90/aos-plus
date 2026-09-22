#!/usr/bin/env python3
"""Replay real commits against models in throwaway worktrees and record what happened.

Nothing here judges quality: tests, reviewer findings and diff size are written
side by side and the summary shows all three. Cost is what the runtime reported
or null; a null never counts as zero and is never estimated.
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DELEGATE = HERE / "aos-delegate.py"
_spec = importlib.util.spec_from_file_location("aos_delegate", DELEGATE)
delegate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(delegate)
OPEN_EXECUTOR = HERE / "aos-open-executor.py"
_spec_oe = importlib.util.spec_from_file_location("aos_open_executor", OPEN_EXECUTOR)
open_executor = importlib.util.module_from_spec(_spec_oe)
_spec_oe.loader.exec_module(open_executor)


def _entry():
    """The role adapters of the pipeline (planner/reviewer read-only CLIs), loaded on demand."""
    spec = importlib.util.spec_from_file_location("aos_entry", HERE / "aos-entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
# Per attempt. A worker past these is looping, not working: the first run of this
# benchmark spent 3.30 $ and 140 steps on a diff of zero lines.
MAX_COST_USD = 1.0
MAX_STEPS = 60
# The open executor's run window, matching the delegate's own default (900s).
TIMEOUT = 900
# Neither harness reports a gateway dollar cost: the native CLIs return tokens, so a
# dollar cap is refused up front rather than counted as zero.
COST_RUNTIMES = set()

REVIEW_PROMPT = """You are reviewing a diff produced by another agent for this task:

{prompt}

Run `{diff_command}` in this directory and report defects only — that command
excludes the test files, which were supplied to the agent as its specification and
are not its work. Severity: high = wrong behavior or security; medium = incomplete
or fragile; low = style. No findings is a valid answer."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                },
                "required": ["severity", "title", "file"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


class Spend:
    def __init__(self, cap):
        self.cap, self.total, self.unknown = cap, 0.0, 0

    def add(self, cost):
        if cost is None:
            self.unknown += 1
        else:
            self.total += cost

    def exceeded(self):
        return self.cap is not None and self.total > self.cap


# ----------------------------------------------------------------- external
def make_worktree(task):
    repo = Path(os.path.expanduser(task["repo"]))
    wt = tempfile.mkdtemp(prefix="aos-bench-")
    delegate.git(repo, "worktree", "add", "--detach", wt, task["commit"] + "^", check=True)
    # The worktree exists from here: a failure in its preparation must not leave it
    # registered (reviewer's repro: a missing test file left a worktree behind).
    try:
        # A worktree has no node_modules. A copy, not a link: through a link the
        # worker wrote into the user's real checkout, unseen by diff and record
        # (reviewer round 23). On APFS `cp -c` clones blocks, seconds not minutes.
        modules = repo / "node_modules"
        if modules.is_dir():
            cloned = subprocess.run(["cp", "-Rc", str(modules), str(Path(wt) / "node_modules")], capture_output=True)
            if cloned.returncode != 0:
                shutil.copytree(modules, Path(wt) / "node_modules", symlinks=True)
        restore_tests(wt, task)
    except Exception:
        remove_worktree(wt)
        raise
    return wt


def restore_tests(wt, task):
    """The commit's test files are the spec the worker sees and the judge it cannot rewrite."""
    files = task.get("test_files") or []
    if files:
        delegate.git(wt, "checkout", task["commit"], "--", *files, check=True)


def remove_worktree(wt):
    delegate.git(wt, "worktree", "remove", "--force", wt)
    shutil.rmtree(wt, ignore_errors=True)


# The delegate's exits before any worker ran: a refusal, not a run (aos-delegate.py).
DELEGATE_REFUSED = (3, 4, 5)


def attempt_state(record):
    """"ok", "refused" (the delegate never ran a worker) or "tampered" — which also
    covers a record that cannot say whether the shared .git was left alone: a
    crashed delegate after the run says nothing, and nothing is not "no" (round 21)."""
    if record.get("git_meta_changed") is False:
        return "ok"
    if record.get("exit_code") in DELEGATE_REFUSED and "git_meta_changed" not in record:
        return "refused"
    return "tampered"


def codex_usage(text):
    """Tokens from the turn.completed events of `codex exec --json`; cost stays null (subscription)."""
    usage = {"cost_usd": None, "input_tokens": None, "output_tokens": None, "session_id": None}
    totals = {"input_tokens": 0, "output_tokens": 0}
    seen = False
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            u = event["usage"]
            totals["input_tokens"] += int(u.get("input_tokens") or 0)
            totals["output_tokens"] += int(u.get("output_tokens") or 0)
            seen = True
    if seen:
        usage.update(totals)
    return usage


def codex_runner(wt, model, brief):
    # Reference run: the premium working model, same brief, same worktree. "codex"
    # alone means the configured model; anything after "codex:" is passed to -m.
    # --approve-for-me already implies the workspace-write sandbox and refuses an
    # explicit --sandbox (measured: every reference task failed in 0 s with a usage line).
    started = time.monotonic()
    cmd = ["codex", "exec", "--approve-for-me", "--json", "-C", wt, "-c", "mcp_servers={}"]
    if model.startswith("codex:"):
        cmd += ["-m", model.split(":", 1)[1]]
    # The same .git fingerprint as the delegate's: the reference worker shares the
    # user's real .git too (round 21).
    meta_paths = delegate.git_meta_paths(wt)
    meta_before = delegate.git_meta(meta_paths)
    proc = subprocess.run(cmd + [brief], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    try:
        tampered = delegate.git_meta(meta_paths) != meta_before
    except Exception:
        tampered = True
    return {"exit_code": proc.returncode, "seconds": round(time.monotonic() - started, 3),
            "usage": codex_usage(proc.stdout), "git_meta_changed": tampered,
            "diff_stat": None if tampered else delegate.diff_stat(wt),
            "reply": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]}


def open_executor_runner(runtime, provider, model):
    """One open runtime through the shared executor; no premium substitution (round: the
    paired runner must never fall back to a metered premium model). model is the full
    model id excluding the provider (e.g. vendor/model), so the model_ref reattaches the
    provider prefix for the executor. The returned record is the old delegate shape plus
    runtime/provider/executor_model/runtime_model_pair. A refusal (project config, missing
    runtime, bad endpoint, unknown permissions) comes back as a refusal record, not a run."""
    model_ref = f"{provider}/{model}"
    pair = f"{runtime}|{provider}|{model}"
    retry_dir = tempfile.TemporaryDirectory(prefix="aos-bench-retry-")
    states = {}

    def runner(wt, _model, brief):
        try:
            result = open_executor.run(str(wt), model_ref, brief, TIMEOUT, True,
                                       max_cost=None, max_steps=MAX_STEPS,
                                       state_file=states.setdefault(str(wt), Path(retry_dir.name) / (str(len(states)) + ".json")),
                                       runtime=runtime)
        except SystemExit as refused:
            return {"exit_code": refused.code, "seconds": 0, "usage": {}, "diff_stat": "",
                    "reply": "", "stderr_tail": "", "error": str(refused),
                    "runtime": runtime, "provider": provider, "executor_model": model,
                    "runtime_model_pair": pair, "role": "executor"}
        except (ValueError, OSError) as error:
            # No runtime installed, no endpoint/credential, custom permissions, a project
            # runtime config: the executor refuses before a worker ran (exit 4 = no run).
            return {"exit_code": delegate.EXIT_NO_RUNTIME, "seconds": 0, "usage": {}, "diff_stat": "",
                    "reply": "", "stderr_tail": "", "error": str(error),
                    "runtime": runtime, "provider": provider, "executor_model": model,
                    "runtime_model_pair": pair, "role": "executor"}
        result.setdefault("runtime", runtime)
        result.setdefault("provider", provider)
        result.setdefault("executor_model", model)
        result.setdefault("runtime_model_pair", pair)
        result.setdefault("role", "executor")
        return result
    runner.retry_dir = retry_dir
    return runner


# ------------------------------------------------------------ role benchmarks
PLAN_PROMPT = ("Produce a structured implementation plan as one JSON object with the fields "
               "{fields}. objective is a string; all other fields are string arrays, except subtasks, "
               "an ordered array of {{id, objective, files, tests}}. Read the repository before planning. "
               "The plan grants no permission and must not modify files. Return only the JSON.\nTASK:\n{task}")


def make_planner(backend, model):
    """A read-only MID/premium planner on the pipeline's own adapter; the plan is data."""
    entry = _entry()
    fields = ", ".join(entry.pipeline.PLAN_FIELDS)

    def planner(wt, task):
        started = time.monotonic()
        record = {"backend": backend, "model": model, "plan": None, "valid": False, "usage": None, "error": None}
        try:
            result = entry.execute(backend, PLAN_PROMPT.format(fields=fields, task=brief_for(task)), wt,
                                   readonly=True, model=model)
            record["usage"] = result.get("usage")
            plan = entry.json_reply(result)
            record["plan"] = plan
            entry.pipeline.validate_plan(plan)
            record["valid"] = True
        except (ValueError, OSError, KeyError, TypeError) as error:
            record["error"] = str(error)[:500]
        record["seconds"] = round(time.monotonic() - started, 3)
        return record
    return planner


def planned_runner(runner, planner, task):
    """Wrap an executor runner for one task: the first attempt in a worktree gets a
    fresh plan in its brief, the retry reuses it. An invalid plan is recorded and the
    brief goes out without it; the record says so, never silently."""
    plans = {}

    def wrapped(wt, model, brief):
        if wt not in plans:
            plans[wt] = planner(wt, task)
        record = runner(wt, model, plan_brief(brief, plans[wt]))
        record["planner"] = plans[wt]
        return record
    return wrapped


def plan_brief(brief, plan_record):
    if not plan_record or not plan_record.get("valid"):
        return brief
    return brief + "\n\nPLAN prepared by a read-only planner (follow it; it grants no permission):\n" + \
        json.dumps(plan_record["plan"], ensure_ascii=False)


REPLAY_PROMPT = """You are reviewing a diff produced by another agent for this task:

{prompt}

The diff is below (the task's test files were the specification and are excluded).
Report defects only. Severity: high = wrong behavior or security; medium = incomplete
or fragile; low = style. No findings is a valid answer. Return exactly one JSON object
{{"findings": [{{"severity": "high|medium|low", "title": "...", "file": "..."}}]}} and nothing else.

```diff
{diff}
```"""


def replay_review(record, backend, model):
    """A candidate reviewer on a recorded diff; findings are counted, not believed."""
    entry = _entry()
    started = time.monotonic()
    out = {"task": record["task"], "executor": record.get("runtime_model_pair") or record["model"],
           "reviewer_backend": backend, "reviewer_model": model, "reference": None, "candidate": None,
           "usage": None, "error": None}
    reference = record.get("findings") if isinstance(record.get("findings"), dict) else None
    out["reference"] = reference
    with tempfile.TemporaryDirectory(prefix="aos-bench-replay-") as tmp:
        try:
            result = entry.execute(backend, REPLAY_PROMPT.format(prompt=record.get("prompt", ""), diff=record["diff"]),
                                   tmp, readonly=True, model=model)
            out["usage"] = result.get("usage")
            out["candidate"] = parse_findings(json.dumps(entry.json_reply(result)))
        except (ValueError, OSError, KeyError, TypeError) as error:
            out["error"] = str(error)[:500]
    out["seconds"] = round(time.monotonic() - started, 3)
    if reference and out["candidate"]:
        ref_files = {f["file"] for f in reference["items"] if f["severity"] in ("high", "medium")}
        cand = [f for f in out["candidate"]["items"] if f["severity"] in ("high", "medium")]
        out["same_file_overlap"] = sum(1 for f in cand if f["file"] in ref_files)
        out["reference_high_medium"] = len([f for f in reference["items"] if f["severity"] in ("high", "medium")])
        out["candidate_high_medium"] = len(cand)
    return out


def replay_summary(rows):
    by = {}
    for r in rows:
        by.setdefault(r["reviewer_model"], []).append(r)
    lines = ["| reviewer | diff letti | errori | high/med/low candidato | high/med/low riferimento | stesso file (h/m) |",
             "|---|---|---|---|---|---|"]
    for model, rs in by.items():
        ok = [r for r in rs if r.get("candidate")]
        errors = sum(1 for r in rs if r.get("error"))
        c = "/".join(str(sum(r["candidate"][k] for r in ok)) for k in ("high", "medium", "low"))
        ref = [r["reference"] for r in rs if r.get("reference")]
        f = "/".join(str(sum(x[k] for x in ref)) for k in ("high", "medium", "low")) if ref else "n/d"
        overlap = sum(r.get("same_file_overlap", 0) for r in ok)
        cand_hm = sum(r.get("candidate_high_medium", 0) for r in ok)
        lines.append(f"| {model} | {len(ok)}/{len(rs)} | {errors} | {c} | {f} | {overlap}/{cand_hm} |")
    return "\n".join(lines)


def safe_name(model):
    return model.replace("/", "_").replace(":", "_").replace("|", "_")


def build_specs(models, executors):
    """The (label, runner) pairs to run, in order: legacy --models first, then each
    --executors entry labelled runtime|provider|model so the summary groups per runtime."""
    specs = []
    if models:
        for model in models.split(","):
            if model.split(":")[0] != "codex":
                raise ValueError("--models accepts only the codex reference run; open models go in --executors")
            specs.append((model, codex_runner))
    for entry in executors:
        runtime, provider, model = entry["runtime"], entry["provider"], entry["model"]
        specs.append((f"{runtime}|{provider}|{model}", open_executor_runner(runtime, provider, model)))
    return specs


def make_tester(task):
    test_cmd = task["test"]

    def tester(wt):
        restore_tests(wt, task)
        try:
            proc = subprocess.run(test_cmd, shell=True, cwd=wt, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return 124, "test timeout (600s)"
        return proc.returncode, (proc.stdout + proc.stderr)[-3000:]
    return tester


def parse_findings(text):
    """Counts by severity plus the list itself: a count alone cannot be checked later."""
    try:
        found = json.loads(text)["findings"]
    except (ValueError, KeyError, TypeError):
        return None
    counts = {s: sum(1 for f in found if isinstance(f, dict) and f.get("severity") == s)
              for s in ("high", "medium", "low")}
    counts["items"] = [{"severity": f.get("severity"), "title": str(f.get("title", ""))[:300],
                        "file": str(f.get("file", ""))[:200]} for f in found if isinstance(f, dict)]
    return counts


def stage_new_files(wt):
    """New files are invisible to `git diff`; intent-to-add shows them without committing.

    Two Qwen tasks that created a module from scratch passed their tests with a
    recorded diff of zero lines, and the reviewer, running `git diff` itself, judged
    an empty change.
    """
    # Only the untracked files: `--all` also staged deletions, and a staged deletion
    # vanishes from a plain `git diff` (reviewer round 5). The node_modules link is ours.
    # -z: without it git quotes non-ASCII names ("caf\303\251.py") and the quoted
    # string names no file (reviewer round 6). A failed staging is an error, not a
    # silent zero: the record would then miss the file the worker created.
    listed = delegate.git(wt, "ls-files", "-z", "--others", "--exclude-standard", "--", ".", ":!node_modules")
    untracked = [f for f in listed.stdout.split("\0") if f]
    if untracked:
        delegate.git(wt, "add", "--intent-to-add", "--", *untracked, check=True)
    return untracked


def diff_text(wt, exclude=()):
    spec = ["--", "."] + [f":!{f}" for f in exclude]
    # Against HEAD: whatever the worker staged or deleted is still the work.
    return delegate.git(wt, "-c", "core.quotepath=false", "diff", "HEAD", *spec).stdout[:60000]


def review_diff_command(task):
    """The diff the reviewer must read: the worker's work, not the restored tests."""
    excludes = " ".join(f"':!{f}'" for f in task.get("test_files") or ())
    return "git diff HEAD -- . " + excludes if excludes else "git diff HEAD"


def make_reviewer(task):
    prompt = REVIEW_PROMPT.format(prompt=task["prompt"], diff_command=review_diff_command(task))

    def reviewer(wt):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text(json.dumps(REVIEW_SCHEMA))
            report = Path(tmp) / "report.json"
            subprocess.run(["codex", "exec", "--sandbox", "read-only", "-C", wt, "-c", "mcp_servers={}",
                            "--output-schema", str(schema), "-o", str(report), prompt],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
            return parse_findings(report.read_text() if report.exists() else "")
    return reviewer


def diff_lines(wt, exclude=()):
    # The restored test files differ from the parent too; they are the judge, not the work.
    spec = ["--", "."] + [f":!{f}" for f in exclude]
    out = delegate.git(wt, "diff", "HEAD", "--numstat", *spec).stdout
    total = 0
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) == 3 and cols[0].isdigit() and cols[1].isdigit():
            total += int(cols[0]) + int(cols[1])
    return total


# --------------------------------------------------------------------- core
def brief_for(task, failure=None):
    text = task["prompt"]
    if task.get("files_hint"):
        text += "\n\nFiles likely involved: " + ", ".join(task["files_hint"])
    if task.get("test_files"):
        text += "\n\nThe specification is the test in " + ", ".join(task["test_files"]) + ": read it first, do not modify it."
    text += f"\n\nRun `{task['test']}` before you finish; it must pass. Do not commit."
    if failure:
        text += "\n\nYour previous attempt left this test output:\n" + failure
    return text


def add_cost(a, b):
    return None if a is None or b is None else a + b


def identity_fields(record):
    """The runtime dimension a paired executor adds to the delegate's record; empty for
    the legacy runners, so their records are byte-for-byte the old shape."""
    identity = {k: record[k] for k in ("runtime", "provider", "executor_model", "runtime_model_pair", "role", "planner") if k in record}
    if "runtime" in identity:
        identity.setdefault("role", "executor")
    return identity


def executor_succeeded(record):
    """A green project test cannot turn an interrupted worker into a benchmark pass."""
    return record.get("exit_code") == 0 and not record.get("error")


def run_task(task, model, runner, tester, reviewer, worktree, cleanup, differ=None, spend=None, no_review=False):
    if differ is None:
        differ = lambda wt: diff_lines(wt, task.get("test_files") or ())  # noqa: E731
    spend = spend if spend is not None else Spend(None)
    wt = worktree(task)
    tampered = False
    # The bench's own fingerprint of the shared .git, taken before the first attempt
    # and checked after every tester run: the test command executes the worker's
    # code, which can write what the delegate's window did not see (round 23).
    meta_paths = delegate.git_meta_paths(wt) if os.path.isdir(wt) else None
    meta_before = delegate.git_meta(meta_paths)

    def meta_changed():
        try:
            return delegate.git_meta(meta_paths) != meta_before
        except Exception:
            return True

    def tampered_record(attempts):
        # The worker changed the .git the worktree shares with the user's real repo
        # (config, hooks, includes, pointers): no git of ours runs there — not the
        # tester, not the reviewer, not the worktree removal (reviewer rounds 19-20),
        # on the first attempt or on the retry.
        last = attempts[-1]
        return {"task": task["id"], "model": model, "first_pass": False, "retry_pass": None,
                "escalated": True, "capped": False, "git_tampered": True,
                "seconds": sum(a["seconds"] for a in attempts),
                "cost_usd": None if any(a["usage"].get("cost_usd") is None for a in attempts)
                else sum(a["usage"]["cost_usd"] for a in attempts),
                "input_tokens": None, "output_tokens": None, "exit_code": last["exit_code"],
                "diff_stat": None, "attempt_costs": [a["usage"].get("cost_usd") for a in attempts],
                "known_costs": [a["usage"].get("cost_known_usd") for a in attempts], "findings": None,
                "diff_lines": 0, "diff": None, "test_tail": last.get("error"), **identity_fields(last)}
    def refused_record(attempts):
        # The last attempt was refused before a worker ran (dirty worktree, project
        # config, no runtime): escalated, nothing to test or review. An earlier
        # attempt that did run keeps its time and cost in the record.
        last, ran = attempts[-1], attempts[:-1]
        return {"task": task["id"], "model": model, "first_pass": False, "retry_pass": None,
                "escalated": True, "capped": False, "refused": True,
                "seconds": sum(a["seconds"] for a in attempts),
                "cost_usd": None if not ran or any(a["usage"].get("cost_usd") is None for a in ran)
                else sum(a["usage"]["cost_usd"] for a in ran),
                "input_tokens": None, "output_tokens": None, "exit_code": last["exit_code"],
                "diff_stat": None, "attempt_costs": [a["usage"].get("cost_usd") for a in ran] + [None],
                "known_costs": [a["usage"].get("cost_known_usd") for a in ran] + [None], "findings": None,
                "diff_lines": 0, "diff": None, "test_tail": str(last.get("error")), **identity_fields(last)}
    try:
        first = runner(wt, model, brief_for(task))
        state = attempt_state(first)
        if state == "tampered":
            tampered = True
            return tampered_record([first])
        if state == "refused":
            return refused_record([first])
        code, out = tester(wt)
        if not executor_succeeded(first):
            code = code or first.get("exit_code") or 1
            out = (out + "\nexecutor failed: " + str(first.get("error") or first.get("exit_code"))).strip()
        if meta_changed():
            tampered = True
            return tampered_record([first])
        result = {"task": task["id"], "model": model, "first_pass": executor_succeeded(first) and code == 0, "retry_pass": None,
                  "escalated": False, "capped": False, "seconds": first["seconds"],
                  "cost_usd": first["usage"].get("cost_usd"),
                  "input_tokens": first["usage"].get("input_tokens"), "output_tokens": first["usage"].get("output_tokens"),
                  "exit_code": first["exit_code"], "diff_stat": first["diff_stat"],
                  "attempt_costs": [first["usage"].get("cost_usd")],
                  # The reported part of each attempt, for the cap; the total above is
                  # null when any step went unreported (reviewer round 2).
                  "known_costs": [first["usage"].get("cost_known_usd")], **identity_fields(first)}
        # The cap is checked between attempts, not after both (reviewer round 3): a
        # retry that starts past the cap is money the cap was meant to stop.
        spend.add(result["known_costs"][0])
        if code != 0 and spend.exceeded():
            result["capped"] = True
            result["findings"] = None
            stage_new_files(wt)  # the record keeps what the worker created (reviewer round 4)
            result["diff_lines"] = differ(wt)
            result["diff"] = diff_text(wt, task.get("test_files") or ())
            result["test_tail"] = out
            return result
        if code != 0:
            second = runner(wt, model, brief_for(task, failure=out))
            state = attempt_state(second)
            if state == "refused":   # the delegate refused before a worker ran: refused, not tampered
                return refused_record([first, second])
            if state == "tampered":
                tampered = True
                return tampered_record([first, second])
            code, out = tester(wt)
            if not executor_succeeded(second):
                code = code or second.get("exit_code") or 1
                out = (out + "\nexecutor failed: " + str(second.get("error") or second.get("exit_code"))).strip()
            if meta_changed():
                tampered = True
                return tampered_record([first, second])
            result["retry_pass"] = executor_succeeded(second) and code == 0
            result["seconds"] += second["seconds"]
            # The per-task total is null when one attempt is unknown; the known part is
            # kept apart, because the spend cap must count every dollar it can see.
            result["attempt_costs"] = [result["cost_usd"], second["usage"].get("cost_usd")]
            result["known_costs"].append(second["usage"].get("cost_known_usd"))
            spend.add(result["known_costs"][1])
            result["cost_usd"] = add_cost(result["cost_usd"], second["usage"].get("cost_usd"))
            result["input_tokens"] = add_cost(result["input_tokens"], second["usage"].get("input_tokens"))
            result["output_tokens"] = add_cost(result["output_tokens"], second["usage"].get("output_tokens"))
            result["exit_code"] = second["exit_code"]
            result["diff_stat"] = second["diff_stat"]
            result["escalated"] = code != 0
        stage_new_files(wt)
        if no_review and not result["escalated"]:
            result["findings"] = "not_run"
        else:
            # A skipped review is labelled, never left to read as "no findings" (a null
            # review is not a PASS); an escalated task was never up for review anyway.
            result["findings"] = reviewer(wt) if not result["escalated"] else None
        result["diff_lines"] = differ(wt)
        result["diff"] = diff_text(wt, task.get("test_files") or ())
        result["test_tail"] = out
        return result
    except BaseException:
        # An exception mid-run (Ctrl-C, a tester that raises) reaches the cleanup
        # with no verdict on the shared .git: read the fingerprint first (round 24).
        tampered = tampered or meta_changed()
        raise
    finally:
        if tampered:
            shutil.rmtree(wt, ignore_errors=True)   # `git worktree remove` would run git there
        else:
            cleanup(wt)


def summary(rows):
    by_model = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    lines = ["| modello | first-pass | retry ok | escalation | sec/task | costo tot | high/med/low | righe diff/task |",
             "|---|---|---|---|---|---|---|---|"]
    for model, rs in by_model.items():
        n = len(rs)
        fp = sum(1 for r in rs if r["first_pass"])
        rp = sum(1 for r in rs if r["retry_pass"])
        esc = sum(1 for r in rs if r["escalated"])
        capped = sum(1 for r in rs if r.get("capped"))
        secs = sum(r["seconds"] for r in rs) / n
        known = [r["cost_usd"] for r in rs if r["cost_usd"] is not None]
        unknown = n - len(known)
        f = [r["findings"] for r in rs if isinstance(r.get("findings"), dict)]
        not_run = sum(1 for r in rs if r.get("findings") == "not_run")
        if f:
            hml = "/".join(str(sum(x[k] for x in f)) for k in ("high", "medium", "low"))
        elif not_run:
            # A skipped review is a fact about the run, not "no findings": it must never
            # read as a clean pass (0/0/0) nor as an acceptance.
            hml = "not_run"
        else:
            hml = "n/d"
        cost = "n/d" if unknown else f"{sum(known):.2f} $"
        if unknown:
            cost += f" (costo n/d: {unknown})"
        diff = sum(r.get("diff_lines", 0) for r in rs) / n
        esc_cell = f"{esc}/{n}" + (f" (+{capped} fermati dal tetto)" if capped else "")
        lines.append(f"| {model} | {fp}/{n} | {rp}/{n} | {esc_cell} | {secs:.0f} | {cost} | {hml} | {diff:.0f} |")
    return "\n".join(lines)


def check_task(task):
    """A task is valid when the commit's test fails on the parent and passes on the commit."""
    tester = make_tester(task)
    wt = make_worktree(task)
    try:
        parent_code, _ = tester(wt)
        delegate.git(wt, "checkout", "-q", "--detach", task["commit"], check=True)
        commit_code, tail = tester(wt)
    finally:
        remove_worktree(wt)
    ok = parent_code != 0 and commit_code == 0
    return ok, f"parent exit={parent_code} commit exit={commit_code}" + ("" if ok else "\n" + tail)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=str(HERE.parent / "evals/bench/tasks.json"))
    parser.add_argument("--models", help="comma-separated provider/model; 'codex' or 'codex:<model>' = reference run")
    parser.add_argument("--executors", help="JSON file: a list of {runtime, provider, model} to run the open runtimes")
    parser.add_argument("--out", help="docs/misure/bench/<date>")
    parser.add_argument("--cap-usd", type=float, default=None)
    parser.add_argument("--only", help="comma-separated task ids")
    parser.add_argument("--check", action="store_true", help="validate the corpus, run nothing external")
    parser.add_argument("--no-review", action="store_true",
                        help="skip the reviewer (deterministic-only runs); the record labels review not_run")
    parser.add_argument("--planners", help="JSON file: a list of {backend, model}; each --executors entry also runs "
                                           "with that planner's plan in the brief (label ...+plan:<model>)")
    parser.add_argument("--review-replay", help="results directory whose recorded diffs the --reviewers re-review")
    parser.add_argument("--reviewers", help="JSON file: a list of {backend, model} candidate reviewers")
    args = parser.parse_args()
    if args.review_replay:
        if not args.reviewers or not args.out:
            parser.error("--review-replay richiede --reviewers e --out")
        reviewers = json.load(open(args.reviewers))
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        rows = []
        for path in sorted(Path(args.review_replay).glob("*.json")):
            record = json.loads(path.read_text())
            if not record.get("diff") or record.get("escalated") or record.get("git_tampered"):
                continue
            if args.only and record["task"] not in set(args.only.split(",")):
                continue
            for reviewer in reviewers:
                target = out / f"{path.stem}-review-{safe_name(reviewer['model'])}.json"
                if target.exists():
                    rows.append(json.loads(target.read_text()))
                    continue
                r = replay_review(record, reviewer["backend"], reviewer["model"])
                r["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                target.write_text(json.dumps(r, ensure_ascii=False, indent=2))
                rows.append(r)
                print(f"{record['task']} × {reviewer['model']}: candidate={r.get('candidate') and {k: r['candidate'][k] for k in ('high','medium','low')}} "
                      f"err={r.get('error')}", flush=True)
        (out / "summary.md").write_text(f"# Review replay {out.name}\n\n{replay_summary(rows)}\n\n"
                                        "Il confronto per file è un indicatore grezzo: due finding sullo stesso file non "
                                        "sono lo stesso difetto. I finding dei candidati vanno arbitrati prima di contare.\n")
        print((out / "summary.md").read_text())
        return 0
    tasks = json.load(open(args.tasks))["tasks"]
    if args.only:
        keep = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep]
    if args.check:
        bad = 0
        for task in tasks:
            ok, detail = check_task(task)
            bad += not ok
            print(f"{'ok ' if ok else 'KO '} {task['id']}: {detail}", flush=True)
        print(f"{len(tasks) - bad}/{len(tasks)} task validi")
        return 1 if bad else 0
    if not args.out or (not args.models and not args.executors):
        parser.error("--out e (--models o --executors) sono obbligatori senza --check")
    executors = []
    if args.executors:
        loaded = json.load(open(args.executors))
        if not isinstance(loaded, list):
            parser.error("--executors deve essere una lista JSON di {runtime, provider, model}")
        for entry in loaded:
            if not isinstance(entry, dict) or not {"runtime", "provider", "model"} <= set(entry):
                parser.error("ogni esecutore deve avere runtime, provider e model")
            if entry["runtime"] not in open_executor.OPEN_EXECUTION_RUNTIMES:
                parser.error(f"runtime non disponibile per open execution: {entry['runtime']}")
        executors = loaded
    if args.cap_usd is not None:
        # Reference runs (--models without --executors) report no cost either.
        uncosted = sorted({e["runtime"] for e in executors if e["runtime"] not in COST_RUNTIMES}) \
            or ([] if COST_RUNTIMES else ["reference"])
        if uncosted:
            parser.error("--cap-usd richiesto ma un runtime non riporta il costo "
                         f"({', '.join(uncosted)}): la spesa resterebbe nulla; togli il tetto e usa i limiti di tempo/step")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    spend = Spend(args.cap_usd)
    rows = []
    stopped = False
    planners = json.load(open(args.planners)) if args.planners else []
    specs = build_specs(args.models, executors)
    if planners:
        planned = []
        for planner in planners:
            for label, runner in specs:
                planned.append((f"{label}+plan:{planner['model']}", runner, make_planner(planner["backend"], planner["model"])))
        specs = planned
    else:
        specs = [(label, runner, None) for label, runner in specs]
    for model, base_runner, planner in specs:
        for task in tasks:
            runner = planned_runner(base_runner, planner, task) if planner else base_runner
            record = out / f"{task['id']}-{safe_name(model)}.json"
            if record.exists():
                rows.append(json.loads(record.read_text()))
                if rows[-1].get("git_tampered"):   # a resume does not skip the stop (round 22)
                    print(f"{task['id']}: record di manomissione del .git di {task['repo']} — fermo; controlla "
                          "quel repo, poi rimuovi il record per riprendere", file=sys.stderr)
                    return 3
                continue
            r = run_task(task, model, runner, make_tester(task), make_reviewer(task),
                         make_worktree, remove_worktree, spend=spend, no_review=args.no_review)
            r["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            record.write_text(json.dumps(r, ensure_ascii=False, indent=2))
            rows.append(r)
            print(f"{task['id']} × {model}: first={r['first_pass']} retry={r['retry_pass']} esc={r['escalated']} "
                  f"capped={r.get('capped', False)} {r['seconds']:.0f}s cost={r['cost_usd']}", flush=True)
            if r.get("git_tampered"):
                print(f"{task['id']}: il worker ha modificato il .git condiviso con {task['repo']} — "
                      "fermo; controlla .git/config, info e hooks di quel repo prima di qualsiasi git, "
                      "poi `git worktree prune`", file=sys.stderr)
                return 3
            if spend.exceeded():
                print(f"tetto superato: {spend.total:.2f} $ > {spend.cap} $ — fermo", file=sys.stderr)
                stopped = True
                break
        if stopped:
            break
    (out / "summary.md").write_text(f"# Benchmark {out.name}\n\n{summary(rows)}\n\n"
                                    f"Task: {len(tasks)} · record: {len(rows)} · costi non riportati dal runtime: "
                                    f"{sum(1 for r in rows if r['cost_usd'] is None)}\n")
    print((out / "summary.md").read_text())
    return 2 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
