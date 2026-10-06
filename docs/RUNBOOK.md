# v2 foundations runbook

This document covers the v1 fixes, M1 config/persistence/audit/backups/CLI and M2 core web
controls on `v2`. The final Linux deployment, isolated shell, browser and live screen
milestones remain unimplemented.

From the repository root, with Python 3.12 and Node.js available:

```sh
python -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation --no-index -e .
.venv/bin/python -m pytest -q
node --test tests/frontend/chat.test.cjs
```

Tests use a fake model; no model server or model API request is needed.

Configure `FORWARDED_ALLOW_IPS` with your reverse proxy's explicit IP or CIDR.
Starting `python -m agent` with `FORWARDED_ALLOW_IPS=*` must exit non-zero with:

```text
FORWARDED_ALLOW_IPS='*' is not allowed; list the proxy IP
```

`tests/test_m0_cli.py` exercises this startup path with a temporary data directory.
Do not use an existing production database to test a rejected configuration.

Run `.venv/bin/ruff check agent tests` for the repository-wide lint check.

## Configuration stage (M1.2)

`.env.example` lists v2 settings and their code defaults. Many are parsed foundations for
later milestones; configuring them does not create those services. Do not deploy this
development baseline as the final 24/7 server setup.

For development with a local llama.cpp server supporting native completions:

```dotenv
MODEL_PROVIDER=llamacpp
MODEL_BASE_URL=http://127.0.0.1:8080
MODEL_TOOL_MODE=native
DATA_DIR=./data
ALLOW_SHELL=false
BROWSER_ENABLED=false
SCREEN_ENABLED=false
```

The existing development compose file still uses its `ollama` service. To use that file,
override `MODEL_PROVIDER=ollama`, `MODEL_BASE_URL=http://ollama:11434/v1`, set
`MODEL_ALLOWED_HOSTS=ollama,127.0.0.1,localhost,::1`, and set `MODEL_NAME` to a locally
installed model tag. Full provider implementations replace this compatibility path in M2.
The static `10.77.6.60` code default belongs to the future v2 deployment, not this old compose.

Make the login hash with `python -m agent hash-password`. Put the printed hash in a private
file and set `AGENT_PASSWORD_HASH_FILE` to its path, or set `AGENT_PASSWORD_HASH` directly
for local development. Apply the same pattern to `MODEL_SERVER_TOKEN` when the model server
requires authentication. Files win over environment values; failed file reads stop startup.
No secret value belongs in the repository. Secret files are not automatically mounted by
the old compose file; host file paths work only when they also exist inside the container.

`AGENT_ENV=production` requires `COOKIE_SECURE=true`, `AGENT_HOST` or `ALLOWED_HOSTS`,
and `MODEL_SERVER_TOKEN` for llama.cpp. It refuses `ALLOW_SHELL=true` with the local backend.
The configured model must stay local in every environment, including terminal chat and jobs.
Setting an unavailable browser, screen, or sandbox feature causes a clear startup refusal;
those flags should remain off until their milestones are implemented.

## Core web controls (M2.3)

Local development keeps `CORE_ALLOWED_PEERS` empty to disable its peer filter; it keeps
browser, screen and shell features off. In production, an empty peer list now defaults to
the future Caddy IP `10.77.1.2`. Use explicit IPs or narrower CIDRs for a different proxy,
and configure `FORWARDED_ALLOW_IPS` separately with the same proxy's address. The peer
filter checks the connection address before processing forwarded visitor headers.

`AGENT_HOST` takes a DNS hostname or IPv4 address, without `https://`, port or path.
It defaults `ALLOWED_HOSTS` and supplies the CSP/websocket origin. Secure mode requires
HTTPS for state changes and websocket Origins. Enabling secure cookies changes the
session cookie to `__Host-agent_session`; log in again once after upgrading. The old
cookie is ignored in secure mode. Local development with `COOKIE_SECURE=false` retains
the legacy cookie. No credentials or session tokens should be committed.

