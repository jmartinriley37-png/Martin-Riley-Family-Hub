"""SQLite -> PostgreSQL importer with dry-run default, validation and source/destination comparison.

The source database is only ever read from a private temporary copy, so the original file is never opened for writing.
"""
import contextlib
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import migrator
from .config import DEFAULT_SQLITE_PATH

KINDS = {"tasks", "posts", "requests", "events", "lists", "dance", "notes", "recognitions", "activities"}
# Insertion order satisfies foreign keys. Sessions and login attempts are ephemeral and intentionally not imported.
TABLES = ("users", "family_members", "records", "audit", "recurrence_series", "recurrence_occurrences",
          "reminders", "recognition_receipts", "notification_settings")
PRIMARY_KEYS = {
    "users": ("name",), "family_members": ("member_id",), "records": ("id",), "audit": ("id",),
    "recurrence_series": ("series_id",), "recurrence_occurrences": ("series_id", "occurrence_date"),
    "reminders": ("id",), "recognition_receipts": ("recognition_id", "recipient"), "notification_settings": ("name",),
}
COLUMNS = {
    "users": ("name", "salt", "hash", "display_name"),
    "family_members": ("member_id", "display_name", "member_type", "account_name", "managed_by", "avatar"),
    "records": ("id", "kind", "body", "deleted"),
    "audit": ("id", "actor", "record_id", "action", "created", "snapshot", "details"),
    "recurrence_series": ("series_id", "active", "rule", "start_date", "created_at", "updated_at", "created_by", "anchor_sequence"),
    "recurrence_occurrences": ("series_id", "occurrence_date", "task_id", "sequence"),
    "reminders": ("id", "account", "source_kind", "source_id", "reminder_key", "reminder_type", "due_at", "created_at", "read_at", "dismissed_at", "snoozed_until"),
    "recognition_receipts": ("recognition_id", "recipient", "seen_at"),
    "notification_settings": ("name", "value"),
}
JSON_COLUMNS = {"records": ("body",), "audit": ("snapshot", "details"), "recurrence_series": ("rule",), "family_members": ("managed_by",)}
BOOL_COLUMNS = {"records": ("deleted",), "recurrence_series": ("active",)}
IDENTITY_TABLES = {"records": "id", "audit": "id", "reminders": "id"}
DANCE_REFERENCES = {
    "competitionIds": "competition", "competitionId": "competition", "routineIds": "routine",
    "costumeIds": "costume", "costumeId": "costume",
}


class ImportError_(Exception):
    pass


class Issue:
    def __init__(self, severity, code, message, table="", key=None):
        self.severity, self.code, self.message, self.table, self.key = severity, code, message, table, key

    def as_dict(self):
        return {"severity": self.severity, "code": self.code, "table": self.table, "key": self.key, "message": self.message}


# ---------- loading ----------

@contextlib.contextmanager
def snapshot_sqlite(path):
    """Yield a read-only connection to a private temp copy (database + WAL) of `path`."""
    source = Path(path)
    if not source.is_file():
        raise ImportError_("Source database file does not exist")
    with tempfile.TemporaryDirectory(prefix="hub-import-") as directory:
        copy = Path(directory) / "source.sqlite3"
        shutil.copyfile(source, copy)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.is_file():
                shutil.copyfile(sidecar, str(copy) + suffix)
        conn = sqlite3.connect(copy)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            conn.close()


def _parse_json(table, column, key, value, issues):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        issues.append(Issue("error", "malformed_json", f"{table}.{column} is not valid JSON", table, key))
        return {"_malformed": value}


def _normalize(table, row, issues):
    key = [row[column] for column in PRIMARY_KEYS[table]]
    key = key[0] if len(key) == 1 else key
    result = {}
    for column in COLUMNS[table]:
        value = row[column] if column in row.keys() else None
        if column in JSON_COLUMNS.get(table, ()):
            value = _parse_json(table, column, key, value, issues)
        elif column in BOOL_COLUMNS.get(table, ()):
            value = bool(value)
        result[column] = value
    return result


def _sort_key(table, row):
    return tuple(str(row[column]) if not isinstance(row[column], int) else f"{row[column]:020d}" for column in PRIMARY_KEYS[table])


def _finish(dataset):
    for table in TABLES:
        dataset[table].sort(key=lambda row, table=table: _sort_key(table, row))
    return dataset


