"""Hosted-PostgreSQL readiness: production configuration, TLS, connection pool, health endpoints and failure recovery.

All data is synthetic. PostgreSQL parts need HUB_TEST_POSTGRES_URL (disposable database; private schema per test).
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit, urlunsplit

import server
import test_server as ts
from test_backend_parity import Harness
from persistence import config, runtime
from persistence.errors import SchemaNotReady, StorageError

PG_URL = ts.PG_URL
ROOT = Path(__file__).parent
SECRET = "pw-must-never-appear-1234"


def with_credentials(url, user=None, password=None):
    parts = urlsplit(url)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, f"{user or parts.username}:{password or parts.password}@{host}", parts.path, parts.query, ""))


def get(path):
    """Issue a GET through the real handler without sockets. Returns (status, parsed body)."""
    h = object.__new__(server.Handler)
    h.path, h.headers, h.wfile = path, Message(), io.BytesIO()
    status = {}
    h.send_response = lambda code: status.update(code=code)
    h.send_header = lambda *_: None
    h.end_headers = lambda: None
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        h.do_GET()
    return status["code"], json.loads(h.wfile.getvalue()), stderr.getvalue()


class ConfigTests(unittest.TestCase):
    def test_production_requires_an_explicit_backend(self):
        with self.assertRaises(config.ConfigError):
            config.backend({"HUB_ENV": "production"})
        self.assertEqual(config.backend({"HUB_ENV": "production", "HUB_DB_BACKEND": "postgres"}), "postgres")

    def test_a_database_url_never_silently_falls_back_to_sqlite(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.backend({"HUB_DATABASE_URL": f"postgresql://u:{SECRET}@h/db"})
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(config.backend({}), "sqlite")

    def test_unknown_environment_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.environment({"HUB_ENV": "staging-ish"})

    def test_production_forces_certificate_verification(self):
        url = "postgresql://u:p@h/db"
        production = {"HUB_ENV": "production"}
        self.assertEqual(config.connect_kwargs(url, production)["sslmode"], "verify-full")
        for weak in ("disable", "allow", "prefer", "require"):
            with self.assertRaises(config.ConfigError, msg=weak):
                config.connect_kwargs(url, {**production, "HUB_DB_SSLMODE": weak})
            with self.assertRaises(config.ConfigError, msg=weak):
                config.connect_kwargs(url + f"?sslmode={weak}", production)
        self.assertEqual(config.connect_kwargs(url, {**production, "HUB_DB_SSLMODE": "verify-ca"})["sslmode"], "verify-ca")
        self.assertEqual(config.connect_kwargs(url + "?sslmode=verify-ca", production)["sslmode"], "verify-ca")

    def test_development_leaves_libpq_defaults_unless_asked(self):
        self.assertNotIn("sslmode", config.connect_kwargs("postgresql://u:p@h/db", {}))
        self.assertEqual(config.connect_kwargs("postgresql://u:p@h/db", {"HUB_DB_SSLMODE": "require"})["sslmode"], "require")
        with self.assertRaises(config.ConfigError):
            config.connect_kwargs("postgresql://u:p@h/db", {"HUB_DB_SSLMODE": "yes"})

    def test_root_certificate_must_exist(self):
        with self.assertRaises(config.ConfigError):
            config.connect_kwargs("postgresql://u:p@h/db", {"HUB_DB_SSLROOTCERT": "/nonexistent/ca.pem"})
        with tempfile.NamedTemporaryFile() as cert:
            self.assertEqual(config.connect_kwargs("postgresql://u:p@h/db", {"HUB_DB_SSLROOTCERT": cert.name})["sslrootcert"], cert.name)

    def test_pool_settings_have_family_scale_defaults_and_are_validated(self):
        self.assertEqual(config.pool_settings({}), {"min_size": 1, "max_size": 5, "timeout": 5.0, "connect_timeout": 5})
        for bad in ({"HUB_DB_POOL_MAX": "0"}, {"HUB_DB_POOL_MAX": "abc"}, {"HUB_DB_POOL_MAX": "500"},
                    {"HUB_DB_POOL_MIN": "9", "HUB_DB_POOL_MAX": "3"}, {"HUB_DB_POOL_TIMEOUT": "0"}):
            with self.assertRaises(config.ConfigError, msg=bad):
                config.pool_settings(bad)

    def test_redaction_drops_user_password_host_and_query(self):
        shown = config.redact(f"postgresql://someone:{SECRET}@db.internal.example:5432/hub?sslmode=verify-full&token=abc")
        for leaked in (SECRET, "someone", "db.internal.example", "5432", "token", "abc"):
            self.assertNotIn(leaked, shown)
        self.assertEqual(config.redact("postgresql://u:p@[bad/db"), "[unparseable url]")


class ProductionStartupFailsClosed(unittest.TestCase):
    """server.py must exit non-zero, name the problem, and never touch SQLite when production is misconfigured."""

    def run_server(self, **env):
        with tempfile.TemporaryDirectory() as directory:
            sqlite_path = Path(directory) / "must-not-be-created.sqlite3"
            clean = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
            clean.update({"HUB_DB": str(sqlite_path), "PORT": "18097", **env})
            result = subprocess.run([sys.executable, str(ROOT / "server.py")], env=clean, capture_output=True, text=True, timeout=60)
            return result, sqlite_path.exists()

    def assert_refused(self, **env):
        result, sqlite_created = self.run_server(**env)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(sqlite_created, "silently fell back to SQLite")
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        return result.stderr

    def test_missing_backend(self):
        self.assertIn("HUB_DB_BACKEND", self.assert_refused(HUB_ENV="production"))

    def test_missing_url(self):
        self.assertIn("HUB_DATABASE_URL", self.assert_refused(HUB_ENV="production", HUB_DB_BACKEND="postgres"))

    def test_malformed_url(self):
        self.assert_refused(HUB_ENV="production", HUB_DB_BACKEND="postgres", HUB_DATABASE_URL=f"postgresql://u:{SECRET}@[bad/db")

    def test_wrong_scheme(self):
        self.assert_refused(HUB_ENV="production", HUB_DB_BACKEND="postgres", HUB_DATABASE_URL=f"mysql://u:{SECRET}@h/db")

    def test_weak_tls_setting(self):
        self.assertIn("verify-full", self.assert_refused(
            HUB_ENV="production", HUB_DB_BACKEND="postgres", HUB_DATABASE_URL=f"postgresql://u:{SECRET}@127.0.0.1:1/db", HUB_DB_SSLMODE="disable"))

    def test_url_without_backend(self):
        self.assert_refused(HUB_DATABASE_URL=f"postgresql://u:{SECRET}@127.0.0.1:1/db")

    def test_unreachable_database(self):
        self.assert_refused(HUB_ENV="production", HUB_DB_BACKEND="postgres", HUB_DATABASE_URL=f"postgresql://u:{SECRET}@127.0.0.1:1/db")


class SQLiteHealthTests(unittest.TestCase):
    def test_liveness_and_readiness_on_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            cleanups, _ = ts.start_backend("sqlite", directory)
            try:
                self.assertEqual(get("/healthz")[:2], (200, {"status": "ok"}))
                self.assertEqual(get("/readyz")[0], 503)  # file exists but is not initialised yet
                server.initialize()
                self.assertEqual(get("/readyz")[:2], (200, {"status": "ready", "database": "ok", "schema": "ok"}))
            finally:
                for cleanup in reversed(cleanups):
                    cleanup()


@unittest.skipUnless(PG_URL, "Set HUB_TEST_POSTGRES_URL to run PostgreSQL hosted-readiness tests")
class PostgresHostedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def backend(self, migrate=True):
        cleanups, url = ts.start_backend("postgres", self.temp.name, migrate=migrate)
        for cleanup in reversed(cleanups):
            self.addCleanup(cleanup)
        return url

    def env(self, url, **extra):
        return {"HUB_DB_BACKEND": "postgres", "HUB_DATABASE_URL": url, **extra}

    def admin(self, url):
        import psycopg
        return psycopg.connect(url, autocommit=True)

    # --- health / readiness
    def test_readiness_reports_only_coarse_status(self):
        url = self.backend()
        server.initialize()
        status, body, _ = get("/readyz")
        self.assertEqual((status, body), (200, {"status": "ready", "database": "ok", "schema": "ok"}))
        self.assertEqual(get("/healthz")[:2], (200, {"status": "ok"}))
        text = json.dumps(body)
        for private in (url, "127.0.0.1", "postgres", "Dad", "records"):
            self.assertNotIn(private, text)

    def test_readiness_when_unmigrated_unreachable_or_ahead(self):
        url = self.backend(migrate=False)
        status, body, logged = get("/readyz")
        self.assertEqual((status, body), (503, {"status": "not_ready", "database": "ok", "schema": "not_ready"}))
        self.assertEqual(get("/healthz")[0], 200)  # liveness is independent of the database
        bad = {"HUB_DB_BACKEND": "postgres", "HUB_DATABASE_URL": f"postgresql://u:{SECRET}@127.0.0.1:1/db"}
        with mock.patch.dict(os.environ, bad):
            status, body, logged = get("/readyz")
        self.assertEqual((status, body), (503, {"status": "not_ready", "database": "unavailable", "schema": "unknown"}))
        self.assertNotIn(SECRET, json.dumps(body) + logged)
        from persistence import migrator
        with self.admin(url) as conn:
            migrator.migrate(conn)
            conn.execute("INSERT INTO schema_migrations(version,name,checksum) VALUES(99,'future','x')")
        runtime.close_pools()
        self.assertEqual(get("/readyz")[1], {"status": "not_ready", "database": "ok", "schema": "not_ready"})

    def test_readiness_notices_a_schema_that_changes_after_startup(self):
        url = self.backend()
        server.initialize()
        self.assertEqual(get("/readyz")[0], 200)
        with self.admin(url) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version=2")
        self.assertEqual(get("/readyz")[1]["schema"], "not_ready")

    # --- startup refusals
    def test_database_version_ahead_of_the_application_is_refused(self):
        url = self.backend()
        with self.admin(url) as conn:
            conn.execute("INSERT INTO schema_migrations(version,name,checksum) VALUES(99,'future','x')")
        with self.assertRaises(SchemaNotReady) as caught:
            server.initialize()
        self.assertIn("newer", str(caught.exception))

    def test_wrong_credentials_fail_generically(self):
        url = self.backend()
        env = self.env(with_credentials(url, password=SECRET))
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(StorageError) as caught:
                server.initialize()
            status, body, logged = get("/api/profiles")
        self.assertEqual(status, 503)
        for text in (str(caught.exception), json.dumps(body), logged):
            self.assertNotIn(SECRET, text)
            self.assertNotIn("authentication", text.lower())
            self.assertNotIn("postgres", text.lower())

    def test_tls_failure_in_production_is_generic_and_never_downgrades(self):
        # The throwaway local container has no TLS, so a verifying client must refuse to talk to it.
        url = self.backend()
        env = {"HUB_ENV": "production", **self.env(url)}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(StorageError) as caught:
                server.initialize()
            status, body, _ = get("/api/profiles")
        self.assertEqual(status, 503)
        self.assertEqual(str(caught.exception), StorageError.public_message)
        # The same database is reachable when development rules allow an unencrypted local connection.
        with mock.patch.dict(os.environ, self.env(url)):
            server.initialize()

    def test_weak_tls_configuration_is_rejected_before_connecting(self):
        url = self.backend()
        env = {"HUB_ENV": "production", **self.env(url, HUB_DB_SSLMODE="require")}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(config.ConfigError):
                server.initialize()

    # --- pool behaviour
    def test_pool_is_bounded_and_recovers_from_exhaustion(self):
        url = self.backend()
        env = self.env(url, HUB_DB_POOL_MAX="2", HUB_DB_POOL_MIN="1", HUB_DB_POOL_TIMEOUT="0.5")
        first, second = runtime.open_repository(None, env), runtime.open_repository(None, env)
        started = time.monotonic()
        with self.assertRaises(StorageError) as caught:
            runtime.open_repository(None, env)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(str(caught.exception), StorageError.public_message)
        first.close()
        third = runtime.open_repository(None, env)
        self.assertEqual(third._rows("SELECT 1 AS one"), [{"one": 1}])
        third.close()
        second.close()
        stats = runtime._pools[url].get_stats()
        self.assertLessEqual(stats["pool_size"], 2)
        self.assertEqual(stats["pool_available"], stats["pool_size"])

    def test_no_connection_leaks_across_success_error_and_threads(self):
        url = self.backend()
        env = self.env(url, HUB_DB_POOL_MAX="3")
        server_env = mock.patch.dict(os.environ, env)
        server_env.start()
        self.addCleanup(server_env.stop)
        server.initialize()
        for _ in range(30):
            with server.repository() as repo:
                repo.list_users()
        for _ in range(10):
            with self.assertRaises(RuntimeError):
                with server.repository() as repo:
                    repo.create_record("tasks", {"title": "x"})
                    raise RuntimeError("handler failure")
        errors = []

        def worker():
            try:
                for _ in range(10):
                    with server.repository() as repo:
                        repo.list_records()
            except Exception as error:  # pragma: no cover
                errors.append(repr(error))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        stats = runtime._pools[url].get_stats()
        self.assertEqual(stats["pool_available"], stats["pool_size"])
        self.assertLessEqual(stats["pool_size"], 3)
        with server.repository() as repo:
            self.assertEqual(repo.list_records(), [])  # the failed requests were rolled back

    def test_broken_pooled_connections_are_replaced(self):
        url = self.backend()
        env = self.env(url, HUB_DB_POOL_MAX="1", HUB_DB_POOL_MIN="1")
        with runtime.open_repository(None, env) as repo:
            pid = repo._rows("SELECT pg_backend_pid() AS pid")[0]["pid"]
        with self.admin(url) as conn:
            conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
        with runtime.open_repository(None, env) as repo:
            self.assertEqual(repo._rows("SELECT pg_backend_pid() AS pid")[0]["pid"] != pid, True)

    def test_an_aborted_transaction_does_not_poison_the_next_request(self):
        url = self.backend()
        env = self.env(url, HUB_DB_POOL_MAX="1")
        with self.assertRaises(StorageError):
            with runtime.open_repository(None, env) as repo:
                repo._rows("SELECT * FROM table_that_does_not_exist")
        with runtime.open_repository(None, env) as repo:
            self.assertEqual(repo._rows("SELECT 1 AS one"), [{"one": 1}])

    def test_database_outage_during_requests_recovers_without_restart(self):
        url = self.backend()
        env = self.env(url, HUB_DB_POOL_MAX="2")
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        server.initialize()
        self.assertEqual(get("/api/profiles")[0], 200)
        with self.admin(url) as conn:
            pids = [row[0] for row in conn.execute("SELECT pid FROM pg_stat_activity WHERE pid<>pg_backend_pid() AND datname=current_database()")]
            for pid in pids:
                conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
        self.assertEqual(get("/api/profiles")[0], 200)

    def test_runtime_role_without_ddl_privileges_can_run_the_application(self):
        import secrets
        url = self.backend()
        schema = url.rsplit("%3D", 1)[1]
        role, password = "hub_rt_" + secrets.token_hex(4), secrets.token_hex(12)
        with self.admin(PG_URL) as admin:
            admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        self.addCleanup(self.drop_role, role, schema)
        with self.admin(url) as admin:
            admin.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
            admin.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {role}")
            admin.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")
        runtime_url = with_credentials(url, user=role, password=password)
        patch = mock.patch.dict(os.environ, self.env(runtime_url))
        patch.start()
        self.addCleanup(patch.stop)
        server.initialize()
        ts.seed_accounts()
        h = Harness()
        dad = h.client("Dad")
        h.create(dad, "tasks", title="Least privilege", who="Daughter", category="Home")
        self.assertEqual(h.call(dad, "state")[0], 200)
        self.assertEqual(get("/readyz")[0], 200)
        with server.repository() as repo:
            with self.assertRaises(StorageError):
                repo._rows("CREATE TABLE should_not_be_allowed(x INT)")

    def drop_role(self, role, schema):
        runtime.close_pools()
        with self.admin(PG_URL) as admin:
            admin.execute(f"DROP OWNED BY {role}")
            admin.execute(f"DROP ROLE {role}")

    def test_shutdown_closes_every_pool(self):
        url = self.backend()
        with runtime.open_repository(None, self.env(url)):
            pass
        pool = runtime._pools[url]
        runtime.close_pools()
        self.assertTrue(pool.closed)
        self.assertEqual(runtime._pools, {})

    def test_concurrent_writes_through_the_pool_do_not_lose_updates(self):
        url = self.backend()
        patch = mock.patch.dict(os.environ, self.env(url, HUB_DB_POOL_MAX="4"))
        patch.start()
        self.addCleanup(patch.stop)
        server.initialize()
        with server.repository() as repo:
            record = repo.create_record("posts", {"count": 0})

        def bump():
            for _ in range(10):
                with server.repository() as repo:
                    repo.begin_write()
                    body = repo.get_record(record)["body"]
                    repo.update_record_body(record, {"count": body["count"] + 1})

        threads = [threading.Thread(target=bump) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        with server.repository() as repo:
            self.assertEqual(repo.get_record(record)["body"]["count"], 60)


if __name__ == "__main__":
    unittest.main()
