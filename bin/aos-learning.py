#!/usr/bin/env python3
"""Persistent verified-learning ledger on sqlite3.

Records candidate lessons only when they are mechanically verified against
host-owned observed checks; persists rejected candidates with reasons and
counts; deduplicates identical candidates; measures outcomes and applies
lessons with an audit trail. Nothing here ever executes a lesson command and
nothing here edits a global policy automatically.

Trust boundary
--------------
Everything below is a host-owned facility, not a model-facing API. The model
may propose a ``lesson`` and a set of *ids* in ``lesson["evidence"]``; the host
is the only party that supplies the verified ``checks`` themselves (their
executed argv, measured exit code, captured output and pinned revision) and, at
application time, the literal ``rollback:<id>`` reference. A check that lacks
real execution data is indistinguishable from a model-only claim and is
rejected. The functions here persist and aggregate host inputs verbatim; they
never accept a command to run and never treat a model-supplied string as
approval.
"""
import argparse
import json
import math
import re
import sqlite3
from datetime import datetime, timezone

LESSON_TYPES = ("execution", "domain", "routing", "verification")
CONFIDENCES = ("low", "medium", "high")
SCOPES = ("project", "global")
ACTIONS = ("test", "policy", "routing", "skill", "guardrail", "benchmark", "documentation")

_ROLLBACK_RE = re.compile(r"rollback:(\S+)")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS lessons ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " lesson_type TEXT NOT NULL,"
    " source_task TEXT NOT NULL,"
    " evidence TEXT NOT NULL,"
    " confidence TEXT NOT NULL,"
    " scope TEXT NOT NULL,"
    " project TEXT,"
    " affected_component TEXT NOT NULL,"
    " recommended_action TEXT NOT NULL,"
    " mechanically_verified INTEGER NOT NULL,"
    " status TEXT NOT NULL,"
    " reason TEXT,"
    " count INTEGER NOT NULL DEFAULT 1,"
    " created_at TEXT NOT NULL"
    ");"
    "CREATE TABLE IF NOT EXISTS outcomes ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " runtime TEXT, provider TEXT, model TEXT, role TEXT, tier TEXT, risk TEXT,"
    " success INTEGER NOT NULL, worker_exit INTEGER, test_pass INTEGER,"
    " error TEXT, cost_class TEXT,"
    " cost REAL, input_tokens INTEGER, output_tokens INTEGER,"
    " task_id TEXT, project TEXT, retry INTEGER NOT NULL DEFAULT 0, main_host TEXT, uncertainty TEXT,"
    " verification_status TEXT,"
    " created_at TEXT NOT NULL"
    ");"
    "CREATE TABLE IF NOT EXISTS applications ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " lesson_id INTEGER NOT NULL,"
    " action TEXT NOT NULL,"
    " evidence TEXT NOT NULL,"
    " approved INTEGER NOT NULL,"
    " rollback_identifier TEXT NOT NULL,"
    " created_at TEXT NOT NULL"
    ");"
    "CREATE TABLE IF NOT EXISTS reservations ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " task_id TEXT NOT NULL,"
    " estimate REAL,"
    " premium INTEGER NOT NULL,"
    " created_at TEXT NOT NULL,"
    " settled_at TEXT"
    ");"
    "CREATE INDEX IF NOT EXISTS idx_lessons_dedup ON lessons (source_task, affected_component, evidence);"
)
# A reservation that no outcome ever settled (crashed session) stops counting
# against concurrent sessions after this many seconds.
RESERVATION_TTL_SECONDS = 3600


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _connect(path):
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _ensure_schema(conn):
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    # Forward-compatible column additions for databases created before these
    # fields existed. ALTER TABLE ADD COLUMN fails harmlessly if already present.
    for column in ("error TEXT", "cost_class TEXT", "retry INTEGER NOT NULL DEFAULT 0", "main_host TEXT", "uncertainty TEXT"):
        try:
            conn.execute("ALTER TABLE outcomes ADD COLUMN %s" % column)
        except sqlite3.OperationalError:
            pass
    # Add verification_status column (backward compatible, nullable)
    try:
        conn.execute("ALTER TABLE outcomes ADD COLUMN verification_status TEXT")
    except sqlite3.OperationalError:
        pass


