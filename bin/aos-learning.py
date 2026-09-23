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
import sys
from pathlib import Path
from datetime import datetime, timezone

LESSON_TYPES = ("execution", "domain", "routing", "verification")
CONFIDENCES = ("low", "medium", "high")
SCOPES = ("project", "global")
ACTIONS = ("test", "policy", "routing", "skill", "guardrail", "benchmark", "documentation")

# Outcome classification dimensions and learning thresholds. One source: the
# router reads the same config/adaptive.json, so a failure type or domain added
# there is valid here too. The built-in values are the fallback when the file is
# absent or unreadable (the module stays usable on its own).
ADAPTIVE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "adaptive.json"
_BUILTIN_DOMAINS = ("GENERAL", "ENGINEERING", "LEGAL_COMPLIANCE", "BUSINESS_OPERATIONS",
                    "RESEARCH", "DATA_ANALYTICS")
_BUILTIN_ATTRIBUTION = {
    "reasoning_failure": "model", "implementation_failure": "model", "tool_failure": "infra",
    "context_failure": "aos", "instruction_failure": "model", "hallucination": "model",
    "test_failure": "model", "timeout": "infra", "routing_failure": "aos",
    "capability_mismatch": "aos", "review_failure": "model", "security_violation": "model",
    "architecture_ambiguity": "aos",
}
_BUILTIN_LEARNING = {
    "half_life_days": 30, "prior_strength": 5, "min_observations": 20, "recent_window": 10,
    "drift_delta": 0.2, "promote_above": 0.85, "demote_below": 0.5, "min_confidence": 0.6,
    "recommend_min_evidence": 5, "recommend_failure_rate": 0.6,
    "retry_credit": 0.8, "escalation_credit": 0.7, "finding_credit": 0.95, "max_counted_findings": 5,
}


def _load_adaptive(path=ADAPTIVE_CONFIG):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_ADAPTIVE = _load_adaptive()
_domains = _ADAPTIVE.get("domains")
DOMAINS = tuple(_domains) if isinstance(_domains, dict) and _domains else _BUILTIN_DOMAINS
_failures = _ADAPTIVE.get("failure_types")
# A failure_type maps to who owns the shortfall. Only "model" failures count
# for/against a model's score; aos/infra failures still appear in failure_types.
FAILURE_ATTRIBUTION = ({name: block.get("attribution", "model") for name, block in _failures.items()
                        if isinstance(block, dict)}
                       if isinstance(_failures, dict) and _failures else dict(_BUILTIN_ATTRIBUTION))
FAILURE_TYPES = tuple(FAILURE_ATTRIBUTION)
_learning = _ADAPTIVE.get("learning") if isinstance(_ADAPTIVE.get("learning"), dict) else {}
LEARNING = {key: _learning.get(key, value) for key, value in _BUILTIN_LEARNING.items()}


def _attribution(failure_type):
    """Owner of a failure_type: 'model' unless explicitly aos/infra."""
    return FAILURE_ATTRIBUTION.get(failure_type, "model")


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
    " domain TEXT, task_type TEXT, capabilities TEXT, failure_type TEXT,"
    " escalated INTEGER, review_findings INTEGER, user_acceptance TEXT,"
    " duration_s REAL, exploration INTEGER,"
    " premium_planning_tokens INTEGER, premium_execution_tokens INTEGER,"
    " premium_review_tokens INTEGER, open_tokens INTEGER,"
    " compactions INTEGER, compaction_recovered INTEGER, routing_appropriate INTEGER,"
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


def _iso(moment):
    # Fixed microsecond precision: stored timestamps are compared as text, and
    # isoformat() drops ".000000", which would sort after a same-second value.
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now():
    return _iso(datetime.now(timezone.utc))


def _moment(now):
    """`now` as an aware UTC datetime: None, an ISO string or a datetime."""
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if now.tzinfo is None or now.utcoffset() is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


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
    # Backward-compatible nullable additions for continuous learning. Older
    # databases survive unchanged; each ALTER TABLE ADD COLUMN is a no-op when
    # the column already exists.
    for column in ("domain TEXT", "task_type TEXT", "capabilities TEXT", "failure_type TEXT",
                   "escalated INTEGER", "review_findings INTEGER", "user_acceptance TEXT",
                   "duration_s REAL", "exploration INTEGER",
                   "premium_planning_tokens INTEGER", "premium_execution_tokens INTEGER",
                   "premium_review_tokens INTEGER", "open_tokens INTEGER",
                   "compactions INTEGER", "compaction_recovered INTEGER", "routing_appropriate INTEGER",
                   "bundle TEXT"):
        try:
            conn.execute("ALTER TABLE outcomes ADD COLUMN %s" % column)
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