def load_sqlite(conn):
    """Return (dataset, load_issues) from an SQLite connection that uses the Build C schema."""
    issues, dataset = [], {}
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in TABLES:
        if table not in existing:
            issues.append(Issue("error", "missing_table", f"Source is missing table {table}", table))
            dataset[table] = []
            continue
        available = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = [column for column in COLUMNS[table] if column not in available]
        if missing:
            issues.append(Issue("error", "missing_column", f"Source table {table} lacks columns {missing}", table))
        select = ", ".join(column if column in available else f"NULL AS {column}" for column in COLUMNS[table])
        dataset[table] = [_normalize(table, row, issues) for row in conn.execute(f"SELECT {select} FROM {table}")]
    return _finish(dataset), issues


def load_postgres(conn):
    from psycopg.rows import tuple_row
    dataset = {}
    with conn.cursor(row_factory=tuple_row) as cursor:
        for table in TABLES:
            cursor.execute(f"SELECT {', '.join(COLUMNS[table])} FROM {table}")
            dataset[table] = [dict(zip(COLUMNS[table], row)) for row in cursor.fetchall()]
    return _finish(dataset)


# ---------- validation ----------

def _id_list(value):
    return value if isinstance(value, list) else []


def _contains_nul(value):
    return "\\u0000" in json.dumps(value) if not isinstance(value, str) else "\x00" in value