def _canon_evidence(ids):
    return json.dumps(sorted(set(str(i) for i in ids)))


def _as_text(value):
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, bool):
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _snapshot(checks):
    entries = [{
        "id": c["id"],
        "argv": list(c["argv"]),
        "exit_code": c["exit_code"],
        "output": c["output"],
        "revision": c["revision"],
    } for c in checks]
    entries.sort(key=lambda c: c["id"])
    return json.dumps({"ids": [c["id"] for c in entries], "checks": entries}, sort_keys=True)


def _evidence_identity(raw):
    """Compare executed evidence, never arbitrary host check labels."""
    try:
        data = json.loads(raw)
        checks = data["checks"]
        normalized = [{key: check[key] for key in ("argv", "exit_code", "output", "revision")}
                      for check in checks]
        return json.dumps(sorted(normalized, key=lambda check: json.dumps(check, sort_keys=True)), sort_keys=True)
    except (KeyError, TypeError, ValueError):
        return raw


# --------------------------------------------------------------------------
# record_lesson
# --------------------------------------------------------------------------

def record_lesson(database, lesson, checks):
    """Validate and persist a candidate lesson. Returns a status dict.

    Approved lessons are persisted once; identical candidates are deduplicated
    and counted; invalid or unverified candidates are persisted as rejected
    with a reason and a running count. Only the host-supplied ``checks`` (real,
    executed evidence) can satisfy ``lesson["evidence"]`` ids. Never executes
    any lesson command.
    """
    if not isinstance(lesson, dict):
        return _reject(database, lesson, checks, "invalid_lesson")

    lesson_type = lesson.get("lesson_type")
    if lesson_type not in LESSON_TYPES:
        return _reject(database, lesson, checks, "invalid_lesson_type")

    confidence = lesson.get("confidence")
    if confidence not in CONFIDENCES:
        return _reject(database, lesson, checks, "invalid_confidence")

    scope = lesson.get("scope")
    if scope not in SCOPES:
        return _reject(database, lesson, checks, "invalid_scope")

    project = lesson.get("project")
    if scope == "project":
        if not isinstance(project, str) or not project.strip():
            return _reject(database, lesson, checks, "missing_project")
    else:
        if project is not None and str(project).strip():
            return _reject(database, lesson, checks, "global_with_project")

    if lesson.get("mechanically_verified") is not True:
        return _reject(database, lesson, checks, "not_mechanically_verified")

    for field in ("source_task", "affected_component", "recommended_action"):
        value = lesson.get(field)
        if not isinstance(value, str) or not value.strip():
            return _reject(database, lesson, checks, "missing_field")

    evidence = lesson.get("evidence")
    if not isinstance(evidence, (list, tuple)) or not evidence:
        return _reject(database, lesson, checks, "unverified_model_only")

    check_map = {}
    if not isinstance(checks, (list, tuple)):
        checks = []
    for check in checks:
        if not isinstance(check, dict):
            return _reject(database, lesson, checks, "invalid_check")
        cid = check.get("id")
        if not isinstance(cid, str) or not cid.strip():
            return _reject(database, lesson, checks, "invalid_check")
        if cid in check_map:
            return _reject(database, lesson, checks, "duplicate_check")
        check_map[cid] = check

    valid_checks = []
    for check_id in evidence:
        if not isinstance(check_id, str) or not check_id.strip():
            return _reject(database, lesson, checks, "invalid_evidence")
        check = check_map.get(check_id)
        if check is None:
            return _reject(database, lesson, checks, "invalid_evidence")
        revision = check.get("revision")
        if not isinstance(revision, str) or not revision.strip():
            return _reject(database, lesson, checks, "invalid_evidence")
        argv = check.get("argv")
        exit_code = check.get("exit_code")
        output = check.get("output")
        if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(a, str) for a in argv):
            return _reject(database, lesson, checks, "invalid_check")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return _reject(database, lesson, checks, "invalid_check")
        if not isinstance(output, str):
            return _reject(database, lesson, checks, "invalid_check")
        valid_checks.append(check)

    valid_checks = list({check["id"]: check for check in valid_checks}.values())

    source_task = lesson["source_task"]
    component = lesson["affected_component"]
    evidence_json = _snapshot(valid_checks)
    project_value = project if scope == "project" else None

    conn = _connect(database)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if scope == "project":
                existing = conn.execute(
                    "SELECT id, count FROM lessons"
                    " WHERE source_task = ? AND affected_component = ? AND evidence = ?"
                    " AND lesson_type = ? AND confidence = ? AND recommended_action = ?"
                    " AND scope = 'project' AND project = ? AND status = 'approved'"
                    " ORDER BY id LIMIT 1",
                    (source_task, component, evidence_json, lesson_type, confidence,
                     lesson["recommended_action"], project_value),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT id, count FROM lessons"
                    " WHERE source_task = ? AND affected_component = ? AND evidence = ?"
                    " AND lesson_type = ? AND confidence = ? AND recommended_action = ?"
                    " AND scope = 'global' AND status = 'approved'"
                    " ORDER BY id LIMIT 1",
                    (source_task, component, evidence_json, lesson_type, confidence,
                     lesson["recommended_action"]),
                ).fetchone()
            if existing is not None:
                new_count = existing["count"] + 1
                conn.execute("UPDATE lessons SET count = ? WHERE id = ?",
                             (new_count, existing["id"]))
                conn.execute("COMMIT")
                return {"status": "duplicate", "lesson_id": existing["id"], "count": new_count}
            created_at = _now()
            cur = conn.execute(
                "INSERT INTO lessons (lesson_type, source_task, evidence, confidence,"
                " scope, project, affected_component, recommended_action,"
                " mechanically_verified, status, reason, count, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', NULL, 1, ?)",
                (lesson_type, source_task, evidence_json, confidence, scope,
                 project_value, component, lesson["recommended_action"], 1, created_at),
            )
            conn.execute("COMMIT")
            return {"status": "approved", "lesson_id": cur.lastrowid, "created_at": created_at}
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _reject(database, lesson, checks, reason):
    """Persist a rejected candidate keyed like a lesson, bumping its count.

    Coerces every NOT NULL field to a plain string so an invalid or partially
    missing lesson never trips a constraint and never binds an arbitrary object
    to SQLite. Rejected rows dedup against rejected rows only, so the same
    source verified later becomes a separate approved lesson.
    """
    lesson_dict = lesson if isinstance(lesson, dict) else {}
    source_task = _as_text(lesson_dict.get("source_task"))
    component = _as_text(lesson_dict.get("affected_component"))
    evidence = lesson_dict.get("evidence")
    if not isinstance(evidence, (list, tuple)):
        evidence = []
    evidence_json = _canon_evidence(str(i) for i in evidence if isinstance(i, (str, int)))

    scope = lesson_dict.get("scope")
    if scope not in SCOPES:
        scope = "project"
    project = lesson_dict.get("project")
    if scope == "project":
        project_value = project if isinstance(project, str) else ""
    else:
        project_value = None

    lesson_type = _as_text(lesson_dict.get("lesson_type"))
    confidence = lesson_dict.get("confidence")
    if confidence not in CONFIDENCES:
        confidence = "low"
    recommended = _as_text(lesson_dict.get("recommended_action"))

    conn = _connect(database)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if scope == "project":
                existing = conn.execute(
                    "SELECT id, count FROM lessons WHERE source_task = ? AND affected_component = ?"
                    " AND evidence = ? AND lesson_type = ? AND confidence = ? AND recommended_action = ?"
                    " AND reason = ? AND scope = 'project' AND project = ? AND status = 'rejected'"
                    " ORDER BY id LIMIT 1",
                    (source_task, component, evidence_json, lesson_type, confidence, recommended,
                     reason, project_value),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT id, count FROM lessons WHERE source_task = ? AND affected_component = ?"
                    " AND evidence = ? AND lesson_type = ? AND confidence = ? AND recommended_action = ?"
                    " AND reason = ? AND scope = 'global' AND status = 'rejected'"
                    " ORDER BY id LIMIT 1",
                    (source_task, component, evidence_json, lesson_type, confidence, recommended, reason),
                ).fetchone()
            if existing is not None:
                new_count = existing["count"] + 1
                conn.execute("UPDATE lessons SET count = ? WHERE id = ?",
                             (new_count, existing["id"]))
                conn.execute("COMMIT")
                return {"status": "rejected", "reason": reason, "count": new_count}
            conn.execute(
                "INSERT INTO lessons (lesson_type, source_task, evidence, confidence,"
                " scope, project, affected_component, recommended_action,"
                " mechanically_verified, status, reason, count, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'rejected', ?, 1, ?)",
                (lesson_type, source_task, evidence_json, confidence, scope, project_value,
                 component, recommended, reason, _now()),
            )
            conn.execute("COMMIT")
            return {"status": "rejected", "reason": reason, "count": 1}
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# record_outcome
# --------------------------------------------------------------------------

