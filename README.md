# Martin-Riley Family Hub

A private, mobile-first organizer for Dad (Android, Adult/Admin), Mom (Android,
Adult/Admin), and Daughter (iPhone, Family Member).

This continues the original V1 rather than replacing it with a template. The
original repository contained a README, HTML, CSS, and a ZIP holding the missing
JavaScript, manifest, and service worker. Those source files were recovered from
the ZIP. The archive remains unchanged as the historical prototype.

## What works now

- V1 Parent Command Center and simplified Daughter Home, original purple/pink
  palette, colorful family tiles, larger touch targets, and celebrations.
- Tasks and Family Chore Board: Normal / Important / High / Urgent, assignment,
  mandatory urgent acknowledgement, completion, and not-completed reasons.
- Daughter's qualifying home-chores, UTC daily chore streak, and extensible
  achievements for streaks, completion milestones, and a perfect week.
- Weekly Recap with task and chore outcomes, streaks, achievements, upcoming
  events, and positive family highlights; parents also see household follow-up.
- Ask Parents: requests and parent-only approval/denial with reply/conditions.
- Bulletin Board with per-person toggled reactions.
- Shared Calendar with dated events, competition dates/deadlines, and
  privacy-filtered Tomorrow Prep for schedule, tasks, packing, and reminders.
- Managed child profiles are separate from login accounts; Maddox has parent-managed
  activities and sports linked to Family Calendar without credentials or sessions.
- Shared checklist items; Dance Hub with separate Competitions, Schedule,
  Routines, Costumes, and Dance Checklists sections, including routine and
  costume associations, competition schedule status, travel details, and
  individually checkable packing lists.
- Parent-issued positive recognition for Arielle's completed work.
- Adult Vault notes and Me Only notes; private activity history.
- Adult Vault entries shared only by the adults and separate, creator-only Me Only
  notes, with optional date reminders. Adult Vault is not a password manager;
  never store passwords, PINs, full SSNs, full payment-card numbers, or auth secrets.
- Adult Access & Privacy dashboard with account/area permissions and conservative
  read-only ChatGPT defaults.
- In-app reminders for open High/Urgent tasks.
- Per-user numbered badges for unseen and actionable tasks, requests, board posts,
  dance/calendar updates, shared lists, weekly recap, recognition, and reminders.
- Installable web manifest, Android/iPhone icons, public-shell-only service worker.
- Individual passwords, HttpOnly sessions, server-side authorization,
  shared SQLite database, refresh every five seconds while the app is visible.

No continuous location tracking or ChatGPT data integration is implemented.

## Run locally (Python 3.12+)

No third-party runtime packages are required.

~~~sh
python3 server.py --user Dad
python3 server.py --user Mom
python3 server.py --user Daughter
python3 server.py
~~~

Each account command prompts for a unique password of at least 12 characters.
There are no default accounts or demo passwords. Open http://127.0.0.1:8080.
Account reset uses the same command and revokes that person's existing sessions.

A static server is **no longer sufficient**. The browser must use the Python
server's API; there is no localStorage fallback or role-switching demo mode.
Prototype localStorage is cleared on startup. No automatic import of prototype
data occurs, so historical/private demo data cannot be accidentally published.

## Privacy enforced by the API

| Visibility | Accounts receiving the record |
| --- | --- |
| Family | Dad, Mom, Daughter |
| Adults Only (stored as Adults) | Dad and Mom |
| Assigned Person (Assigned) | The selected individual only |
| Me Only (Me) | The creator only, including for parent-created items |

Parents cannot read another person's Me Only items. Daughter does not receive
Adults Only or another person's Me Only records. Activity is filtered against
the same visible records; hidden item titles/reasons are never included in
history. Tasks can only be acknowledged/completed by the assignee (or a family
member for Everyone tasks). Only parents can create tasks or decide requests.
These checks apply even when callers bypass the UI.

Passwords use salted scrypt. Sessions are random, hashed in the database, and
expire after seven days. Login attempts are limited per server-observed address.
Mutations require a custom request header, no CORS is enabled, and production
requests must match HUB_ORIGIN. User content is escaped before rendering.