def validate(dataset):
    issues = []
    users = {row["name"] for row in dataset["users"]}
    records = {row["id"]: row for row in dataset["records"]}
    series_ids = {row["series_id"] for row in dataset["recurrence_series"]}
    members = {row["member_id"]: row for row in dataset["family_members"]}

    for table in TABLES:
        for row in dataset[table]:
            if any(_contains_nul(value) for value in row.values()):
                issues.append(Issue("error", "unsupported_nul", "NUL characters cannot be stored in PostgreSQL", table, row[PRIMARY_KEYS[table][0]]))

    for row in dataset["records"]:
        if row["kind"] not in KINDS:
            issues.append(Issue("error", "unknown_kind", f"Unknown record kind {row['kind']!r}", "records", row["id"]))
        if not isinstance(row["body"], dict) or "_malformed" in row["body"]:
            issues.append(Issue("error", "body_not_object", "Record body must be a JSON object", "records", row["id"]))

    for row in dataset["audit"]:
        if row["record_id"] is not None and row["record_id"] not in records:
            issues.append(Issue("error", "orphan_audit_record", f"Audit entry points to missing record {row['record_id']}", "audit", row["id"]))

    for row in dataset["reminders"]:
        if row["account"] not in users:
            issues.append(Issue("error", "orphan_reminder_account", f"Reminder belongs to unknown account {row['account']!r}", "reminders", row["id"]))
        if row["source_kind"] in KINDS:
            source = records.get(row["source_id"])
            if source is None or source["kind"] != row["source_kind"]:
                issues.append(Issue("error", "orphan_reminder_source", f"Reminder source {row['source_kind']}:{row['source_id']} does not exist", "reminders", row["id"]))

    for row in dataset["recognition_receipts"]:
        key = [row["recognition_id"], row["recipient"]]
        if row["recognition_id"] not in records or records[row["recognition_id"]]["kind"] != "recognitions":
            issues.append(Issue("error", "orphan_receipt_recognition", "Receipt points to a missing recognition", "recognition_receipts", key))
        if row["recipient"] not in users:
            issues.append(Issue("error", "orphan_receipt_recipient", "Receipt recipient is not an account", "recognition_receipts", key))

    for row in dataset["recurrence_series"]:
        if row["series_id"] not in records:
            issues.append(Issue("error", "orphan_series", "Recurrence series has no record", "recurrence_series", row["series_id"]))
    owners = {row["task_id"]: row["series_id"] for row in dataset["recurrence_occurrences"]}
    for row in dataset["recurrence_series"]:
        if owners.get(row["series_id"], row["series_id"]) != row["series_id"]:
            issues.append(Issue("warning", "series_from_generated_occurrence",
                                "A generated occurrence was turned into its own series (older restart defect); review recurring items before relying on them",
                                "recurrence_series", row["series_id"]))
    for row in dataset["recurrence_occurrences"]:
        key = [row["series_id"], row["occurrence_date"]]
        if row["series_id"] not in series_ids:
            issues.append(Issue("error", "orphan_occurrence_series", "Occurrence has no series", "recurrence_occurrences", key))
        if row["task_id"] not in records:
            issues.append(Issue("error", "orphan_occurrence_record", "Occurrence points to a missing record", "recurrence_occurrences", key))

    account_names = set()
    for row in dataset["family_members"]:
        member_id = row["member_id"]
        if row["member_type"] == "account":
            account_names.add(row["account_name"])
            if row["account_name"] not in users:
                issues.append(Issue("error", "family_account_missing", "Account profile has no user", "family_members", member_id))
        elif row["member_type"] == "managed_child":
            if row["account_name"] is not None:
                issues.append(Issue("error", "managed_child_has_account", "Managed child must not be linked to a user", "family_members", member_id))
            if member_id in users:
                issues.append(Issue("error", "managed_child_has_user", "Managed child must not have a login user", "family_members", member_id))
        else:
            issues.append(Issue("error", "unknown_member_type", f"Unknown member type {row['member_type']!r}", "family_members", member_id))
        for manager in _id_list(row["managed_by"]):
            if manager not in members and manager not in users:  # accounts are valid managers even before their profile row exists
                issues.append(Issue("error", "orphan_managed_by", f"Managed-by {manager!r} is not a family member", "family_members", member_id))

    for rid, row in records.items():
        body = row["body"] if isinstance(row["body"], dict) else {}
        active = not row["deleted"]
        dance_source, activity_source = body.get("sourceDanceId"), body.get("sourceActivityId")
        if dance_source is not None and activity_source is not None:
            issues.append(Issue("error", "mirror_multiple_sources", "Calendar record has both a Dance and an activity source", "records", rid))
        for field, source_kind, source_value in (("sourceDanceId", "dance", dance_source), ("sourceActivityId", "activities", activity_source)):
            if source_value is None:
                continue
            source = records.get(source_value) if isinstance(source_value, int) and not isinstance(source_value, bool) else None
            if source is None:
                issues.append(Issue("error", "mirror_source_missing", f"{field} {source_value!r} does not match exactly one record", "records", rid))
            elif source["kind"] != source_kind:
                issues.append(Issue("error", "mirror_source_wrong_kind", f"{field} points to a {source['kind']} record", "records", rid))
            elif active and source["deleted"]:
                issues.append(Issue("warning", "mirror_source_deleted", "Active calendar record outlives its deleted source", "records", rid))
            elif active and source_kind == "dance" and bool(source["body"].get("archived")) != bool(body.get("archived")):
                issues.append(Issue("warning", "mirror_archive_mismatch", "Calendar record archive state differs from its Dance source", "records", rid))
        if row["kind"] == "dance" and active:
            for field, expected in DANCE_REFERENCES.items():
                value = body.get(field)
                for target_id in (value if isinstance(value, list) else [] if value is None else [value]):
                    target = records.get(target_id)
                    if target is None:
                        issues.append(Issue("error", "dance_ref_missing", f"{field} references missing record {target_id!r}", "records", rid))
                    elif target["kind"] != "dance" or target["body"].get("danceType") != expected:
                        issues.append(Issue("error", "dance_ref_wrong_type", f"{field} references a record that is not a {expected}", "records", rid))
                    elif target["deleted"]:
                        issues.append(Issue("warning", "dance_ref_deleted", f"{field} references deleted record {target_id}", "records", rid))
        series_id = body.get("seriesId")
        if series_id is not None and series_id not in series_ids:
            issues.append(Issue("warning", "record_series_missing", f"seriesId {series_id!r} has no recurrence series", "records", rid))
    return issues


# ---------- summary, checksums, comparison ----------

def _canonical(row):
    return json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)


