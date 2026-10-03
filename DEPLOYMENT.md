# Deploy the private family server

Use one persistent Linux host with Python 3.12+, HTTPS, and a domain.
GitHub Pages/static hosting cannot run this API. No Supabase/Firebase account
or third-party auth API key is required by this implementation.

1. Place the repository on the host. The original ZIP is retained in source
   control but is not served by the application.
2. Choose a private directory owned by the service account for SQLite.
   Restrict permissions to that account, e.g. directory mode 700.
3. Set the same HUB_DB value when provisioning accounts and running the service.
   Run server.py --user Dad, --user Mom, --user Daughter interactively.
4. Configure the service environment:

   ~~~sh
   HUB_DB=/var/lib/martin-riley/hub.sqlite3
   HUB_HOST=127.0.0.1
   PORT=8080
   HUB_ORIGIN=https://family.example.com
   HUB_SECURE_COOKIE=1
   ~~~

5. Run python3 server.py as a non-root service account under a process supervisor.
   Restart on failure. Expose only HTTPS; block public port 8080.
6. Terminate TLS at a reverse proxy, reject unknown Host headers, cap request
   bodies at 32 KiB, and enforce header/request timeouts and connection limits.
   Example Caddy site:

   ~~~caddy
   family.example.com {
       reverse_proxy 127.0.0.1:8080
       header Strict-Transport-Security "max-age=31536000"
   }
   ~~~

   Behind a proxy, the built-in login limit sees the proxy address and shares its
   limit across the family. Add proxy-level per-client login rate limiting as needed;
   do not blindly trust caller-supplied forwarding headers.

7. Back up using SQLite's online backup API, not by copying only the main file
   while writes are active (WAL may contain committed data). Encrypt off-host
   backups, retain several versions, and test restoring them.
8. Verify all three sign-ins on HTTPS. Create Family, Adults Only, Assigned,
   and Me Only items; inspect Daughter's actual /api/state response. Check that
   Mom/Dad cannot read each other's Me Only items. Test task completion, approvals,
   and changes appearing on another phone within five seconds.
9. Android: Chrome → Install/Add to Home Screen.
   iPhone: Safari → Share → Add to Home Screen. Serve the app at the domain root;
   manifest and API URLs are root-relative.

Before family use, review authentication, HTTP serving, backup security, device
installation, and privacy end to end. Rotate passwords after device loss using
the account reset command; this invalidates that person's sessions. Stop the
service before operating on a restored database.

Push notifications, self-service password recovery, MFA, and independently
reviewed production security are separate follow-up work.

## Hosted PostgreSQL deployment (future procedure)

Nothing here has been run against real family data. Rehearse every step on a
synthetic copy first (`test_rehearsal.py` does this automatically).

### Procedure

1. **Provision** a managed PostgreSQL 14+ database in a private network or with
   an IP allow-list. Create two roles: a *migration* role that owns the schema, and
   a *runtime* role with only `SELECT, INSERT, UPDATE, DELETE` on the tables and
   `USAGE` on the sequences, plus `SELECT` on `schema_migrations` (no DDL, not a superuser).
2. **Configure secrets** in the service manager or secret store, never in Git or
   shell history: `HUB_ENV=production`, `HUB_DB_BACKEND=postgres`,
   `HUB_DATABASE_URL` (runtime role), `HUB_MIGRATION_DATABASE_URL` (migration role,
   present only on the machine/session that runs the migration tool).
3. **Verify TLS**: use `sslmode=verify-full` (the production default) and
   `HUB_DB_SSLROOTCERT` if the provider's CA is not in the system store. A
   connection that cannot be verified must fail; do not lower `sslmode`.
4. **Apply migrations**: `python3 -m persistence status`, then
   `python3 -m persistence migrate --dry-run`, then `python3 -m persistence migrate`.
5. **Test readiness**: start the server; `GET /readyz` must return `200` with
   `database: ok, schema: ok`. `GET /healthz` is liveness only.
6. **Dry-run the SQLite import** against a *copy* of the SQLite database made with
   the SQLite online backup API: `python3 -m persistence import-dry-run --source COPY.sqlite3 --destination-env HUB_MIGRATION_DATABASE_URL`.