## Deployment and configuration

See [DEPLOYMENT.md](DEPLOYMENT.md). True shared synchronization works when all
phones connect to **the same hosted server and persistent database**. This
repository does not provision or deploy a public server.

| Variable | Local default | Production requirement |
| --- | --- | --- |
| HUB_DB | data/hub.sqlite3 beside server.py | Absolute path on a persistent private volume |
| HUB_HOST | 127.0.0.1 | Keep loopback behind a same-host reverse proxy |
| PORT | 8080 | Internal server port |
| HUB_ORIGIN | unset | Exact HTTPS origin, e.g. https://family.example.com |
| HUB_SECURE_COOKIE | unset | Set to 1 under HTTPS |

Do not commit passwords, database files, backup files, or .env secrets.
SQLite is suitable for this small family on one server; do not run independent
replicas with separate disks or put the database on a network filesystem.

## Remaining production work

- Provision an HTTPS host, persistent disk, domain, process supervisor, backups
  with restore testing, monitoring, and all three accounts.
- Review/harden the HTTP serving layer and deployment before exposing private
  family information. The included Python HTTP server is a runnable foundation,
  not a managed, independently audited production service.
- Operator-driven password resets exist; self-service recovery, MFA, and
  device/session management are not implemented.
- Adult Vault is access-controlled notes, **not** an encrypted password manager.
  Database and backups contain plaintext family content: secure the disk and backups.
- Notifications are in-app only. Background/push reminders need Web Push
  subscriptions/VAPID infrastructure, scheduling, consent, and device tests.
- No offline private-data access or queued writes. Connection failures keep edits
  unsent and show an error; reconnect to save. Service worker caches public assets only.
- Calendar has dated entries, not external calendar sync or timed alarms.
  Dance competition dates and deadlines are linked to the family calendar.
  Attachments and automatic daily resets are future work.
- Streaks use UTC completion days, not the family's local timezone.
  Historical records are retained; no records are prepopulated.
- Perform Android/iPhone installation, accessibility, and HTTPS browser acceptance
  tests on the deployed application. Polling is foreground synchronization, not push.

## Persistence and PostgreSQL (Build D)

The server runs the same API and authorization logic on SQLite or PostgreSQL,
selected by `HUB_DB_BACKEND`. User-facing behaviour is unchanged.

| Component | Role |
| --- | --- |
| `server.py` | HTTP handlers, validation and authorization (`visible`, `adult`, `dance_record_for_viewer`, ...). Contains no SQL |
| `persistence/repository.py` | Every SQL statement. One shared implementation; `SQLiteRepository` and `PostgresRepository` only supply connection handling, JSON/boolean conversion, `json_extract` vs `->>`, and text collation |
| `persistence/runtime.py` | Opens the configured repository; owns the PostgreSQL connection pool; readiness check |
| `persistence/config.py` | Environment parsing: backend, production rules, TLS, pool size. Never prints credentials |
| `persistence/errors.py` | `StorageError` and friends: messages are safe for browsers and logs |
| `persistence/migrations/postgres/*.sql`, `migrator.py` | Versioned PostgreSQL schema |
| `persistence/importer.py`, `cli.py` | SQLite -> PostgreSQL importer and the `python -m persistence` tool |

Authorization stays on the server; repositories only store and return data.
SQLite-only code (schema bootstrap, `PRAGMA`, in-place upgrades of old files)
lives in `SQLiteRepository.ensure_schema`. `BEGIN IMMEDIATE` on SQLite and a
PostgreSQL advisory lock serialise read-modify-write requests, so concurrent
duplicate actions are applied once on both backends.

### Configuration

Environment variables (names only; values are secrets and never belong in Git):

