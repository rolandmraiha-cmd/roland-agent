# roland-agent

Roland's single-user assistant, running all day on a Contabo VPS. Chat through its own
password-protected web page on a phone or computer. The model runs on the same server;
chats, files and browser snapshots are never sent to a hosted inference service.

**2.0.0 release candidate.** M0–M8 are merged on `v2`; Roland accepted M6/M7 on 8 October
and M8 on 9 October 2026. Roland authorized merging M9 PR #64 on 9 October; it is merged
into `v2` at **`720c58c`**. All seven candidate CI jobs passed at `afee233`; CI on the
updated integration tip is required too. Production still runs `7ef26de` with training off.
Next: Roland deploys `v2`, completes the M9 server checklist, and supplies results for any
improvements before the reviewed `v2` → `main` release merge. Host acceptance is pending.

## What it does

- Streams replies and keeps chats, facts and approved scheduled jobs in SQLite.
- Runs shell commands in a separate container and offers upload, download and file trash/restore.
- Drives a persistent Chromium browser, reads pages, fills ordinary fields and takes screenshots.
- Shows that same browser through a private screen for watching or taking control.
  Roland types site passwords himself; the agent pauses during sign-in.
- Offers persona versions and feedback. An opt-in pipeline can scrub labelled examples,
  train on a separate machine, evaluate a candidate and request a model change.

The UI uses ordinary HTML/CSS/JavaScript, without a frontend build toolchain. Services,
state, internal networks, limits, secrets and backups are managed through Docker Compose.

## Safety in plain words

The model's suggestions are untrusted. Code decides what may run. Consequential actions
need a specific approval card; payments, messages, public posts and deletion need a second
tap. Writing “yes” in chat never approves anything. Reading external content escalates
approval requirements for commands, memory changes and file overwrites. Scheduled jobs
cannot approve their own actions or ask for a sign-in.

Only Caddy publishes ports. Core, shell, browser, screen relay and model have separate
containers and networks. The model has no internet connection. Site cookies stay in the
browser's own volume; screenshots go to Roland's UI rather than the text-only model.

There are limits: a misleading site can fool a small model or hide consequences behind a
GET link or a background request. Roland accepted background POSTs on 8 October. A host
compromise can expose the stored data and browser profile. Read [security details](docs/SECURITY.md)
before relying on the agent for a consequential task. The approval card needs human judgment.

## Model and speed

Default: **Qwen3-4B-Instruct-2507**, Apache-2.0, Unsloth Q4_K_M GGUF (about 2.5 GB),
served by the pinned llama.cpp image on CPU. Thinking is off by default. It is a small model
with modest reasoning and tool accuracy; a multi-step task may take minutes.

Contabo uses **3072 context tokens, 3 threads and a 3840 MiB model cap**. Keep these host
settings. Repo defaults still specify 4096 context tokens; that higher setting has not been
accepted on this VPS. RAM prompt caching is disabled. The previous browser-task watch
recorded model peak 3084.29 MiB, browser peak 822.80 MiB and minimum host available 3553 MiB,
without observed restarts or OOM kills (8 October). These are earlier M6 measurements.

M9 prompt/generation tokens per second and time to first token have **not been measured on
Contabo yet**. Run the fixed public-prompt benchmark and loaded browser watch in the
[runbook](docs/RUNBOOK.md), then record results in [master status](docs/AGENT.md).

## Training stays off for this release

Capture, weekly training, the trainer endpoint and the `training` profile stay off through
2.0.0, by Roland's decision on 9 October. Training data storage/backup policy, capture/export
smoke and a paid GPU run wait until afterwards. The pipeline has automated CPU and
model-switch tests; it has not trained on Roland's data on the host.

Training uses labelled, scrubbed examples and seed replay on a separate machine. Import
checks integrity and evaluation against the current model. Import and scheduling never
switch the serving model. Roland must review and approve every promotion; failed switch
checks restore the previous version. Pattern scrubbing cannot remove every identifying
sentence. See [model operations](docs/MODEL.md) and [GPU instructions](training/README.md).

## Deploy and operate

Follow [docs/RUNBOOK.md](docs/RUNBOOK.md) for a fresh install, an update, backup/restore,
model install, promotion/rollback, domain switch and the release acceptance checklist.
Host changes require `APPLY=1`. M9 does not change production flags, DNS, memory limits
or the current model. Known phone screen-opening and mobile-data/DNS problems remain
deferred until after 2.0.0; they are recorded in [the handoff](docs/NEXT.md).

## Develop without Docker

Use Python 3.12 and Node 20 or newer. Start a local llama.cpp server separately on
`127.0.0.1:8080` with your owned GGUF. No model is needed for unit tests.

```bash
git clone --branch v2 https://github.com/rolandmraiha-cmd/roland-agent.git
cd roland-agent
python3.12 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock
pip install --no-deps --no-build-isolation --no-index -e .
cp .env.example .env
python -m agent hash-password
```

Put the hash printed by that command in your private `.env`, then set:

```dotenv
AGENT_ENV=development
AGENT_HOST=localhost
ALLOWED_HOSTS=localhost,127.0.0.1
HOST=127.0.0.1
PORT=8000
COOKIE_SECURE=false
MODEL_BASE_URL=http://127.0.0.1:8080
DATA_DIR=./data
WORKSPACE_DIR=./workspace
BACKUP_DIR=./backups
TRAINING_DATA_DIR=./data/training
ALLOW_SHELL=false
SHELL_BACKEND=local
BROWSER_ENABLED=false
SCREEN_ENABLED=false
TRAINING_CAPTURE=false
TRAINING_LOOP_ENABLED=false
TRAINER_URL=
COMPOSE_PROFILES=
```

```bash
python -m agent                 # open http://localhost:8000
make lint
make test                      # Python + frontend; Docker checks need its CLI
make test-integration          # disposable GitHub CI edge/model/relay fixtures
make test-browser              # real browser + screen against a local fixture site
```

`SHELL_BACKEND=local` runs as your own user when enabled, so leave shell off on a personal
computer. Never set `SANDBOX_REAP_ALL` on a host. CI runs lint, unit, frontend,
`no-hosted-llm`, edge/model/restore, live browser/screen/sandbox and CPU training/model swap.

## Project records

[Master status and rules](docs/AGENT.md) · [Remaining release work](docs/NEXT.md) ·
[Technical specification](docs/v2-spec.md) · [Changelog](CHANGELOG.md).
Every PR updates README, AGENT and NEXT for changed code, plans or deployment state;
accepted security limits also update SECURITY. Roland authorized the implementation/docs
merge on 9 October; actual deployment and final
release acceptance are still pending. Local M9
validation after the review fixes passed 1526 Python tests and all 88 frontend tests; 58 Docker CLI checks and
one existing hard-link check were skipped, with 49 live cases excluded. Ruff/ShellCheck
pass. Live CI caught an unexpected IPv6 listener in the screen server; M9 closes it and
retains the strict socket check. Verification reads Compose's resolved feature settings.
All seven candidate CI jobs passed after these fixes; see AGENT for evidence and the
integration-tip acceptance requirement. A temporary guarded maintenance workflow is
removing the merged `v2-m9-release` branch and will be removed immediately afterwards.
