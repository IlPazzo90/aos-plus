#!/usr/bin/env python3
"""Replay real commits against models in throwaway worktrees and record what happened.

Nothing here judges quality: tests, reviewer findings and diff size are written
side by side and the summary shows all three. Cost is what the runtime reported
or null; a null never counts as zero and is never estimated.
"""
import argparse
import datetime as dt
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
# Per attempt. A worker past these is looping, not working: the first run of this
# benchmark spent 3.30 $ and 140 steps on a diff of zero lines.
MAX_COST_USD = 1.0
MAX_STEPS = 60

REVIEW_PROMPT = """You are reviewing a diff produced by another agent for this task:

{prompt}

Run `git diff` in this directory and report defects only. Severity: high = wrong
behavior or security; medium = incomplete or fragile; low = style. No findings is
a valid answer."""

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
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", wt, task["commit"] + "^"],
                   check=True, capture_output=True, text=True)
    # The worktree exists from here: a failure in its preparation must not leave it
    # registered (reviewer's repro: a missing test file left a worktree behind).
    try:
        # A worktree has no node_modules; the main checkout's are read-only for a test run.
        modules = repo / "node_modules"
        if modules.is_dir():
            os.symlink(modules, Path(wt) / "node_modules")
        restore_tests(wt, task)
    except Exception:
        remove_worktree(wt)
        raise
    return wt


def restore_tests(wt, task):
    """The commit's test files are the spec the worker sees and the judge it cannot rewrite."""
    files = task.get("test_files") or []
    if files:
        subprocess.run(["git", "-C", wt, "checkout", task["commit"], "--", *files],
                       check=True, capture_output=True, text=True)


def remove_worktree(wt):
    subprocess.run(["git", "-C", wt, "worktree", "remove", "--force", wt], capture_output=True)
    shutil.rmtree(wt, ignore_errors=True)


def opencode_runner(wt, model, brief):
    proc = subprocess.run([sys.executable, str(DELEGATE), "--repo", wt, "--model", model,
                           "--brief", "-", "--json", "--allow-dirty",
                           "--max-cost", str(MAX_COST_USD), "--max-steps", str(MAX_STEPS)],
                          input=brief, capture_output=True, text=True)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"exit_code": proc.returncode, "seconds": 0, "usage": {}, "diff_stat": "",
                "reply": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]}


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
    proc = subprocess.run(cmd + [brief], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return {"exit_code": proc.returncode, "seconds": round(time.monotonic() - started, 3),
            "usage": codex_usage(proc.stdout),
            "diff_stat": subprocess.run(["git", "-C", wt, "diff", "--stat"], capture_output=True, text=True).stdout.strip(),
            "reply": proc.stdout[-4000:], "stderr_tail": proc.stderr[-4000:]}


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
    # The node_modules link is ours, not the worker's.
    subprocess.run(["git", "-C", wt, "add", "--intent-to-add", "--all", "--", ".", ":!node_modules"],
                   capture_output=True)


def diff_text(wt, exclude=()):
    spec = ["--", "."] + [f":!{f}" for f in exclude]
    return subprocess.run(["git", "-C", wt, "diff", *spec], capture_output=True, text=True).stdout[:60000]


def make_reviewer(prompt):
    def reviewer(wt):
        with tempfile.TemporaryDirectory() as tmp:
            schema = Path(tmp) / "schema.json"
            schema.write_text(json.dumps(REVIEW_SCHEMA))
            report = Path(tmp) / "report.json"
            subprocess.run(["codex", "exec", "--sandbox", "read-only", "-C", wt, "-c", "mcp_servers={}",
                            "--output-schema", str(schema), "-o", str(report), REVIEW_PROMPT.format(prompt=prompt)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
            return parse_findings(report.read_text() if report.exists() else "")
    return reviewer


def diff_lines(wt, exclude=()):
    # The restored test files differ from the parent too; they are the judge, not the work.
    spec = ["--", "."] + [f":!{f}" for f in exclude]
    out = subprocess.run(["git", "-C", wt, "diff", "--numstat", *spec], capture_output=True, text=True).stdout
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


def run_task(task, model, runner, tester, reviewer, worktree, cleanup, differ=None, spend=None):
    if differ is None:
        differ = lambda wt: diff_lines(wt, task.get("test_files") or ())  # noqa: E731
    spend = spend if spend is not None else Spend(None)
    wt = worktree(task)
    try:
        first = runner(wt, model, brief_for(task))
        code, out = tester(wt)
        result = {"task": task["id"], "model": model, "first_pass": code == 0, "retry_pass": None,
                  "escalated": False, "capped": False, "seconds": first["seconds"],
                  "cost_usd": first["usage"].get("cost_usd"),
                  "input_tokens": first["usage"].get("input_tokens"), "output_tokens": first["usage"].get("output_tokens"),
                  "exit_code": first["exit_code"], "diff_stat": first["diff_stat"],
                  "attempt_costs": [first["usage"].get("cost_usd")],
                  # The reported part of each attempt, for the cap; the total above is
                  # null when any step went unreported (reviewer round 2).
                  "known_costs": [first["usage"].get("cost_known_usd")]}
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
            code, out = tester(wt)
            result["retry_pass"] = code == 0
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
        result["findings"] = reviewer(wt) if not result["escalated"] else None
        result["diff_lines"] = differ(wt)
        result["diff"] = diff_text(wt, task.get("test_files") or ())
        result["test_tail"] = out
        return result
    finally:
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
        f = [r["findings"] for r in rs if r["findings"]]
        hml = "/".join(str(sum(x[k] for x in f)) for k in ("high", "medium", "low")) if f else "n/d"
        cost = f"{sum(known):.2f} $" + (f" (costo n/d: {unknown})" if unknown else "")
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
        subprocess.run(["git", "-C", wt, "checkout", "-q", "--detach", task["commit"]], check=True,
                       capture_output=True, text=True)
        commit_code, tail = tester(wt)
    finally:
        remove_worktree(wt)
    ok = parent_code != 0 and commit_code == 0
    return ok, f"parent exit={parent_code} commit exit={commit_code}" + ("" if ok else "\n" + tail)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=str(HERE.parent / "evals/bench/tasks.json"))
    parser.add_argument("--models", help="comma-separated provider/model; 'codex' or 'codex:<model>' = reference run")
    parser.add_argument("--out", help="docs/misure/bench/<date>")
    parser.add_argument("--cap-usd", type=float, default=None)
    parser.add_argument("--only", help="comma-separated task ids")
    parser.add_argument("--check", action="store_true", help="validate the corpus, run nothing external")
    args = parser.parse_args()
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
    if not args.models or not args.out:
        parser.error("--models e --out sono obbligatori senza --check")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    spend = Spend(args.cap_usd)
    rows = []
    stopped = False
    for model in args.models.split(","):
        runner = codex_runner if model.split(":")[0] == "codex" else opencode_runner
        for task in tasks:
            record = out / f"{task['id']}-{model.replace('/', '_').replace(':', '_')}.json"
            if record.exists():
                rows.append(json.loads(record.read_text()))
                continue
            r = run_task(task, model, runner, make_tester(task), make_reviewer(task["prompt"]),
                         make_worktree, remove_worktree, spend=spend)
            r["recorded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            record.write_text(json.dumps(r, ensure_ascii=False, indent=2))
            rows.append(r)
            print(f"{task['id']} × {model}: first={r['first_pass']} retry={r['retry_pass']} esc={r['escalated']} "
                  f"capped={r.get('capped', False)} {r['seconds']:.0f}s cost={r['cost_usd']}", flush=True)
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