| Variable | Meaning |
| --- | --- |
| `HUB_ENV` | `development` (default), `staging` or `production`. Staging and production refuse weak settings (below) and require a database labelled for that environment |
| `HUB_DB_BACKEND` | `sqlite` or `postgres`. **Required** when `HUB_ENV` is `staging` or `production` (staging is PostgreSQL only). If `HUB_DATABASE_URL` is set but this is not, startup fails instead of using SQLite |
| `HUB_DB` | SQLite file path (SQLite mode only) |
| `HUB_DATABASE_URL` | Runtime PostgreSQL URL used by the web server |
| `HUB_MIGRATION_DATABASE_URL` | Optional separate URL (a more privileged role) used only by `python -m persistence`. The runtime role can then be limited to reading/writing application tables |
| `HUB_ALLOW_REMOTE_DEV_DATABASE` | `1` lets `development` use a non-local PostgreSQL host. Only the staging test runner should set it |
| `HUB_SECURE_COOKIE`, `HUB_ORIGIN` | Mandatory (`1` and an `https://` origin) in staging and production |
| `HUB_DB_SSLMODE`, `HUB_DB_SSLROOTCERT` | TLS mode and optional CA bundle path |
| `HUB_DB_POOL_MIN` / `HUB_DB_POOL_MAX` | Pool size, default 1 / 5 (1-50) |
| `HUB_DB_POOL_TIMEOUT`, `HUB_DB_CONNECT_TIMEOUT` | Seconds to wait for a pooled connection (default 5) / to open one (default 5) |

Production startup **fails closed**: a missing, malformed or wrong-scheme URL,
an unreachable database, bad credentials, a TLS failure, or a database that is
unmigrated, partially migrated or newer than the code makes `server.py` exit with
a non-secret message. It never falls back to SQLite. Redacted diagnostics show
only the scheme and database name.

Install the drivers only where PostgreSQL is used: `pip install -r requirements-postgres.txt`.

#### TLS

- **Production** (`HUB_ENV=production`): certificate verification is mandatory.
  The default is `sslmode=verify-full`; `verify-ca` is also accepted. `disable`,
  `allow`, `prefer` and `require` (including via `?sslmode=` in the URL) are
  rejected before any connection is attempted. Use `HUB_DB_SSLROOTCERT` when the
  provider's CA is not in the system store. Verification is never switched off
  to make a connection work.
- **Development**: libpq defaults (`prefer`) unless `HUB_DB_SSLMODE` is set, so a
  local throwaway database without TLS works.

#### Connection pool

PostgreSQL connections come from one small bounded pool per process
(`psycopg_pool`): size limits, a wait timeout that becomes a generic `503`,
health validation on every checkout (stale or killed connections are replaced),
rollback of any open transaction when a connection is returned, and a clean
close at shutdown. SQLite opens a connection per request.

#### Health endpoints (no authentication, no private data)

- `GET /healthz` - liveness: `{"status":"ok"}` whenever the process runs.
- `GET /readyz` - readiness: `200 {"status":"ready","database":"ok","schema":"ok"}`, or
  `503` with `database` (`ok`/`unavailable`) and `schema` (`ok`/`not_ready`/`unknown`).
  No URLs, versions, counts or names are returned. Point load-balancer/startup
  probes at `/readyz` and process-supervisor liveness at `/healthz`.

### Migration tool

Separate operations, each reading the destination URL from an environment
variable (default `HUB_MIGRATION_DATABASE_URL`; the URL is never printed):

~~~sh
python3 -m persistence status                              # A. inspect (read-only; creates nothing)
python3 -m persistence verify                              #    read-only row counts and per-table checksums (restore / cut-over checks)
python3 -m persistence migrate --dry-run                   #    list pending migrations
python3 -m persistence migrate                             # B. apply schema migrations
python3 -m persistence import-dry-run --source COPY.sqlite3 [--destination-env VAR]   # C. validate; writes nothing
python3 -m persistence import-execute --source COPY.sqlite3 --destination-env VAR \
    --confirm-write --confirm-database DBNAME              # D. real import
~~~

Safeguards: `import-execute` needs `--destination-env` named explicitly,
`--confirm-write`, and `--confirm-database` equal to the destination's actual
database name (shown by `import-dry-run --destination-env`). It also requires a
fully migrated, **empty** destination and a source with zero validation errors.
The source is read from a private temporary copy and is never written; the live
`data/hub.sqlite3` is refused unless `--allow-live-source` is given. The import is one
transaction that re-reads PostgreSQL and compares counts, key sets, status
fields, checksums and references; any difference rolls everything back. A second
import into a populated database is refused.

