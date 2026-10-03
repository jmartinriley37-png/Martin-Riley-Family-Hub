"""Opens the repository selected by HUB_DB_BACKEND. Connection settings never appear in raised errors.

SQLite opens a connection per request. PostgreSQL uses one small bounded pool per process; the schema version is
verified (read-only) before the pool is created, and every checkout is health-checked so stale connections are replaced.
"""
import atexit
import logging
import sqlite3
import threading
from pathlib import Path

from . import config, migrator
from .errors import SchemaNotReady, StorageError
from .repository import PostgresRepository, SQLiteRepository

# Driver and pool log records include host, user and database names; keep them out of process logs.
for _name in ("psycopg", "psycopg.pool"):
    logging.getLogger(_name).addHandler(logging.NullHandler())
    logging.getLogger(_name).propagate = False

_pools = {}
_lock = threading.Lock()


def _create_pool(url, env):
    import psycopg
    from psycopg_pool import ConnectionPool
    kwargs = config.connect_kwargs(url, env)
    settings = config.pool_settings(env)
    try:
        # A direct connection first: bad credentials, TLS failures and an unmigrated schema fail immediately and specifically.
        with psycopg.connect(url, **kwargs) as conn:
            migrator.check_ready(conn)
        pool = ConnectionPool(url, kwargs=kwargs, min_size=settings["min_size"], max_size=settings["max_size"],
                              timeout=settings["timeout"], check=ConnectionPool.check_connection,
                              reconnect_timeout=30, open=False, name="family-hub")
        pool.open()
        return pool
    except psycopg.Error:
        raise StorageError() from None


def _pool_for(url, env):
    with _lock:
        pool = _pools.get(url)
        if pool is None:
            pool = _pools[url] = _create_pool(url, env)
        return pool


def close_pools():
    with _lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        try:
            pool.close(timeout=5)
        except Exception:
            pass


atexit.register(close_pools)


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
    pool = _pool_for(config.postgres_url(env=env), env)
    try:
        conn = pool.getconn()
    except psycopg.Error:
        raise StorageError() from None
    return PostgresRepository(conn, release=pool.putconn)


def check_readiness(sqlite_path, env=None):
    """(ready, details) with no identifying information: only whether the database answers and has its schema."""
    details = {"database": "unavailable", "schema": "unknown"}
    try:
        with open_repository(sqlite_path, env) as repo:
            details["database"] = "ok"
            repo.check_schema()
            details["schema"] = "ok"
    except SchemaNotReady:
        details["database"], details["schema"] = "ok", "not_ready"
    except (StorageError, config.ConfigError):
        pass
    return details["schema"] == "ok", details
