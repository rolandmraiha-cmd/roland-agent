# roland-agent — master status

> Snapshot: 2026-10-07. Verify against remote `v2` before coding.

## 1. What we are building

A private HTTPS agent on Roland's Contabo VPS with:

1. Password-protected web chat, persistent chats/facts/jobs, streamed replies  
2. Self-hosted open-weight model only (no third-party inference)  
3. Later: isolated terminal sandbox, workspace files UI, persistent Chromium + live screen for **human** sign-in, code-enforced approval gate for consequential actions  
4. Later still: optional persona + scrubbed training on a **separate** GPU machine (never the VPS)

Default model: **Qwen3-4B-Instruct-2507 Q4_K_M** via llama.cpp on CPU. Small local model — measure quality/speed/RAM on Contabo before calling it production-ready.

“24/7” means durable services and recoverable state, not unbounded autonomous action while Roland is away.

## 2. Working rules

1. Branch from latest remote `v2`; PR targets `v2`. Never push to `main` until M9 release.  
2. **Code Shipper reviews before merge**; Roland approves each PR. A prior merge does not approve the next one.  
3. No secrets in the repo, no telemetry, no hosted model APIs, no obfuscated code.  
4. Shell / browser / screen / training stay off in production until their milestones ship.  
5. Docs and instruction markdown: draft → **Roland approves text** → then PR.  
6. Do not run host-mutating deploy commands against Contabo without Roland's go-ahead and `APPLY=1`.

## 3. Done on `v2` (merged)

| Area | Result | Trace |
|---|---|---|
| v1 | Web chat, SQLite, tools, jobs, caps | PRs #1–#6 |
| M0 | Proxy trust, tool-step cap, job overrun, UI fixes | #7, #8, #14 |
| M1a | CI, ruff, locks, Makefile | #12, #15 |
| M1 | File secrets, local model URL guard, migrations, audit, backups, CLI | #16–#18 |
| M2 web | Peer allowlist, auth middleware, cookies, CSP | #19 |
| M2 edge | Caddy + core compose, limits, only Caddy publishes 80/443 | #20 |
| M2 model runtime | Isolated llama.cpp, installer, CI probes | #21 |
| M2 providers | Local brains, grammar actions, context budget; `test_no_hosted_llm` in unit CI | #24 → tip **`07340cc`** |

- DB schema version **2**. App version still **0.1.0** (bump to 2.0.0 at M9).  
- Contabo: VPS exists and is healthy; **`/opt/roland-agent` empty** — nothing deployed.  
- Keep PR #21; do not revert.

## 4. Not done (do not document as available)

- **M2.5–6:** deploy scripts + Makefile targets on `v2-m2-deploy` (this PR). `model-bench` deferred.
- Dedicated CI **job** `no-hosted-llm`: deferred until credentials have `workflow` scope. Unit/pytest already runs `tests/test_no_hosted_llm.py`.
- Contabo acceptance checklist (§6) numbers  
- M3 approval gate · M4 sandbox · M5 files UI · M6 browser · M7 screen/sign-in · M8 persona/training · M9 release  

**Known gaps to fix in coding (not docs):**

- ~~Core legacy OpenAI-compatible client~~ replaced by `agent/models` (llama.cpp / Ollama over httpx).  
- `deploy/model_store.py`: writing `registry.json` before `current` can leave a stuck incomplete registry (fix before Contabo model-fetch). Prefer `--network none` for install-only.  
- `SECRET_ENV` in `tools.py` omits `MODEL_SERVER_TOKEN` — fix before M4 enables shell.  
- ~~Compose `MODEL_MAX_CONCURRENCY` / core semaphore~~ wired via `config.model_max_concurrency`.

## 5. Commands that exist (complete)

### Make

```bash
make lint
make fmt-check
make test                 # pytest + frontend node tests (includes test_no_hosted_llm)
make test-integration     # real edge/model Docker; disposable fixtures; not production
make build
make compose-config
make preflight-edge       # read-only partial edge checks only
make preflight            # full host preflight (read-only; APPLY=1 only to write AGENT_HOST)
make secrets              # create missing secrets/*; never prints values; APPLY=1 to chown 1000
make hash-password        # writes secrets/agent_password_hash (FORCE=1 to overwrite)
make firewall             # dry-run; APPLY=1 installs iptables/nft rules + systemd unit
make workspace-fs         # dry-run; APPLY=1 creates 10G loop FS (never reformats)
make deploy               # APPLY=1 required: preflight → build → up → smoke
make ship HOST=… REF=v2   # APPLY=1: remote git pull + make deploy
make verify               # PASS/FAIL checklist; skips unavailable checks
make backup / restore FILE=… / restore-test FILE=…
make migrate-v1-workspace # APPLY=1; fresh Contabo usually skips
make model-fetch MODEL=qwen3-4b-q4km
make model-install FILE=/absolute/path.gguf ID=qwen3-4b-q4km
# make model-bench        # deferred
```

Host-mutating steps need `APPLY=1`. No live Contabo/DNS/ACME from this PR. `make model-bench` is deferred.

### CLI (`python -m agent …`)

`hash-password` · serve (default) · `chat` · `migrate --check` · `audit-verify` · `backup-now` · `restore <file.db.gz>` · `healthcheck` · `gen-token`

### Local dev setup

```bash
git clone --branch v2 https://github.com/rolandmraiha-cmd/roland-agent.git
cd roland-agent
git fetch origin && git switch v2 && git pull --ff-only
python3.12 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock
pip install --no-deps --no-build-isolation --no-index -e .
cp .env.example .env
# localhost HTTP: COOKIE_SECURE=false; set MODEL_BASE_URL to a local server if chatting
python -m agent hash-password
make lint && make test
```

