"""Command line for inspecting, migrating and importing into the PostgreSQL database.

  python -m persistence status         [--destination-env VAR]          read-only
  python -m persistence migrate        [--destination-env VAR] [--dry-run]
  python -m persistence import-dry-run --source PATH [--destination-env VAR] [--allow-live-source]
  python -m persistence import-execute --source PATH --destination-env VAR --confirm-write --confirm-database NAME

The destination URL is read from an environment variable (default HUB_MIGRATION_DATABASE_URL, which may be a more
privileged account than the application's HUB_DATABASE_URL) and is never printed.
"""
import argparse
import json
import sys

from . import config, importer, migrator


def _connect(var):
    import psycopg
    url = config.postgres_url(var)
    return psycopg.connect(url, autocommit=True, **config.connect_kwargs(url))


def _emit(payload, as_json):
    print(json.dumps(payload, indent=2, default=str) if as_json else _text(payload))


def _text(payload):
    lines = []
    for key, value in payload.items():
        if isinstance(value, (dict, list)) and value:
            lines.append(f"{key}:")
            lines.append(json.dumps(value, indent=2, default=str))
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _database(conn):
    return conn.execute("SELECT current_database()").fetchone()[0]


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m persistence")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(item, destination_required=False):
        item.add_argument("--destination-env", default=None if destination_required else config.MIGRATION_URL_VAR,
                          required=destination_required, help="Environment variable holding the PostgreSQL URL"
                          + ("" if destination_required else f" (default {config.MIGRATION_URL_VAR})"))
        item.add_argument("--json", action="store_true")

    common(sub.add_parser("status", help="Read-only: schema version and pending migrations"))
    migrate = sub.add_parser("migrate", help="Apply pending schema migrations")
    common(migrate)
    migrate.add_argument("--dry-run", action="store_true", help="List pending migrations without applying them")
    for name, help_text in (("import-dry-run", "Validate a SQLite source; writes nothing"),
                            ("import-execute", "Import a SQLite source into an empty, migrated PostgreSQL database")):
        item = sub.add_parser(name, help=help_text)
        common(item, destination_required=name == "import-execute")
        item.add_argument("--source", required=True, help="Explicit path to a SQLite database file")
        item.add_argument("--allow-live-source", action="store_true", help="Permit data/hub.sqlite3 as the source")
    execute = sub.choices["import-execute"]
    execute.add_argument("--confirm-write", action="store_true", help="Required: acknowledges that PostgreSQL will be written")
    execute.add_argument("--confirm-database", help="Required: the destination database name, as shown by import-dry-run")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command in {"status", "migrate"}:
            with _connect(args.destination_env) as conn:
                if args.command == "migrate" and not args.dry_run:
                    ran = migrator.migrate(conn)
                else:
                    ran = []
                state = migrator.status(conn)
                _emit({"applied_now": ran, "dry_run": bool(getattr(args, "dry_run", False)), **state}, args.json)
            return 0
        source = importer.check_source_path(args.source, args.allow_live_source)
        if args.command == "import-execute":
            if not args.confirm_write or not args.confirm_database:
                raise importer.ImportError_("import-execute requires --confirm-write and --confirm-database NAME")
            with _connect(args.destination_env) as conn:
                if args.confirm_database != _database(conn):
                    raise importer.ImportError_("--confirm-database does not match the destination database name")
                with importer.snapshot_sqlite(source) as source_conn:
                    _emit(importer.execute_import(source_conn, conn), args.json)
            return 0
        with importer.snapshot_sqlite(source) as source_conn:
            report = importer.dry_run(source_conn)
            if config_var_present(args):
                with _connect(args.destination_env) as conn:
                    report["destination"] = {"database": _database(conn), **importer.destination_report(conn)}
            _emit(report, args.json)
            return 0 if report["ready"] else 2
    except (config.ConfigError, importer.ImportError_, migrator.MigrationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"error: {type(error).__name__} (details withheld to avoid exposing connection settings)", file=sys.stderr)
        return 1


def config_var_present(args):
    import os
    return bool(os.environ.get(args.destination_env or "", "").strip())


if __name__ == "__main__":
    sys.exit(main())