COST_CLASSES = {"CHEAP", "MID", "PREMIUM"}
VERIFICATION_STATUSES = {"verified", "pending", "not_scored"}


def record_outcome(database, event):
    """Store one measured outcome. success is derived, never trusted verbatim.

    ``worker_exit`` must be a real integer (not a bool); ``cost`` must be a
    finite nonnegative number; token counts must be numeric and nonnegative. A
    truthy worker ``error`` field forces success off even when the exit code
    and tests look green.
    """
    if not isinstance(event, dict):
        raise ValueError("event must be a dict")

    worker_exit = event.get("worker_exit")
    if worker_exit is not None and (isinstance(worker_exit, bool) or not isinstance(worker_exit, int)):
        raise ValueError("worker_exit must be an integer")
    retry = event.get("retry")
    if retry is None:
        retry = 0
    if isinstance(retry, bool) or not isinstance(retry, int) or retry < 0:
        raise ValueError("retry must be a nonnegative integer")

    cost = event.get("cost")
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ValueError("cost must be a number")
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("cost must be finite and nonnegative")

    input_tokens = event.get("input_tokens")
    if input_tokens is not None and (isinstance(input_tokens, bool) or not isinstance(input_tokens, (int, float))
                                     or input_tokens < 0):
        raise ValueError("input_tokens must be numeric and nonnegative")
    output_tokens = event.get("output_tokens")
    if output_tokens is not None and (isinstance(output_tokens, bool) or not isinstance(output_tokens, (int, float))
                                      or output_tokens < 0):
        raise ValueError("output_tokens must be numeric and nonnegative")

    # Validate cost_class if present
    cost_class = event.get("cost_class")
    if cost_class is not None:
        if cost_class not in COST_CLASSES:
            raise ValueError("cost_class must be one of CHEAP, MID, PREMIUM if present")
    verification_status = event.get("verification_status")
    if verification_status is not None and verification_status not in VERIFICATION_STATUSES:
        raise ValueError("verification_status must be verified, pending, not_scored, or null")

    test_pass = event.get("test_pass")
    error = event.get("error")
    success = (worker_exit == 0) and (test_pass is True) and (not error)

    conn = _connect(database)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO outcomes (runtime, provider, model, role, tier, risk,"
                " success, worker_exit, test_pass, error, cost_class, cost, input_tokens, output_tokens,"
                " task_id, project, retry, main_host, uncertainty, verification_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.get("runtime"), event.get("provider"), event.get("model"),
                 event.get("role"), event.get("tier"), event.get("risk"),
                 1 if success else 0, worker_exit, test_pass,
                 event.get("error"), cost_class,
                 cost, input_tokens, output_tokens,
                 event.get("task_id"), event.get("project"), retry, event.get("main_host"), event.get("uncertainty"), verification_status, _now()),
            )
            # The outcome carries the measured cost of exactly one reserved call: release
            # that hold only. Sibling holds of the same task stay until their own outcome.
            reservation_id = event.get("reservation_id")
            if reservation_id is not None:
                if isinstance(reservation_id, bool) or not isinstance(reservation_id, int):
                    raise ValueError("reservation_id must be an integer")
                conn.execute("UPDATE reservations SET settled_at = ? WHERE id = ? AND settled_at IS NULL",
                             (_now(), reservation_id))
            conn.execute("COMMIT")
            return {"outcome_id": cur.lastrowid, "success": bool(success)}
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _open_reservations(conn, now_text):
    """Unsettled, unexpired holds of every session, shaped like outcome records."""
    rows = conn.execute("SELECT id, task_id, estimate, premium, created_at FROM reservations"
                        " WHERE settled_at IS NULL ORDER BY id").fetchall()
    now = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    live = []
    for r in rows:
        created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        if (now - created).total_seconds() > RESERVATION_TTL_SECONDS:
            continue
        live.append({"reservation_id": r["id"], "task_id": r["task_id"], "cost": r["estimate"],
                     "premium": bool(r["premium"]), "cost_class": "PREMIUM" if r["premium"] else None,
                     "created_at": r["created_at"]})
    return live


