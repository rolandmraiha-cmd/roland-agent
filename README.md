# roland-agent

Roland's own always-on AI agent. You chat with it on its own password-protected web page, it
remembers things, uses tools, and runs scheduled jobs in the background while nobody is chatting.

The `v2` branch is under development. Its brain connects only to an approved **local model**,
such as llama.cpp or [Ollama](https://ollama.com). The current client uses native `/v1`
completions; the full v2 provider implementations come in M2.

The isolated terminal, browser, screen viewing and sign-in services are still planned.
See [RUNBOOK](docs/RUNBOOK.md) for the implemented edge, configuration and persistence stages.

## What it can do

- **Web chat page:** works on phone and computer. Replies stream in as the agent types, and old
  chats are kept.
- **Memory:** everything is stored in one SQLite file (`/data/agent.db`): chats, saved facts,
  jobs, job results and usage.
- **Tools:**
  - read public web pages
  - run shell commands (off unless you set `ALLOW_SHELL=true`; see the security notes)
  - read, write and list files in its workspace (`/data/workspace`)
  - save and forget facts
  - schedule, list and cancel jobs (jobs the agent makes wait for your OK)
- **Background jobs:** cron schedules in your time zone, e.g. `0 7 * * *` for every day at 07:00.
  Ask in chat ("every morning at 7, check X") or add one on the **Jobs** tab. A job the agent
  creates from chat stays off until you press **Approve** on the Jobs tab, so a web page can't
  trick it into setting up its own repeating task. The Jobs tab also shows what each run did,
  and lists the facts the agent has saved so you can delete them.
- **Safety:** a daily cap on model calls (`DAILY_CALL_LIMIT`) and a cap on tool steps per message
  (`MAX_TOOL_STEPS`).

## Docker edge foundation (M2.1–M2.2)

The root compose file now contains **Caddy and core**. Caddy is the only service publishing
ports (80/tcp, 443/tcp and 443/udp); core binds only to its private edge address. Both run
as uid 1000 with read-only roots, dropped capabilities, bounded resources and persistent
data. Caddy blocks public health/internal routes and proxies streamed replies without buffering.

This is a tested edge foundation, with the model container/providers, full Linux preflight,
firewall and workspace quota still to follow. Chat inference and the browser/screen/sandbox
services are not supplied by this compose slice. The old 8 GB Ollama development service is
removed. Use the native development instructions below for an already installed local model.

`.env.example` has no credentials. Compose mounts private `agent_password_hash` and
`model_server_token` files from `secrets/`, and requires an existing workspace bind directory.
Both secret files must be uid 1000/mode 0400, and their directory and the workspace must be
uid 1000/mode 0700. Full bootstrap/deployment commands arrive with the next deployment work.
The edge's read-only checks are `make compose-config` and `make preflight-edge`; `make build`
builds the pinned images. No `make deploy` is provided for this incomplete stage.

GitHub CI builds the real images and tests HTTPS login, CSRF, forwarded-address spoofing,
screen refusal, private storage permissions and restart persistence with disposable fixtures.
It uses only a localhost test CA and makes no model request. See the runbook for scope and limits.

## Run it without Docker (development)

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock
pip install --no-deps --no-build-isolation --no-index -e .
cp .env.example .env              # set MODEL_BASE_URL=http://localhost:11434/v1, DATA_DIR=./data
# For Ollama, also set MODEL_PROVIDER=ollama, MODEL_NAME to an installed tag and MODEL_TOOL_MODE=native.
# Local HTTP development also needs COOKIE_SECURE=false and BACKUP_DIR left empty.
python -m agent hash-password     # paste the printed line into .env
python -m agent                   # web page on http://localhost:8080
python -m agent chat              # or chat in the terminal
pytest                            # tests (no model needed)
```

Shell commands are off by default everywhere. Never set `ALLOW_SHELL=true` on your own computer.

## Putting it online

Final Linux installation is still pending. The implemented edge computes the hostname from
`AGENT_DOMAIN`, or `AGENT_FALLBACK_HOST` when no domain is set. Caddy uses ACME by default;
`CADDY_TLS=internal` is for a local test CA and is not publicly trusted. It passes the same
hostname to core and enforces secure cookies, production mode and trust of only its fixed
private proxy IP. Core has no published port. Shell, browser, screen and training flags
remain forced off until those services are implemented.

## Security notes

- **Login:**
  - Only an argon2 hash of the password is stored.
  - Sessions are random tokens, and only their hash is kept in the database.
  - The cookie is `HttpOnly`, `SameSite=Strict` and `Secure`.
  - A login ends after `SESSION_DAYS`, or after `SESSION_IDLE_HOURS` (default 72) unused.
  - Changing the configured password hash (the mounted file in Compose) and restarting logs out every device.
- **Wrong passwords:**
  - 5 wrong passwords from one address within 15 minutes lock that address for 15 minutes.
    Other addresses, like yours, can still log in. IPv6 is counted per /64 network, since one
    user usually holds a whole /64.
  - After 20 wrong passwords from everywhere together, each further wrong guess is slowed down,
    but nobody is locked out.
  - Password checks run one at a time, and at most 8 can wait; more get "busy, try again".
    Each address can have only one attempt in progress, and the delay after a wrong guess
    happens outside the queue, so wrong guesses don't hold up your login.
  - Restarting the agent clears all of this.
- **Cross-site requests:** every request that changes something must come from the page's own
  origin and carry the session's CSRF token in an `X-CSRF-Token` header. Strict security headers
  (CSP, no framing) are set, and `ALLOWED_HOSTS` limits which hostnames the page answers to.
- **Shell (off by default):**
  - It's off unless you set `ALLOW_SHELL=true`, and the agent logs a warning at startup when on.
  - When on, commands run as the agent's own user. A web page that tricks the model could then
    try to use a command against the agent itself, such as changing its database. Only turn it
    on if you accept that; v2 will move commands into a separate sandbox container.
  - In Docker it runs as a non-root user with no Docker socket, no host folders, no Linux
    capabilities and a read-only filesystem apart from `/data` and `/tmp`, with CPU, memory and
    process limits.
  - Each command stops after 60 seconds or 8000 characters of output.
  - Commands don't get model tokens or the password hash in their environment, and the agent
    process blocks other processes from reading its memory (`/proc/<pid>/environ`).
- **Untrusted tool output:** web pages, files and command output reach the model marked as
  untrusted data, and it's told never to follow instructions inside them. That lowers the risk
  but can't remove it, which is why agent-made jobs need your OK.
- **Web fetch:** local and private addresses (localhost, 10.x, 192.168.x, 169.254.x, IPv6 forms
  that wrap them, and so on) are refused and checked again on every redirect. The request then
  connects to the exact address that passed the check, so a DNS trick can't swap in a private one
  afterwards. Fetches never go through a proxy from the environment, and a whole fetch stops
  after 45 seconds.
- **Supply chain:** Docker installs dependencies from `requirements.lock` with checked hashes, and
  the base image is pinned to an exact digest.
- **Upgrades:** an older `agent.db` is updated on start. Jobs from before approvals need your
  OK once, and everyone logs in again.
- **Limits:** at most 3 model calls run at once (others wait), and the daily cap is counted in
  one database step, so parallel chats and jobs can't slip past it. The database uses WAL mode
  and waits for a busy lock instead of failing. Job runs cut off by a restart are marked failed.
- **Known limit:** with the shell on, a command runs as the agent's user and can reach its
  database. Production refuses this local backend. The isolated sandbox arrives in M4;
  startup refuses to enable it until implemented.

## v2 M1 configuration

- File-based secrets override environment values without logging their contents.
- Production checks refuse insecure cookies, missing web hosts, local shell execution and
  incomplete service secrets. `ALLOWED_HOSTS` defaults to `AGENT_HOST`.
- Model connections use approved local hosts only. DNS is checked for every request and the
  checked address is pinned. Environment proxies, redirects and public destinations are refused.
- Browser, screen and sandbox integrations remain unavailable; enabling them stops startup.
- New settings for later milestones are parsed foundations. They do not activate those features.
- Versioned upgrades, redacted audit records and optional nightly backups are described below.

## v2 M1 database migrations

Opening the agent's database applies numbered upgrades automatically. Each upgrade and its
version number commit together; a failure rolls back that upgrade. Existing v1 chats, facts,
jobs, results, usage and sessions with idle timestamps are preserved. Very old jobs still need
approval, and sessions from before idle tracking still require a fresh login, as in v1.

Inspect the database without creating it or applying upgrades:

```sh
python -m agent migrate --check
```

The target schema is version 2. New tables and storage helpers prepare approvals, action
history, file metadata, sign-in requests and screen sessions. They do not activate those
features or add new web routes. Text-only chat history keeps its existing format.

When `serve` upgrades an existing database (including unversioned v1) and `BACKUP_DIR` points
to an existing directory, it first writes a private compressed snapshot under `BACKUP_DIR/db`.
Fresh empty databases do not need a pre-upgrade snapshot.

## v2 M0 fixes

The `v2` branch starts with fixes to the v1 agent; the later v2 services are not installed yet.

- `FORWARDED_ALLOW_IPS=*` is rejected. List the IP or CIDR of your actual reverse proxy.
  The right-most untrusted forwarded hop identifies the visitor. If all hops are trusted,
  the direct peer is kept and a warning is logged once.
- A migrated job's origin is described as unknown; it still needs approval before running.
- `MAX_TOOL_STEPS` limits tool rounds. The last model call can provide a final answer but
  cannot execute another tool round.
- A job that overruns its cron interval waits for a future scheduled time after it finishes.
- Deleting the open chat suppresses errors from its pending history request.

M0 preserved the original model setup; M1 now restricts it to local destinations. Tests use
fake models and a loopback HTTP fixture, without real inference calls. See
[RUNBOOK](docs/RUNBOOK.md) and [SECURITY](docs/SECURITY.md) for validation and the regex audit.

The M1.4 audit foundation records existing login/logout, job and fact-deletion events with
loaded-secret redaction and an append-only SHA-256 chain. Use `python -m agent audit-verify`
to inspect it read-only without making a model request. Related state changes roll back if
logging fails. See [the runbook](docs/RUNBOOK.md#audit-log-m14) for limits and verification.

Optional local nightly backups run at `BACKUP_TIME` in `TIMEZONE` once `BACKUP_DIR` is set.
They retain daily/weekly SQLite snapshots and bounded workspace archives. Use
`python -m agent backup-now` for a manual backup and `python -m agent restore <file.db.gz>`
after stopping the server and database-writing commands. Restore verifies and upgrades a
temporary copy before replacing the database, preserves its previous files, and clears old
login sessions. See [backup and restore steps](docs/RUNBOOK.md#backups-and-restore-m15).

`python -m agent healthcheck` checks the minimal local `/healthz` endpoint without a model
request. `python -m agent gen-token` prints a newly generated 32-byte service token; keep it
in a private secret file. No token or other credential is supplied in the repository.
CI runs separate lint, Python unit and frontend jobs. The isolated Linux deployment,
terminal, browser and screen are still later milestones.
