# roland-agent

Roland's private, always-on AI agent: password-protected web chat, a self-hosted open-weight model only (no hosted LLM), tools with a code-enforced approval gate, a sandboxed terminal, workspace files, and approved background jobs.

**Live:** https://37-60-226-214.sslip.io/ (Contabo VPS, Ubuntu 24.04).
**Branch:** `v2` is the integration branch (last verified tip `5104fb6`, #33). `main` is untouched until the M9 release.

**Not the same as Grok:** this app is Contabo-hosted **roland-agent**. Roland also has **Grok Bot** teammates (Crew Chief, Code Builder, Code Shipper, …) in a separate chat; they are not this runtime. If those Grok bots are parked, an outside AI Roland gave this repo to may still work — see [docs/NEXT.md](docs/NEXT.md) §0.

## What works now

- Web chat with SQLite memory, jobs, hash-chained audit log and local backups.
- Docker stack: **Caddy** (only public ports 80/443) → **core** → isolated **llama.cpp** model (Qwen3-4B Q4_K_M on CPU) and **sandbox**.
- **M3 approval gate:** risky tool calls wait for Approve/Reject in the UI; untrusted content taints the run; chat text never approves.
- **M4 sandbox:** `run_shell` runs in an isolated `sandboxd` container, not in core.
- **M5 workspace and files:** Files UI and tools (list, preview, download, move, delete to trash, restore).
- On Contabo the model runs with `MODEL_CTX=3072` (4096 ran out of memory at the 3840m limit). Small CPU model: expect slow, modest answers.

## What does not work yet

Browser (M6), live screen and human sign-in (M7), persona and training pipeline (M8), and the 2.0.0 release (M9). Their flags stay off in production. See [docs/NEXT.md](docs/NEXT.md).

## Develop

```bash
git clone --branch v2 https://github.com/rolandmraiha-cmd/roland-agent.git && cd roland-agent
python3.12 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock
pip install --no-deps --no-build-isolation --no-index -e .
cp .env.example .env            # local HTTP: COOKIE_SECURE=false
python -m agent hash-password
make lint && make test          # unit + frontend; no model needed
make test-integration           # Docker edge/model fixtures
```

Never set `ALLOW_SHELL=true` on a personal computer. Host-mutating `make` targets require `APPLY=1`.

## Docs

- [docs/AGENT.md](docs/AGENT.md): living master status, rules, commands, host facts.
- [docs/NEXT.md](docs/NEXT.md): implementation plan for M6 → M9.
- [docs/v2-spec.md](docs/v2-spec.md): detailed target design (not an inventory of shipped code).
- [docs/SECURITY.md](docs/SECURITY.md): accepted security limits so far.

**Keep them updated:** every PR, every edit on that branch, and every squash merge must update `docs/AGENT.md`, `docs/NEXT.md`, and this README in that same PR before merge, whenever code, deploy state, plans, or instructions change. Status, instructions, and plans stay in these files, not only in chat. Do not squash-merge a PR whose README or instruction docs are stale. `docs/SECURITY.md` updates in that same PR when an accepted limit changes.