def reserve_budget(database, policy, estimate, task_id, check, premium=False, now=None):
    """Admit `estimate` against the caps and hold it, atomically across sessions.

    `check` is the pure budget function (operations.budget_check): it sees the
    committed outcomes plus every open reservation of other sessions, inside one
    BEGIN IMMEDIATE transaction, so two concurrent sessions cannot both pass a cap
    that admits only one. Returns the check report plus `reservation_id`.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("reservation requires a task_id")
    conn = _connect(database)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            now_text = now if isinstance(now, str) else _now()
            records = _outcome_rows(conn.execute("SELECT * FROM outcomes ORDER BY id").fetchall())
            records += _open_reservations(conn, now_text)
            report = check(policy, records, estimate, premium=premium, task_id=task_id, now=now)
            if not isinstance(report, dict) or report.get("allowed") is not True:
                raise ValueError("budget admission was not explicitly allowed")
            cur = conn.execute("INSERT INTO reservations (task_id, estimate, premium, created_at) VALUES (?, ?, ?, ?)",
                               (task_id, None if estimate is None else float(estimate), 1 if premium else 0, now_text))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return dict(report, reservation_id=cur.lastrowid, open_reservations=len([r for r in records if "reservation_id" in r]))


def _outcome_rows(rows):
    return [{
        "id": r["id"], "runtime": r["runtime"], "provider": r["provider"],
        "model": r["model"], "role": r["role"], "tier": r["tier"], "risk": r["risk"],
        "success": bool(r["success"]), "worker_exit": r["worker_exit"],
        "test_pass": r["test_pass"], "error": r["error"], "cost_class": r["cost_class"],
        "cost": r["cost"], "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
        "task_id": r["task_id"], "project": r["project"], "retry": r["retry"],
        "main_host": r["main_host"], "uncertainty": r["uncertainty"], "verification_status": r["verification_status"],
        "created_at": r["created_at"],
    } for r in rows]


def list_outcomes(database, project=None):
    """Return every stored outcome as a dict, preserving timestamps and cost_class.

    Unlike ``history``, this is the raw ledger: one dict per row, with the
    committed ``created_at`` timestamp, the recorded ``cost_class`` and the full
    usage (cost / token) columns intact, ready for budget or aggregate
    integration. Filter by ``project`` when given.
    """
    conn = _connect(database)
    try:
        _ensure_schema(conn)
        if project is None:
            rows = conn.execute("SELECT * FROM outcomes ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM outcomes WHERE project = ? ORDER BY id",
                                (project,)).fetchall()
    finally:
        conn.close()
    return _outcome_rows(rows)


def routing_advice(database, project, runtime, role, tier, risk,
                   min_observations=5, minimum_success_rate=.6):
    """Return project-scoped, temporary model exclusions from verified outcomes.

    One latest final outcome per task ID is considered for each exact model/runtime
    tuple. Only verified rows are considered; pending/unscored outcomes never steer model.
    This is advice only: it writes no policy and never promotes a model.
    """
    for name, value in (("project", project), ("runtime", runtime), ("role", role),
                        ("tier", tier), ("risk", risk)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(name + " must be a nonempty string")
    if isinstance(min_observations, bool) or not isinstance(min_observations, int) or min_observations < 1:
        raise ValueError("min_observations must be a positive integer")
    if (isinstance(minimum_success_rate, bool) or not isinstance(minimum_success_rate, (int, float))
            or not math.isfinite(minimum_success_rate) or not 0 <= minimum_success_rate <= 1):
        raise ValueError("minimum_success_rate must be a finite number from zero to one")

    rows = [row for row in list_outcomes(database, project=project)
            if row["runtime"] == runtime and row["role"] == role
            and row["tier"] == tier and row["risk"] == risk
            and isinstance(row["task_id"], str) and row["task_id"]
            and row["verification_status"] == "verified"]
    latest = {}
    for row in rows:
        key = (row["provider"], row["model"], row["task_id"])
        latest[key] = row

    groups = {}
    for (provider, model, task_id), row in latest.items():
        if row["error"] == "pending mandatory review":
            continue
        group = groups.setdefault((provider, model), [])
        group.append(row)
    recommendations = []
    for (provider, model), records in sorted(groups.items(), key=lambda item: ((item[0][0] or ""), (item[0][1] or ""))):
        records.sort(key=lambda row: row["task_id"])
        observations = len(records)
        successes = sum(row["success"] is True for row in records)
        success_rate = successes / observations
        costs = [row["cost"] for row in records]
        known_cost = None if any(cost is None for cost in costs) else sum(costs)
        exclude = observations >= min_observations and success_rate < minimum_success_rate
        recommendations.append({
            "runtime": runtime, "provider": provider, "model": model, "role": role,
            "tier": tier, "risk": risk, "task_ids": [row["task_id"] for row in records],
            "observations": observations, "successes": successes, "success_rate": success_rate,
            "source_tasks": [row["task_id"] for row in records], "cost": known_cost,
            "recommendation": "exclude" if exclude else "keep",
            "reason": ("success rate below threshold" if exclude else "insufficient evidence or threshold met"),
        })
    return {
        "project": project, "runtime": runtime, "role": role, "tier": tier, "risk": risk,
        "min_observations": min_observations, "minimum_success_rate": minimum_success_rate,
        "recommendations": recommendations,
        "excluded_models": [row["model"] for row in recommendations if row["recommendation"] == "exclude"],
    }


# --------------------------------------------------------------------------
# history / report
# --------------------------------------------------------------------------

def history(database, project=None):
    """Aggregate measured observations by runtime/provider/model/role/tier/risk."""
    conn = _connect(database)
    try:
        _ensure_schema(conn)
        if project is None:
            rows = conn.execute("SELECT * FROM outcomes ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM outcomes WHERE project = ? ORDER BY id",
                                (project,)).fetchall()
    finally:
        conn.close()

    groups = {}
    for row in rows:
        key = (row["runtime"], row["provider"], row["model"], row["role"],
               row["tier"], row["risk"])
        group = groups.setdefault(key, {
            "runtime": row["runtime"], "provider": row["provider"],
            "model": row["model"], "role": row["role"],
            "tier": row["tier"], "risk": row["risk"],
            "observations": 0, "first_pass": 0, "retry": 0,
            "success": 0, "total_cost": 0.0, "known_cost": 0,
        })
        group["observations"] += 1
        if row["success"]:
            group["success"] += 1
            if row["retry"]:
                group["retry"] += 1
            else:
                group["first_pass"] += 1
        if row["cost"] is not None:
            group["total_cost"] += row["cost"]
            group["known_cost"] += 1

    result = []
    for key in sorted(groups):
        group = groups[key]
        successes = group["success"]
        observations = group["observations"]
        if successes > 0 and group["known_cost"] == observations:
            cost_per_success = group["total_cost"] / successes
        else:
            cost_per_success = None
        result.append({
            "runtime": group["runtime"], "provider": group["provider"],
            "model": group["model"], "role": group["role"],
            "tier": group["tier"], "risk": group["risk"],
            "observations": observations,
            "first_pass": group["first_pass"], "retry": group["retry"],
            "success": successes, "cost_per_success": cost_per_success,
            "historical_success_rate": (successes / observations) if observations else None,
        })
    return {"groups": result}


def _evidence_ids(evidence_json):
    try:
        data = json.loads(evidence_json)
    except (ValueError, TypeError):
        return evidence_json
    if isinstance(data, dict) and "ids" in data:
        return data["ids"]
    return data


def report(database, project=None, since=None):
    """Ledger counts, rejected candidates, applications and repeated patterns.

    If project is given, only return lessons and applications for that project.
    If since is given (ISO UTC timestamp), only return lessons created >= that time.
    Returns existing keys plus lessons_detail and applications_detail lists.
    """
    if project is not None and (not isinstance(project, str) or not project.strip()):
        raise ValueError("project must be a nonempty string")
    if since is not None:
        try:
            parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if parsed_since.tzinfo is None or parsed_since.utcoffset() is None:
                raise ValueError
            since = parsed_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, TypeError):
            raise ValueError("Invalid ISO UTC timestamp for 'since'")

    conn = _connect(database)
    try:
        _ensure_schema(conn)
        filters, params = [], []
        if project is not None:
            filters.append("scope = 'project' AND project = ?")
            params.append(project)
        if since is not None:
            filters.append("created_at >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(filters)) if filters else ""
        status_rows = conn.execute("SELECT status, COUNT(*) AS n FROM lessons" + where + " GROUP BY status", params).fetchall()
        repeated_where = (" WHERE count > 1" + (" AND " + " AND ".join(filters) if filters else ""))
        repeated = conn.execute("SELECT source_task, affected_component, evidence, count FROM lessons" + repeated_where, params).fetchall()
        lessons_rows = conn.execute("SELECT * FROM lessons" + where + " ORDER BY id", params).fetchall()
        app_filters, app_params = [], []
        if project is not None:
            app_filters.append("lesson_id IN (SELECT id FROM lessons WHERE scope = 'project' AND project = ?)")
            app_params.append(project)
        if since is not None:
            app_filters.append("created_at >= ?")
            app_params.append(since)
        app_where = (" WHERE " + " AND ".join(app_filters)) if app_filters else ""
        applications_rows = conn.execute("SELECT * FROM applications" + app_where + " ORDER BY id", app_params).fetchall()
    finally:
        conn.close()

    counts = {"approved": 0, "rejected": 0, "duplicate": 0}
    for row in status_rows:
        if row["status"] in counts:
            counts[row["status"]] = row["n"]
    total = sum(counts.values())

    repeated_patterns = [
        {"source_task": r["source_task"], "affected_component": r["affected_component"],
         "evidence": _evidence_ids(r["evidence"]), "count": r["count"]}
        for r in repeated
    ]

    lessons_detail = []
    for row in lessons_rows:
        evidence_parsed = _evidence_ids(row["evidence"])
        lessons_detail.append({
            "id": row["id"],
            "lesson_type": row["lesson_type"],
            "scope": row["scope"],
            "project": row["project"],
            "source_task": row["source_task"],
            "component": row["affected_component"],
            "recommended_action": row["recommended_action"],
            "confidence": row["confidence"],
            "status": row["status"],
            "reason": row["reason"],
            "count": row["count"],
            "mechanically_verified": bool(row["mechanically_verified"]),
            "evidence": evidence_parsed,
            "created_at": row["created_at"],
        })

    applications_detail = []
    for row in applications_rows:
        applications_detail.append({
            "id": row["id"],
            "lesson_id": row["lesson_id"],
            "action": row["action"],
            "evidence": row["evidence"],
            "approved": bool(row["approved"]),
            "rollback_identifier": row["rollback_identifier"],
            "created_at": row["created_at"],
        })

    return {
        "lessons": dict(counts, total=total),
        "status_counts": counts,
        "candidates": counts["rejected"],
        "applied": len(applications_detail),
        "repeated_patterns": repeated_patterns,
        "lessons_detail": lessons_detail,
        "applications_detail": applications_detail,
    }


# --------------------------------------------------------------------------
# apply_lesson
# --------------------------------------------------------------------------

def apply_lesson(database, lesson_id, action, evidence, approved=False):
    """Record the application of a lesson. Never executes anything.

    ``approved`` must be exactly ``True`` (a truthy string is not approval).
    Global lessons require explicit approval, or at least three independent
    source_task evidences for the same component and action (never for a
    ``low`` confidence lesson). The ``evidence`` string must carry the host's
    rollback reference as ``rollback:<id>``; that id is stored verbatim as the
    rollback identifier, not an invented UUID.
    """
    if action not in ACTIONS:
        return {"status": "refused", "reason": "invalid_action"}
    if not isinstance(evidence, str) or not evidence.strip():
        return {"status": "refused", "reason": "missing_evidence"}
    match = _ROLLBACK_RE.search(evidence)
    if match is None:
        return {"status": "refused", "reason": "missing_rollback"}
    rollback_id = match.group(1)

    conn = _connect(database)
    try:
        _ensure_schema(conn)
        lesson = conn.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
        if lesson is None or lesson["status"] != "approved":
            return {"status": "refused", "reason": "lesson_not_approved"}
        if lesson["scope"] == "global" and approved is not True:
            if lesson["confidence"] == "low":
                return {"status": "refused", "reason": "insufficient_confidence"}
            distinct_sources = conn.execute(
                "SELECT COUNT(DISTINCT source_task) FROM lessons"
                " WHERE scope = 'global' AND status = 'approved'"
                " AND affected_component = ? AND recommended_action = ?",
                (lesson["affected_component"], lesson["recommended_action"]),
            ).fetchone()[0]
            if distinct_sources < 3:
                return {"status": "refused", "reason": "insufficient_evidence"}
            evidence_rows = conn.execute(
                "SELECT evidence FROM lessons"
                " WHERE scope = 'global' AND status = 'approved'"
                " AND affected_component = ? AND recommended_action = ?",
                (lesson["affected_component"], lesson["recommended_action"]),
            ).fetchall()
            if len({_evidence_identity(row["evidence"]) for row in evidence_rows}) < 3:
                return {"status": "refused", "reason": "insufficient_evidence"}
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO applications (lesson_id, action, evidence, approved,"
                " rollback_identifier, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (lesson_id, action, evidence, 1 if approved is True else 0, rollback_id, _now()),
            )
            conn.execute("COMMIT")
            return {"status": "applied", "application_id": cur.lastrowid,
                    "rollback_identifier": rollback_id}
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report_parser = sub.add_parser("report", help="print the learning ledger report")
    report_parser.add_argument("--database", required=True, help="sqlite database path")
    history_parser = sub.add_parser("history", help="print aggregated outcome history")
    history_parser.add_argument("--database", required=True, help="sqlite database path")
    history_parser.add_argument("--project", default=None, help="filter by project")
    report_parser.add_argument("--project", default=None, help="filter by project")
    report_parser.add_argument("--since", default=None, help="filter lessons since ISO UTC timestamp")
    args = parser.parse_args(argv)
    if args.command == "report":
        payload = report(args.database, project=args.project, since=args.since)
    else:
        payload = history(args.database, project=args.project)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
