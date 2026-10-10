# roland-agent

Roland's single-user assistant, running all day on a Contabo VPS. Chat through its own
password-protected web page on a phone or computer. The model runs on the same server;
chats, files and browser snapshots are never sent to a hosted inference service.

**2.0.0 — accepted 10 October 2026.** The release is validated on Contabo at
`7ce7c2c`: verify 12 pass / 0 fail / 2 manual categories, schema 3 current, valid 604-row
audit and 3820 MiB idle available RAM. The real 768 MiB workspace download is complete:
its full size, SHA-256 and ZIP CRC passed verification. The 180-second watch recorded
core at 87.99 / 640 MiB sampled peak, minimum host headroom 3817 MiB, and zero OOM kills
or restarts across all services.

All seven jobs passed on that deployed head:
[CI](https://github.com/rolandmraiha-cmd/roland-agent/actions/runs/38048061008) and
[CPU/model switch](https://github.com/rolandmraiha-cmd/roland-agent/actions/runs/38048060939).
[Release PR #66](https://github.com/rolandmraiha-cmd/roland-agent/pull/66) records the final
documentation-head checks, review and the main merge Roland authorized. Full M9 acceptance
at `bb76263`, browser/screen/tool smoke at `7b3de9d` and the streaming-fix deployment are
preserved with their measured scopes in [the acceptance note](docs/releases/m9-acceptance-2026-10-09.md).

[Fix PR #70](https://github.com/rolandmraiha-cmd/roland-agent/pull/70) streams downloads from
validated file descriptors in 64 KiB chunks and bounds metadata hashing. Chunked HTTP
framing handles concurrent truncation. The extra feature branches and temporary cleanup
workflows are removed. Release PR #66 merged into `main` at `9beaf6e`.
Use `main` for deployments and as the base of future feature PRs. Roland requested
retirement of the fully merged `v2` branch; the cleanup PR records its guarded deletion.
The branch/CI cleanup changes no runtime code and needs no server rebuild.
Training stays off; [NEXT](docs/NEXT.md) preserves the completed plan and follow-ups,
including [Ollama fallback #68](https://github.com/rolandmraiha-cmd/roland-agent/issues/68)
and [directory metadata #69](https://github.com/rolandmraiha-cmd/roland-agent/issues/69).

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
accepted on this VPS. RAM prompt caching is disabled.

The M9 fixed-prompt benchmark recorded **18.1–19.8 prompt tokens/s**, **6.7–9.9 generated
tokens/s**, and **2.2–3.5 seconds to the first token** over three uncached 128-token samples.
These rates describe that benchmark, not a complete tool task. The loaded ten-minute watch
and thirty-minute soak recorded minimum available RAM of **3746 MiB** and **3705 MiB**, with
no OOM kills or container restarts. Full sample timings and service peaks are in
[the acceptance record](docs/releases/m9-acceptance-2026-10-09.md).

A longer chat gave an unsupported browser answer without running a browser tool. Direct
inspection and fresh-chat tool tests confirmed execution and the real browser state. In the
final smoke the page title was correct, while text visible as a paragraph was labelled a
heading. Accurate action and content reporting remain follow-ups; the approval gate still
needs human judgment.

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
git clone --branch main https://github.com/rolandmraiha-cmd/roland-agent.git
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

Build fix PR #67 is merged at `6146038`. All seven jobs passed at that merged `v2`
integration tip: [CI](https://github.com/rolandmraiha-cmd/roland-agent/actions/runs/37994798247)
and [CPU/model switch](https://github.com/rolandmraiha-cmd/roland-agent/actions/runs/37994798251).
Verified Docker Official Image copies on Amazon ECR Public bypass the observed Docker Hub
metadata, pull-limit and token failures. Caddy remains `2.11.7-alpine`; its current index
`d8542f48…` and the earlier index select the identical Linux/amd64 image `173b2630…`.
All six Python builds retain the complete `dddfd7e0…` index and base contents. Manifest
hashes, the exact mirror namespace, versions, digest pins and non-root policy were checked.
Host controls, resource limits and training settings are unchanged.
The unchanged merged `v2-m9-caddy-index` branch was deleted by guarded
[cleanup run 37994793408](https://github.com/rolandmraiha-cmd/roland-agent/actions/runs/37994793408);
its temporary workflow is removed. Release PR #66 records checks on the final cleanup head
and the focused deploy. The tested host checkpoint remains `bb76263`.


[Master status and rules](docs/AGENT.md) · [Remaining release work](docs/NEXT.md) ·
[Technical specification](docs/v2-spec.md) · [Changelog](CHANGELOG.md).
Every PR updates README, AGENT and NEXT for changed code, plans or deployment state.
M9 host acceptance is recorded in the approved note. PR #65 is merged; release PR #66
tracks final CI, the focused polish deploy and release review. The model-reporting, phone and training-data follow-ups are listed
in NEXT; training and the current production limits remain unchanged.