def table_checksum(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_canonical(row).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def summarize(dataset):
    records = dataset["records"]
    by_kind = {}
    for row in records:
        entry = by_kind.setdefault(row["kind"], {"total": 0, "deleted": 0})
        entry["total"] += 1
        entry["deleted"] += row["deleted"]
    dance = [row for row in records if row["kind"] == "dance" and isinstance(row["body"], dict)]
    return {
        "counts": {table: len(dataset[table]) for table in TABLES},
        "records_by_kind": dict(sorted(by_kind.items())),
        "dance_archived": sum(1 for row in dance if row["body"].get("archived") and not row["deleted"]),
        "calendar_mirrors": sum(1 for row in records if isinstance(row["body"], dict) and (row["body"].get("sourceDanceId") is not None or row["body"].get("sourceActivityId") is not None)),
        "reminders_unread": sum(1 for row in dataset["reminders"] if not row["read_at"]),
        "reminders_dismissed": sum(1 for row in dataset["reminders"] if row["dismissed_at"]),
        "checksums": {table: table_checksum(dataset[table]) for table in TABLES},
    }


def _status_fields(dataset):
    fields = {}
    for row in dataset["records"]:
        body = row["body"] if isinstance(row["body"], dict) else {}
        fields[("records", row["id"])] = (row["kind"], row["deleted"], body.get("visibility"), bool(body.get("archived")),
                                          body.get("sourceDanceId"), body.get("sourceActivityId"))
    for row in dataset["reminders"]:
        fields[("reminders", row["id"])] = (row["read_at"], row["dismissed_at"], row["snoozed_until"])
    return fields


def compare(source, destination):
    """Return a list of mismatch strings; an empty list means the destination faithfully matches the source."""
    problems = []
    for table in TABLES:
        src_keys = {_canonical([row[c] for c in PRIMARY_KEYS[table]]) for row in source[table]}
        dst_keys = {_canonical([row[c] for c in PRIMARY_KEYS[table]]) for row in destination[table]}
        if len(source[table]) != len(destination[table]):
            problems.append(f"{table}: row count {len(source[table])} != {len(destination[table])}")
        if src_keys - dst_keys:
            problems.append(f"{table}: {len(src_keys - dst_keys)} source keys missing from destination")
        if dst_keys - src_keys:
            problems.append(f"{table}: {len(dst_keys - src_keys)} unexpected keys in destination")
        if table_checksum(source[table]) != table_checksum(destination[table]):
            problems.append(f"{table}: content checksum differs")
    source_status, destination_status = _status_fields(source), _status_fields(destination)
    problems += [f"status differs for {table}:{key}" for (table, key), value in source_status.items() if destination_status.get((table, key)) != value]
    errors = [issue for issue in validate(destination) if issue.severity == "error"]
    problems += [f"destination integrity: {issue.code} ({issue.table} {issue.key})" for issue in errors]
    return problems


# ---------- import ----------

def check_source_path(path, allow_live=False):
    resolved = Path(path).resolve()
    if resolved == DEFAULT_SQLITE_PATH.resolve() and not allow_live:
        raise ImportError_("Refusing to use the live data/hub.sqlite3 without --allow-live-source")
    return resolved


def dry_run(source_conn):
    dataset, load_issues = load_sqlite(source_conn)
    issues = load_issues + validate(dataset)
    return {
        "mode": "dry-run", "wrote_to_destination": False, "summary": summarize(dataset),
        "errors": [i.as_dict() for i in issues if i.severity == "error"],
        "warnings": [i.as_dict() for i in issues if i.severity == "warning"],
        "ready": not any(i.severity == "error" for i in issues),
    }


def destination_report(pg_conn):
    """Read-only destination checks: schema version and whether data tables are empty."""
    report = migrator.status(pg_conn)
    report["non_empty_tables"] = [t for t in TABLES if pg_conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()] if not report["pending"] else None
    return report


def execute_import(source_conn, pg_conn):
    """Import inside one transaction; any mismatch rolls the whole import back."""
    from psycopg.types.json import Jsonb
    dataset, load_issues = load_sqlite(source_conn)
    errors = [i for i in load_issues + validate(dataset) if i.severity == "error"]
    if errors:
        raise ImportError_(f"Source has {len(errors)} validation error(s); run a dry run to review them")
    state = migrator.status(pg_conn)
    if state["pending"] or state["unknown"]:
        raise ImportError_("Destination schema is not at the latest migration; run the migrate command first")
    occupied = [t for t in TABLES if pg_conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()]
    if occupied:
        raise ImportError_(f"Destination is not empty: {occupied}")
    with pg_conn.transaction():
        for table in TABLES:
            columns = COLUMNS[table]
            sql = f"INSERT INTO {table}({', '.join(columns)}) VALUES({', '.join(['%s'] * len(columns))})"
            for row in dataset[table]:
                pg_conn.execute(sql, [Jsonb(row[c]) if c in JSON_COLUMNS.get(table, ()) else row[c] for c in columns])
        for table, column in IDENTITY_TABLES.items():
            pg_conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}','{column}'), GREATEST((SELECT COALESCE(MAX({column}),0) FROM {table}),1), (SELECT COUNT(*) > 0 FROM {table}))")
        problems = compare(dataset, load_postgres(pg_conn))
        if problems:
            raise ImportError_("Post-import validation failed: " + "; ".join(problems[:10]))
    return {"mode": "execute", "wrote_to_destination": True, "summary": summarize(dataset), "validation": "passed"}