Never set `ALLOW_SHELL=true` on a personal computer.

### Compose services today

- **caddy** — only published ports: 80/tcp, 443/tcp, 443/udp  
- **core** — private edge IP; data + backups volumes; workspace bind  
- **model** — internal `model` network only; read-only weights; `model_server_token`  

Before any real `docker compose up`: uid **1000** / mode **0400** secret files under `secrets/`; secrets dir and workspace dir mode **0700** uid 1000; `.env` from `.env.example` with non-secret values (`ACME_EMAIL`, hosts, etc.).

## 6. Model runtime

| Item | Value |
|---|---|
| Catalogue default | `qwen3-4b-q4km` (~2.5 GB); hashes in `deploy/models.lock` |
| CI-only | `test-tiny` (not an assistant) |
| Image pin | `docker/model/VERSION` → llama.cpp `server-b11434` digest |
| Context | `MODEL_CTX` ≤ **6144** (hard-capped in `docker/model/run.sh`) |
| Mem limit | Compose default **3840m**; acceptance peak ≤ **3600 MiB** |
| Isolation | No published port, no egress, digest-pinned, read-only weights |

### Contabo acceptance checklist (all Pending)

| Check | Required | Actual |
|---|---|---|
| Install size + SHA-256 match catalogue | Pass | Pending |
| Authenticated reply via **agent** path (after M2.10–13) | Pass | Pending |
| Peak model memory at ctx 6144 | ≤ 3600 MiB; else `MODEL_CTX=5120` and remeasure | Pending |
| Prompt tok/s (short and ~4096), generation tok/s, TTFT | Record | Pending |
| Soak + restart recovery | Healthy | Pending |
| No model egress / no published model port | Pass | Pending |
| Free RAM with caddy+core+model | Record; keep headroom before browser | Pending |

**Rules:** do not raise `MODEL_MEM_LIMIT` to “make it fit.” Do not enable browser until this table is filled. If model + browser cannot leave ~800–1200 MiB free, prefer Contabo **12 GB**. Take Contabo’s snapshot before first live deploy.

## 7. Security — implemented vs pending

### Implemented

Auth (argon2 hash, secure/`__Host-` cookie, CSRF/Origin, login rate limits), peer allowlist, hardened forwarded-for, CSP without `unsafe-inline`, Caddy-only public ports, local-only model transport with DNS pin and redirect refusal, isolated model container, verified installer, migrations, redacted audit chain, local backups, feature flags forcing shell/browser/screen/training off, hash-locked deps and digest-pinned images.

### Pending

full host preflight/firewall/secrets/deploy · Contabo measurements · M3 gate · M4 sandbox · M5 files · M6 browser · M7 screen/sign-in · M8 training · M9 release review.

Grammar/constrained decoding is **formatting**, not authorization. Untrusted tool/web text can still try to influence the model; agent-created jobs need Approve.

## 8. Next coding order (team agreement)

1. Optional small PR: model-store interrupted-install recovery (+ install without egress)  
2. **M2.10–13** on e.g. `v2-m2-provider` → PR to `v2` (Shipper review, Roland merge)  
3. M2 deploy baseline scripts that match reality  
4. Contabo: secrets, model-fetch, fill checklist above, smoke  
5. Then M3 → M4 → M5 → M6 → M7 → M8 → M9  

Hold until Roland names who codes the next slice (Code Builder vs Codex via AI Relay).

## 9. Host facts (verify before use)

| Item | Recorded target |
|---|---|
| Provider / OS | Contabo, Ubuntu 24.04 |
| Size | ~4 vCPU / ~8 GB RAM / ~100 GB disk |
| Public IPv4 | `37.60.226.214` |
| Fallback HTTPS | `37-60-226-214.sslip.io` |
| Intended paths | `/opt/roland-agent`, workspace `/srv/roland-agent/workspace` |
| Compose mem today | model 3840 + core 640 + Caddy 96 = **4576 MiB** |

## 10. Spec vs this document

Repo `docs/v2-spec.md` is the **detailed target** (long). This **AGENT.md** is the **living status**. If they disagree: Roland's latest decision → this file's Done/Not done → then the spec task list.

## 11. Code map (current)

| Path | Role |
|---|---|
| `agent/config.py` | Config, secrets, startup policy |
| `agent/brain.py`, `agent/core.py` | Legacy local client + tool loop (**next rewrite**) |
| `agent/models/endpoint_guard.py` | Local-only transport (keep) |
| `agent/tools.py`, `scheduler.py`, `memory.py`, `audit.py`, `backup.py` | Tools, jobs, persistence |
| `agent/web/` | FastAPI + static UI |
| `docker-compose.yml`, `docker/caddy/`, `docker/model/` | Edge + model |
| `deploy/model.sh`, `model_store.py`, `models.lock`, `preflight_edge.py` | Installer + partial edge preflight |
| `deploy/{preflight,secrets,workspace-fs,firewall,deploy,ship,verify,restore,hash-password}.sh` | M2.5 host deploy baseline (APPLY=1 for mutations) |
| `tests/` | Unit, frontend, edge/model integration |

## 12. Last verified tests (handoff)

Remote `v2` tip **`07340cc`** (#24). Locally rechecked after M2.5–6 scripts: re-run `make test` before claiming. CI TinyStories does **not** prove Qwen RAM or Contabo speed.
