"""Command line for migrations and the SQLite -> PostgreSQL importer.

  python -m persistence status  --destination-env HUB_DATABASE_URL
  python -m persistence migrate --destination-env HUB_DATABASE_URL
  python -m persistence import  --source PATH [--destination-env VAR] [--execute --confirm-write]

Connection URLs are read from the named environment variable and are never printed.
"""
import argparse
import json
import sys

from . import config, importer, migrator


def _connect(var):
    import psycopg
    return psycopg.connect(config.postgres_url(var), autocommit=True)


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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m persistence")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "migrate"):
        item = sub.add_parser(name)
        item.add_argument("--destination-env", required=True, help="Name of the environment variable holding the PostgreSQL URL")
        item.add_argument("--json", action="store_true")
    load = sub.add_parser("import")
    load.add_argument("--source", required=True, help="Explicit path to a SQLite database file")
    load.add_argument("--destination-env", help="Environment variable holding the PostgreSQL URL (required with --execute)")
    load.add_argument("--execute", action="store_true", help="Write to PostgreSQL. Without it the run is a dry run.")
    load.add_argument("--confirm-write", action="store_true", help="Required together with --execute")
    load.add_argument("--allow-live-source", action="store_true", help="Permit data/hub.sqlite3 as the source")
    load.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command in {"status", "migrate"}:
            with _connect(args.destination_env) as conn:
                ran = migrator.migrate(conn) if args.command == "migrate" else []
                _emit({"applied_now": ran, **migrator.status(conn)}, args.json)
            return 0
        source = importer.check_source_path(args.source, args.allow_live_source)
        if args.execute and not (args.confirm_write and args.destination_env):
            raise importer.ImportError_("--execute requires --destination-env and --confirm-write")
        with importer.snapshot_sqlite(source) as source_conn:
            if not args.execute:
                report = importer.dry_run(source_conn)
                if args.destination_env:
                    with _connect(args.destination_env) as conn:
                        report["destination"] = importer.destination_report(conn)
                _emit(report, args.json)
                return 0 if report["ready"] else 2
            with _connect(args.destination_env) as conn:
                _emit(importer.execute_import(source_conn, conn), args.json)
            return 0
    except (config.ConfigError, importer.ImportError_, migrator.MigrationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"error: {type(error).__name__} (details withheld to avoid exposing connection settings)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
