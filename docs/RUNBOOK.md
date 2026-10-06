# v2 foundations runbook

This document covers the v1 fixes, CI, M1.2 configuration and M1.3 persistence on `v2`. The final Linux
deployment, isolated shell, browser and live screen milestones remain unimplemented.

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

For an automatic snapshot before `serve` upgrades a database already at version 1 or higher,
set `BACKUP_DIR` to an existing private directory accessible by the core. It writes
`BACKUP_DIR/db/pre-migrate-v<from>-to-v<to>-<timestamp>.db.gz` using SQLite's online backup
API, checks integrity, compresses to a private temporary file, fsyncs and atomically renames
it. The file has mode 0600. Failure stops startup before the upgrade. This hook is off when
`BACKUP_DIR` is empty or its directory does not exist. The old development compose does not
mount that directory automatically. Nightly backups, retention and restore are separate work.

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
