# roland-agent

Roland's private, always-on AI agent: password-protected web chat, a self-hosted open-weight model only (no hosted LLM), tools with a code-enforced approval gate, a sandboxed terminal, workspace files, and approved background jobs.

**Live:** https://37-60-226-214.sslip.io/ (Contabo VPS, Ubuntu 24.04).
**Branch:** `v2` is the integration branch; verified tip and deployed Contabo HEAD are `23fd1fc` (#47, 8 Oct 2026). M6 browser is enabled; basic smoke and the A6.5 memory watch passed, while final isolation verification and M6 acceptance remain pending. Before the browser deployment the host was `98971cc`, not the previously documented `a27b5ff` (same M5 code). Confirm the remote tip before coding. `main` is untouched until M9.

**Not the same as Grok:** this app is Contabo-hosted **roland-agent**. Roland also has **Grok Bot** teammates (Crew Chief, Code Builder, Code Shipper, …) in a separate chat; they are not this runtime. If those Grok bots are parked, an outside AI Roland gave this repo to may still work — see [docs/NEXT.md](docs/NEXT.md) §0.

## What works now

- Web chat with SQLite memory, jobs, hash-chained audit log and local backups.
- Docker stack: **Caddy** (only public ports 80/443) → **core** → isolated **llama.cpp** model (Qwen3-4B Q4_K_M on CPU) and **sandbox**.
- **M3 approval gate:** risky tool calls wait for Approve/Reject in the UI; untrusted content taints the run; chat text never approves.
- **M4 sandbox:** `run_shell` runs in an isolated `sandboxd` container, not in core.
- **M5 workspace and files:** Files UI and tools (list, preview, download, move, delete to trash, restore).
- **M6 browser:** a persistent Chromium in its own container, on as Roland's Contabo trial with Chromium's sandbox enabled. Example.com navigation and a harmless form approval/rejection were smoked; final acceptance remains pending.
- On Contabo the model stays at `MODEL_CTX=3072` / `MODEL_MEM_LIMIT=3840m`. The OOM at 3072 came from llama-server's RAM prompt cache, now disabled (`--cache-ram 0`, #43). The browser-enabled memory watch passed: model peak 3084.29 MiB, browser peak 822.80 MiB, minimum host available 3553 MiB. The context decision stays open. Small CPU model: expect slow, modest answers.
- Fixes #45–#47 are deployed: preserve length-limited replies, require the confirming tap 1–5 s later, and suppress repeat cards for an action rejected in the same run. The form smoke still needs repeating after those approval fixes.

## What does not work yet

Live screen and human sign-in (M7), persona and training pipeline (M8), and the 2.0.0 release (M9). Their flags stay off in production. See [docs/NEXT.md](docs/NEXT.md).

**M6 is still awaiting final acceptance.** The supplied `WATCH=600 make memory-report` says `A6.5 PASS`; a completed `sudo make verify` including browser isolation (A6.4) is still needed, followed by the form smoke after #46/#47 and Roland's confirmation. Repo browser defaults stay off; the host has both `COMPOSE_PROFILES=browser` and `BROWSER_ENABLED=true`. **Do not start M7 until Roland confirms M6 is done.** Snapshot: `pre-m6-deploy-2026-10-08`. Roland runs host commands himself; deploy uses `sudo env APPLY=1 make deploy` because of uid-1000 secret permissions.

**File-use prompt follow-up (pending Contabo deploy):** the model is told to use files only when Roland asks to use them or names one, keep notes only when asked, and treat missing files as empty. Notes Roland requests remain supported; unsolicited notes are forbidden. Tests cover chats and jobs with the browser off/on at the host context budget. This is a short prompt rule; file tools, their policies and approvals are unchanged. Roland still needs to deploy and check the model's live behaviour.

**Verification follow-up (pending host update):** `make verify` stalled at a sandbox network probe. The same probe finished with the expected blocked connection when its timeout ran inside the sandbox. `isolation.sh` now bounds those commands and stuck Docker clients, reports the active check, and fails when a probe breaks or exceeds its deadline. Regression tests cover actual stuck fixture processes and unexpected exit codes. Roland must rerun `sudo make verify` after updating; one blocked probe does not complete M6 acceptance.

**Merge order for these follow-ups:** use GitHub's **Create a merge commit** for documentation PR #48, then merge #49. Both target `v2`; preserving #48's commit avoids conflicts in the documentation shared by the branches.

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
make test-browser               # the real browser against a test site, in Docker (not on the server)
```

Never set `ALLOW_SHELL=true` on a personal computer. Host-mutating `make` targets require `APPLY=1`.

## Docs

- [docs/AGENT.md](docs/AGENT.md): living master status, rules, commands, host facts.
- [docs/NEXT.md](docs/NEXT.md): implementation plan for M6 → M9.
- [docs/v2-spec.md](docs/v2-spec.md): detailed target design (not an inventory of shipped code).
- [docs/SECURITY.md](docs/SECURITY.md): accepted security limits so far.

**Keep them updated:** every PR, every edit on that branch, and every squash merge must update `docs/AGENT.md`, `docs/NEXT.md`, and this README in that same PR before merge, whenever code, deploy state, plans, or instructions change. Status, instructions, and plans stay in these files, not only in chat. Do not squash-merge a PR whose README or instruction docs are stale. `docs/SECURITY.md` updates in that same PR when an accepted limit changes. After a squash merge, the tip line in this README and in `docs/AGENT.md` must name the new `v2` tip.
