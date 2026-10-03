"""Ordered, checksummed, versioned PostgreSQL migrations."""
import hashlib
import re
from pathlib import Path

from .errors import SchemaNotReady

MIGRATIONS_DIR = Path(__file__).parent / "migrations" / "postgres"
NAME_PATTERN = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
LOCK_KEY = 7_419_052_001


class MigrationError(Exception):
    pass


class Migration:
    def __init__(self, version, name, sql):
        self.version, self.name, self.sql = version, name, sql
        self.checksum = hashlib.sha256(sql.replace("\r\n", "\n").encode()).hexdigest()


def discover(directory=MIGRATIONS_DIR):
    found = []
    for path in sorted(Path(directory).glob("*.sql")):
        match = NAME_PATTERN.match(path.name)
        if not match:
            raise MigrationError(f"Migration file name must look like 0001_name.sql: {path.name}")
        found.append(Migration(int(match.group(1)), match.group(2), path.read_text(encoding="utf-8")))
    versions = [item.version for item in found]
    if len(set(versions)) != len(versions):
        raise MigrationError("Duplicate migration version numbers")
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError("Migration versions must be contiguous starting at 0001")
    return found


def _ensure_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations(
        version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")


def applied(conn, create=True):
    if create:
        _ensure_table(conn)
    elif not conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]:
        return {}
    return {row[0]: (row[1], row[2]) for row in conn.execute("SELECT version,name,checksum FROM schema_migrations ORDER BY version")}


def current_version(conn):
    done = applied(conn)
    return max(done) if done else 0


def status(conn, directory=MIGRATIONS_DIR):
    known, done = discover(directory), applied(conn, create=False)  # inspecting must never write
    return {
        "current": max(done) if done else 0,
        "latest": known[-1].version if known else 0,
        "pending": [item.version for item in known if item.version not in done],
        "unknown": sorted(set(done) - {item.version for item in known}),
    }


def environment_label(conn):
    """The environment this database is labelled for, or None. Read-only."""
    if not conn.execute("SELECT to_regclass('hub_environment') IS NOT NULL").fetchone()[0]:
        return None
    row = conn.execute("SELECT name FROM hub_environment").fetchone()
    return row[0] if row else None


def label_environment(conn, name):
    conn.execute("INSERT INTO hub_environment(name) VALUES(%s) ON CONFLICT DO NOTHING", (name,))
    return environment_label(conn)


def check_ready(conn, directory=MIGRATIONS_DIR):
    """Read-only startup check; never creates or alters anything. Raises SchemaNotReady with a non-secret message."""
    known = discover(directory)
    from psycopg.rows import tuple_row
    cursor = conn.cursor(row_factory=tuple_row)
    done = {}
    if cursor.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]:
        done = {row[0]: (row[1], row[2]) for row in cursor.execute("SELECT version,name,checksum FROM schema_migrations")}
    required = known[-1].version if known else 0
    current = max(done) if done else 0
    fix = "Run: python -m persistence migrate --destination-env HUB_DATABASE_URL"
    if {item.version for item in known} - set(done):
        raise SchemaNotReady(f"The PostgreSQL database is at schema version {current:04d} but {required:04d} is required. {fix}")
    if set(done) - {item.version for item in known}:
        raise SchemaNotReady("The PostgreSQL database schema is newer than this application. Deploy a matching application version.")
    for item in known:
        if done[item.version] != (item.name, item.checksum):
            raise SchemaNotReady(f"Applied migration {item.version:04d} does not match this application's migration file.")


def migrate(conn, directory=MIGRATIONS_DIR, target=None):
    """Apply pending migrations in order; each runs in its own transaction. Safe to re-run."""
    known = discover(directory)
    conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        done = applied(conn)
        by_version = {item.version: item for item in known}
        for version, (name, checksum) in done.items():
            item = by_version.get(version)
            if item is None:
                raise MigrationError(f"Database has migration {version:04d} that this code does not know about")
            if item.checksum != checksum or item.name != name:
                raise MigrationError(f"Applied migration {version:04d} was modified after it was applied")
        ran = []
        for item in known:
            if item.version in done or target is not None and item.version > target:
                continue
            with conn.transaction():
                conn.execute(item.sql)
                conn.execute("INSERT INTO schema_migrations(version,name,checksum) VALUES(%s,%s,%s)", (item.version, item.name, item.checksum))
            ran.append(item.version)
        return ran
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