The health CLI can still request only `/healthz` through the core's own literal bind IP;
this exception grants no access to other routes. Internal paths are restricted to a peer
explicitly listed in both allowlists. `/internal/screen-auth` remains refused until M7.
The forthcoming Caddy configuration must block external health and internal paths; the
old development compose does not yet provide that edge isolation.

```sh
pytest -q tests/test_web_v2.py
```

These tests inventory HTTP routes and mount a test-only websocket to check sessions,
expiry, Origin, peer restrictions, cookie attributes, CSRF and security headers. They
make no model request and add no production websocket route. See SECURITY.md for the
handshake-only limit and the next screen milestone's revocation responsibilities.

## Database upgrades (M1.3)

The database is now upgraded through `agent/migrations/`, using SQLite `user_version`.
Migration 1 preserves the v1 schema and its legacy job/session upgrades. Migration 2 adds
the v2 columns and tables. Every migration runs inside `BEGIN IMMEDIATE`; schema edits,
data edits and the version number roll back together if that migration fails. A previously
completed migration stays committed. Databases from a newer unsupported version are refused.

```sh
python -m agent migrate --check
```

This reads the version in read-only mode and prints current/target versions. It does not
create a missing database or apply upgrades, and it does not construct a model client.
Normal agent startup applies pending upgrades. There is no downgrade command.

For an automatic snapshot before `serve` upgrades any existing non-empty database (including unversioned v1),
set `BACKUP_DIR` to an existing private directory accessible by the core. It writes
`BACKUP_DIR/db/pre-migrate-v<from>-to-v<to>-<timestamp>.db.gz` using SQLite's online backup
API, checks integrity, compresses to a private temporary file, fsyncs and atomically renames
it. The file has mode 0600. Failure stops startup before the upgrade. This hook is off when
`BACKUP_DIR` is empty or its directory does not exist. The old development compose does not
mount that directory automatically. Fresh empty databases do not need a pre-upgrade snapshot.

The fixture `tests/fixtures/v1_4fb0950.sql` contains the exact v1 baseline schema; tests add
synthetic data to it. No real database, account data or credentials are committed. Run
`pytest -q tests/test_migrations.py tests/test_memory_v2.py` for migration, rollback and helper
coverage. The existing full suite continues to run with `make test`.

## Audit log (M1.4)

The agent writes startup, login success/failure, logout, job creation/approval/deletion and
fact-deletion events into `audit_log`. Model-created jobs are recorded too. Password attempts
and session cookies are never included. Loaded Config secrets are replaced by `[redacted]`
in nested string values, keys and labels before UTF-8 byte truncation. Detail stays valid JSON;
truncated details have `truncated` and `preview` fields. The default limit is 8192 bytes;
`AUDIT_DETAIL_MAX_BYTES` must be at least 64.

```sh
python -m agent audit-verify
```

Verification opens the existing database read-only, without upgrades or model requests.
It prints JSON containing `ok`, `rows` (rows checked), and `first_bad_id`, and exits non-zero
on a broken chain or an unavailable audit table. No missing database is created. The hash
uses canonical JSON with `detail` decoded as an object, and excludes `id`, `prev_hash` and
`hash`. The first previous hash is 64 zeros. Related session/job/fact changes and their audit
entry commit in one transaction. A logging failure prevents that change.

The audit API, UI, tool/gate/browser/screen events arrive in their later milestones. This
stage does not add those features or log screen input. Run `pytest -q tests/test_audit.py`
for focused checks.

## Backups and restore (M1.5)

These commands and workspace archives use Linux file locks and descriptor-based traversal.
Set `BACKUP_DIR` to a private location outside the workspace, accessible to the core. The
old development compose does not mount it; the final deployment will provide the volume.
An empty BACKUP_DIR keeps automatic backups off. The directory is created when a manual
or nightly backup runs. Existing directory permissions are the administrator's settings;
new backup directories use 0700 and archives use 0600.

```dotenv
BACKUP_DIR=/srv/roland-agent/backups
BACKUP_TIME=03:30
BACKUP_KEEP_DAILY=14
BACKUP_KEEP_WEEKLY=8
BACKUP_WORKSPACE=true
BACKUP_WORKSPACE_KEEP=3
BACKUP_WORKSPACE_MAX_MB=2048
```

