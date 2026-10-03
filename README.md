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

## Persistence and PostgreSQL (Build D, phase 2)

The server runs the same API and authorization logic on SQLite or PostgreSQL,
selected by `HUB_DB_BACKEND`. User-facing behaviour is unchanged.

| Component | Role |
| --- | --- |
| `server.py` | HTTP handlers, validation and authorization (`visible`, `adult`, `dance_record_for_viewer`, ...). Contains no SQL |
| `persistence/repository.py` (`SQLiteRepository`, `PostgresRepository`) | Every SQL statement. One shared implementation; dialects only supply connection handling, JSON/boolean conversion, `json_extract` vs `->>`, and text collation |
| `persistence/runtime.py` | Opens the configured repository; verifies the PostgreSQL schema version |
| `persistence/errors.py` | `StorageError`/`StorageConflict`/`StorageInvalid`/`SchemaNotReady`: messages are safe for browsers and logs |
| `persistence/migrations/postgres/*.sql` + `migrator.py` | Versioned PostgreSQL schema |
| `persistence/importer.py` + `python -m persistence import` | SQLite -> PostgreSQL importer (dry run by default) |

Authorization stays on the server; repositories only store and return data.
SQLite-only code (schema bootstrap, `PRAGMA`, in-place upgrades of old files)
lives in `SQLiteRepository.ensure_schema`. `BEGIN IMMEDIATE` on SQLite and a
PostgreSQL advisory lock (`begin_write`) serialise read-modify-write requests,
so concurrent duplicate actions are applied once on both backends.

### Modes

- **SQLite (default):** `HUB_DB=/path/to/hub.sqlite3 python3 server.py`.
- **PostgreSQL:** `HUB_DB_BACKEND=postgres HUB_DATABASE_URL=postgresql://... python3 server.py`
  (placeholders in `.env.example`). The server never creates or alters PostgreSQL
  schema: if the database is not migrated to the version this code needs it exits
  with a clear, non-secret message (and answers `503` per request). Run
  `python3 -m persistence migrate` first.
- Install the driver only where PostgreSQL is used: `pip install -r requirements-postgres.txt`.
- Database failures return a generic `503` (`409` for a write conflict, `400` for
  data the database cannot store). Driver messages, URLs and credentials are never
  returned or logged; only the exception class name is logged.

### Migrations

Files in `persistence/migrations/postgres/` are named `NNNN_name.sql`, are
applied in order, and each runs in its own transaction. Applied versions are
recorded with a checksum in `schema_migrations`; editing an applied file, or a
database that is ahead of the code, is an error. The application does not run
`CREATE`/`ALTER` on PostgreSQL at startup. Add a schema change as a new file;
never edit an applied one.

~~~sh
export HUB_DATABASE_URL='postgresql://USER:PASSWORD@HOST:5432/DBNAME'   # never commit this
python3 -m persistence status  --destination-env HUB_DATABASE_URL
python3 -m persistence migrate --destination-env HUB_DATABASE_URL
~~~

Timestamps stay ISO-8601 `TEXT` and record bodies are `JSONB` so imported data
is byte-for-byte comparable; IDs are preserved and identity sequences are
advanced after import. Managed children such as Maddox exist only in
`family_members` (a check constraint and importer validation reject a login).
Sessions and login-attempt counters are ephemeral and are not imported.

### Importer: dry run, execute, validation

~~~sh
# Dry run (default): reads a private temp copy of the source, writes nothing.
python3 -m persistence import --source /path/to/copy.sqlite3
# Optionally also check the destination schema version/emptiness (read-only):
python3 -m persistence import --source /path/to/copy.sqlite3 --destination-env HUB_DATABASE_URL
# Write (requires both flags, a migrated and empty destination, and zero validation errors):
python3 -m persistence import --source /path/to/copy.sqlite3 --destination-env HUB_DATABASE_URL --execute --confirm-write
~~~

- `--source` is always explicit; `data/hub.sqlite3` is refused unless `--allow-live-source` is given.
- The dry run reports table counts, records by kind, soft-deleted and archived
  Dance counts, calendar-mirror counts, reminder state and per-table SHA-256
  checksums. Exit code 2 means validation errors.
- Validation reports (never discards) malformed JSON, unknown kinds, orphaned
  audit/reminder/receipt/recurrence/family references, Dance link problems,
  generated calendar events whose `sourceDanceId`/`sourceActivityId` does not
  resolve to exactly one record of the right kind, and any login for a managed child.
- An executed import runs in one transaction and re-reads PostgreSQL to compare
  counts, key sets, status fields (deleted/archived/visibility/reminder state),
  checksums and reference integrity; any difference rolls everything back.

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
python3 -m unittest -v test_persistence test_backend_parity
HUB_TEST_BACKEND=postgres python3 -m unittest -v test_server     # the full HTTP/API suite on PostgreSQL
~~~

- `test_server.py` runs the real request handlers on `HUB_TEST_BACKEND` (`sqlite` default).
- `test_backend_parity.py` runs one scripted scenario on both backends and requires
  identical normalised responses for Dad, Mom and Arielle, plus failure-mode,
  concurrency and query-count tests.
- `test_persistence.py` covers the repository contract, migrations and the importer.
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