def _normalized_capabilities(capabilities):
    """Validate a capabilities dict and return it name-sorted.

    Every value must be a finite float in [0, 1]; bools are rejected as they are
    ints in Python but not a legitimate capability score.
    """
    normalized = {}
    for name, value in capabilities.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("capabilities must be a dict of string names to finite floats in [0,1]")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("capabilities must be a dict name -> finite float in [0,1]")
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("capabilities must be a dict name -> finite float in [0,1]")
        normalized[name] = float(value)
    return dict(sorted(normalized.items()))


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

    # --- continuous-learning fields (all nullable, validated when present) ---

    domain = event.get("domain")
    if domain is not None:
        if not isinstance(domain, str) or not domain.strip():
            raise ValueError("domain must be one or more of %s joined by '+'" % ("+".join(DOMAINS),))
        for part in domain.split("+"):
            if part not in DOMAINS:
                raise ValueError("domain must be one or more of %s joined by '+'" % ("+".join(DOMAINS),))

    task_type = event.get("task_type")
    if task_type is not None:
        if not isinstance(task_type, str) or len(task_type) > 40:
            raise ValueError("task_type must be a string of at most 40 characters")
    # The bundle id from aos-orchestrate.py route (requirements, implementation, ...):
    # it separates an independent deliverable of the same task from a retry.
    bundle = event.get("bundle")
    if bundle is not None and (not isinstance(bundle, str) or not bundle.strip() or len(bundle) > 40):
        raise ValueError("bundle must be a non-empty string of at most 40 characters")

    capabilities = event.get("capabilities")
    if capabilities is not None:
        if not isinstance(capabilities, dict):
            raise ValueError("capabilities must be a dict name -> finite float in [0,1]")
        _normalized_capabilities(capabilities)  # raises on non-finite / out-of-range values

    failure_type = event.get("failure_type")
    if failure_type is not None and failure_type not in FAILURE_TYPES:
        raise ValueError("failure_type must be one of FAILURE_TYPES")

    escalated = event.get("escalated")
    if escalated is not None and not isinstance(escalated, int) and not isinstance(escalated, bool):
        raise ValueError("escalated must be a boolean")

    review_findings = event.get("review_findings")
    if review_findings is not None:
        if isinstance(review_findings, bool) or not isinstance(review_findings, int) or review_findings < 0:
            raise ValueError("review_findings must be a nonnegative integer")

    user_acceptance = event.get("user_acceptance")
    if user_acceptance is not None and user_acceptance not in ("accepted", "rejected"):
        raise ValueError("user_acceptance must be accepted or rejected")

    duration_s = event.get("duration_s")
    if duration_s is not None:
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
            raise ValueError("duration_s must be a number")
        if not math.isfinite(duration_s) or duration_s < 0:
            raise ValueError("duration_s must be finite and nonnegative")

    exploration = event.get("exploration")
    if exploration is not None and not isinstance(exploration, int) and not isinstance(exploration, bool):
        raise ValueError("exploration must be a boolean")

    # Token-class and compaction counters: numeric, nonnegative integers.
    for field_name in ("premium_planning_tokens", "premium_execution_tokens",
                       "premium_review_tokens", "open_tokens", "compactions",
                       "compaction_recovered"):
        value = event.get(field_name)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("%s must be a nonnegative integer" % field_name)

    routing_appropriate = event.get("routing_appropriate")
    if routing_appropriate is not None and not isinstance(routing_appropriate, int) and not isinstance(routing_appropriate, bool):
        raise ValueError("routing_appropriate must be a boolean")

    test_pass = event.get("test_pass")
    error = event.get("error")
    success = (worker_exit == 0) and (test_pass is True) and (not error)
    # A recorded failure_type forces success off, like a truthy ``error``.
    if failure_type is not None:
        success = False

    capabilities_json = None if capabilities is None else json.dumps(
        _normalized_capabilities(capabilities), sort_keys=True)

    outcome_columns = (
        "runtime", "provider", "model", "role", "tier", "risk",
        "success", "worker_exit", "test_pass", "error", "cost_class", "cost",
        "input_tokens", "output_tokens", "task_id", "project", "retry",
        "main_host", "uncertainty", "verification_status", "domain", "task_type",
        "capabilities", "failure_type", "escalated", "review_findings",
        "user_acceptance", "duration_s", "exploration", "premium_planning_tokens",
        "premium_execution_tokens", "premium_review_tokens", "open_tokens",
        "compactions", "compaction_recovered", "routing_appropriate", "bundle", "created_at",
    )
    outcome_values = (
        event.get("runtime"), event.get("provider"), event.get("model"),
        event.get("role"), event.get("tier"), event.get("risk"),
        1 if success else 0, worker_exit, test_pass,
        event.get("error"), cost_class,
        cost, input_tokens, output_tokens,
        event.get("task_id"), event.get("project"), retry, event.get("main_host"),
        event.get("uncertainty"), verification_status,
        domain, task_type, capabilities_json, failure_type,
        None if escalated is None else (1 if escalated else 0), review_findings,
        user_acceptance, duration_s, None if exploration is None else (1 if exploration else 0),
        event.get("premium_planning_tokens"), event.get("premium_execution_tokens"),
        event.get("premium_review_tokens"), event.get("open_tokens"),
        event.get("compactions"), event.get("compaction_recovered"),
        None if routing_appropriate is None else (1 if routing_appropriate else 0),
        bundle, _now(),
    )
    assert len(outcome_columns) == len(outcome_values)

    conn = _connect(database)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ", ".join("?" for _ in outcome_columns)
            cur = conn.execute(
                "INSERT INTO outcomes (%s) VALUES (%s)"
                % (", ".join(outcome_columns), placeholders),
                outcome_values,
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
            # One clock for the admission check and the reservation's TTL.
            moment = _moment(now)
            now_text = _iso(moment)
            records = _outcome_rows(conn.execute("SELECT * FROM outcomes ORDER BY id").fetchall())
            records += _open_reservations(conn, now_text)
            report = check(policy, records, estimate, premium=premium, task_id=task_id, now=moment)
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
        "domain": r["domain"], "task_type": r["task_type"], "capabilities": r["capabilities"],
        "bundle": r["bundle"],
        "failure_type": r["failure_type"], "escalated": r["escalated"],
        "review_findings": r["review_findings"], "user_acceptance": r["user_acceptance"],
        "duration_s": r["duration_s"], "exploration": r["exploration"],
        "premium_planning_tokens": r["premium_planning_tokens"],
        "premium_execution_tokens": r["premium_execution_tokens"],
        "premium_review_tokens": r["premium_review_tokens"],
        "open_tokens": r["open_tokens"], "compactions": r["compactions"],
        "compaction_recovered": r["compaction_recovered"],
        "routing_appropriate": r["routing_appropriate"],
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
# performance matrix / model status
# --------------------------------------------------------------------------

def _capability_map(capabilities_json):
    """Decode the stored capabilities JSON string into a plain dict (or {})."""
    if not capabilities_json:
        return {}
    try:
        data = json.loads(capabilities_json)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, (int, float))}


