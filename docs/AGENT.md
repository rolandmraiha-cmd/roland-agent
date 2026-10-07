# roland-agent — master status

> Snapshot: 2026-10-07. Docs base **`a27b5ff`** (#37) plus the M6 fixture site (#38) and **M6 part 1** (#39: core-side browser code, dormant). M5 code **`98971cc`** (#32). Verify against remote `v2` before coding.
>
> **Standing rule:** every PR, every edit on that branch, and every squash merge updates this file, `docs/NEXT.md`, and `README.md` in that same PR before merge when code, deploy state, plans, or instructions change. Do not leave plans only in chat. Update `docs/SECURITY.md` in that PR when an accepted limit changes. After squash-merge, the tip line names the new `v2` tip.

## 1. What we are building

A private HTTPS agent on Roland's Contabo VPS with:

1. Password-protected web chat, persistent chats/facts/jobs, streamed replies.
2. Self-hosted open-weight model only (no third-party inference).
3. A code-enforced approval gate, an isolated terminal sandbox and a workspace files UI (shipped).
4. Next: a persistent Chromium the agent drives (M6; core side merged and dormant, browser service not built) and a live screen of that same browser for Roland's own sign-ins (M7).
5. Later: persona editing and scrubbed, opt-in training on a **separate** GPU machine, never the VPS (M8), then the 2.0.0 release (M9).

Default model: **Qwen3-4B-Instruct-2507 Q4_K_M** via llama.cpp on CPU. It is a small local model; quality and speed are modest.

"24/7" means durable services and recoverable state, not unbounded autonomous action while Roland is away.

## 2. Working rules

**Names:** **roland-agent** = this Contabo product. **Code Builder / Code Shipper / Crew Chief** = Roland's **Grok Bot** teammates in group chat THE SCAM CALL CENTER (not this app's runtime). An AI on another platform is not those bots unless Roland assigned it that role for the job — see `docs/NEXT.md` §0.

1. Branch from the latest remote `v2` (`v2-m<N>-<slug>`); PRs target `v2`. Never push to `main` until the M9 release PR, which Roland merges.
2. When Grok bots do the work: **Code Builder (Grok)** codes → **Code Shipper (Grok)** reviews and Contabo-smokes → Shipper merges to `v2`. An external AI codes only if Roland directed it; it does not inherit Contabo/merge rights from the Grok room.
3. Roland's standing rule for **Code Shipper (Grok)**: may merge after a passing review and smoke without waiting. Pings Roland first for substantial manual tests and for destructive or host-level changes (DNS, deletes, memory raises, paid GPU, model promotion).
4. Code Builder and Code Shipper (Grok) share one GitHub identity, so Shipper leaves a **COMMENT** review, not a formal APPROVE.
5. No secrets in the repo, no telemetry, no hosted model APIs, no obfuscated code.
6. Browser / screen / training stay off in production until their milestones are merged and smoked.
7. Host-mutating commands need `APPLY=1` and must follow the rules above.
8. Docs stay current on every change. Anyone (human or AI) who changes code, deploy state, plans, or instructions must update `docs/AGENT.md`, `docs/NEXT.md`, and `README.md` in the **same PR**, before squash-merge. Every edit on that branch, including review fixes, updates those files again if the story changed. A squash merge does not replace that update and is not a reason to leave docs for a follow-up. New plans go into `docs/NEXT.md` (or AGENT.md next-order) — not only in chat. Instruction set is one master `docs/AGENT.md`, a short `README.md`, and `docs/NEXT.md`. Large instruction-MD replacements still follow draft → **Roland approves the text** → PR; routine status updates that match shipped work ship with the feature PR after **Code Shipper (Grok)** review. Update `docs/SECURITY.md` in that same PR when an accepted limit changes.
9. Roland's **Grok** bots in THE SCAM CALL CENTER are **parked** until he unpauses them. That only pauses those Grok bots — it does **not** freeze the project. An external AI Roland hands `docs/NEXT.md` to may keep working. Contabo SSH / DNS / memory raises still need Roland's explicit OK.

## 3. Done on `v2` (merged)

