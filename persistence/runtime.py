"""Opens the repository selected by HUB_DB_BACKEND. Connection settings never appear in raised errors."""
import sqlite3
from pathlib import Path

from . import config, migrator
from .errors import StorageError
from .repository import PostgresRepository, SQLiteRepository

_verified_urls = set()


def open_repository(sqlite_path, env=None):
    if config.backend(env) == "sqlite":
        try:
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(sqlite_path, timeout=15)
            conn.execute("PRAGMA foreign_keys=ON")
        except (sqlite3.Error, OSError):
            raise StorageError() from None
        return SQLiteRepository(conn)
    import psycopg
    url = config.postgres_url(env=env)
    conn = None
    try:
        conn = psycopg.connect(url, connect_timeout=5)
        if url not in _verified_urls:
            migrator.check_ready(conn)
            conn.rollback()
            _verified_urls.add(url)
        return PostgresRepository(conn)
    except psycopg.Error:
        if conn is not None:
            conn.close()
        raise StorageError() from None
    except BaseException:
        if conn is not None:
            conn.close()
        raise
