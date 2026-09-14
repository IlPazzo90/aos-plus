#!/usr/bin/env python3
"""Create explicit task measurements without reading history or contacting providers."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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
    atomic_write(args.record, data, exclusive=True)


def finish(args):
    counters = (args.input_tokens, args.output_tokens, args.cached_input_tokens)
    if any(value is not None for value in counters) and args.metric_source is None:
        raise ValueError("--metric-source obbligatorio con contatori provider")
    if args.rtk_saved_estimate is not None and args.rtk_source is None:
        raise ValueError("--rtk-source obbligatorio con stima RTK")
    if args.cached_input_tokens is not None and args.input_tokens is not None and args.cached_input_tokens > args.input_tokens:
        raise ValueError("cached-input-tokens non può superare input-tokens")
    # Serialize this CLI's finish operations; a completed record is immutable.
    lock = args.record.with_name(args.record.name + ".lock")
    lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.close(lock_fd)
        if args.record.is_symlink():
            raise ValueError("record simbolico non supportato")
        with args.record.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise ValueError("schema record non valido")
        required = ("task", "runtime", "model", "version", "started_at")
        if not all(isinstance(data.get(key), str) and data[key].strip() for key in required):
            raise ValueError("identità record incompleta")
        if "finished_at" not in data or "outcome" not in data:
            raise ValueError("stato record incompleto")
        if data["finished_at"] is not None or data["outcome"] is not None:
            raise ValueError("record già completato")
        try:
            started = datetime.fromisoformat(data["started_at"].replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("data di avvio non valida") from None
        now = utc_now()
        if started.tzinfo is None or started.utcoffset() is None or started > now:
            raise ValueError("data di avvio non valida o futura")
        data.update(finished_at=timestamp(now), elapsed_seconds=(now - started).total_seconds(),
                    outcome=args.outcome, corrections=args.corrections,
                    provider_metrics={"input_tokens": args.input_tokens, "output_tokens": args.output_tokens,
                                      "cached_input_tokens": args.cached_input_tokens, "source": args.metric_source},
                    rtk_estimate={"saved": args.rtk_saved_estimate, "source": args.rtk_source})
        atomic_write(args.record, data)
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    begin = commands.add_parser("start", help="crea un nuovo record esclusivo")
    end = commands.add_parser("finish", help="completa una volta il record indicato")
    for command in (begin, end):
        command.add_argument("--record", required=True, type=Path)
    for name in ("task", "runtime", "model", "version"):
        begin.add_argument("--" + name, required=True, type=text_value)
    end.add_argument("--outcome", required=True, choices=("accepted", "rejected", "partial"))
    for name in ("corrections", "input-tokens", "output-tokens", "cached-input-tokens", "rtk-saved-estimate"):
        end.add_argument("--" + name, type=nonnegative)
    for name in ("metric-source", "rtk-source"):
        end.add_argument("--" + name, type=text_value)
    args = parser.parse_args()
    try:
        (start if args.command == "start" else finish)(args)
    except (OSError, ValueError, TypeError) as error:
        # Do not expose record contents or parse-error excerpts.
        detail = str(error) if type(error) is ValueError else type(error).__name__
        print(f"ERRORE: {detail}; nessun record completato sovrascritto.", file=sys.stderr)
        return 1
    print(f"OK: {args.command} {args.record}; dati espliciti, nessuna raccolta automatica.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
