#!/usr/bin/env python3
"""Create explicit task measurements without reading history or contacting providers."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def text_value(value):
    if not value.strip():
        raise argparse.ArgumentTypeError("il testo non può essere vuoto")
    return value


def nonnegative(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("richiesto intero non negativo")
    if number < 0:
        raise argparse.ArgumentTypeError("richiesto intero non negativo")
    return number


def utc_now():
    return datetime.now(timezone.utc)


def timestamp(value):
    return value.isoformat().replace("+00:00", "Z")


def atomic_write(path, data, exclusive=False):
    # A sibling temporary file keeps replacement atomic on the same filesystem.
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Atomic creation fails even when an existing target is a symlink.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def start(args):
    data = {
        "schema": 1,
        "task": args.task,
        "runtime": args.runtime,
        "model": args.model,
        "version": args.version,
        "started_at": timestamp(utc_now()),
        "finished_at": None,
        "elapsed_seconds": None,
        "outcome": None,
        "corrections": None,
        "provider_metrics": {"input_tokens": None, "output_tokens": None,
                             "cached_input_tokens": None, "source": None},
        "rtk_estimate": {"saved": None, "source": None},
    }
    # The record is mandatory at T2/T3, so a missing parent directory must not be the
    # reason a task closes without one. Only the explicit --record path is created.
    args.record.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(args.record, data, exclusive=True)


def git(top, *argv):
    result = subprocess.run(["git", "-C", str(top), *argv], capture_output=True, text=True, timeout=20)
    return result.stdout if result.returncode == 0 else None


def work_observed(record, started_at):
    """True/False when Git can tell whether any work happened after start; None when it cannot.

    The first real record measured 65 seconds because start ran after the work was
    already committed. Elapsed time then covers nothing, and nothing said so.
    `git status` says what is dirty, not since when, so a dirty file counts only if its
    mtime is later than start. Untracked files are listed one by one: a grouped
    `?? dir/` entry would be judged by the directory's mtime, which the lock itself
    updates. A deletion has no mtime and no directory is a safe proxy (same lock), so a
    deletion with nothing else observed leaves the answer unknown rather than wrong.
    `-z` output is parsed as records: a rename carries its old path as the next record,
    and " -> " inside a file name is just a file name.
    """
    record = record.resolve()
    top = git(record.parent, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    top = Path(top.strip())
    relative = os.path.relpath(record, top)
    status = git(top, "status", "--porcelain", "-z", "--untracked-files=all")
    last_commit = "" if git(top, "rev-parse", "-q", "--verify", "HEAD") is None else git(top, "log", "-1", "--format=%ct")
    if status is None or last_commit is None:
        return None
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
    unknown = False
    if last_commit.strip():
        # Git dates commits to the second; a commit in the same second as start is
        # not orderable against it, so it is unknown, neither after nor before.
        if int(last_commit.strip()) > int(started):
            return True
        if int(last_commit.strip()) == int(started):
            unknown = True
    # The index's mtime is not a signal: `git status` itself refreshes the index, so
    # the call above would have made every finish look like work. A rename keeps the
    # file's mtime, so a rename of an old file after start reads as unknown, not as work.
    entries = status.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            index += 1  # the old path follows as its own record
        if path in (relative, relative + ".lock"):
            continue
        target = top / path
        try:
            mtime = target.stat().st_mtime
        except OSError:
            unknown = True  # deleted or unreadable: no timestamp to trust
            continue
        if mtime > started:
            return True
        if code[0] in "RC":
            unknown = True  # a rename keeps the old mtime: not known either way
    return None if unknown else False


def load_record(record):
    if record.is_symlink():
        raise ValueError("record simbolico non supportato")
    with record.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("schema record non valido")
    required = ("task", "runtime", "model", "version", "started_at")
    if not all(isinstance(data.get(key), str) and data[key].strip() for key in required):
        raise ValueError("identità record incompleta")
    if "finished_at" not in data or "outcome" not in data:
        raise ValueError("stato record incompleto")
    return data


def locked(record):
    # Serialize this CLI's writes to one record. Returns the lock path to unlink.
    lock = record.with_name(record.name + ".lock")
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError(f"lock presente ({lock}): un finish precedente è stato interrotto; "
                         "se nessun processo lo sta usando, rimuovi il lock e ripeti") from None
    os.close(lock_fd)
    return lock


def finish(args):
    counters = (args.input_tokens, args.output_tokens, args.cached_input_tokens)
    if any(value is not None for value in counters) and args.metric_source is None:
        raise ValueError("--metric-source obbligatorio con contatori provider")
    if args.rtk_saved_estimate is not None and args.rtk_source is None:
        raise ValueError("--rtk-source obbligatorio con stima RTK")
    if args.cached_input_tokens is not None and args.input_tokens is not None and args.cached_input_tokens > args.input_tokens:
        raise ValueError("cached-input-tokens non può superare input-tokens")
    lock = locked(args.record)
    try:
        data = load_record(args.record)
        if data["finished_at"] is not None or data["outcome"] is not None:
            raise ValueError("record già completato")
        try:
            started = datetime.fromisoformat(data["started_at"].replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("data di avvio non valida") from None
        now = utc_now()
        if started.tzinfo is None or started.utcoffset() is None or started > now:
            raise ValueError("data di avvio non valida o futura")
        observed = work_observed(args.record, data["started_at"])
        if observed is False:
            print("AVVISO: nessuna modifica né commit dopo started_at — il record è partito dopo il "
                  "lavoro e elapsed_seconds non lo copre.", file=sys.stderr)
        data.update(finished_at=timestamp(now), elapsed_seconds=(now - started).total_seconds(),
                    work_observed_after_start=observed,
                    outcome=args.outcome, judged_at=None, corrections=args.corrections,
                    provider_metrics={"input_tokens": args.input_tokens, "output_tokens": args.output_tokens,
                                      "cached_input_tokens": args.cached_input_tokens, "source": args.metric_source},
                    rtk_estimate={"saved": args.rtk_saved_estimate, "source": args.rtk_source})
        atomic_write(args.record, data)
    finally:
        lock.unlink()


def judge(args):
    """Record the user's verdict on a finished task, once.

    Both records before this existed said `accepted`, written by the author minutes
    before the user had read the result. The author cannot close this axis: `finish`
    writes what was delivered, and this command writes what the user said about it.
    """
    lock = locked(args.record)
    try:
        data = load_record(args.record)
        if data["finished_at"] is None or data["outcome"] is None:
            raise ValueError("record non ancora completato: esegui finish prima di judge")
        if data.get("judged_at") is not None or data["outcome"] in ("accepted", "rejected"):
            raise ValueError("verdetto già registrato")
        if data["outcome"] == "blocked":
            raise ValueError("un lavoro bloccato non ha un risultato da giudicare")
        data.update(outcome=args.verdict, judged_at=timestamp(utc_now()), delivered_as=data["outcome"])
        atomic_write(args.record, data)
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="crea un nuovo record esclusivo")
    end = commands.add_parser("finish", help="completa una volta il record indicato")
    verdict = commands.add_parser("judge", help="registra una volta il verdetto dell'utente su un record completato")
    for command in (begin, end, verdict):
        command.add_argument("--record", required=True, type=Path)
    for name in ("task", "runtime", "model", "version"):
        begin.add_argument("--" + name, required=True, type=text_value)
    # "blocked" is not a shade of "partial": a task stopped by a missing capability,
    # an exhausted quota or a declared gate produced no deliverable to accept in part.
    # "accepted" and "rejected" are not choices here: they are the user's words, and
    # the author writing them at finish is the author grading their own work.
    end.add_argument("--outcome", required=True, choices=("delivered", "partial", "blocked"))
    verdict.add_argument("--verdict", required=True, choices=("accepted", "rejected"))
    for name in ("corrections", "input-tokens", "output-tokens", "cached-input-tokens", "rtk-saved-estimate"):
        end.add_argument("--" + name, type=nonnegative)
    for name in ("metric-source", "rtk-source"):
        end.add_argument("--" + name, type=text_value)
    args = parser.parse_args()
    try:
        {"start": start, "finish": finish, "judge": judge}[args.command](args)
    except (OSError, ValueError, TypeError) as error:
        # Do not expose record contents or parse-error excerpts.
        detail = str(error) if type(error) is ValueError else type(error).__name__
        print(f"ERRORE: {detail}; nessun record completato sovrascritto.", file=sys.stderr)
        return 1
    print(f"OK: {args.command} {args.record}; dati espliciti, nessuna raccolta automatica.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