| Area | Result | Trace |
|---|---|---|
| v1 | Web chat, SQLite, tools, jobs, caps | #1–#6 |
| M0 | Proxy trust, tool-step cap, job overrun, UI fixes | #7, #8, #14 |
| M1a | CI, ruff, locks, Makefile | #12, #15 |
| M1 | File secrets, local model URL guard, migrations, audit, backups, CLI | #16–#18 |
| M2 web | Peer allowlist, auth middleware, cookies, CSP | #19 |
| M2 edge | Caddy + core compose, limits, only Caddy publishes 80/443 | #20 |
| M2 model runtime | Isolated llama.cpp, verified installer, CI probes | #21, #23 (model-store bootstrap recovery, isolated install network) |
| Docs | Short README + master AGENT.md | #22 |
| M2 providers | Local brains, grammar actions, context budget, `test_no_hosted_llm` | #24 |
| M2.5–6 deploy | Host deploy scripts + Makefile targets (`APPLY=1` guarded) | #25 |
| Contabo fixes | Thinking stuck, tool-loop and duplicate failed-tool fixes | #27, #28 |
| M3 gate | Approve risky tools in the UI before they run; Shipper nits | #29, #30 |
| M4 sandbox | `sandboxd` container, `SHELL_BACKEND=sandbox`, shell classifier | #31 |
| M5 files | Workspace + Files UI/API, trash and restore (A5.4 is the Contabo smoke; #32 title still says A5.1–A5.3) | #32; docs tip **`5104fb6`** |
| M6 part 1 (core side, **dormant**) | Click classifier, `browserd` client, 16 browser tools with gate policies, element fingerprint pinned to approvals, screenshot on approval cards; A6.1 and A6.2 green. Not usable: no `browserd` yet | #39 |

- M6.4 fixture site exists at `tests/fixtures/site/server.py` with the `fixture-web` test service (#38); browser integration remains pending.
- Deployed and live on Contabo: https://37-60-226-214.sslip.io/ with `caddy`, `core`, `model`, `sandbox` healthy.
- DB schema version **2**. App version still **0.1.0** (bump to 2.0.0 at M9).

## 4. Not done (do not document as available)

- **M6** browser (rest of it) · **M7** screen/sign-in · **M8** persona/training · **M9** release. Plan and acceptance: [docs/NEXT.md](NEXT.md).
- **M6 still missing:** `browserd/` (launcher, session, server, `snapshot.js`), `docker/browser/` image and Chromium policy, the `browser` compose service and secret, firewall rules, the live tests against the fixture site (A6.3, A6.4), the Browser tab, the Contabo memory measurement and smoke (A6.5). Until then `python -m agent` refuses `BROWSER_ENABLED=true` ("not implemented yet").
- Dedicated CI **job** `no-hosted-llm`: deferred until credentials have `workflow` scope. Unit CI already runs `tests/test_no_hosted_llm.py`.
- `make model-bench`: deferred.
- Full measured model acceptance table (§6): not yet recorded in the repo.
- Repo default `MODEL_CTX` is still 4096 (`docker-compose.yml`, `.env.example`) while Contabo runs 3072. Align after Roland decides (§6).

## 5. Commands that exist

### Make

```bash
make lint
make fmt-check
make test                 # pytest + frontend node tests (includes test_no_hosted_llm)
make test-integration     # real edge/model Docker; disposable fixtures; not production
make test-sandbox         # live sandbox stack; needs Docker + secrets/sandbox_api_token
make build
make compose-config
make up / down / ps / logs [S=service]
make preflight-edge       # read-only partial edge checks
make preflight            # full host preflight (read-only; APPLY=1 only to write AGENT_HOST)
make memory-report        # read-only browser headroom verdict; WATCH=seconds reports peaks
make secrets              # create missing secrets/*; never prints values; APPLY=1 to chown 1000
make hash-password        # writes secrets/agent_password_hash (FORCE=1 to overwrite)
make firewall             # dry-run rules
make firewall-install     # APPLY=1: install unit/script + apply rules
make workspace-fs         # dry-run; APPLY=1 creates 10G loop FS (never reformats)
make deploy               # APPLY=1: preflight → build → up → smoke
make ship HOST=… REF=v2   # APPLY=1: remote git pull + make deploy
make verify               # PASS/FAIL checklist incl. isolation; skips unavailable checks
make backup / restore FILE=… / restore-test FILE=…
make migrate-v1-workspace # APPLY=1; fresh Contabo usually skips
make model-fetch MODEL=qwen3-4b-q4km
make model-install FILE=/absolute/path.gguf ID=qwen3-4b-q4km
```

### CLI (`python -m agent …`)

serve (default) · `hash-password` · `chat` · `migrate --check` · `audit-verify` · `backup-now` · `restore <file.db.gz>` · `healthcheck` · `gen-token`

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

Never set `ALLOW_SHELL=true` on a personal computer. Never set `SANDBOX_REAP_ALL` on a host.

### Compose services today

| Service | Role | mem_limit |
|---|---|---|
| `caddy` | Only published ports: 80/tcp, 443/tcp, 443/udp; screen routes present but no backend yet | 96m |
| `core` | App, gate, tools, SQLite/backups volumes, workspace bind; `ALLOW_SHELL=true`, `SHELL_BACKEND=sandbox`; browser/screen/training flags `"false"` | 640m |
| `model` | Internal `model` network only; read-only weights; `model_server_token` | `${MODEL_MEM_LIMIT:-3840m}` |
| `sandbox` | `sandboxd` on `sandbox_ctl`; peer + Bearer `sandbox_api_token`; own egress network | 1g |

Config caps total 5600 MiB. Caps are not measured usage. Secret files under `secrets/` are uid **1000**, mode **0400**; secrets and workspace dirs mode **0700** uid 1000.

## 6. Model runtime

| Item | Value |
|---|---|
| Catalogue default | `qwen3-4b-q4km` (~2.5 GB); hashes in `deploy/models.lock` |
| CI-only | `test-tiny` (not an assistant) |
| Image pin | `docker/model/VERSION` → llama.cpp `server-b11434` digest |
| Context on Contabo | **`MODEL_CTX=3072`** in host `.env` (lowered after an OOM kill at 4096) |
| Context repo default | 4096 in compose/`.env.example`; hard cap 6144 in `docker/model/run.sh` |
| Mem limit | **`MODEL_MEM_LIMIT=3840m`** — never raise without Roland's OK |
| Isolation | No published port, no egress, digest-pinned, read-only weights |

**Open decision (Roland):** keep 3072 and align the repo default, raise the memory limit (costs browser headroom), or move to a larger VPS. Until decided, keep 3072/3840m.

**Rules:** do not raise `MODEL_MEM_LIMIT` to make things fit. Measure host memory with all live services under load before enabling the browser; if browser cap plus live usage leaves less than ~800 MiB available, stop and ask Roland. Planned full stack is ~6944 MiB (7072 with trainer) of ~7987 MiB. Take a Contabo snapshot before each new service goes live.

## 7. Security — implemented vs pending

### Implemented

- Auth: argon2 hash, secure `__Host-` cookie, CSRF/Origin checks, login rate limits; peer allowlist; hardened forwarded-for; CSP without `unsafe-inline`.
- Edge: only Caddy publishes ports; internal networks; hardened containers (read-only, `cap_drop: ALL`, no-new-privileges, uid 1000, no swap).
- Model: local-only transport with DNS pin and redirect refusal; isolated model container; verified installer.
- Data: migrations, redacted hash-chained audit, local backups; hash-locked deps; digest-pinned images.
- **M3 gate:** per-tool SAFE/GATED/FORBIDDEN policies; untrusted-content tools taint the run; approvals bound to `args_hash` with expiry; extra confirmation for payment/message/public_post/delete; composer locked while pending; chat text never approves; agent-created jobs need Approve.
- **M4 sandbox:** shell runs only in `sandboxd` (peer + Bearer auth, output cap, timeout, concurrency limit); container-only leftover reap; secret env stripped. Accepted limits in `docs/SECURITY.md`.
- **M5 files:** workspace path confinement, trash/restore instead of hard delete.
- **M6 core side (dormant until `browserd` ships):** every browser tool has a policy and taints the run; clicks are classified from a fresh look at the element (keywords in English and Finnish, submit controls, default-deny); typing into password, code or card fields is FORBIDDEN; the element fingerprint and a digest of every fact the classifier read, including the full page address, are pinned into the stored approval args (`_pin`, which the model can never supply); before an approved action runs, core looks at the element again and stops if anything differs, and `browserd` re-checks the fingerprint; an approved upload sends only the exact bytes that were in the file when Roland was asked; actions run in `safe` mode unless Roland approved that exact action; a `safe` action that tries to submit a form is reported and gated on retry; approval cards carry a screenshot for Roland; screenshots are never handed to the model; refs and tab ids are validated before they reach a URL; `browserd` answers are size-capped and type-checked.

### Pending

M6 `browserd` itself (navigation and POST guards, no cookie/eval/CDP surface, sensitive-field rules in `snapshot.js`) and browser container isolation · M7 screen auth and sign-in locking · M8 capture/scrubber/promotion controls · M9 full §10 security review and `docs/SECURITY.md` write-up.

Grammar/constrained decoding is **formatting**, not authorization. Untrusted tool/web text can still try to influence the model.

## 8. Next coding order

1. **Grok bots:** wait for Roland to unpause THE SCAM CALL CENTER before those bots start M6. **External AI:** if Roland already handed you this plan, start when he said — Grok parking does not block you.
2. Decide MODEL_CTX vs memory (§6) and record host memory under load.
3. **M6 part 2** (`browserd`, image, compose, firewall, fixture site, live tests, Browser tab) → **M7** screen/sign-in → **M8** persona/training → **M9** release.

Details, acceptance checklists and Contabo smoke steps: [docs/NEXT.md](NEXT.md).

## 9. Host facts

| Item | Value |
|---|---|
| Provider / OS | Contabo, Ubuntu 24.04 |
| Size | ~4 vCPU / ~8 GB RAM |
| Public IPv4 | `37.60.226.214` |
| URL | https://37-60-226-214.sslip.io/ |
| Paths | `/opt/roland-agent`, workspace `/srv/roland-agent/workspace` |
| Live services | caddy, core, model, sandbox (healthy) |
| Host `.env` | `MODEL_CTX=3072`, `MODEL_MEM_LIMIT=3840m` |

### Gotchas

- `MODEL_CTX=4096` with `MODEL_MEM_LIMIT=3840m` OOM-killed the model on Contabo.
- sandboxd leftover reap must run only inside its container; on a host it kills the machine.
- A chat "yes" must never bypass the approval UI.
- `_pin` in tool args is reserved for classifiers (`Decision.pinned`). `call_tool` drops a model-supplied one; browser handlers refuse to act without it.
- Browser tool errors start with `Error:` (nothing happened) and a blocked form submission starts with `Not done:` (ask again to get an approval card). The loop forgets earlier browser failures after a browser call succeeds, so a ref that failed can be retried after a new snapshot.
- Grammar-valid output is not authorization.

## 10. Spec vs this document

`docs/v2-spec.md` is the **detailed target**. This **AGENT.md** is the **living status**. `docs/NEXT.md` is the M6–M9 plan. If they disagree: Roland's latest decision → this file → NEXT.md → the spec.

## 11. Code map (current)

| Path | Role |
|---|---|
| `agent/config.py` | Config, secrets (`*_FILE`), startup policy and feature-flag checks |
| `agent/core.py`, `agent/brain.py` | Agent loop and model step |
| `agent/models/` | Local providers (llama.cpp, Ollama), grammar actions, parsing, context budget, `endpoint_guard.py` |
| `agent/gate.py` | M3 policies, approval lifecycle, run state |
| `agent/tools.py`, `agent/tools_files.py` | Tool registry and file tools |
| `agent/policy_browser.py`, `agent/browser_client.py`, `agent/tools_browser.py` | M6 core side: click/key/address classifier, `browserd` HTTP client, browser tools and their gate classifiers (dormant) |
| `agent/policy_shell.py`, `agent/sandbox_client.py`, `agent/local_shell.py` | Shell classifier and backends |
| `sandboxd/` | Sandbox exec service |
| `agent/workspace.py` | Workspace confinement, trash |
| `agent/memory.py`, `agent/migrations/`, `agent/audit.py`, `agent/backup.py`, `agent/scheduler.py` | Persistence, audit, backups, jobs |
| `agent/web/` | FastAPI app, auth, middleware, `routes_approvals.py`, `routes_files.py`, static UI |
| `docker-compose.yml`, `docker/{caddy,model,sandbox,model-installer}/` | Stack and images |
| `deploy/` | Preflight, secrets, firewall, deploy, ship, verify, restore, model store |
| `tests/` | Unit, frontend, integration (`edge.sh`, `isolation.sh`, `test_sandbox_live.py`) |

## 12. Last verified

Remote `v2` at **`a27b5ff`** (#37; M5 code `98971cc`) is what is deployed on Contabo, with caddy/core/model/sandbox healthy and M5 A5.4 green. M6 part 1 and the fixture site were verified off the server only: `make lint`, `pytest` (853 passed) and the frontend tests (31 passed) in a dev container, plus CI. The fixture site was also driven with a real headless Chromium and started as a container from `docker-compose.test.yml`. It changes nothing on Contabo while `BROWSER_ENABLED=false`. Not run for it: any Contabo smoke. `tests/test_sandboxd.py::test_leftover_background_process_is_reaped` fails in that dev container on untouched `v2` as well; it passes in CI. Re-run `make test` before claiming anything newer. CI's tiny model does not prove Qwen RAM or Contabo speed.
