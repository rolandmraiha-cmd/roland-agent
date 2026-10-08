# roland-agent

Roland's private, always-on AI agent: password-protected web chat, a self-hosted open-weight model only (no hosted LLM), tools with a code-enforced approval gate, a sandboxed terminal, workspace files, and approved background jobs.

**Live:** https://37-60-226-214.sslip.io/ (Contabo VPS, Ubuntu 24.04).
**Branch:** `v2` is the integration branch; verified baseline and Contabo checkout are `a0dbf22` (#50, 8 Oct 2026); the running app image is from `ea7e429`. M6 browser is enabled; A6.4 isolation and the A6.5 memory watch passed. Signed-in smoke passed the approval controls but found repeated reads and a false file-save reply. This tool-completion follow-up needs a rebuild and live retest before M6 acceptance and Roland's confirmation. Confirm the remote tip before coding. `main` is untouched until M9.

**Not the same as Grok:** this app is Contabo-hosted **roland-agent**. Roland also has **Grok Bot** teammates (Crew Chief, Code Builder, Code Shipper, …) in a separate chat; they are not this runtime. If those Grok bots are parked, an outside AI Roland gave this repo to may still work — see [docs/NEXT.md](docs/NEXT.md) §0.

## What works now

- Web chat with SQLite memory, jobs, hash-chained audit log and local backups.
- Docker stack: **Caddy** (only public ports 80/443) → **core** → isolated **llama.cpp** model (Qwen3-4B Q4_K_M on CPU) and **sandbox**.
- **M3 approval gate:** risky tool calls wait for Approve/Reject in the UI; untrusted content taints the run; chat text never approves.
- **M4 sandbox:** `run_shell` runs in an isolated `sandboxd` container, not in core.
- **M5 workspace and files:** Files UI and tools (list, preview, download, move, delete to trash, restore).
- **M6 browser:** a persistent Chromium in its own container, on as Roland's Contabo trial with Chromium's sandbox enabled. Example.com navigation and a harmless form approval/rejection were smoked; final acceptance remains pending.
- On Contabo the model stays at `MODEL_CTX=3072` / `MODEL_MEM_LIMIT=3840m`. The OOM at 3072 came from llama-server's RAM prompt cache, now disabled (`--cache-ram 0`, #43). The browser-enabled memory watch passed: model peak 3084.29 MiB, browser peak 822.80 MiB, minimum host available 3553 MiB. The context decision stays open. Small CPU model: expect slow, modest answers.
- Fixes #45–#47 are deployed: preserve length-limited replies, require the confirming tap 1–5 s later, and suppress repeat cards for an action rejected in the same run. The signed-in smoke confirmed double-click protection, rejection without submission, and a separately approved dummy submission.

## What does not work yet

Live screen and human sign-in (M7), persona and training pipeline (M8), and the 2.0.0 release (M9). Their flags stay off in production. See [docs/NEXT.md](docs/NEXT.md).

**M6 is still awaiting final acceptance.** The memory watch reports `A6.5 PASS`. Normal verification at `a0dbf22` reports **11 pass / 0 fail / 1 skip**, including every A6.4 isolation probe, with 3640 MiB available at the idle check. The skip is the human login/chat/approval checklist; browser smoke used an existing signed-in session and a phone-sized viewport, not a fresh phone login. Rebuild this follow-up, repeat the file/form completion tests, then obtain Roland's confirmation. Repo browser defaults stay off; the host has `COMPOSE_PROFILES=browser` and `BROWSER_ENABLED=true`. **Do not start M7 until Roland confirms M6 is done.** Snapshot: `pre-m6-deploy-2026-10-08`. Roland runs host commands himself; deploy uses `sudo env APPLY=1 make deploy` because of uid-1000 secret permissions.

**Tool-completion follow-up (rebuild/retest pending):** grammar-mode prompts now list the offered tools and JSON action format within the existing context budget. Repeated successful file/page reads finish using the existing result; actions invalidate the read cache. English file-save acknowledgements are checked against successful writes in the current run before being shown or saved in chat history. If no tool has been attempted, a missing write gets one bounded correction through the normal tool loop and approval gate; it never retries an earlier action. `MAX_TOOL_STEPS=6`, context and memory limits stay unchanged. Live smoke passed ordinary chat, uploads and missing-file handling; an existing-file read and both form runs hit the tool limit, and a requested note returned “saved” without a file. Those failures need live retesting after this fix.

**Verification follow-up (#50, verified on Contabo):** scripts close stdin and disable Docker input attachment for sandbox/browser probes. Normal verification now finishes **11 / 0 / 1** without terminal redirection. The earlier killed probe (137) was a verifier input problem, not evidence of an OOM.

**Completion review fixes:** web fetches also use the repeated-read guard. Save checks recognise acknowledgements after introductory words, arbitrary filename extensions and names without extensions. Generic “saved” needs exact evidence for every requested output path; input files and honest partial/failure reports remain distinct. These checks apply to recognised file-write requests; reading file contents and writing ordinary chat text do not require a file save.

**Merged follow-ups:** #48–#50 landed in `v2` with merge commits and their temporary branches were removed. The tool-completion follow-up starts from `a0dbf22` and targets `v2`.

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
