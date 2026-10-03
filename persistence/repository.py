"""Persistence boundary: one repository interface with SQLite and PostgreSQL implementations.

Authorization is NOT handled here. Repositories store and return data; the server decides who may see or change it.
All SQL lives in this module. SQL is written once with ``?`` placeholders; each dialect supplies only its own
connection handling, JSON/boolean conversion and a few fragments.
"""
import json
import sqlite3

from .errors import SchemaNotReady, StorageConflict, StorageError, StorageInvalid

SOURCE_FIELDS = {"sourceDanceId", "sourceActivityId", "sourceRequestId", "seriesId"}
WRITE_LOCK_KEY = 7_419_052_002  # serialises read-modify-write requests on PostgreSQL (SQLite uses BEGIN IMMEDIATE)

SQLITE_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, name TEXT REFERENCES users(name), expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT, record_id INTEGER REFERENCES records(id), action TEXT, created TEXT, snapshot TEXT NOT NULL DEFAULT '{}', details TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS attempts(address TEXT PRIMARY KEY, count INTEGER, reset INTEGER);
CREATE TABLE IF NOT EXISTS recurrence_series(series_id INTEGER PRIMARY KEY REFERENCES records(id), active INTEGER NOT NULL DEFAULT 1, rule TEXT NOT NULL, start_date TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT '', anchor_sequence INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS recurrence_occurrences(series_id INTEGER NOT NULL REFERENCES recurrence_series(series_id), occurrence_date TEXT NOT NULL, task_id INTEGER NOT NULL UNIQUE REFERENCES records(id), sequence INTEGER NOT NULL, PRIMARY KEY(series_id, occurrence_date));
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL REFERENCES users(name), source_kind TEXT NOT NULL, source_id INTEGER NOT NULL, reminder_key TEXT NOT NULL, reminder_type TEXT NOT NULL, due_at TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT, dismissed_at TEXT, snoozed_until TEXT, UNIQUE(account, source_kind, source_id, reminder_key));
CREATE TABLE IF NOT EXISTS recognition_receipts(recognition_id INTEGER NOT NULL REFERENCES records(id), recipient TEXT NOT NULL REFERENCES users(name), seen_at TEXT NOT NULL, PRIMARY KEY(recognition_id, recipient));
CREATE TABLE IF NOT EXISTS notification_settings(name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS family_members(member_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, member_type TEXT NOT NULL, account_name TEXT UNIQUE REFERENCES users(name), managed_by TEXT NOT NULL DEFAULT '[]', avatar TEXT NOT NULL DEFAULT '');
"""
# Columns added by earlier builds; older SQLite files are upgraded in place.
SQLITE_LEGACY_COLUMNS = (
    ("users", "display_name", "TEXT NOT NULL DEFAULT ''"),
    ("records", "deleted", "INTEGER NOT NULL DEFAULT 0"),
    ("audit", "snapshot", "TEXT NOT NULL DEFAULT '{}'"),
    ("audit", "details", "TEXT NOT NULL DEFAULT '{}'"),
    ("recurrence_series", "created_by", "TEXT NOT NULL DEFAULT ''"),
    ("recurrence_series", "anchor_sequence", "INTEGER NOT NULL DEFAULT 0"),
)


class Repository:
    """Shared logic. Subclasses supply connection handling, JSON/bool conversion and dialect fragments."""

    source_field_sql = ""
    text_order = ""  # collation used when ordering/comparing ISO timestamp text
    driver_errors = ()

    def __init__(self, conn, release=None):
        self.conn = conn
        self._release = release  # returns a pooled connection instead of closing it
        self._closed = False

    # dialect hooks
    def _run(self, sql, params=()):
        raise NotImplementedError

    def _translate(self, error):
        raise NotImplementedError

    def _json(self, value):
        raise NotImplementedError

    def _unjson(self, value):
        raise NotImplementedError

    def _bool(self, value):
        return bool(value)

    def begin_write(self):
        """Start a write transaction that serialises with other writers."""
        raise NotImplementedError

    def _rows(self, sql, params=()):
        try:
            return self._run(sql, params)
        except self.driver_errors as error:
            raise self._translate(error) from None

    def commit(self):
        try:
            self.conn.commit()
        except self.driver_errors as error:
            raise self._translate(error) from None

    def rollback(self):
        try:
            self.conn.rollback()
        except self.driver_errors:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._release:
                self.rollback()  # no-op after commit; discards anything left open by a caller that never committed
                self._release(self.conn)
            else:
                self.conn.close()
        except self.driver_errors:
            pass

    def check_schema(self):
        """Raise SchemaNotReady unless the database has the schema this code needs."""
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            self.rollback() if exc_type else self.commit()
        finally:
            self.close()

    # users and sessions
    def add_user(self, name, salt, password_hash, display_name=""):
        self._rows("INSERT INTO users(name,salt,hash,display_name) VALUES(?,?,?,?)", (name, salt, password_hash, display_name))

    def save_user_credentials(self, name, salt, password_hash, display_name):
        self._rows("INSERT INTO users(name,salt,hash,display_name) VALUES(?,?,?,?) "
                   "ON CONFLICT(name) DO UPDATE SET salt=excluded.salt, hash=excluded.hash", (name, salt, password_hash, display_name))

    def get_user(self, name):
        rows = self._rows("SELECT name,display_name FROM users WHERE name=?", (name,))
        return rows[0] if rows else None

    def get_user_credentials(self, name):
        rows = self._rows("SELECT name,salt,hash FROM users WHERE name=?", (name,))
        return rows[0] if rows else None

    def list_users(self):
        return self._rows("SELECT name,display_name FROM users ORDER BY name")

    def backfill_display_name(self, name, display_name):
        self._rows("UPDATE users SET display_name=? WHERE name=? AND display_name=''", (display_name, name))

    def create_session(self, token_hash, name, expires):
        self._rows("INSERT INTO sessions(token,name,expires) VALUES(?,?,?)", (token_hash, name, expires))

    def session_user(self, token_hash, now):
        rows = self._rows("SELECT name FROM sessions WHERE token=? AND expires>?", (token_hash, now))
        return rows[0]["name"] if rows else None

    def delete_session(self, token_hash):
        self._rows("DELETE FROM sessions WHERE token=?", (token_hash,))

    def delete_expired_sessions(self, now):
        self._rows("DELETE FROM sessions WHERE expires<?", (now,))

    def delete_sessions_for(self, name):
        self._rows("DELETE FROM sessions WHERE name=?", (name,))

    def get_attempt(self, address):
        rows = self._rows("SELECT address,count,reset FROM attempts WHERE address=?", (address,))
        return rows[0] if rows else None

    def save_attempt(self, address, count, reset):
        self._rows("INSERT INTO attempts(address,count,reset) VALUES(?,?,?) "
                   "ON CONFLICT(address) DO UPDATE SET count=excluded.count, reset=excluded.reset", (address, count, reset))

    def clear_attempt(self, address):
        self._rows("DELETE FROM attempts WHERE address=?", (address,))

    # family members
    def upsert_family_member(self, member_id, display_name, member_type, account_name=None, managed_by=(), avatar=""):
        self._rows(
            "INSERT INTO family_members(member_id,display_name,member_type,account_name,managed_by,avatar) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(member_id) DO UPDATE SET display_name=excluded.display_name, member_type=excluded.member_type, "
            "account_name=excluded.account_name, managed_by=excluded.managed_by, avatar=excluded.avatar",
            (member_id, display_name, member_type, account_name, self._json(list(managed_by)), avatar))

    def ensure_family_member(self, member_id, display_name, member_type, account_name=None, managed_by=(), avatar=""):
        self._rows(
            "INSERT INTO family_members(member_id,display_name,member_type,account_name,managed_by,avatar) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(member_id) DO NOTHING",
            (member_id, display_name, member_type, account_name, self._json(list(managed_by)), avatar))

    def set_account_member_name(self, member_id, account_name, display_name):
        self._rows("UPDATE family_members SET display_name=? WHERE member_id=? AND account_name=?", (display_name, member_id, account_name))

    def get_family_member(self, member_id):
        rows = self._rows("SELECT member_id,display_name,member_type FROM family_members WHERE member_id=?", (member_id,))
        return rows[0] if rows else None

    def list_family_members(self):
        rows = self._rows("SELECT member_id,display_name,member_type,account_name,managed_by,avatar FROM family_members ORDER BY member_id")
        return [{**row, "managed_by": self._unjson(row["managed_by"])} for row in rows]

    # records
    def _record(self, row):
        return {"id": row["id"], "kind": row["kind"], "body": self._unjson(row["body"]), "deleted": bool(row["deleted"])}

    def create_record(self, kind, body):
        return self._rows("INSERT INTO records(kind,body,deleted) VALUES(?,?,?) RETURNING id", (kind, self._json(body), self._bool(False)))[0]["id"]

    def get_record(self, record_id, include_deleted=False):
        rows = self._rows("SELECT id,kind,body,deleted FROM records WHERE id=?", (record_id,))
        if not rows or rows[0]["deleted"] and not include_deleted:
            return None
        return self._record(rows[0])

    def list_records(self, kind=None, include_deleted=False, kinds=None):
        clauses, params = [], []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
            params.extend(kinds)
        if not include_deleted:
            clauses.append("deleted=?")
            params.append(self._bool(False))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return [self._record(row) for row in self._rows(f"SELECT id,kind,body,deleted FROM records{where} ORDER BY id", params)]

    def update_record_body(self, record_id, body):
        self._rows("UPDATE records SET body=? WHERE id=?", (self._json(body), record_id))

    def soft_delete_record(self, record_id, body=None):
        if body is None:
            self._rows("UPDATE records SET deleted=? WHERE id=?", (self._bool(True), record_id))
        else:
            self._rows("UPDATE records SET body=?, deleted=? WHERE id=?", (self._json(body), self._bool(True), record_id))

    def find_by_source(self, field, value, kind="events", include_deleted=False):
        if field not in SOURCE_FIELDS:
            raise ValueError("Unsupported source field")
        deleted = "" if include_deleted else " AND deleted=?"
        params = [kind, self.source_value(value)] + ([] if include_deleted else [self._bool(False)])
        rows = self._rows(f"SELECT id,kind,body,deleted FROM records WHERE kind=? AND {self.source_field_sql.format(field=field)}=?{deleted} ORDER BY id", params)
        return [self._record(row) for row in rows]

    def source_value(self, value):
        return value

    # audit
    def add_audit(self, actor, record_id, action, created, snapshot=None, details=None):
        return self._rows("INSERT INTO audit(actor,record_id,action,created,snapshot,details) VALUES(?,?,?,?,?,?) RETURNING id",
                          (actor, record_id, action, created, self._json(snapshot or {}), self._json(details or {})))[0]["id"]

    def _audit(self, row):
        return {**row, "snapshot": self._unjson(row["snapshot"]), "details": self._unjson(row["details"])}

    def list_audit(self, record_id=None, limit=50):
        where, params = ("WHERE record_id=? ", [record_id]) if record_id is not None else ("", [])
        rows = self._rows(f"SELECT id,actor,record_id,action,created,snapshot,details FROM audit {where}ORDER BY id DESC LIMIT ?", params + [limit])
        return [self._audit(row) for row in rows]

    def list_audit_feed(self, limit):
        """Newest audit rows with the kind/body of their record (any state) in one query."""
        rows = self._rows("SELECT a.id,a.actor,a.record_id,a.action,a.created,a.snapshot,a.details,r.kind AS record_kind,r.body AS record_body "
                          "FROM audit a LEFT JOIN records r ON r.id=a.record_id ORDER BY a.id DESC LIMIT ?", (limit,))
        return [{**self._audit(row), "record_body": self._unjson(row["record_body"]) if row["record_body"] is not None else None} for row in rows]

    def max_audit_id(self):
        return self._rows("SELECT COALESCE(MAX(id),0) AS value FROM audit")[0]["value"]

    def list_audit_since(self, after_id, up_to_id, created_since):
        """Audit rows in (after_id, up_to_id] created at/after created_since whose record still exists."""
        rows = self._rows("SELECT a.id,a.actor,a.record_id,a.action,a.created,a.snapshot,r.kind AS record_kind,r.body AS record_body "
                          "FROM audit a JOIN records r ON r.id=a.record_id "
                          f"WHERE a.id>? AND a.id<=? AND a.created{self.text_order}>=? AND r.deleted=? ORDER BY a.id",
                          (after_id, up_to_id, created_since, self._bool(False)))
        return [{**row, "snapshot": self._unjson(row["snapshot"]), "record_body": self._unjson(row["record_body"])} for row in rows]

    # reminders
    def upsert_reminder(self, account, source_kind, source_id, reminder_key, reminder_type, due_at, created_at):
        rows = self._rows("INSERT INTO reminders(account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at) VALUES(?,?,?,?,?,?,?) "
                          "ON CONFLICT(account,source_kind,source_id,reminder_key) DO NOTHING RETURNING id",
                          (account, source_kind, source_id, reminder_key, reminder_type, due_at, created_at))
        return bool(rows)

    REMINDER_COLUMNS = "id,account,source_kind,source_id,reminder_key,reminder_type,due_at,created_at,read_at,dismissed_at,snoozed_until"

    def list_reminders(self, account):
        return self._rows(f"SELECT {self.REMINDER_COLUMNS} FROM reminders WHERE account=? ORDER BY id", (account,))

    def recent_reminders(self, account, limit=200):
        return self._rows(f"SELECT {self.REMINDER_COLUMNS} FROM reminders WHERE account=? ORDER BY due_at{self.text_order} DESC, id LIMIT ?", (account, limit))

    def reminder_keys(self):
        """Identity of every stored reminder, so callers can skip inserts that would be no-ops."""
        return {(row["account"], row["source_kind"], row["source_id"], row["reminder_key"])
                for row in self._rows("SELECT account,source_kind,source_id,reminder_key FROM reminders")}

    def get_reminder(self, reminder_id, account):
        rows = self._rows(f"SELECT {self.REMINDER_COLUMNS} FROM reminders WHERE id=? AND account=?", (reminder_id, account))
        return rows[0] if rows else None

    def mark_reminder_read(self, reminder_id, account, at):
        self._rows("UPDATE reminders SET read_at=COALESCE(read_at,?) WHERE id=? AND account=?", (at, reminder_id, account))

    def mark_reminders_read(self, account, reminder_types, at):
        marks = ",".join("?" for _ in reminder_types)
        self._rows(f"UPDATE reminders SET read_at=COALESCE(read_at,?) WHERE account=? AND dismissed_at IS NULL AND read_at IS NULL AND reminder_type IN ({marks})",
                   (at, account, *reminder_types))

    def dismiss_reminder(self, reminder_id, account, at):
        self._rows("UPDATE reminders SET read_at=COALESCE(read_at,?), dismissed_at=COALESCE(dismissed_at,?) WHERE id=? AND account=?", (at, at, reminder_id, account))

    def snooze_reminder(self, reminder_id, account, until, at):
        self._rows("UPDATE reminders SET read_at=COALESCE(read_at,?), snoozed_until=? WHERE id=? AND account=?", (at, until, reminder_id, account))

    def dismiss_reminders_for_source(self, source_kind, source_id, at):
        self._rows("UPDATE reminders SET dismissed_at=COALESCE(dismissed_at,?) WHERE source_kind=? AND source_id=?", (at, source_kind, source_id))

    # recognition receipts and settings
    def mark_recognition_seen(self, recognition_id, recipient, seen_at):
        self._rows("INSERT INTO recognition_receipts(recognition_id,recipient,seen_at) VALUES(?,?,?) ON CONFLICT DO NOTHING", (recognition_id, recipient, seen_at))

    def recognition_seen_at(self, recognition_id, recipient):
        rows = self._rows("SELECT seen_at FROM recognition_receipts WHERE recognition_id=? AND recipient=?", (recognition_id, recipient))
        return rows[0]["seen_at"] if rows else None

    def seen_recognitions(self, recipient):
        return {row["recognition_id"]: row["seen_at"] for row in self._rows("SELECT recognition_id,seen_at FROM recognition_receipts WHERE recipient=?", (recipient,))}

    def set_setting(self, name, value):
        self._rows("INSERT INTO notification_settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))

    def set_setting_if_absent(self, name, value):
        self._rows("INSERT INTO notification_settings(name,value) VALUES(?,?) ON CONFLICT(name) DO NOTHING", (name, value))

    def get_setting(self, name):
        rows = self._rows("SELECT value FROM notification_settings WHERE name=?", (name,))
        return rows[0]["value"] if rows else None

    # recurrence
    def create_series(self, series_id, rule, start_date, created_at, updated_at, created_by=""):
        self._rows("INSERT INTO recurrence_series(series_id,active,rule,start_date,created_at,updated_at,created_by) VALUES(?,?,?,?,?,?,?)",
                   (series_id, self._bool(True), self._json(rule), start_date, created_at, updated_at, created_by))

    def _series(self, row):
        return {**row, "rule": self._unjson(row["rule"]), "active": bool(row["active"])}

    SERIES_COLUMNS = "series_id,active,rule,start_date,created_by,anchor_sequence"

    def list_active_series(self):
        return [self._series(row) for row in self._rows(f"SELECT {self.SERIES_COLUMNS} FROM recurrence_series WHERE active=? ORDER BY series_id", (self._bool(True),))]

    def get_series(self, series_id, active_only=True):
        clause = " AND active=?" if active_only else ""
        rows = self._rows(f"SELECT {self.SERIES_COLUMNS} FROM recurrence_series WHERE series_id=?{clause}", (series_id, *([self._bool(True)] if active_only else [])))
        return self._series(rows[0]) if rows else None

    def update_series(self, series_id, rule, start_date, anchor_sequence, active, updated_at):
        self._rows("UPDATE recurrence_series SET rule=?,start_date=?,anchor_sequence=?,active=?,updated_at=? WHERE series_id=?",
                   (self._json(rule), start_date, anchor_sequence, self._bool(active), updated_at, series_id))

    def deactivate_series(self, series_id, updated_at):
        self._rows("UPDATE recurrence_series SET active=?,updated_at=? WHERE series_id=?", (self._bool(False), updated_at, series_id))

    def add_occurrence(self, series_id, occurrence_date, task_id, sequence):
        self._rows("INSERT INTO recurrence_occurrences(series_id,occurrence_date,task_id,sequence) VALUES(?,?,?,?)", (series_id, occurrence_date, task_id, sequence))

    def add_occurrence_if_absent(self, series_id, occurrence_date, task_id, sequence):
        self._rows("INSERT INTO recurrence_occurrences(series_id,occurrence_date,task_id,sequence) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                   (series_id, occurrence_date, task_id, sequence))

    def list_occurrences(self, series_id):
        return self._rows("SELECT series_id,occurrence_date,task_id,sequence FROM recurrence_occurrences WHERE series_id=? ORDER BY sequence", (series_id,))

    def occurrence_dates_by_series(self):
        result = {}
        for row in self._rows("SELECT series_id,occurrence_date FROM recurrence_occurrences"):
            result.setdefault(row["series_id"], set()).add(row["occurrence_date"])
        return result

    def get_occurrence(self, series_id, task_id):
        rows = self._rows("SELECT occurrence_date,sequence FROM recurrence_occurrences WHERE series_id=? AND task_id=?", (series_id, task_id))
        return rows[0] if rows else None

    def get_occurrence_by_date(self, series_id, occurrence_date):
        rows = self._rows("SELECT task_id,sequence FROM recurrence_occurrences WHERE series_id=? AND occurrence_date=?", (series_id, occurrence_date))
        return rows[0] if rows else None

    def list_series_members(self, series_id, from_sequence=None):
        """Occurrences with their record body/deleted flag, ordered by sequence."""
        clause, params = (" AND o.sequence>=?", [series_id, from_sequence]) if from_sequence is not None else ("", [series_id])
        rows = self._rows("SELECT o.task_id,o.occurrence_date,o.sequence,r.body,r.deleted FROM recurrence_occurrences o JOIN records r ON r.id=o.task_id "
                          f"WHERE o.series_id=?{clause} ORDER BY o.sequence", params)
        return [{**row, "body": self._unjson(row["body"]), "deleted": bool(row["deleted"])} for row in rows]

    def set_occurrence_date(self, task_id, occurrence_date):
        self._rows("UPDATE recurrence_occurrences SET occurrence_date=? WHERE task_id=?", (occurrence_date, task_id))

    def remove_occurrences(self, task_ids):
        task_ids = list(task_ids)
        if task_ids:
            self._rows("DELETE FROM recurrence_occurrences WHERE task_id IN (%s)" % ",".join("?" for _ in task_ids), task_ids)


class SQLiteRepository(Repository):
    source_field_sql = "json_extract(body,'$.{field}')"
    driver_errors = (sqlite3.Error,)

    def __init__(self, conn, release=None):
        super().__init__(conn, release)
        conn.row_factory = sqlite3.Row

    def _run(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, tuple(params)).fetchall()]

    def _translate(self, error):
        if isinstance(error, sqlite3.IntegrityError):
            return StorageConflict()
        if isinstance(error, sqlite3.DataError):
            return StorageInvalid()
        return StorageError()

    def _json(self, value):
        return json.dumps(value)

    def _unjson(self, value):
        return json.loads(value) if isinstance(value, str) else value

    def _bool(self, value):
        return int(value)

    def begin_write(self):
        if not self.conn.in_transaction:
            self._rows("BEGIN IMMEDIATE")

    def check_schema(self):
        if not self._rows("SELECT name FROM sqlite_master WHERE type='table' AND name='records'"):
            raise SchemaNotReady("The SQLite database has not been initialised.")

    def ensure_schema(self):
        """Create/upgrade the SQLite file. PostgreSQL schema is managed only by explicit migrations."""
        try:
            self.conn.executescript(SQLITE_SCHEMA)
            for table, column, definition in SQLITE_LEGACY_COLUMNS:
                if column not in {row["name"] for row in self._run(f"PRAGMA table_info({table})")}:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.Error:
            raise StorageError() from None


class PostgresRepository(Repository):
    source_field_sql = "body->>'{field}'"
    text_order = ' COLLATE "C"'  # byte-order comparison, identical to SQLite for ISO timestamps

    def __init__(self, conn, release=None):
        import psycopg
        from psycopg.rows import dict_row
        self.driver_errors = (psycopg.Error,)
        self._psycopg = psycopg
        conn.row_factory = dict_row
        super().__init__(conn, release)

    def check_schema(self):
        from . import migrator
        try:
            migrator.check_ready(self.conn)
        except self.driver_errors:
            raise StorageError() from None
        finally:
            self.rollback()

    def _run(self, sql, params=()):
        cursor = self.conn.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))
        return cursor.fetchall() if cursor.description else []

    def _translate(self, error):
        errors = self._psycopg.errors
        if isinstance(error, errors.IntegrityError):
            return StorageConflict()
        if isinstance(error, errors.DataError):
            return StorageInvalid()
        return StorageError()

    def _json(self, value):
        from psycopg.types.json import Jsonb
        return Jsonb(value)

    def _unjson(self, value):
        return json.loads(value) if isinstance(value, str) else value

    def begin_write(self):
        self._rows("SELECT pg_advisory_xact_lock(?)", (WRITE_LOCK_KEY,))

    def ensure_schema(self):
        pass  # PostgreSQL schema is created only by explicit, versioned migrations

    def source_value(self, value):
        return str(value)