def _age_weight(created_at, now, half_life_days):
    """Recency weight: 0.5 ** (age_days / half_life_days)."""
    created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    if created.tzinfo is None or created.utcoffset() is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = (now - created).total_seconds() / 86400.0
    return 0.5 ** (age_days / half_life_days)


def _scored_outcome(row, now, half_life_days):
    """Return (is_counted, success, weight) for one outcome row.

    Only rows with a model are considered. A `user_acceptance == 'rejected'`
    row counts as a failure. Rows whose failure_type attribution is 'aos' or
    'infra' are not counted for/against the score (but remain in failure_types).
    """
    if not row["model"]:
        return None
    # Same rule as routing_advice: a pending or unscored outcome never steers a
    # model, neither toward PROMOTE nor toward DEMOTE.
    if row["verification_status"] != "verified":
        return None
    weight = _age_weight(row["created_at"], now, half_life_days)
    attribution = _attribution(row["failure_type"]) if row["failure_type"] else "model"
    if attribution in ("aos", "infra"):
        return None
    success = row["success"] is True and row["user_acceptance"] != "rejected"
    # A success that needed retries, an escalation or review findings earns less
    # credit than a clean first pass (spec section 12 penalties; factors in config).
    credit = 1.0 if success else 0.0
    if success:
        credit *= LEARNING["retry_credit"] ** max(int(row["retry"] or 0), 0)
        if row["escalated"]:
            credit *= LEARNING["escalation_credit"]
        findings = min(max(int(row["review_findings"] or 0), 0), LEARNING["max_counted_findings"])
        credit *= LEARNING["finding_credit"] ** findings
    return (True, credit, weight)


