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

### Environments and database separation

| Environment | Database | Rules enforced by the application |
| --- | --- | --- |
| `development` (default) | SQLite, or a **local** PostgreSQL (loopback host only) | A remote database host is refused unless `HUB_ALLOW_REMOTE_DEV_DATABASE=1` (used only by the staging test runner) |
| `staging` | Hosted PostgreSQL, **synthetic data only**, its own credentials | `HUB_DB_BACKEND=postgres` required, verified TLS, `HUB_SECURE_COOKIE=1`, `https://` `HUB_ORIGIN`, database must carry the `staging` label |
| `production` | A different hosted database with different credentials | Same strict rules; database must carry the `production` label |

- `python -m persistence migrate` writes the environment label (`hub_environment`,
  migration 0003) from `HUB_ENV`. At startup the server refuses a database whose
  label differs from `HUB_ENV`, and refuses an unlabelled database in staging or
  production. The migration tool also refuses a mismatched label. Staging and
  production therefore cannot share a database even if a URL is pasted into the
  wrong place. Error messages never echo the label, host, user or URL.
- Use a separate provider project (or at minimum a separate database and role) per
  environment, with different credentials stored in the provider's secret manager.
- Never reuse a staging URL in production configuration or the reverse.

### Staging rehearsal runbook (synthetic data only)

1. Create the staging database at a managed PostgreSQL provider (see the provider
   checklist below). Use a **direct, non-pooled** connection string for the
   migration tool and the test runner (`options=` start-up parameters, used by the
   per-test schemas, are rejected by pooling proxies such as PgBouncer).
2. Put the URL in an environment variable or the provider's secret store, never in a file
   in the repository. The URL must include `sslmode=verify-full`.
3. `export HUB_ENV=staging HUB_DB_BACKEND=postgres HUB_SECURE_COOKIE=1 HUB_ORIGIN=https://<staging-host>`, then
   `python3 -m persistence status`, `migrate --dry-run`, `migrate`, `verify`.
4. Run the full test matrix against it. Tests only create and drop private
   schemas, but they kill pooled connections on purpose, so use a dedicated staging database:

   ~~~sh
   export HUB_TEST_POSTGRES_URL=...   # staging URL with sslmode=verify-full (secret)
   export HUB_TEST_REMOTE_CONFIRM=staging
   export HUB_ALLOW_REMOTE_DEV_DATABASE=1   # lets the development-mode tests reach the hosted staging host
   python3 -m unittest test_persistence test_backend_parity test_hosted_readiness test_rehearsal
   HUB_TEST_BACKEND=postgres python3 -m unittest test_server
   HUB_PERF_REPORT=1 python3 -m unittest test_backend_parity.QueryBudgetTests
   ~~~

   The runner refuses a remote URL without `HUB_TEST_REMOTE_CONFIRM=staging` or
   without verified TLS, and refuses any database labelled `production`.
5. Import the synthetic family (`test_rehearsal.py` builds one) with
   `import-execute`, then `verify` and run the privacy checks against the running staging server.
6. Rehearse backup and restore (below) and record results.

### Provider checklist (verify each item in the provider's console; do not assume)

- PostgreSQL major version (14+), region, and whether storage is encrypted at rest.
- TLS: certificate chain verifiable with the system store or a downloadable CA.
- Automated backups: frequency, **retention period on your plan**, and whether
  point-in-time recovery (PITR) is included or extra cost.
- Restore mechanism: restore to a *new* branch/database, not in place.
- Role model: can you create a separate runtime role without DDL rights?
- Connection limits (the pool defaults to at most 5), idle-suspend/cold-start
  behaviour (first request after idle may be slow), and cost of compute and storage.
- Whether a direct (non-pooled) endpoint is offered for migrations.

### Production procedure (future; none of it has been run)

**Principle: the original SQLite database is never the import working copy.**
It is only ever copied. It stays untouched as the rollback source.

1. **Prerequisites**: staging passed every test above; provider backups verified;
   production database created **empty**, with its own credentials, in a project separate from staging.
2. **Staging verification**: re-run the staging matrix on the exact commit to be deployed.
3. **Back up the original SQLite**: stop the application, then take a backup with
   the SQLite online backup API to encrypted storage.
4. **Record the original's SHA-256, size and mtime** (database, `-wal`, `-shm`).
5. **Make a private COPY** of the database and its WAL/SHM files (mode 700 directory).
6. **Upgrade the COPY if needed** through the normal startup path with `HUB_DB` pointing at the copy only (see the precondition section).
7. **`import-dry-run` against the upgraded copy.**
8. **Human review checkpoint**: zero errors; every warning understood; record counts
   match expectations. Nothing proceeds without explicit sign-off.
9. **Migrate the production database**: `HUB_ENV=production`, then `migrate` with the migration role; confirm the `production` label with `verify`.
10. **Synthetic production smoke test before real import**: import the synthetic
    family into the *empty* production database in a rehearsal window, run the privacy checks, then
    empty it again (drop and recreate the database or restore the empty snapshot); re-migrate. Real data
    never goes in until this passes. `import-execute` refuses a non-empty destination.