7. **Review the dry run**: zero errors; counts and per-table checksums are what
   you expect; read every warning (for example `series_from_generated_occurrence`).
8. **Stop the SQLite-backed app**, take a final verified backup copy, and run
   `import-execute --confirm-write --confirm-database <name>` from that copy. The
   import is one transaction with validation; any mismatch rolls back.
9. **Verify privacy and integrity** on PostgreSQL: sign in as Dad, Mom and Arielle,
   inspect each real `/api/state` response (Adult Vault, Adults Only, both Me Only
   spaces, fees and parent notes), and compare counts with the dry run.
10. **Start the application** on PostgreSQL behind HTTPS. Everyone signs in again
    (sessions are not migrated).
11. **Device acceptance**: Android and iPhone installs, task completion, approvals,
    and changes appearing on another phone within five seconds.
12. **Establish backup monitoring** (below) before relying on the system.

### Precondition: never import the original SQLite file directly

An older `hub.sqlite3` may be on an older schema (missing tables such as
`family_members`, `recognition_receipts` and `notification_settings`), and the
importer rejects such a source (`missing_table`). Do **not** upgrade or import the
original. The original SQLite database is the rollback source and must not be
modified, opened for writing, or moved during this procedure.

1. Stop the application and record the original's SHA-256, size and
   modification time (database, `-wal` and `-shm` files). Keep it untouched.
2. Make a private temporary **copy** (database plus its `-wal`/`-shm` files) in a
   directory only the service account can read.
3. **Upgrade the copy** through the normal current SQLite startup path, pointing
   at the copy only: `HUB_DB=/private/tmp/copy/hub.sqlite3 python3 -c "import server; server.initialize()"`
   (never run this against the original path).
4. **Verify the upgraded copy**: it opens, has the three accounts, and the record
   and audit counts match the original's.
5. Run `python3 -m persistence import-dry-run --source <upgraded copy>`
   (optionally with `--destination-env` to check the destination read-only).
6. **Review every error, warning and count.** The result must be `ready` with zero
   errors; understand each warning (for example `series_from_generated_occurrence`).
7. Only then use the **upgraded copy** as the `--source` of `import-execute`.
8. Afterwards, confirm the original's SHA-256, size and mtime are unchanged and
   keep it, with its backups, until the PostgreSQL system has been accepted.
   Delete the private copies securely when finished.

### Backups and recovery design

- **Primary: provider-native automated backups** with point-in-time recovery
  (WAL archiving), taken at least daily with continuous WAL, retained 14-35 days.
  Alert on a failed or stale backup, not only on a failed job.
- **Independent: a scheduled logical dump** (`pg_dump --format=custom`) by a
  read-only role, encrypted and stored off-provider, weekly retention for 3 months
  and monthly for a year. It survives loss of the provider account.
- **Never** keep dumps or credentials in the repository.
- **Restore into a scratch database first** (never over production): restore,
  then run `python3 -m persistence status` against it (must report no pending
  or unknown migrations), start a throwaway server pointed at it and check
  `/readyz`, compare record/audit counts with the source, and repeat the privacy
  check for Dad, Mom and Arielle. Only then switch `HUB_DATABASE_URL` to it (or
  restore over production in a maintenance window), restart the app, and have
  everyone sign in again if sessions were lost.
- **Deploying a schema change**: take a backup (or note the PITR timestamp), run
  `migrate --dry-run`, then `migrate`, then deploy code. Migrations are additive
  and each runs in its own transaction, so a failed migration leaves the previous
  schema version intact. Code refuses to start on a schema older or newer than it
  requires, so apply the migration first and only then start the new code.
- **Deployment succeeded but migration failed**: the new code refuses to start
  (`/readyz` is `503`, `schema: not_ready`) rather than guess. Fix the migration
  and re-run it, or roll the code back to the previous release; no data is
  modified by a failed migration.
- **Rollback after cut-over**: while testing, the SQLite file is untouched and
  can be put back in service. After family use begins on PostgreSQL, rollback means
  restoring from a PostgreSQL backup; changes made since cut-over exist only there.
- **Stop the application before** restoring over a live database.