def _weighted_summary(items):
    """items: list of (success:bool, weight:float, created_at:str).

    Returns the recursive-decay summary: weighted score, unweighted sample
    size, effective n (sum of weights) and the latest timestamp.
    """
    if not items:
        return {"score": 0.0, "sample_size": 0, "confidence": 0.0,
                "n_eff": 0.0, "last_updated": None}
    weight_sum = sum(w for _, w, _ in items)
    success_sum = sum(w * float(s) for s, w, _ in items)
    score = success_sum / weight_sum if weight_sum else 0.0
    last = max(ts for _, _, ts in items)
    return {"score": score, "sample_size": len(items), "n_eff": weight_sum,
            "last_updated": last}


def matrix(database, half_life_days=None, now=None, prior_strength=None, min_observations=None,
           recent_window=None, drift_delta=None, promote_above=None, demote_below=None,
           min_confidence=None):
    """Recency-weighted performance matrix over recorded outcomes.

    Only counted (model-attribution) outcomes move a model's score; ''aos'' and
    ''infra'' failures appear in ``failure_types`` but never against the model.
    History is never deleted: decay only down-weights old outcomes.
    ``now`` may be a datetime or an ISO string for deterministic tests.
    """
    # Unset thresholds come from config/adaptive.json "learning".
    half_life_days = LEARNING["half_life_days"] if half_life_days is None else half_life_days
    prior_strength = LEARNING["prior_strength"] if prior_strength is None else prior_strength
    min_observations = LEARNING["min_observations"] if min_observations is None else min_observations
    recent_window = LEARNING["recent_window"] if recent_window is None else recent_window
    drift_delta = LEARNING["drift_delta"] if drift_delta is None else drift_delta
    promote_above = LEARNING["promote_above"] if promote_above is None else promote_above
    demote_below = LEARNING["demote_below"] if demote_below is None else demote_below
    min_confidence = LEARNING["min_confidence"] if min_confidence is None else min_confidence
    if isinstance(half_life_days, bool) or not isinstance(half_life_days, (int, float)) or half_life_days <= 0:
        raise ValueError("half_life_days must be a positive number")
    moment = _moment(now)

    rows = list_outcomes(database)
    # Group counted outcomes by model.
    by_model = {}
    failure_types = {}
    for row in rows:
        if not row["model"]:
            continue
        model = row["model"]
        failure_types.setdefault(model, {})
        if row["failure_type"]:
            ft = failure_types[model]
            ft[row["failure_type"]] = ft.get(row["failure_type"], 0) + 1
        scored = _scored_outcome(row, moment, half_life_days)
        if scored is None:
            continue
        counted, success, weight = scored
        by_model.setdefault(model, []).append(
            {"id": row["id"], "success": success, "weight": weight,
             "created_at": row["created_at"], "domain": row["domain"],
             "task_type": row["task_type"], "capabilities": _capability_map(row["capabilities"])})

    models = {}
    for model, outcomes in by_model.items():
        outcomes.sort(key=lambda o: o["created_at"])
        overall_items = [(o["success"], o["weight"], o["created_at"]) for o in outcomes]
        overall = _weighted_summary(overall_items)
        confidence = round(overall["n_eff"] / (overall["n_eff"] + prior_strength), 4)
        score = round(overall["score"], 4)
        sample_size = overall["sample_size"]

        # Capabilities: outcome contributes to k with weight w * need_k (needs >= 0.3).
        cap_items = {}
        for o in outcomes:
            for cap, need in o["capabilities"].items():
                if not isinstance(need, (int, float)) or need < 0.3:
                    continue
                cap_items.setdefault(cap, []).append(
                    (o["success"], o["weight"] * need, o["created_at"]))
        capabilities = {}
        for cap, items in cap_items.items():
            s = _weighted_summary(items)
            capabilities[cap] = {
                "score": round(s["score"], 4), "sample_size": s["sample_size"],
                "confidence": round(s["n_eff"] / (s["n_eff"] + prior_strength), 4),
                "last_updated": s["last_updated"],
            }

        # Domains: each "+"-joined domain gets the full outcome weight.
        dom_items = {}
        for o in outcomes:
            if not o["domain"]:
                continue
            for domain in o["domain"].split("+"):
                domain = domain.strip()
                if domain:
                    dom_items.setdefault(domain, []).append(
                        (o["success"], o["weight"], o["created_at"]))
        domains = {}
        for domain, items in dom_items.items():
            s = _weighted_summary(items)
            domains[domain] = {
                "score": round(s["score"], 4), "sample_size": s["sample_size"],
                "confidence": round(s["n_eff"] / (s["n_eff"] + prior_strength), 4),
                "last_updated": s["last_updated"],
            }

        # Task types: each outcome with a task_type gets the full outcome weight.
        type_items = {}
        for o in outcomes:
            if o["task_type"]:
                type_items.setdefault(o["task_type"], []).append(
                    (o["success"], o["weight"], o["created_at"]))
        task_types = {}
        for task_type, items in type_items.items():
            s = _weighted_summary(items)
            task_types[task_type] = {
                "score": round(s["score"], 4), "sample_size": s["sample_size"],
                "confidence": round(s["n_eff"] / (s["n_eff"] + prior_strength), 4),
                "last_updated": s["last_updated"],
            }

        # Drift: recent streak vs historical.
        drift = False
        status = "KEEP"
        if sample_size < 3:
            status = "NEW"
        else:
            historical = outcomes[:-recent_window]
            recent = outcomes[-recent_window:]
            if len(historical) >= 10 and len(recent) >= 5:
                hist_rate = sum(o["success"] for o in historical) / len(historical)
                recent_rate = sum(o["success"] for o in recent) / len(recent)
                if recent_rate < hist_rate - drift_delta:
                    drift = True
                    status = "WATCH"
            if not drift:
                if sample_size >= min_observations and confidence >= min_confidence:
                    if score >= promote_above:
                        status = "PROMOTE"
                    elif score < demote_below:
                        status = "DEMOTE"
                    else:
                        status = "KEEP"
                else:
                    status = "KEEP"

        models[model] = {
            "status": status,
            "drift": drift,
            "overall": {"score": score, "sample_size": sample_size,
                        "confidence": confidence, "last_updated": overall["last_updated"]},
            "capabilities": capabilities,
            "domains": domains,
            "task_types": task_types,
            "failure_types": failure_types.get(model, {}),
        }

    return {
        "generated_at": _iso(moment),
        "half_life_days": half_life_days,
        "models": models,
    }