Migration files in `persistence/migrations/postgres/` are named `NNNN_name.sql`,
run in order each in its own transaction, and are recorded with a checksum in
`schema_migrations`; editing an applied file, or a database ahead of the code, is
an error. The application never runs `CREATE`/`ALTER` on PostgreSQL itself. Add a
schema change as a new file; never edit an applied one.

Timestamps stay ISO-8601 `TEXT` and record bodies are `JSONB`; IDs are preserved
and identity sequences are advanced after import. Managed children such as
Maddox exist only in `family_members`. Sessions and login-attempt counters are
ephemeral and are not imported (everyone signs in again after cut-over).

The dry run warns about `series_from_generated_occurrence`: Build C (before this
build) turned every generated recurring occurrence into its own series each time
the server restarted. That is fixed, but a database that was restarted with
recurring items may already contain such rows; review them in the dry run.

### Rollback

Build C commit `d565c06606e5dada3d0cc9466bd1eadf50cd24dd` is the known-good
SQLite release. The importer never writes to SQLite, so rolling back means
pointing the server at the untouched SQLite file. Keep a verified backup of
the SQLite file before any real migration and rehearse on a copy first.

### Tests

~~~sh
python3 -m unittest -v        # everything on SQLite; PostgreSQL tests are reported as skipped
# Everything on PostgreSQL (disposable database; each test class gets a private schema):
export HUB_TEST_POSTGRES_URL='postgresql://USER:PASSWORD@127.0.0.1:PORT/DBNAME'
python3 -m unittest -v test_persistence test_backend_parity test_hosted_readiness test_rehearsal test_secret_hygiene
HUB_TEST_BACKEND=postgres python3 -m unittest -v test_server     # the full HTTP/API suite on PostgreSQL
~~~

- `test_server.py` runs the real request handlers on `HUB_TEST_BACKEND` (`sqlite` default).
- `test_backend_parity.py` runs one scripted scenario on both backends and requires
  identical normalised responses for Dad, Mom and Arielle, plus failure-mode,
  concurrency and query-count tests.
- `test_persistence.py` covers the repository contract, migrations and the importer.
- `test_hosted_readiness.py` covers production configuration, TLS rules, the pool, health endpoints and recovery.
- `test_secret_hygiene.py` fails if a database file, key, `.env` or credential-bearing URL would be committed.
- Running the PostgreSQL suites against a **hosted staging** database is documented in `DEPLOYMENT.md`; it needs `HUB_TEST_REMOTE_CONFIRM=staging` and a URL with `sslmode=verify-full`.
- `test_rehearsal.py` builds a synthetic family, runs dry run -> import -> comparison, then the privacy and attack tests on PostgreSQL.
- Server tests freeze the clock (20:00 UTC) and timezone, so they do not depend on the time of day, `HUB_TIMEZONE` or the machine's timezone.

#### Temporary PostgreSQL for development

Never commit credentials. Create a throwaway local container with a random password,
bound to localhost only, and remove it when finished:

~~~sh
docker run -d --name hub-pg-test -e POSTGRES_PASSWORD="$(openssl rand -hex 12)" -e POSTGRES_DB=hubtest \
  -p 127.0.0.1:55432:5432 postgres:16-alpine
export HUB_TEST_POSTGRES_URL="postgresql://postgres:$(docker inspect hub-pg-test --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^POSTGRES_PASSWORD=//p')@127.0.0.1:55432/hubtest"
docker rm -f hub-pg-test        # removes the container and all its data
~~~

## Verification

~~~sh
python3 -m unittest -v
python3 -m py_compile server.py
node --check app.js
node test_client.cjs
~~~

Server tests execute real request handlers with a temporary SQLite or PostgreSQL database without sockets.
They verify filtered payloads/history, forbidden direct API writes, urgent
acknowledgement, shared-state visibility, parent decisions, CSRF header
enforcement, and logout. Client smoke tests exercise every renderer and HTML
escaping. Real browser/device verification remains required.