11. **Real import**: from the upgraded copy only, with `--confirm-write --confirm-database <name>`.
12. **Row/count/checksum verification**: `python -m persistence verify` on production versus the dry-run counts and per-table checksums.
13. **Dad privacy test**: sign in as Dad; inspect the raw `/api/state` for Family, Adults Only, his Me Only, Adult Vault; no Mom Me Only.
14. **Mom privacy test**: the same for Mom; no Dad Me Only.
15. **Child privacy test**: no Adults Only, no Me Only, no Vault, no financial or parent-private fields; then attempt direct API mutations and confirm rejection.
16. **Calendar-mirror integrity**: each competition and activity event has exactly one mirror; direct edit/delete of a mirror is rejected.
17. **Recurrence integrity**: no series created from generated occurrences; restart the server three times and compare series and occurrence counts.
18. **Application cut-over**: set `HUB_ENV=production` secrets, start the server, `GET /readyz` must be 200, point the proxy at it, have everyone sign in again.
19. **Rollback**: stop the new application and restart the **original** SQLite deployment (unchanged since step 4). Changes made on PostgreSQL after cut-over exist only there;
    after the family has used PostgreSQL, rollback means restoring a PostgreSQL backup instead.
20. **Post-cutover backup verification**: confirm the provider's first automated
    backup exists, run the restore rehearsal into a scratch database, `verify` it against production, and set up backup-age alerts.

### Backup and restore rehearsal (verified on hosted Neon staging, synthetic data)

Verified on the hosted staging database and, earlier, a local throwaway PostgreSQL:
`pg_dump --format=custom --no-owner --schema=public` -> intentionally damage staging ->
`pg_restore --clean --if-exists --no-owner --no-privileges` into a **separate scratch
database** -> `python -m persistence verify` on the restore showed row counts, record kinds and every
per-table checksum identical to the pre-damage state, the `staging` label and schema version were
restored, and the Dad/Mom/Arielle privacy checks passed through the real API on the restored copy.
Staging itself was left untouched by the restore, the scratch database was dropped afterwards.

- Use a `pg_dump` whose major version is **at least the server's** (Neon staging reported PostgreSQL 18; a
  `postgres:18-alpine` container was used so no client is installed on the host).
- Pass the connection string to the tool through an environment variable, not on the command line.
- `--no-privileges` is required on Neon: the dump otherwise contains provider-owned `ALTER DEFAULT PRIVILEGES`
  statements that an ordinary role is not allowed to replay.
- A dump of a 190-record synthetic family is about 55 KB and took about 3 s to take and 4 s to restore.

**Not verified:** Neon's own point-in-time restore/branching, its retention period and cost on your plan.
Those are only visible in the provider console; confirm them there (checklist above) and repeat the
rehearsal with the provider's restore into a new branch. The logical dump above is the independent,
provider-neutral copy and should be scheduled off-provider regardless.

Manual procedure: dump as above (encrypt and store off-provider); restore into a new database with
`pg_restore --clean --if-exists --no-owner --no-privileges -d <new database>`; run `python -m persistence verify`
against both and compare; run the privacy checks; only then point the application at it.

### Hosted staging findings (Neon)

- Connection: use the **direct (non-pooled)** endpoint. The stored string asked for `sslmode=require`
  (encrypts but does **not** verify the certificate) and `channel_binding=require`. For staging and production
  change `sslmode` to `verify-full` and add `sslrootcert=/etc/ssl/certs/ca-certificates.crt` (the system bundle
  on Debian/Ubuntu images). Negative controls confirmed that a missing CA file, a CA bundle without the
  issuing CA, and `sslmode=disable` are all refused. Keep `channel_binding=require`.
- The owner role can create roles and databases; a DML-only runtime role ran the full application and was denied
  DDL. PostgreSQL 16+ requires `GRANT <role> TO <owner>` before the owner may `DROP OWNED BY` that role.
- Latency: one SQL round trip from the Codespace averaged about 22 ms, so `/api/state` (18 queries plus pool health
  check and two commits) took 570-750 ms. Put the database in the **same region as the application host**
  and re-measure; most of the cost is network round trips, not database work.
- The first connection after idle took about 0.9 s (compute wake-up); expect a slow first request.

### Authentication review for internet exposure

Suitable for **private family staging** behind HTTPS; **not yet sufficient for open internet exposure** without the follow-ups below.

| Area | Finding |
| --- | --- |
| Password storage | scrypt (N=16384, r=8, p=1), 16-byte random salt, constant-time compare. Acceptable; N could be raised |
| Password policy and reset | 12+ characters; set or reset only through the interactive CLI on the server, which also revokes that person's sessions. No self-service reset or recovery |
| Default credentials | None; accounts exist only after the CLI creates them |
| Session tokens | 256-bit random token, only its SHA-256 is stored, 7-day fixed lifetime, deleted on logout; expired rows are purged at login. No sliding renewal, no per-user session list |
| Cookie flags | `HttpOnly`, `SameSite=Strict`, `Path=/`; `Secure` only when `HUB_SECURE_COOKIE=1` (now mandatory in staging/production) |
| CSRF | Custom `X-Hub-Request` header on every POST, optional `Origin` check (`HUB_ORIGIN`, now mandatory in staging/production), `SameSite=Strict` |
| Brute force | 10 attempts per client address per 15 minutes; behind a proxy all users share one address, so add proxy-level per-client limits. No per-account lockout or alerting |
| Account enumeration | Login returns one generic message and compares a dummy hash for unknown names, **but `GET /api/profiles` is unauthenticated and returns the three accounts' first names**. Remove or protect before public exposure |
| Logging | Request logging is disabled; storage errors log only a class name; no passwords or cookies are logged |
| Transport and headers | The app does not terminate TLS or send HSTS or CSP; the reverse proxy must (see above). Static files send `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy` |
| No MFA | No second factor |

**Next checkpoint for production auth (not done here):** protect `/api/profiles`, add per-account
throttling and proxy rate limiting, sliding session expiry with a "sign out everywhere" option, a
password-change flow, optional MFA, CSP/HSTS at the proxy, and an external security review.

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