def model_status(database):
    """Compact per-model status summary derived from the performance matrix."""
    result = matrix(database)
    status = {}
    for model, entry in result["models"].items():
        status[model] = {
            "status": entry["status"],
            "drift": entry["drift"],
            "sample_size": entry["overall"]["sample_size"],
            "score": entry["overall"]["score"],
        }
    return status


# --------------------------------------------------------------------------
# kpi
# --------------------------------------------------------------------------

def kpi(database, project=None, since=None):
    """Group outcomes by task_id and derive operational KPIs.

    NULL task_id becomes its own task. Denomators of zero yield ``None`` (never a
    fabricated zero) across every ratio.
    """
    rows = list_outcomes(database)
    if project is not None:
        rows = [r for r in rows if r["project"] == project]
    if since is not None:
        since_moment = _moment(since)
        rows = [r for r in rows if _moment(r["created_at"]) >= since_moment]

    # Tasks: per task, the earliest row is the first attempt.
    tasks = {}
    for r in rows:
        # The same task id in two projects is two tasks.
        key = (r["project"], r["task_id"]) if r["task_id"] is not None else ("__null__", r["id"])
        task = tasks.setdefault(key, [])
        task.append(r)

    attempts = len(rows)
    n_tasks = len(tasks)

    first_pass_success_count = 0
    verified_success_count = 0
    retry_tasks = 0
    escalation_tasks = 0
    findings_tasks = 0
    verified_cost_total = 0.0
    verified_cost_reported = 0
    premium_planning = 0
    premium_execution = 0
    premium_review = 0
    open_tokens_total = 0
    premium_tokens_total = 0
    measured_tokens = 0
    measured_token_rows = 0
    worker_total = 0
    worker_success = 0
    compaction_tasks = 0
    compaction_total = 0
    compaction_recovered_total = 0
    routing_appropriate_sum = 0
    routing_appropriate_count = 0
    accepted = 0
    rejected = 0

    for task_rows in tasks.values():
        sorted_rows = sorted(task_rows, key=lambda r: (r["created_at"], r["id"]))
        # Work rows produce the deliverable; planner and reviewer rows are roles
        # around it, never an attempt and never the task's outcome on their own. A
        # premium_executor row is a bundle's escalated final attempt, still work.
        work = [r for r in sorted_rows if r["role"] in (None, "executor", "fixer", "premium_executor")]
        # Attempts are grouped by bundle: a bundle's later row after a failure is a
        # retry, another bundle's row is an independent deliverable. Rows without a
        # bundle id belong to one implicit bundle (the ledger cannot tell them apart).
        bundles = {}
        for r in work:
            bundles.setdefault(r["bundle"], []).append(r)
        is_retry = any(r["retry"] for r in sorted_rows) or any(
            not earlier["success"] for rows_ in bundles.values() for earlier, _ in zip(rows_, rows_[1:]))
        if bundles and all(rows_[0]["success"] and not rows_[0]["retry"] for rows_ in bundles.values()):
            first_pass_success_count += 1
        if bundles:
            def order(row):
                return (row["created_at"], row["id"])

            def review_passed(review):
                # A pending or unscored review proves nothing.
                return review["success"] and review["verification_status"] == "verified"

            reviews = [r for r in sorted_rows if r["role"] == "reviewer"]
            last_work = order(work[-1])
            ok = True
            for bundle_id, rows_ in bundles.items():
                final = rows_[-1]
                # A review of this bundle after its final attempt still stands; a
                # review without a bundle id covers the whole task, after all work.
                standing = [r for r in reviews
                            if (r["bundle"] == bundle_id and bundle_id is not None and order(r) > order(final))
                            or (r["bundle"] is None and order(r) > last_work)]
                if not (final["success"] and final["verification_status"] == "verified"
                        and all(review_passed(r) for r in standing)):
                    ok = False
            if ok:
                verified_success_count += 1
        if is_retry:
            retry_tasks += 1
        if any(r["escalated"] for r in sorted_rows):
            escalation_tasks += 1
        if any(r["review_findings"] for r in sorted_rows):
            findings_tasks += 1
        if any(r["compactions"] for r in sorted_rows):
            compaction_tasks += 1
        for r in sorted_rows:
            if r["compactions"]:
                compaction_total += r["compactions"]
            if r["compaction_recovered"]:
                compaction_recovered_total += r["compaction_recovered"]

    for r in rows:
        # Failed attempts and retries cost money too: every reported cost counts.
        if r["cost"] is not None:
            verified_cost_total += r["cost"]
            verified_cost_reported += 1
        if r["premium_planning_tokens"]:
            premium_planning += r["premium_planning_tokens"]
        if r["premium_execution_tokens"]:
            premium_execution += r["premium_execution_tokens"]
        if r["premium_review_tokens"]:
            premium_review += r["premium_review_tokens"]
        if r["open_tokens"]:
            open_tokens_total += r["open_tokens"]
        # Per row: measured input/output tokens (every class reports them), else the
        # row's role counters; never both, so nothing is counted twice.
        if r["input_tokens"] is not None or r["output_tokens"] is not None:
            measured_tokens += (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
            measured_token_rows += 1
        else:
            role_counters = [r[name] for name in ("premium_planning_tokens", "premium_execution_tokens",
                                                  "premium_review_tokens", "open_tokens") if r[name] is not None]
            if role_counters:
                measured_tokens += sum(role_counters)
                measured_token_rows += 1
        if r["role"] == "executor" and r["cost_class"] == "CHEAP":
            worker_total += 1
            if r["success"]:
                worker_success += 1
        if r["routing_appropriate"] is not None:
            routing_appropriate_sum += (1 if r["routing_appropriate"] else 0)
            routing_appropriate_count += 1
        if r["user_acceptance"] == "accepted":
            accepted += 1
        elif r["user_acceptance"] == "rejected":
            rejected += 1

    # Premium dependency: premium tokens over premium + open tokens.
    premium_tokens_total = premium_planning + premium_execution + premium_review
    denom_premium = premium_tokens_total + open_tokens_total

    return {
        "first_pass_success_rate": (first_pass_success_count / n_tasks) if n_tasks else None,
        "verified_success_rate": (verified_success_count / n_tasks) if n_tasks else None,
        "retry_rate": (retry_tasks / n_tasks) if n_tasks else None,
        "escalation_rate": (escalation_tasks / n_tasks) if n_tasks else None,
        "review_findings_rate": (findings_tasks / n_tasks) if n_tasks else None,
        "cost_per_verified_task": (verified_cost_total / verified_success_count) if verified_success_count and verified_cost_reported else None,
        # Measured input/output tokens first; role counters only when none were measured;
        # nothing measured at all is unknown, not zero.
        "tokens_per_verified_task": (measured_tokens / verified_success_count)
                                    if verified_success_count and measured_token_rows else None,
        "premium_dependency_ratio": (premium_tokens_total / denom_premium) if denom_premium else None,
        "premium_leverage": {
            "planning": (premium_planning / verified_success_count) if verified_success_count else None,
            "execution": (premium_execution / verified_success_count) if verified_success_count else None,
            "review": (premium_review / verified_success_count) if verified_success_count else None,
        },
        "worker_success_rate": (worker_success / worker_total) if worker_total else None,
        "compaction_rate": (compaction_tasks / n_tasks) if n_tasks else None,
        "compaction_recovery_rate": (compaction_recovered_total / compaction_total) if compaction_total else None,
        "routing_accuracy": (routing_appropriate_sum / routing_appropriate_count) if routing_appropriate_count else None,
        "user_acceptance": (accepted / (accepted + rejected)) if (accepted + rejected) else None,
        "tasks": n_tasks,
        "attempts": attempts,
    }


# --------------------------------------------------------------------------
# recommend (lesson stages)
# --------------------------------------------------------------------------

def recommend(database, min_evidence=None, failure_rate_threshold=None):
    """Derive lesson stages from counted outcomes; never applies anything.

    Group model-attribution outcomes by (model, domain) and (model, task_type);
    each group with at least one failure yields an observation/hypothesis/
    routing_recommendation stage. ``applied`` is always False.
    """
    min_evidence = LEARNING["recommend_min_evidence"] if min_evidence is None else min_evidence
    if failure_rate_threshold is None:
        failure_rate_threshold = LEARNING["recommend_failure_rate"]
    if isinstance(min_evidence, bool) or not isinstance(min_evidence, int) or min_evidence < 1:
        raise ValueError("min_evidence must be a positive integer")
    if (isinstance(failure_rate_threshold, bool) or not isinstance(failure_rate_threshold, (int, float))
            or not math.isfinite(failure_rate_threshold) or not 0 <= failure_rate_threshold <= 1):
        raise ValueError("failure_rate_threshold must be a finite number in [0,1]")

    groups = {}
    for row in list_outcomes(database):
        if not row["model"] or row["verification_status"] != "verified":
            continue
        if row["failure_type"] and _attribution(row["failure_type"]) in ("aos", "infra"):
            continue
        success = row["success"] is True and row["user_acceptance"] != "rejected"
        if row["domain"]:
            for domain in row["domain"].split("+"):
                domain = domain.strip()
                if domain:
                    key = (row["model"], "domain", domain)
                    groups.setdefault(key, {"model": row["model"], "group": {"domain": domain},
                                            "attempts": 0, "failures": 0, "evidence": []})
                    g = groups[key]
                    g["attempts"] += 1
                    if not success:
                        g["failures"] += 1
                    g["evidence"].append(row["id"])
        if row["task_type"]:
            key = (row["model"], "task_type", row["task_type"])
            groups.setdefault(key, {"model": row["model"], "group": {"task_type": row["task_type"]},
                                    "attempts": 0, "failures": 0, "evidence": []})
            g = groups[key]
            g["attempts"] += 1
            if not success:
                g["failures"] += 1
            g["evidence"].append(row["id"])

    items = []
    for entry in groups.values():
        if entry["failures"] < 1:
            continue
        attempts = entry["attempts"]
        failures = entry["failures"]
        failure_rate = failures / attempts
        stage = None
        recommendation = None
        if attempts == 1:
            stage = "observation"
        elif attempts < min_evidence:
            if failure_rate >= failure_rate_threshold:
                stage = "hypothesis"
        else:
            if failure_rate >= failure_rate_threshold:
                stage = "routing_recommendation"
                value = next(iter(entry["group"].values()))
                recommendation = "deprioritize %s for %s" % (entry["model"], value)
        if stage is None:
            # Under threshold with >= 2 attempts -> omitted.
            continue
        item = {
            "model": entry["model"],
            "group": entry["group"],
            "stage": stage,
            "attempts": attempts,
            "failures": failures,
            "failure_rate": failure_rate,
            "evidence": entry["evidence"],
        }
        if recommendation is not None:
            item["recommendation"] = recommendation
        items.append(item)

    return {
        "items": items,
        "applied": False,
        "note": "recommendations only; apply_lesson with rollback is the only path to a change",
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
            since = _iso(parsed_since)
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

def _write_atomic(path, text):
    """Write ``text`` to ``path`` atomically via a temp file + rename."""
    import os
    import tempfile
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".aos-learning-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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

    record_outcome_parser = sub.add_parser("record-outcome", help="store one measured outcome")
    record_outcome_parser.add_argument("--database", required=True, help="sqlite database path")
    record_outcome_parser.add_argument("--event", required=True, help="path to a JSON event file, or '-' for stdin")

    matrix_parser = sub.add_parser("matrix", help="print the model performance matrix")
    matrix_parser.add_argument("--database", required=True, help="sqlite database path")
    matrix_parser.add_argument("--half-life-days", type=float, default=None,
                               help="recency half-life in days (default: config/adaptive.json learning)")
    matrix_parser.add_argument("--now", default=None, help="ISO timestamp to weight recency from")
    matrix_parser.add_argument("--output", default=None, help="optionally write JSON atomically to this file")

    model_status_parser = sub.add_parser("model-status", help="print per-model status summary")
    model_status_parser.add_argument("--database", required=True, help="sqlite database path")

    kpi_parser = sub.add_parser("kpi", help="print operational KPIs from outcomes")
    kpi_parser.add_argument("--database", required=True, help="sqlite database path")
    kpi_parser.add_argument("--project", default=None, help="filter by project")
    kpi_parser.add_argument("--since", default=None, help="ISO UTC timestamp lower bound")

    recommend_parser = sub.add_parser("recommend", help="derive lesson stages (never applies)")
    recommend_parser.add_argument("--database", required=True, help="sqlite database path")
    recommend_parser.add_argument("--min-evidence", type=int, default=None,
                                  help="attempts required for a routing recommendation (default: config)")

    args = parser.parse_args(argv)
    if args.command == "report":
        payload = report(args.database, project=args.project, since=args.since)
    elif args.command == "history":
        payload = history(args.database, project=args.project)
    elif args.command == "record-outcome":
        if args.event == "-":
            raw = sys.stdin.read()
        else:
            with open(args.event, "r") as fh:
                raw = fh.read()
        payload = record_outcome(args.database, json.loads(raw))
    elif args.command == "matrix":
        payload = matrix(args.database, half_life_days=args.half_life_days, now=args.now)
        if args.output:
            _write_atomic(args.output, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    elif args.command == "model-status":
        payload = model_status(args.database)
    elif args.command == "kpi":
        payload = kpi(args.database, project=args.project, since=args.since)
    elif args.command == "recommend":
        payload = recommend(args.database, min_evidence=args.min_evidence)
    else:
        payload = history(args.database, project=args.project)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