The separate scheduler task runs once per local date at or after BACKUP_TIME, catches up on
restart, and retries a failed attempt after an hour. Long model jobs do not delay that task.
Shutdown waits for an already started backup thread before closing Memory or releasing the
restore lock. Manual backups also satisfy that day's scheduled backup.

```sh
python -m agent backup-now
```

No model client is built. Normal Memory startup upgrades still apply; if this command opens
an older database it first makes a pre-migration snapshot. The SQLite online backup uses a
separate read connection, checks integrity, compresses, fsyncs, and atomically publishes
`BACKUP_DIR/db/agent-YYYYMMDD-HHMM.db.gz`. Names use UTC; a repeat within the same minute
replaces that minute's snapshot atomically. Retention keeps the newest snapshot on each of
the latest 14 UTC dates, plus the newest snapshot in each of the latest 8 ISO weeks.
Overlapping daily and weekly selections are stored once. Pre-migration files and unrelated
files are not pruned. A second backup process is refused while the backup lock is held.

Workspace archives use `BACKUP_DIR/workspace/workspace-YYYYMMDD.tar.gz` and keep the newest
3; another backup on the same UTC day atomically replaces that day's archive. They exclude
`.trash/`, `.uploads-tmp/`, and `.sandbox-home/.cache/`. Symlinks are stored as links, never
followed. Sockets, FIFOs and devices are omitted; hard links are refused. Files are read
through directory descriptors with O_NOFOLLOW, including after a hostile symlink swap.
The size limit sums regular-file bytes as files are archived. An oversized workspace is
skipped while the database backup succeeds; the audit entry records `skipped_over_limit`.
A workspace archive is a live file copy, not a filesystem snapshot; concurrent deletions or
shortened files can fail that backup safely without replacing its previous archive.

Success/failure updates `last_backup_ok`/`last_backup_error` in meta and `/api/status` and
writes a redacted audit entry. Stored errors include only the error class, without paths
or exception values. The core checkpoints WAL after a successful backup. A busy checkpoint
does not invalidate the consistent snapshot. Admin backup API/UI remain M2 work.

To restore, first stop the core and any chat/run-jobs/backup command. Keep the previous files
until you have checked the result:

```sh
python -m agent restore /srv/roland-agent/backups/db/agent-YYYYMMDD-HHMM.db.gz
python -m agent migrate --check
python -m agent audit-verify
```

The command holds exclusive `/data/.serve.lock` access (the path uses DATA_DIR); normal
serve/chat/jobs/backup commands hold a shared lock for their lifetime. A retained lock file
with no process holding it does not block restore. Restore accepts a compressed SQLite
agent database, verifies integrity and foreign keys, applies migrations and checks its
audit chain on a private temporary copy. Future schemas and invalid/tampered snapshots are
refused before touching current data. Old login sessions are removed and a restore audit
entry is added to the staged copy. Existing `agent.db`, WAL/SHM/journal files are moved to
`agent.db.pre-restore-<UTC timestamp>*` before installing it. Caught rename/install failures
put those old files back. Restoring requires a fresh web login. Workspace restoration is
manual and is not part of this command; inspect archived symlinks before extracting an
archive to an empty directory.

## Health and token utilities (M1.6)

```sh
python -m agent healthcheck
python -m agent gen-token
```

Healthcheck returns exit 0 only for HTTP 200 with exactly `{"ok":true}` from the configured
literal bind address and PORT (wildcard binds become loopback). It supplies the allowed
web Host header, ignores environment proxies, refuses redirects and has a three-second
HTTP timeout. It does not create a database, upgrade it or call the model. `/healthz` is
public and minimal; M2 Caddy/firewall work restricts its external reachability.

Gen-token prints a cryptographically random, URL-safe token encoding 32 bytes. It reads no
configuration and creates no database or file. Redirect its output directly to a private
secret file when configuring the later service containers; never commit the token.
