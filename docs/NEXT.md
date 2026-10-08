# roland-agent — NEXT: implementation handoff for M6 → M9

> Snapshot: 8 Oct 2026. Deployed code baseline, Contabo checkout and rebuilt app image **`2de54f4`** (#58). Verification passed 13 / 0 / 1 with 3746 MiB available and the screen on at `4d0f703`, and 13 / 0 / 1 with 3488 MiB available after #58. M6 is enabled; A6.4 isolation and the A6.5 memory watch passed. The plain dummy form test passes live at `50767ec`; rejection handling passed at `7b09b90`. A fresh login on Roland's phone passed. **Roland confirmed M6 as done on 8 Oct 2026** and asked for M7 to start. Confirm the remote tip before coding. **Roland confirmed M7 as done on 8 Oct 2026.** Both parts and the sign-in follow-up (#58) are merged and deployed, and the screen is on on Contabo. **The chat-refresh fix (#59) is merged at `e2f0792`; its host deploy remains with Roland. Next: review M8 draft #56, now rebased on this final M7 baseline.**
> **Standing rule:** every PR, every edit on that branch, and every squash merge updates `docs/AGENT.md`, this file, and `README.md` in that same PR before merge when code, deploy state, plans, or instructions change. Plans do not live only in chat. After squash-merge, the tip line names the new `v2` tip.
> Audience: an AI coder that has the repository but has not seen any earlier chat.


## 0. Who is who (read this first)

Three different things share the word "agent". Do not mix them up.

| Name | What it is | Who runs it |
|---|---|---|
| **roland-agent** | This software repository and the Contabo HTTPS app (chat, tools, model). | Docker on Contabo; code in GitHub `rolandmraiha-cmd/roland-agent`. |
| **Roland's Grok bots** | Named assistants in Roland's Grok Bot group chat **THE SCAM CALL CENTER**: **Crew Chief** (routes work), **Code Builder** (writes code / opens PRs), **Code Shipper** (reviews, Contabo smoke, merges), **Researcher**, **AI Relay**. They are Grok Bot teammates, not this repo's runtime. | Only inside Roland's Grok product / that group chat. |
| **You (reader)** | Any human or AI on **another platform** (Claude, Codex, ChatGPT, Cursor, etc.) that Roland gave this handoff to. | You are **not** automatically Code Builder or Code Shipper. |

**If you are an external AI implementing this handoff:**

1. Treat **Code Builder** / **Code Shipper** / **Crew Chief** as roles Roland may assign to his Grok bots **or** to you for this job. Follow whatever Roland wrote when he handed you the task.
2. If Roland asked *you* to implement a milestone, you act as the **coder**: branch from `v2`, open a PR to `v2`, update docs in the same PR. Do not wait for a Grok bot named Code Builder unless Roland said that bot will code.
3. Merging, Contabo deploy, and live smoke are normally **Code Shipper (Grok)** or Roland himself. An external AI must **not** SSH to Contabo, raise memory limits, change DNS, or merge to `main` unless Roland explicitly ordered that for you in writing.
4. Never assume chat messages in THE SCAM CALL CENTER are instructions to you unless Roland pasted them into your session. Never invent Grok-bot actions you cannot perform.
5. The Contabo product (**roland-agent**) is separate from Grok. Enabling browser/screen/training flags affects only Contabo, not Grok bots.
6. **Grok parked ≠ project frozen.** If Roland gave this handoff to an external AI, that AI may code and open PRs while the Grok room stays quiet. Contabo SSH, merges, DNS, and memory raises still need Roland's explicit OK (or Code Shipper (Grok) when he unpauses that role).

## 1. Purpose and how to use this document

This document tells you what to build next, in what order, under which rules, and how each milestone is accepted. It covers M6 (browser), M7 (screen and sign-in), M8 (persona and training) and M9 (release).

Use the three instruction documents as follows:

1. **`docs/AGENT.md`** is the living status. It says what is shipped and what is not. Trust it over the spec for "what exists".
2. **`docs/NEXT.md`** (this file) is the work plan for M6–M9. It summarises and orders the spec tasks and adds the current production constraints.
3. **`docs/v2-spec.md`** is the detailed target design (sections cited below as §N). It is normative for names, endpoints, classifiers and acceptance tests. It is **not** an inventory of shipped code.

If the documents disagree, follow this order: Roland's latest explicit decision → `docs/AGENT.md` → this file → `docs/v2-spec.md`.

Before writing any code:

1. Run `git fetch origin && git switch v2 && git pull --ff-only` and confirm the tip. If it is newer than the M6 part 1 merge (#39), read the new commits and update your assumptions.
2. Read `docs/AGENT.md`, then the spec sections listed for your milestone.
3. Verify every file path mentioned here in the tree. Paths marked "expected" come from `docs/v2-spec.md` and do not exist yet.

## 2. Non-negotiable rules

### 2.1 Branching and merging

1. Cut each milestone branch from the latest remote `v2`: `v2-m6-browser`, `v2-m7-screen`, `v2-m8-model`, `v2-m9-release`. A milestone may be split into several PRs; never combine two milestones in one PR.
2. Every PR targets `v2`. **Never push to `main`.** The only path to `main` is the single M9 release PR `v2` → `main`, which Roland merges.
3. Never force-push a branch with an open PR unless the reviewer asks for it.
4. Process when Roland's **Grok bots** do the work: **Code Builder (Grok)** writes the code and opens the PR → **Code Shipper (Grok)** reviews and runs the Contabo smoke → Shipper merges to `v2`. **Crew Chief (Grok)** only routes; it does not write or merge code.
5. Process when an **external AI** (non-Grok) does the work: that AI codes and opens the PR as Roland directed; review/merge/Contabo smoke stay with **Code Shipper (Grok)** and/or Roland unless Roland explicitly assigned those steps to you. See §0.
6. Roland's standing rule for **Code Shipper (Grok)**: may merge after a passing review and a passing smoke without waiting for Roland. Must ping Roland first for substantial manual tests and for destructive or host-level changes (DNS, deleting data or volumes, raising memory limits, renting paid GPU, model promotion).
7. Code Builder and Code Shipper (Grok) share one GitHub identity, so Shipper leaves a **COMMENT** review, not a formal APPROVE. An external AI on a different GitHub login should use a normal review when reviewing.
8. Roland's **Grok** bots in THE SCAM CALL CENTER are currently **parked** (they will not start M6 in that room until he unpauses them). **Parking that Grok room does not stop an external AI** from implementing this handoff if Roland gave *you* the work in your own session. Grok-parked ≠ project-frozen.

### 2.2 Secrets, privacy and dependencies

1. No secrets in the repository, in logs, in audit rows, in test fixtures or in PR text. Secrets are files under `secrets/` (uid 1000, mode 0400), mounted as Docker secrets and read through `*_FILE` variables.
2. No telemetry. No obfuscated code.
3. Every new dependency is hash-locked (`requirements-*.lock`, `pip --require-hashes`) and every image is digest-pinned. State the reason for each new dependency in the PR (§13.5).

### 2.3 Model

1. **No hosted LLM, ever.** Inference runs only in the local `model` container (llama.cpp). `tests/test_no_hosted_llm.py` must stay green.
2. The default model is text-only. Screenshots and images must never reach a text-only model (§6.5, M6.3).

### 2.4 Host operations

1. Every host-mutating `make` target requires `APPLY=1`. Never bypass that guard and never add a mutating target without it.
2. Never raise `MODEL_MEM_LIMIT`, never change DNS and never delete host data, volumes or backups without Roland's explicit OK.
3. Never set `ALLOW_SHELL=true` on a personal computer. Never set `SANDBOX_REAP_ALL` on a host.

### 2.5 Approval gate

1. Authorisation happens only through the approval UI (approve/reject buttons with `args_hash`, plus the typed confirmation for confirm categories). A chat message such as "yes", "ok" or "done" must never approve anything or resolve a sign-in.
2. Grammar-constrained decoding is formatting, not authorisation. Never treat a well-formed action as permitted.
3. Feature flags stay off in production until their milestone is merged and smoked: `BROWSER_ENABLED`, `SCREEN_ENABLED`, `TRAINING_CAPTURE`, `TRAINING_LOOP_ENABLED`, `TRAINER_URL`.

### 2.6 Documentation

1. Instruction markdown is limited to: one master `docs/AGENT.md`, a short `README.md`, and this `docs/NEXT.md`. Do not add more handoff files; fold M4-style notes into `docs/AGENT.md`.
2. **Keep docs current (standing rule):** anyone (human or AI) who changes code, Contabo/deploy state, plans, or instructions must update `docs/AGENT.md`, `docs/NEXT.md`, and `README.md` in the **same PR**, before squash-merge. Every edit on the branch, including review fixes, updates those files again if status, instructions, or plans changed. Squash-merge does not carry a chat-only plan into the repo and is not a reason to defer the doc update. If a new plan appears mid-work, write it into `docs/NEXT.md` (or AGENT.md next-order) before or with the code — never leave plans only in chat.
3. Large instruction-MD replacements still follow draft → Roland approves the text → PR. Routine status updates that match already-shipped work may land in the feature PR after Shipper review.
4. Each PR, not only milestone PRs, updates the Done/Not done tables and tip in `docs/AGENT.md`, the remaining plan in `docs/NEXT.md`, and the short “what works / what does not” in `README.md` when that reality changed, plus `docs/SECURITY.md` for any accepted limit. Do that on every edit that changes the story, and again before squash-merge so the merged commit is not ahead of the docs.

## 3. Current production state (8 Oct 2026)

| Item | Value |
|---|---|
| Repository | https://github.com/rolandmraiha-cmd/roland-agent |
| Integration branch | `v2`; Contabo checkout and app image **`2de54f4`** (#58). M6, fixes #43–#53 and M7 (#55, #57, #58) are live; verification passed normally. Before the first browser deployment the host was `98971cc`, not the previously documented `a27b5ff` (same M5 code) |
| `main` | Untouched since v1; do not push until M9 |
| Live URL | https://37-60-226-214.sslip.io/ |
| Host | Contabo VPS, Ubuntu 24.04, ~4 vCPU / ~8 GB RAM, IPv4 `37.60.226.214` |
| Code path | `/opt/roland-agent` |
| Workspace | `/srv/roland-agent/workspace` (bind-mounted as `/workspace` in core and sandbox) |
| Healthy services | `caddy`, `core`, `model`, `sandbox`, `browser`, and `novnc` (the screen relay, since 8 Oct 2026) |
| Host `.env` | `MODEL_CTX=3072`, `MODEL_MEM_LIMIT=3840m`, `COMPOSE_PROFILES=browser,screen`, `BROWSER_ENABLED=true`, `SCREEN_ENABLED=true`, `BROWSER_CHROMIUM_SANDBOX=true`, `BROWSER_SECCOMP=./docker/browser/seccomp-chromium.json`, `MAX_TOOL_STEPS=6` |
| Repo defaults | `docker-compose.yml` and `.env.example` still default `MODEL_CTX` to 4096 |
| Shell | On, via the sandbox (`ALLOW_SHELL=true`, `SHELL_BACKEND=sandbox`) |
| On / off | Browser on as Roland's M6 trial; the screen on since 8 Oct 2026 (`SCREEN_ENABLED=true` and the `screen` profile; the file before that is kept as `.env.before-screen`); training stays off (compose sets those flags to `"false"`) |
| Snapshot | `pre-m6-deploy-2026-10-08` |
| Host commands | Roland runs `sudo env APPLY=1 make deploy`; read-only verification is `sudo make verify`. The deploy user is not uid 1000; secrets stay uid 1000, directory 0700 / files 0400 |
| App version | `0.1.0` in `pyproject.toml` (bump to `2.0.0` in M9) |

`MODEL_CTX` was lowered from 4096 to 3072 on Contabo after the model container was OOM-killed at 4096 with the 3840m limit. Do not "fix" this by raising memory.

**8 Oct 2026, M6 pre-step under load:** the model was OOM-killed again, at 3072. The kernel log showed `Memory cgroup out of memory: Killed process (llama-server) anon-rss:3921812kB`, and Docker restarted the model. The cause was not the context size: llama-server keeps a RAM prompt cache (`--cache-ram`, default 8192 MiB in this build). It copies earlier conversations' KV state into that cache inside the 3840 MiB limit. A fresh model sat at 2915 MiB; before the kill it was at 3806 MiB. `docker/model/run.sh` now passes `--cache-ram 0`. `make memory-report` passed that run anyway, because Docker clears `OOMKilled` on restart. It now fails when a service restarts or a process in it is killed for lack of memory during the observation. Roland re-measured after #43: with two chats plus "continue", model peak 3038 MiB, minimum host available 4045 MiB, projected 2765 MiB available after the 1280 MiB browser cap: PRE-STEP PASS. Decision 1 stays open; keep 3072/3840m. #44 was closed as a duplicate of #43.

Shipped milestones on `v2`: v1 (#1–#6), M0, M1a, M1, M2 web/edge/model/providers/deploy, **M3 gate (#29, #30)**, **M4 sandbox (#31)**, **M5 workspace and files (#32; A5.4 green on Contabo)**. See `docs/AGENT.md` §3 for the full table.

## 4. Architecture already live

### 4.1 Services (see `docker-compose.yml`)

| Service | Role | Networks (IP) | mem_limit |
|---|---|---|---|
| `caddy` | Only service with published ports: 80/tcp, 443/tcp, 443/udp. TLS (ACME), reverse proxy to core. Screen routes (`/screen/websockify`, `/screen/novnc/*` with forward-auth to `/internal/screen-auth`) lead to the `novnc` relay, which only runs with the `screen` profile (M7; off on Contabo so far). | `public` 10.77.0.2, `edge` 10.77.1.2, `screen` 10.77.2.2 | 96m |
| `core` | FastAPI app, agent loop, gate, tools, SQLite (`/data`), backups (`/backups`), workspace (`/workspace`). Only accepts peer 10.77.1.2. | `edge` 10.77.1.10, `sandbox_ctl` 10.77.3.10, `model` 10.77.6.10, `core_egress` | 640m |
| `model` | llama.cpp server (digest-pinned, `docker/model/VERSION`), read-only weights, bearer `model_server_token`, no egress, no published port. | `model` 10.77.6.60 | `${MODEL_MEM_LIMIT:-3840m}` |
| `sandbox` | `sandboxd` on 10.77.3.20:7000; peer 10.77.3.10 + Bearer `sandbox_api_token`; output cap 64 KiB, timeout ≤ 300 s, concurrency 2; container-gated leftover reap. | `sandbox_ctl` 10.77.3.20, `sandbox_egress` 10.77.11.20 | 1g |
| `browser` (**live on Contabo** with the `browser` profile; repo default remains off) | Chromium on Xvfb plus `browserd` on 10.77.4.40:7100; peer 10.77.4.10 + Bearer `browser_api_token`; profile volume mounted here only. | `browser_ctl` 10.77.4.40, `browser_egress` 10.77.12.40 | 1280m |

All services are `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, uid 1000, with `memswap_limit == mem_limit`.

Since M6, core is also on `browser_ctl` (10.77.4.10) and mounts `browser_api_token`, whether or not the browser runs. Reserved, unused networks already declared: `vnc` 10.77.5.0/24, `trainer_ctl` 10.77.7.0/24, `trainer_egress` 10.77.13.0/24, plus `screen` (attached to caddy only today).

Config caps today: 3840 + 640 + 96 + 1024 = **5600 MiB**; with the browser on, 6880 MiB. Config limits are not proof of headroom; real usage must be measured.

### 4.2 M3 confirmation gate (live)

Implemented in `agent/gate.py`, `agent/web/routes_approvals.py` and the static UI. Behaviour you must preserve:

1. Each tool has a policy in `POLICIES` (`agent/gate.py`) that classifies a call as `SAFE`, `GATED` or `FORBIDDEN`, with a category.
2. Untrusted-content tools (`fetch_url`, `read_file`, `run_shell`) **taint** the run. After taint, risky tools are gated.
3. A GATED call creates an approval row bound to `args_hash`, with an expiry. The run waits. The UI shows an approval card; the composer is locked while an approval is pending.
4. Categories in `CONFIRM_CATEGORIES` (`payment`, `message`, `public_post`, `delete`) need an extra confirmation (`needs_confirm`); approve without it fails.
5. Approve fails on `args_hash` mismatch or expiry. Stopping a run cancels its pending approvals. Pending approvals expire on startup.
6. Some tools are unavailable in jobs (`in_jobs=False`, e.g. `schedule_job`). Agent-created jobs need Approve.
7. Decisions are audited in the hash-chained audit log (`agent/audit.py`, `/api/audit/verify`).

New browser, sign-in and training tools must register policies in this same registry, not bypass it.

### 4.3 M4 sandbox and M5 files (live)

- `run_shell` goes through `agent/sandbox_client.py` to `sandboxd/`; `agent/policy_shell.py` is a usability classifier, not the boundary. See `docs/SECURITY.md` for the accepted uid-1000 token limit.
- `agent/workspace.py`, `agent/tools_files.py` and `agent/web/routes_files.py` implement workspace files: list, content, download, preview, mkdir, move, delete to trash, and trash restore under `/api/files*` and `/api/trash*`.

## 5. Milestones

Work strictly in order: M6 → M7 → M8 → M9. M7 depends on M6's browser process. M8 is mostly independent of M6/M7 but its capture rules depend on M7's sign-in state, so build M8 after M7.

---

### M6 — Browser (`v2-m6-browser`)

**Spec:** §5.5, §6.5, §8.4, §9.3, §9.4.1, §10.3, §13.2–13.3, M6 in §12.

#### Status: accepted by Roland (8 Oct 2026)

M6 was split into PRs to `v2`.

- **Part 1, core side (#39): done.** `agent/policy_browser.py`, `agent/browser_client.py`, `agent/tools_browser.py`, the browser policies in `agent/gate.py`, the screenshot on approval cards, and the tests for A6.1 and A6.2.
- **Part 2, the browser service (#42): deployed on Contabo, off by default in the repo.** Deliverables 1–3, 5 and 8 below, the browser part of `docker-compose.test.yml`, and A6.3 (`make test-browser`). `agent/__main__.py` accepts `BROWSER_ENABLED=true` (and, since M7 part 2, `SCREEN_ENABLED=true` with it). Roland's host `.env` now has **both** `COMPOSE_PROFILES=browser` and `BROWSER_ENABLED=true`.
- **Fixture site (deliverable 7): done in #38**, extended in #42 with pages for dialogs, new tabs, frames, self-submitting forms, secret field names, uploads and more.
- **Also merged:** the Browser tab (deliverable 9, #40) and the server-side checks (isolation probes from the browser container for A6.4, `make memory-report`, #41).
- **Contabo evidence:** pre-step memory PASS after #43, browser smoke, then the `WATCH=600` report with **A6.5 PASS**. Roland rebuilt/deployed #51 at `60974e6`; ordinary verification passed **11 / 0 / 1**, including every sandbox/browser isolation probe (A6.4), with 3698 MiB available at the idle check; only the human checklist was skipped. Live retest passed ordinary chat, one existing-file read/final answer, an actual requested note, truthful missing-file handling, browser navigation, the first rejection/final answer, double-click protection and phone-sized controls. The deliberate-submission attempt then selected wrong fields from a short snapshot and asked at another field after rejection; both cards were rejected and that run stopped. Roland then rebuilt/deployed #52 at `7b09b90`: verification **11 / 0 / 1**, 3633 MiB available. Live retest at `7b09b90` (8 Oct 2026, signed-in session): asked to fill and submit the httpbin.org dummy form, the model requested `browser_snapshot` with `max_chars` 200, received the 500-character minimum showing 8 of 13 controls with a “5 more elements not shown” note, typed the dummy name, then asked to click “Telephone:”. That card was rejected: the turn ended with one truthful final answer, no second card, an unlocked composer and nothing submitted. A follow-up message in the same chat was answered from the chat history without a tool call. In a new chat that named the Submit order button and asked for the whole page, the model took a full-size snapshot and asked for “Click “Submit order” (button) on httpbin.org”; after the two-tap approval the audit recorded one click that navigated to `https://httpbin.org/post`, the echo page showed `custname` “Claude M6 Retest”, and the model reported the submission.
- **Accepted:** Roland then repeated the plain request on his phone after a fresh login (8 Oct 2026, about 17:51–17:53 Helsinki time): the open-page card and the Submit order card were approved there, the agent reported the submission, and the Browser tab showed the echo page with `custname` “Claude M6 Retest”. **Roland confirmed M6 as done on 8 Oct 2026** and asked for M7 to start. No code change is pending for M6. Keep `MAX_TOOL_STEPS=6`, `MODEL_CTX=3072`, `MODEL_MEM_LIMIT=3840m` and the browser cap.
- **File-use prompt follow-up (deployed in #49 at `ea7e429`):** the short rule in `agent/core.py` limits file use to when Roland asks to use files or names one, keeps notes only when asked, and treats missing files as empty. Requested notes remain supported; unsolicited note-taking is forbidden. Added prompt text is 115 characters including its newline, estimated at 39 tokens by the repo's conservative counter (within the 40-token budget). The regression test checks the actual model-bound prompt for chats/jobs with the browser off/on and a full fact store at `MODEL_CTX=3072`; file tools remain available. Tool policies and approval behaviour are unchanged. This addresses the unrequested `notes/title.txt`, `notes/car_engine_explanation.txt`, `notes/last_form_submission.txt`, and `notes/battery_chemistry_notes.txt` calls that cost about 30–60 s each on Contabo and pushed form tasks into `MAX_TOOL_STEPS`. Roland checks the model's live behaviour before claiming the live issue resolved. **Stop and report after this PR; do not start M7 until Roland confirms M6 is done.**
- **Tool-completion follow-up (#51, deployed):** grammar-mode prompts expose offered tool names/arguments and JSON action format within 1500 estimated system tokens, including bounded facts. Repeated successful reads/web fetches finish from existing results with tools disabled; actions invalidate cached reads. The final tool-budget round offers only an answer. English save acknowledgements require exact current-run write evidence, with one normal-loop correction before any tool attempt. Live retest read the retained 87-byte `m6-smoke-codex-2026-10-08.txt` once and answered `PINE-4827`. One write created the requested 17-byte `m6-requested-note-codex-2026-10-08.txt`; downloading it through Files independently confirmed exact contents `REQUESTED_NOTE_OK`. Ordinary chat used no tools and missing-file handling invented no contents. No automatic writes, approval bypass, policy changes or cap increases. M7 stays on hold.
- **Snapshot/rejection follow-up (#52, deployed at `7b09b90`):** if the model asks for less than the full snapshot size and every control fits in short form in that budget, the snapshot shows all refs, roles, names and safety states rather than a rich prefix that omits the submit button. Values and link targets are left out, page text is added only when at least 80 characters fit, and the closing note says to omit `max_chars` for the full page. Full-size snapshots are unchanged (link targets, page text, pagination with `start`); longer control lists retain original pagination. Grammar prompts explain current refs, matching names/roles and how to read missing targets. The first human rejection ends tools for the turn, including other refs, revised actions and remaining native-batch side effects, and requests a final explanation. A separate user request can still be approved. Regressions check a 13-control form's submit ref at 500 characters, hidden sensitive values, disabled/checked states, an unchanged full-size snapshot of a 45-link page, the short view only for reduced requests, no extra card/click/fact after rejection, complete native tool results and a later separately approved request. Review of the first version found that it also shortened full-size snapshots, which removed page text and link targets from a test page with 30, 45 or 60 links; that is fixed in the same PR, together with two gate tests that still expected the #47 behaviour. The existing system/output budgets and all resource caps stay unchanged. Deploy, reject one dummy submission, then separately approve the correctly labelled Submit card and require the echo response plus a final answer. Never approve a card for Telephone or Customer name to submit a form.
- **Snapshot-minimum follow-up (#53, deployed at `50767ec`):** a `max_chars` below 1500 is raised to 1500 (or to the full size when `MODEL_TOOL_OUTPUT_CHARS` makes that smaller; 500 stays the absolute floor). At 500 characters neither the rich list nor the short list could show the 13-control form: the short list needs 558 characters there because browserd marks the four topping tick boxes sensitive (their field name `topping` contains `pin`). At 1500 the whole form is listed with `[e13] button "Submit order" (submits form POST httpbin.org/post)` and its page text. Tests replay the exact live snapshot at 500, the same request raised to 1500, the limit table, and the short view on a 31-control form. Full-size snapshots, approval policies and resource caps are unchanged.
- **Live retest of #53:** Roland rebuilt/deployed `50767ec`: verification **11 / 0 / 1**, 3730 MiB available. At `50767ec` (8 Oct 2026, signed-in session, new chat, the plain request with no hints): the model asked for `browser_snapshot` with `max_chars` 1000, which was raised to 1500 and listed the whole form; it typed the dummy name and asked for “Click “Submit order” (button) on httpbin.org”. Roland approved that card himself with two taps. The audit recorded one click that navigated to `https://httpbin.org/post`, the echo page showed `custname` “Claude M6 Retest”, and the model reported the submission.
- **Verification follow-up (#50, complete):** scripts close stdin and set `--interactive=false` on isolation probes. Three regression cases keep the caller's input pipe open with finite 15-second deadlines. Ordinary verification passed **11 / 0 / 1** on Contabo after pulling `a0dbf22`; the prior killed probe (137) was a verifier input problem, not evidence of an OOM.
- **Correction bound:** offer the one file-save correction only before any tool attempt and with tool budget remaining. After a failure, rejection or other action, withhold an unverified acknowledgement without retrying. The check recognises English file-save claims; it is not a general verifier of model statements or shell side effects.
- **Completion review fixes:** test acknowledgements such as “Sure, I've saved …” and “Successfully saved …”; `Save othernote.txt` must not accept a write to `note.txt`, and `Save a.txt and b.txt` must not accept only `a.txt`. Cover `.toml`, `.xml`, extensionless and quoted/spaced names, preserve input/output distinction and honest failure/partial reports, and keep ordinary chat writing free of file-save requirements. Repeated identical successful `fetch_url` calls also finish from the existing result. These stay bounded English completion checks, not an authorisation parser.
- **Merged follow-ups:** #48–#53 used merge commits. The #48–#51 branches were removed; `v2-m6-live-retest` (#52) and `v2-m6-snapshot-floor` (#53) are merged and can be removed.

#### Goal

The agent can drive one persistent, headed Chromium through a private `browserd` service. Consequential actions are gated by code. Only text and accessibility output reaches the default text-only model.

#### Dependencies

1. M3 gate and M5 workspace on `v2` (done).
2. **Pre-step (mandatory, before enabling on Contabo):** record measured memory on the live host with `caddy`, `core`, `model`, `sandbox` running and the model under load (`docker stats`, `free -m`). Put the numbers in the PR. If adding the planned browser cap (1280 MiB) would leave less than ~800 MiB available, stop and raise the open decision in §6 with Roland. Do not enable the browser to "see what happens".

#### Deliverables

Expected locations per `docs/v2-spec.md`. All nine are in the tree after #40, #41 and #42. Deliverable 6's firewall rules were already in `deploy/firewall.sh`.

1. **Image** `docker/browser/Dockerfile`: FROM a digest-pinned `mcr.microsoft.com/playwright/python` image whose Chromium matches the pinned Playwright version; `xvfb`, `x11vnc`, `tini`, fonts; `requirements-browser.lock` (hash-checked); `USER 1000:1000`; `/profile` and `/files` owned by 1000.
2. **Chromium policy** `docker/browser/chromium-policy.json`: password manager, autofill, sync, sign-in, metrics off; downloads to `/files/downloads`; devtools disabled; `file://`, `chrome://`, `devtools://`, `view-source:`, `chrome-extension://` blocked. If managed policies are not honoured by the bundled Chromium, apply the same settings via launch args and profile preferences and document which worked.
3. **`browserd/`** package:
   - `launcher.py` supervises Xvfb (`:99`, `-nolisten tcp`), and browserd, with restart backoff capped at 30 s. x11vnc is added in M7; leave a clean hook for it.
   - `session.py` uses Playwright `launch_persistent_context(user_data_dir="/profile", headless=False, …)` against the Xvfb display. Use Playwright's default local launch (pipe transport). Do **not** expose a CDP/remote-debugging port on any network.
   - `server.py` implements exactly the §8.4 routes (except `/v1/user-mode` and `/v1/vnc/disconnect`, which may be stubbed for M7): `/healthz`, `/v1/status`, `/v1/navigate`, `/v1/snapshot`, `/v1/describe`, `/v1/click`, `/v1/type`, `/v1/press`, `/v1/select`, `/v1/scroll`, `/v1/back`, `/v1/forward`, tab activate/close, `/v1/screenshot`, `/v1/upload`, `/v1/downloads`. Every `/v1/*` route checks peer 10.77.4.10 and the Bearer `browser_api_token`; bodies ≤ 64 KB.
   - `snapshot.js`: fixed, reviewed file in the image; never agent-supplied. Assigns `data-ra-ref` refs, returns element descriptors with fingerprint, and **never** includes values of sensitive fields (password, OTP, card, etc.).
   - Guards: navigation guard (http/https/about/blob/data-images only; private IPs, `localhost`, `*.internal` blocked unless in `BROWSER_ALLOW_PRIVATE_HOSTS`, test-only); POST-navigation guard in `mode="safe"` returning `blocked_submission`; dialogs auto-dismissed; at most `BROWSER_MAX_TABS=2`.
4. **Core side (done in part 1, except where marked):**
   - `agent/browser_client.py` (httpx client, token from `BROWSER_API_TOKEN_FILE`).
   - `agent/tools_browser.py`: every browser tool in §9.3 except `request_signin` (M7).
   - `agent/policy_browser.py`: the §9.4.1 classifier — token-based keyword matching (no regex), English + Finnish keyword sets, category precedence payment > delete > message > public_post > form_submit, submit controls gated, default-deny for unknown clickable elements. Removing keywords requires Roland.
   - Register every browser tool in `agent/gate.py` `POLICIES`. Snapshot/read tools taint the run. Typing into a sensitive field is FORBIDDEN. Approved actions re-check the element fingerprint and fail on mismatch.
   - Approval cards for browser actions include a screenshot saved under `screenshots/approval-<id>.png` in the workspace, shown to Roland only.
   - `MODEL_VISION` stays false. Images are added to the model request only if `MODEL_VISION=true` **and** the active model manifest lists `vision`. Part 1 has no code path that hands an image to the model at all, so `MODEL_VISION=true` changes nothing yet; build that only when a vision model is actually installed.
   - How part 1 does it, so part 2 fits: the classifier looks at the element through `/v1/describe` just before the gate and returns `Decision.pinned = {"fingerprint": …}`. `call_tool` stores that under the reserved `_pin` key with the approval args and drops any `_pin` the model sent. Handlers send `mode="approved"` only while `call_tool` runs them with an approved approval row for that tool; otherwise `mode="safe"`. A `blocked_submission` answer is remembered on the run (`RunState.blocked_submissions`), so the same call is GATED `form_submit` next time. The approval screenshot is taken by `Decision.card_screenshot` inside `Gate.request` and saved as `screenshots/approval-<id>.png`.
5. **Compose:** `browser` service on `browser_ctl` (10.77.4.40) and `browser_egress`; core joins `browser_ctl` at 10.77.4.10. Planned limits: `mem_limit`/`memswap_limit` 1280m (includes shm), `shm_size: 320m`, tmpfs `/tmp` 256m, cpus 2.0, pids 512, `oom_score_adj: 500`. Named volumes for `/profile` (browser-only) and a downloads location that core can expose as workspace files. New secret `browser_api_token` (add to `deploy/secrets.sh`). `agent/config.py` already refuses `BROWSER_ENABLED=true` without `BROWSER_API_TOKEN`; keep that.
6. **Firewall** (`deploy/firewall.sh`): browser egress allowed to public internet only; no route from `browser` to core (10.77.1.10:8080, 10.77.4.10:8080), model, sandbox or host. Extend `tests/integration/isolation.sh`.
   Browser isolation probes and read-only `make memory-report` are available (#41); the Contabo A6.4 isolation and A6.5 memory checks passed; the form/file-use smoke passed and Roland accepted M6 on 8 Oct 2026. In `--server` mode a probed service that is not running fails the run. Without a host firewall the probes were run once against real core, sandbox and browser containers in the test stack (#42; the model was not running, so its probe was skipped): all blocked, each target confirmed listening from its own side. `make test-browser` repeats the sandbox and browser part every time.
7. **Fixture site** `tests/fixtures/site/` and `fixture-web` service in `docker-compose.test.yml` (§13.2–13.3): order form, injection page, SPA div-POST, prefilled password, login + `/whoami`, download.
   The fixture site exists with unit tests and the internal `fixture-web` service; the live browser tests run against it (#42).
8. **Chromium sandbox experiment** (`BROWSER_CHROMIUM_SANDBOX`, default `false`): try `true` with a pinned seccomp profile; report the result in the PR. Do not weaken host AppArmor to make it work.
   Result (#42, dev container without AppArmor): with Docker's own seccomp profile Chromium refuses to start sandboxed. With Playwright's published profile it starts only if the container keeps `CAP_SYS_CHROOT`, because the profile allows `chroot` only with that capability. With that one rule changed (`docker/browser/seccomp-chromium.json`) it runs fully sandboxed with `cap_drop: ALL`, `no-new-privileges` and a read-only root, and the whole live suite passes (`CHROMIUM_SANDBOX=1 make test-browser`). Roland chose the Contabo trial with `BROWSER_CHROMIUM_SANDBOX=true` and `BROWSER_SECCOMP=./docker/browser/seccomp-chromium.json` (§6, decision 3). The service is healthy; the main Chromium process has no `--no-sandbox`, and the private-address MAP rules are present in `--host-resolver-rules`. The repo default stays `false`.
9. **UI:** Browser tab showing status, current URL/title and a thumbnail. Vanilla JS, `textContent` only; keep function names used by `tests/frontend/chat.test.cjs`.
   The Browser tab and core status/thumbnail routes exist (#40). Signed-in smoke confirmed navigation, dummy-form cards, composer locking, rejection, delayed confirmation, double-click protection and a phone-sized viewport; fresh phone login and final task completion remain pending.

#### Contract between core and browserd (fixed by part 1; part 2 must implement it)

Core calls exactly the §8.4 routes. Part 1 relies on these details and small additions:

1. **`POST /v1/describe`** takes `{"ref": "e5"}` or `{"focused": true}`. It answers one flat object: `ref, tag, role, name, type, href, value, in_form, form_method, form_action, disabled, sensitive, fingerprint, focused, inside_dialog_title`, plus `url`, `title`, `mode` for the page. Optional extras the classifier uses when present: `aria_label`, `title_attr` (the element's own `title` attribute; plain `title` is always the page title), `form_submit_name` (name of the form's submit control), `submits` (true when the DOM says a click submits a form), `aria_expanded`, `aria_haspopup`, `contenteditable`. `type` is the attribute in lower case, `""` when absent. `href` and `form_action` are absolute URLs. Nothing focused → `404 {"error":"no_focused_element"}`.
2. **`fingerprint`** is 16–128 ASCII letters and digits (a SHA-256 hex digest fits). Core refuses anything else. **It must cover more than the spec's eight fields:** hash every element fact listed in item 10 (use `""` or `false` for a missing one). browserd checks the fingerprint in the same step as the action, so this is what protects actions that run without approval: core's own second look (item 10) only runs for approved ones, and a second `/v1/describe` from core could never be in the same step as the click anyway.
3. **`/v1/press`** accepts an optional `fingerprint` of the focused element and answers `409 element_changed` if the focus moved. **`/v1/select`** accepts `mode` like click. Keys arrive in canonical form: `Enter`, `Space`, `Tab`, `Shift+Tab`, `Escape`, `ArrowUp/Down/Left/Right`, `PageUp`, `PageDown`, `Home`, `End`, `Backspace`, `Delete`, `Control+Enter`, `Meta+Enter`.
4. **`/v1/snapshot`** `elements` may also list structure with no `ref` (headings, landmarks, lists): give those `role`, `name`, optional `level` (1–6) and `depth` (nesting). Core renders every line itself and drops the value of any sensitive or `type=password` element, whatever was sent.
5. **Action answers** carry `url`, `title`, `navigated`, optional `dialogs: [{type, message}]` and, when the POST guard fired, `blocked_submission: {method, url}`. `/v1/back` and `/v1/forward` are guarded in `safe` mode too.
6. **Errors** are `{"error": "<code>"}` with these codes: `user_mode` (423), `element_changed` (409), `sensitive_field` (403), `no_such_element`, `no_focused_element`, `no_such_tab` (404), `too_many_tabs`, `disabled`, `not_typeable`, `not_a_file_input`, `not_a_select`, `no_such_file`, `bad_key`, `blocked_url`, `timeout`. `/v1/navigate` reports a refused address as `200 {"blocked": "<why>"}`.
7. **Limits core enforces on answers:** JSON ≤ 1 MB, screenshot ≤ 5 MB and it must start with the PNG signature.
8. **`browser_wait`** has no route: core sleeps, or polls `/v1/snapshot` once a second for up to 10 s.
9. **`browser_upload`** copies the approved workspace file to `browser/uploads/<name>` (≤ 25 MB) and sends `path: "<name>"` and `sha256` of the bytes. browserd must hash the staged file and answer `{"error": "file_changed"}` if it differs, because the sandbox can write to that folder too.
10. **Second look before approved actions.** browserd's fingerprint covers eight fields (spec §6.5), but the classifier also reads `value`, `aria_label`, `title_attr`, `form_submit_name`, `submits`, `disabled`, `sensitive`, `aria_expanded`, `aria_haspopup`, `contenteditable`, `inside_dialog_title` and the page address. Core pins a SHA-256 of all of these together with the **full page address** (`_pin.seen`) and calls `/v1/describe` again just before every approved action; any difference fails the action. The full address matters in one-page apps, where the same button can mean something else after a route change. For a key press with nothing focused, core binds the page address from `/v1/status` instead. So `/v1/describe` must give the same answer for an unchanged element every time (stable key set, stable values).

**What part 2 added to the contract (#42).** All optional for core, and core treats every one as untrusted data:

11. **More error codes:** `load_failed` (502), `not_clickable`, `no_such_option`, `unavailable` (503, the browser is starting or restarting), `too_large`, `file_changed` (409), `bad_request` (400), `failed` (500). Core has words for each in `agent/browser_client.py` (`ERROR_TEXT`); a test fails if `browserd` gains a code without them.
12. **Answers may also carry** `blocked_submission.new_tab: true` (the form would have posted into a new tab, which is never sent; core does not offer an approval for it), `blocked_background: [{method, url}]` (a form the page tried to submit by itself, outside any action), `popup_closed: true` (the page opened a tab over the limit), and on `/v1/navigate` `download: true` (the address was a file) or `blocked: "leave_dialog"` (the page asked "leave without saving?", which is always answered no). Snapshot elements may carry `checked` (tick boxes, radio buttons) and `options` / `more_options` (drop-down lists).
13. **`/v1/user-mode` is implemented**, not a stub: on hand-back `browserd` takes the focus off the focused element and empties the X selections. The screen for it is M7.

#### Where part 1 differs from the spec (for the reviewer)

Each one is stricter than, or an addition to, `docs/v2-spec.md`; none loosens a rule.

1. **Keyword matching** cuts each text field (name, value, label, title, link path, form address) to 600 characters on its own. The spec cuts the joined text, which lets a page pad one field to push a keyword in another past the cut.
2. **Space key:** also GATED on a tick box outside a form and on unrecognised controls, the same as a click on them. The spec only names buttons.
3. **`browser_select`** is SAFE as in the spec, but becomes GATED `form_submit` for a list whose choice already tried to submit a form. `/v1/select` therefore takes `mode`.
4. **`browser_open`** also refuses addresses that carry a user name or password.
5. **`browser_snapshot`** has an extra optional `start` argument to read a long page in pieces, and its default length follows `MODEL_TOOL_OUTPUT_CHARS` (about 40 elements per piece at 3000).
6. **Tool schemas** carry no `maxLength`/`minimum`/`maximum`, like the existing tools; the limits are enforced in the handlers. Large length bounds make the llama.cpp grammar big.
7. **Approval cards** name the element by what it is ("button", "textbox", "link") and replace quote marks in the page's own text, so a page cannot imitate the card's wording.
8. **No sign-in tool yet** (`request_signin` is M7): a page with a sign-in form tells the agent to stop and tell Roland.
9. **Approvals are bound to more than the fingerprint.** Core re-reads the element before an approved action and compares every fact the classifier used and the full page address (contract item 10). An approved upload is bound to the file's SHA-256, so a file rewritten while the card waits is not sent. `browser_type` is GATED `form_submit` for a field whose typing already tried to submit a form. "The same element again" is recognised by what the element is (page, tag, role, name, type, link and form facts), not by its fingerprint, because the fingerprint changes as soon as text is typed. These came from the automated reviews of #39.

#### Where part 2 differs from the spec (for the reviewer)

Stricter or an addition unless marked **cannot be done as written**; those need Roland to know.

1. **POST guard is always on in agent mode**, not only while a `safe` action runs. A form that a page submits by itself, or a moment after a harmless click has returned, is stopped too and reported with the next answer. It opens only for the action Roland approved (until 2 s without requests, 10 s at most) and in user mode. **It opens for that action's tab only** (and a tab that tab opens during the action): a page in another tab cannot get its own form through while an approval is carried out. One consequence: a form that posts **into a new tab** (`target=_blank`) is never sent by the agent, approved or not, because Playwright cannot say which tab such a request came from until after it has been let through. The agent is told to hand that step to Roland.
2. **Private addresses are also refused inside Chromium** (`--host-resolver-rules`), because Playwright does not pass redirects or WebSockets to the request guard. The guard also refuses the forms a browser reads as an IPv4 address that don't look like one (`127.1`, `2130706433`, `0x7f.0.0.1`), `.local`, `.lan`, `.home.arpa`, single-label names and addresses with a user name.
3. **The fingerprint is computed by `browserd` in Python**, from facts it has checked and cut, and it covers every fact in contract item 10. `snapshot.js` runs inside the page, so its answer is treated like any other page data.
4. **Secret fields:** by default the spec's substring rule **and** a whole-word rule that also reads the label and placeholder; a field is secret if either says so. `BROWSER_SENSITIVE_MATCH=word` uses the word rule alone (§6, decision 8). The rule applies to fields, not to links or buttons whose id happens to contain "pin".
5. **Blocked requests are aborted the way the Stop button does it**, so the tab stays on the page it was on and the agent can ask Roland properly. Aborting with "blocked by client" replaced the page with an error page.
6. **Extra Chromium settings:** service workers are blocked (they would bypass the guards), the page cache has a size limit, the profile's preferences are also written before every start, and managed policy additionally blocks extensions, leak detection, search suggestions and DNS-over-HTTPS.
7. **`DeveloperToolsAvailability=2`: cannot be done as written.** That policy also switches off the pipe Playwright drives Chromium through; the browser never starts. It is left out. The agent has no key or address that opens developer tools.
8. **Policy folder: differs from the spec.** Playwright's Chromium is "Chrome for Testing" and reads `/etc/opt/chrome_for_testing/policies/managed/`. The file is copied to both that folder and `/etc/chromium/policies/managed/`. Checked by hand: with the file only in the spec's folder nothing is applied.
9. **Not built from the spec's element list:** `rect` (core has no use for coordinates) and lists/tables in the outline (headings, page regions, frames and the page text are there).
10. **Not in M6:** the browser container did not join the `vnc` network and nothing started x11vnc (the package is in the image). Both came with M7 part 2.
11. **Compose:** the service sits behind the `browser` profile, so an ordinary deploy does not start it. Its seccomp profile comes from `BROWSER_SECCOMP` (default: Docker's own).
12. **The `tester` service has its own image now** (`docker/tester/Dockerfile`, test tools installed at build time). As it was written it installed pytest when the container started, with a read-only root and only internal networks; in the dev container that ended in "pytest: command not found", so `make test-sandbox` did not get as far as the tests there.
13. **Downloads** arrive in `browser/.incoming` and are moved to `browser/downloads` under a plain, unique file name; a name from a website is never used as a path. A download that grows past the limit (`BROWSER_MAX_DOWNLOAD_MB`, 200 by default) is stopped while it arrives, not after.
14. **Tabs:** the last tab is replaced by a blank one before it closes, because Chromium quits with its last tab. A tab a page opens over the limit is closed and reported.

#### Security requirements

1. Profile storage (`/profile`) is mounted only into `browser`. Never mount it into core, sandbox or the workspace. Never add any endpoint that exports, reads or sets cookies, passwords, local storage or arbitrary JS (`evaluate`). `test_no_cookie_or_eval_endpoints` enforces this.
2. browserd listens only on `browser_ctl`. No published port, no CDP port.
3. Page text is untrusted: wrap it as tool output and taint the run.
4. Consequential submissions (payment, message, public post, delete, form submit, default-deny) require approval through the gate; the POST guard is a runtime backstop for misclassified SAFE clicks.
5. Screenshots never go to a text-only model.

#### Tests / acceptance

Automated (must pass in CI or `make test-integration`):

- [x] **A6.1** `tests/test_policy_browser.py`: ≥ 60 table-driven descriptors covering every §9.4.1 rule, both languages, default-deny, submit default type, disabled elements, precedence. *(Part 1: 99 descriptors, plus address, key and typing tables.)*
- [x] **A6.2** `tests/test_browser_tools.py` (fake browserd via `httpx.MockTransport`): password typing forbidden; Enter gated except search; non-http forbidden; fingerprint mismatch fails; blocked submission reported; user-mode 423 mapped; snapshot output wrapped and taints. *(Part 1.)*
- [x] `test_screenshot_not_sent_to_text_only_model`. *(Part 1, in `tests/test_browser_tools.py`.)*
- [x] **A6.3** `tests/integration/test_browser_live.py` against the fixture site: place-order needs approval (0 POSTs before, exactly 1 after); injection page cannot trigger delete; SPA div POST blocked in safe mode; password value never in snapshot; profile persists across browser restart; downloads land in workspace; no cookie/eval endpoints; private-IP navigation blocked. *(Part 2: 32 tests, run by `make test-browser` in the compose test stack. **Not in CI yet:** adding a CI job means editing `.github/workflows/ci.yml`, which the credentials used so far cannot push. Whoever has `workflow` scope adds a job that runs `make secrets` and `make test-browser` on a disposable runner.)*
- [x] Unit tests for the service itself: `tests/test_browserd_guards.py`, `tests/test_browserd_server.py` (exact route table, auth, limits, user mode, no cookie/eval surface) and `tests/frontend/snapshot.test.cjs` (the page script; run by `pytest` and `make test`).
- [x] **A6.4** isolation script: all sandbox/browser targets blocked on Contabo at `60974e6`; normal verification after #51's rebuild passed **11 pass / 0 fail / 1 skip**. #50 makes stdin closure automatic for ordinary verification.
- [x] **A6.5** Contabo memory acceptance: supplied `WATCH=600 make memory-report` reports **A6.5 PASS** (peaks below); completed A6.4 verification also passed. Final M6 acceptance was separate: the form/file-use smoke passed and Roland confirmed M6 on 8 Oct 2026.
- [x] Existing suites still green: `make lint`, `make test`. *(`make test-integration` only runs in GitHub CI.)*

Code Shipper (Grok) smoke on Contabo (after deploy with `BROWSER_ENABLED=true`):

1. `docker compose ps` shows `caddy`, `core`, `model`, `sandbox`, `browser` healthy.
2. In chat: "Open https://example.com and tell me the page title." Expect a correct answer with no approval card.
3. Ask the agent to submit a form on a harmless public test page. Expect an approval card with a screenshot; reject it; confirm nothing was submitted.
4. Type "yes" in chat while a card is pending. Expect the composer to be locked or the text to have no effect on the approval.
5. **A6.5:** run a ~10-minute browsing task; record `docker stats` peaks and `free -m`. Browser stays ≤ 1280 MiB, no OOM kill in `dmesg`/`docker inspect`, host available memory ≥ 800 MiB. Record in the PR.
6. `make verify` passes, including isolation.

#### Recorded Contabo smoke and memory evidence (8 Oct 2026)

- All five services healthy. "Open https://example.com and tell me the page title" was answered correctly with no approval card. Chromium's own sandbox is on; the main process has no `--no-sandbox`, and the private-address MAP rules are present.
- On `httpbin.org/forms/post`, "Submit order" produced a payment-category approval card with a screenshot and two-tap confirmation. Run 1 was approved by a double-click and sent the harmless form (fixed by #46). Run 2 kept the composer locked; typing "yes" did nothing. Rejection sent nothing and left the browser on the form page, but the model asked for the same click again (fixed by #47).
- Earlier smoke used the `ea7e429` app image and `a0dbf22` host checkout. #46/#47 controls passed: double-click did not approve, rejection left `/forms/post` unchanged, and a separate delayed confirmation submitted the dummy form to `/post`. The model still repeated reads/snapshots to six steps without an answer and falsely replied “saved”. #51 addressed those completion defects. After its rebuild at `60974e6`, one file read answered `PINE-4827`, one write saved a note with independently verified contents `REQUESTED_NOTE_OK`, and the first form rejection produced a truthful final answer without a read loop. However, a separate submission request took a 500-character snapshot, asked to click Telephone, then Customer name after rejection; both were rejected and the run stopped. The compact-snapshot/rejection follow-up still needs deployment and deliberate-submission retesting. Upload, ordinary chat, missing-file handling and phone-sized controls passed across these sessions. Fresh physical-phone login was not tested. #45 preserves a length-limited reply with a "send continue" note.
- Snapshot before M6: `pre-m6-deploy-2026-10-08`. The deploy advanced from `c81ef05` to `23fd1fc` and ended with `Deploy OK`; the original pre-browser host was `98971cc`, not `a27b5ff`.

| Supplied `WATCH=600` report | Observed peak / minimum | Configured cap |
|---|---|---|
| Model peak | 3084.29 MiB | 3840 MiB |
| Browser peak | 822.80 MiB | 1280 MiB |
| Core peak | 70.37 MiB | 640 MiB |
| Sandbox peak | 71.83 MiB | 1024 MiB |
| Caddy peak | 16.46 MiB | 96 MiB |
| Minimum host available | 3553 MiB | — |

The report ends with **A6.5 PASS** and records no OOM kills or restarts; swap usage is zero in the supplied samples. This is measured usage, not the sum of service caps. Bare verification originally failed on the first probe at 137; closing stdin fixed it, and #50 made that automatic. Normal verification at `a0dbf22` passed **11 / 0 / 1** with **3640 MiB** available. Roland's latest rebuild/deploy of `60974e6` also completed **11 pass / 0 fail / 1 skip**, with **3698 MiB** available; the human login/chat/approval checklist remains skipped. A6.4 and A6.5 are complete; file-read/save retests now pass. After #52 was rebuilt at `7b09b90`, verification again completed **11 pass / 0 fail / 1 skip**, with **3633 MiB** available. After #53 was rebuilt at `50767ec`, verification completed **11 pass / 0 fail / 1 skip**, with **3730 MiB** available, and the unhinted form test passed. The fresh phone login then passed and Roland confirmed M6.

#### Production flags

`BROWSER_ENABLED=true` is live on Contabo. `SCREEN_ENABLED`, x11vnc listening, `novnc` and `request_signin` remain off there until Roland switches the screen on (M7, "Contabo deploy notes"). Training stays off. **Roland confirmed M6 on 8 Oct 2026; M7 may be built, with its flags off in production until it is merged and smoked.**

#### Contabo deploy notes

0. **Even with the browser left off,** the first deploy of a version that contains #42 needs `secrets/browser_api_token`, because core now mounts it. Run `make secrets` first (it creates what is missing and leaves the rest alone). `make preflight` fails without the file, so the deploy stops before anything is recreated.
1. `make secrets` to create `secrets/browser_api_token` (uid 1000, 0400). Never print it.
2. `APPLY=1 make firewall-install` to apply the browser rules before starting the service.
3. To switch the browser on, put both `COMPOSE_PROFILES=browser` and `BROWSER_ENABLED=true` in `.env`, then deploy. To switch it off again, remove both and deploy; `docker compose --profile browser stop browser` stops the container at once. The `browser-profile` volume keeps Roland's sign-ins; never delete it without him.
4. Build the browser image when the model is idle (builds need ~1–1.5 GB of memory temporarily). The image takes about 3.8 GB of disk: Playwright's base image is 3.5 GB, and removing the browsers that are not used does not give that space back. Check free disk first (`df -h`).
5. Keep `MODEL_CTX=3072` and `MODEL_MEM_LIMIT=3840m`. If headroom fails, stop and ask Roland (§6). Do not lower other services' limits silently.
6. Take a Contabo snapshot before the first browser deploy.
7. Roland chose Chromium's own sandbox on as a trial (deliverable 8 / decision 3); the supplied Contabo smoke shows it on and the browser healthy. Do not change host AppArmor.

---

### M7 — Screen and sign-in (`v2-m7-screen`)

**Spec:** §6.6, §6.7, §6.8, §8.2 (screen/sign-in endpoints), §8.4 (`/v1/user-mode`, `/v1/vnc/disconnect`), M7 in §12.

#### Status: accepted by Roland (8 Oct 2026)

M7 is split into PRs to `v2`, like M6.

- **Part 1, core side: merged (#55).** Deliverables 4, 5, 6 and 7 below, the core half of 2 (the client calls for `/v1/user-mode` and `/v1/vnc/disconnect`), and the tests for A7.1 and A7.2.
- **Part 2, the services: merged (#57).** Deliverables 1, 3 and 8, the browserd half of 2 (a real disconnect, `healthz.vnc`), the compose service and networks, `SCREEN_ENABLED` accepted by `python -m agent`, the Caddy route changes, preflight/verify/isolation, and A7.3. The repo default stays off: `.env.example` has `SCREEN_ENABLED=false` and no `screen` profile.
- **Verified off the server:**
  - Lint, the unit suite, the page tests and shellcheck.
  - `make test-browser` in Docker with the real browser image, x11vnc and the relay: 34 browser tests, 7 screen tests (`tests/integration/test_screen_live.py`), the container checks and the isolation probes. The screen tests talk to x11vnc with a small VNC client: the view-only password cannot type, click or paste; a wrong password gets nowhere and a password is always asked; the screen's clipboard is never sent out; one call cuts every viewer and the server is back at once; an address that is not noVNC cannot log in even with the passwords; and a whole sign-in, with the test playing Roland at the screen while the real agent loop waits.
  - A real browser (Chromium, phone and desktop width) against the production compose file with Caddy, core, the relay, the browser container and a scripted stand-in for the model: sign-in card, sign-in screen, typing a user name on the picture and a password through the phone typing box, I'm done, the agent's answer from the signed-in page; then Watch, Take control, Hand back, and logout cutting a watcher. No script or CSP error. The typed password reached the test site and appeared in no container log. Peak memory of the relay: 32 MiB of its 64.
  - The CI edge job runs the screen phase without a browser container (see A7.3).
- **On Contabo (8 Oct 2026):** deployed at `4d0f703` in the two steps below. Screen off: verify 12 / 0 / 2, 3806 MiB available. Screen on: verify 13 / 0 / 1, 3746 MiB available, with the screen server check and the noVNC isolation probes. The smoke test (steps 1 to 3 of the list under "Tests / acceptance") passed in a logged-in desktop browser.
- **Sign-in follow-up: merged (#58) and deployed** after Roland's first try on the server (next list).
- **Second try, after #58 (22:37–22:39):** the plain "log in to https://www.kotipizza.fi/" produced the card after one model call; Roland opened the sign-in screen, clicked Kirjaudu, signed in and pressed I'm done; the agent was handed the page and answered "Signed in successfully. The page shows the Kotipizza homepage with available menu items and order options."; Close returned him to the chat. The audit log has `signin_requested`, `screen_session_start` / `end` (128 s, `signin_done`) and `signin_resolved`, and nothing typed. The answer names nothing on the page that shows he is signed in, so he then asked "Take a snapshot of the page and tell me whose name is shown in the top bar."; the agent took a snapshot and answered "The name shown in the top bar is 'Roland'." It was done on his PC in a desktop app's browser pane, not on the phone (see the DNS note under "Known issues").
- **Chat refresh: written** (item 5 of the next list).
- **After #58:** `make verify` ended 13 / 0 / 1 with 3488 MiB available. The password check over the logs of browser, novnc, core and caddy found nothing: with the command in the deploy notes (22:57) his password was received and was in 0 of 527 log lines, and the e-mail address he signed in with was not there either. Earlier runs, with a prompt that showed nothing, printed 0 and, when Enter was pressed with nothing typed, 506: an empty search matches every line.
- **Accepted:** at about 23:04 Roland said to record M7 as finished: the browser screen works to the standard he wants, no password was saved, and other faults can be fixed later if they come. (His first try did not count: he pressed I'm done without signing in.) Known and left as they are: the sign-in was done on his PC, not the phone (the phone could not look up the server's name on mobile data that evening); the agent opens the address it is given and does not look for the site's sign-in form; a new tab and the address bar's suggestion box show "This page is blocked"; and a form that posts into a new tab leaves a blank tab behind. No code change is pending for M7 beyond the chat-refresh fix.

**Found by running the real thing, and fixed in part 2:**

1. **The agent was told "Roland finished" before it had the browser back** (part 1). The tool returned on the stored status while core was still cutting the screen; the next call came back "Roland is using the browser right now". The tool now waits for the hand-over (`agent/signin.py`, test in `tests/test_signin.py`).
2. **Core refused Caddy's question about the websocket**, because the question carried the browser's upgrade headers. Caddy now strips them from the question only.
3. **The relay ran out of memory** when the page asked for noVNC's fifty files at once over kept-alive connections. Caddy now uses one connection per request, eight at a time, on that route.
4. **Pasting into the screen did nothing for 45 seconds** after connecting (an x11vnc default meant for login screens). Switched off with `X11VNC_AVOID_WINDOWS=never`.
5. **The screen showed the wrong tab.** A tab browserd cannot see had opened in front of the agent's. browserd now puts the agent's tab in front after every action and whenever core says who has the browser.
6. **A live M6 test expected `request_signin` among the browser tools** of an agent without the screen. Corrected.

**Found on the server by Roland's sign-in tries (8 Oct 2026). Items 1 to 4 are from the first try and were handled in the sign-in follow-up (#58); item 5 is from the second:**

1. **The model refused "log in to this site https://www.kotipizza.fi/"** with "I can't assist with logging into websites" and no tool call. In grammar mode the prompt lists tools by name and argument types only, so the one sentence about `request_signin` in the browser note was all it had. One line next to the tool list now says what to do when Roland asks to log in (`SIGNIN_HINT`, `agent/models/context.py`); the browser note is plainer too. "use sign in tool for the site" had worked.
2. **After I'm done the agent said "successfully signed in" without looking**, and Roland had not signed in. The tool result said "Roland says he finished signing in… Take a snapshot to confirm" and the model stopped there. The result now says the button proves nothing and carries the page as it is, read by core once the browser is back (`page_now` in `agent/tools_browser.py`). A sign-in form that is still showing is called out, and the model is told not to ask again unless Roland does. If the page can't be read, the model is told to take a snapshot. The card's end state reads "You pressed I'm done", not "Signed in".
3. **Close on the screen page shut the tab that held the chat.** In the app pane Roland used, the sign-in screen loaded in the chat's own tab, not a new one. Close now goes back to the chat when the tab has shown another page before (`history.length` above 1) and only shuts a tab opened for the screen.
4. **The screen showed the site's front page, not its sign-in form.** That is how the tool works: it opens the address it is given and does not look for the form. Finding the form would cost three or four more model calls (about a minute each on Contabo) and often an approval card for the Log in button. Not changed; the card and the screen now say that the sign-in form may have to be opened on the site first. Roland can ask for the other behaviour.
5. **Back in the chat it said "The agent is still answering here… reopen the chat in a moment"** and stayed that way. With Close now returning to the chat in the same tab, the chat is reloaded while the agent is still working, and a reloaded chat cannot join the running answer. The chat page now asks again every three seconds while the chat is busy and redraws when the answer, an approval card or a sign-in card is new (`watchBusyChat` in `app.js`); the note says so.

The wording for 1 and 2 was tried against the real model before it was sent: the pinned `Qwen3-4B-Instruct-2507-Q4_K_M.gguf` (SHA-256 checked) under the pinned llama.cpp image with the production flags, `MODEL_CTX=3072`, grammar mode and six tool steps, driving the real agent loop with canned browser results. Before: the plain request was refused 2 times out of 2, and once more with only the browser note reworded. After: 12 sign-in requests out of 12 called `request_signin` first ("log in to this site …", "sign in to …", "can you log me into …", "use sign in tool …", "check my order history on …", and one in Finnish). With the sign-in form still showing it answered "You are not signed in yet"; with an account page, that he is signed in; after Cancel, that it was cancelled. Three ordinary requests (open a page and read its heading, list items from a page, a sum) behaved as before and did not ask for a sign-in. This is a small sample on one model file, not a guarantee.

**Known issues:**

- **"This page is blocked" in a new tab and under the address bar** (seen on the server while in control). Chromium's policy blocks its internal pages, and the new-tab page and, most likely, the suggestion box under the address bar are such pages in this Chromium. Typing an address and pressing Enter works. Allowing those two pages would loosen an M6 policy and needs Roland's OK and its own PR.
- **The phone could not find the server on mobile data** (8 Oct 2026, 21:49): the operator's DNS answered "no such name" for `37-60-226-214.sslip.io` while public DNS resolved it. Nothing on the server is involved. Private DNS on the phone (for example `dns.google`) or Wi-Fi gets round it; a domain name of Roland's own would end the dependence on sslip.io (his decision, see §6).
- **From M6, now visible:** a form that posts into a new tab is refused (by design), but the blank tab it opened stays in the browser window. Playwright never reports it, so browserd cannot count or close it. Roland can close it on the screen; a browser restart also removes it. A fix would answer that first request with an empty page instead of aborting it, so the tab becomes visible to browserd and can be closed. Not done here: it changes an M6 guard and needs its own review.

#### Contract between core and the services (fixed by part 1; part 2 must implement it)

1. **`POST /v1/user-mode` `{"on": bool}`** answers `{"mode": "user"|"agent"}`. Core treats any other answer as a failure and then refuses to hand out a VNC password.
2. **`POST /v1/vnc/disconnect`** must drop every VNC client and answer `{"ok": true}`. Core calls it when a screen session ends for any reason, when a sign-in is resolved, and once after core starts. It is called *before* user mode is switched off.
3. **`/v1/status` stays open in user mode** and reports `mode`. Core reads it every 10 seconds while the screen is on and corrects browserd when the mode differs from the records.
4. **Caddy `forward_auth`** sends `GET /internal/screen-auth?kind=ws|static` with the visitor's `Cookie` and, for the websocket, `Origin`. Core answers 200 (pass), 401 (no valid login) or 403 (screen off, no active screen session for that login, unknown `kind`, or a websocket `Origin` other than `https://{AGENT_HOST}`), with no body.
5. **The page** imports `RFB` from `/screen/novnc/core/rfb.js` and connects to `wss://{host}/screen/websockify`. It uses `viewOnly`, `scaleViewport`, `clipViewport`, `dragViewport`, `focusOnClick`, `sendKey(keysym, null)`, `disconnect()` and the events `connect`, `disconnect`, `securityfailure`, `credentialsrequired`. The noVNC release pinned in part 2 must provide these.
6. **Passwords:** core returns `VNC_VIEW_PASSWORD` for watch and `VNC_PASSWORD` for control. x11vnc must enforce view-only for the first.

#### Where part 1 differs from the spec (for the reviewer)

Each one is stricter than, or an addition to, `docs/v2-spec.md`; none loosens a rule.

1. **A second lock in core.** Besides browserd's user mode, `BrowserClient` refuses the agent's calls itself while Roland has control or a sign-in waits. browserd forgets user mode when it restarts.
2. **`request_signin` takes the site from the address.** `site` is accepted but not used, so the card cannot name one site and open another.
3. **`request_signin` does not open an address `browser_open` would ask about.** The model must open that page with approval first; when the browser already shows the address, nothing is navigated.
4. **One sign-in at a time**, and none while Roland already controls the browser.
5. **I'm done counts without the screen having been opened** (he may be signed in already).
6. **A restart cancels waiting sign-ins and ends screen sessions.**
7. **`screen_session_end` also records a reason** (released, expired, logout, replaced, restart, signin_done …) next to mode and duration.
8. **The screen page keeps the normal CSP** (`frame-ancestors 'none'`); it is not shown in a frame.
9. **The screen page has three extra controls:** Take control (from watching), Keyboard (a phone needs a text box to show its keyboard) and Zoom in / Fit screen. It also gives the browser back when the page is left or the connection drops, instead of waiting for the idle timeout.
10. **Jobs:** `request_signin` is not offered; the job is told to answer "Needs sign-in to <site>". There is no separate notice on the Approvals tab.
11. **`GET /api/signin?status=pending`** lists sign-ins that are waiting or under way; no other status is served.

#### Where part 2 differs from the spec (for the reviewer)

Each one is stricter than, or an addition to, `docs/v2-spec.md`, except 4 and 12, which are choices Roland can reverse.

1. **x11vnc has five more flags and one environment setting** than §6.5 lists: `-no6` (this build also listens on IPv6 unless told not to), `-allow` (noVNC's address only), `-safer` and `-nocmds` (no remote control, no reverse connections, no external commands), `-norc`, and `X11VNC_AVOID_WINDOWS=never` (fix 4 above). Every flag was checked against x11vnc 0.9.16 in the image.
2. **Disconnecting replaces x11vnc** instead of `x11vnc -R disconnect:all` (the spec allows either). The service signals the launcher, which ends x11vnc and starts a new one; the route answers once the new one listens, about a tenth of a second. A process that has ended holds no connections, and remote control can stay closed.
3. **websockify is installed alone** (`pip --no-deps`): numpy, requests, jwcrypto and redis, which it declares, are for its token plug-ins and for speed that a screen's key and pointer traffic doesn't need. It runs with `--file-only` (no folder listings) and `--heartbeat=30` (a ping every 30 seconds, so a phone network doesn't drop an idle screen).
4. **Only noVNC's `core/` and `vendor/` are in the image**, not its own pages. So `/screen/novnc/vnc.html` is 401 without a login and 403 without a screen session as the spec asks, and 404 for someone who has both. noVNC is **1.7.0**; its SHA-256 pin was made by rebuilding the release tarball from the tag and matching Gentoo's published checksum for the same file (GitHub's tarball download was blocked where this was written). The CI edge job builds the image from GitHub, which checks the pin again.
5. **Caddy:** the upgrade headers are stripped from the websocket question to core; the script route refuses anything but GET and any websocket; keep-alive is off and at most eight connections go to the relay for scripts; the websocket is exempt from the 2 MB request-size cap; `X-Content-Type-Options: nosniff` is added; both routes are wrapped in `route { }` so their steps run in the written order.
6. **Core checks the VNC passwords more closely:** they must differ in their first eight characters and be one plain line x11vnc can read. `SCREEN_ENABLED=true` without `BROWSER_ENABLED=true` is refused.
7. **Core mounts the two VNC secrets whether or not the screen is on** (as in §11.3). Preflight therefore asks for the files always and says how to create them.
8. **Housekeeping only talks to browserd when something is open or owed**, not every 10 seconds all day.
9. **The screen page starts zoomed in on a phone** (fitted, a 1280-wide screen is unreadable there) and shows the picture at the top instead of centred.
10. **browserd reports `vnc` in its health answer without letting it decide `ok`**; `python -m browserd healthcheck screen` asks for both, and `make verify` uses it when the screen is on.
11. **The test stack lets the tester in to x11vnc** at 10.77.5.31 (`VNC_ALLOWED_PEERS`, a new browser setting). Production has noVNC's address only; preflight checks it.
12. **The relay's limits are the spec's** (64 MiB, 0.25 CPU, 32 processes). They hold because of 5; without it they don't.
13. **What the model is told after I'm done** (sign-in follow-up) is not §6.7's "Roland says he finished signing in to {site}. Take a snapshot to confirm." It is that he pressed the button, that this proves nothing, and the page as it is now. Stricter: the model answered from the old line without looking. Because page content now comes back with the answer, `request_signin` taints the run like a snapshot does (the §9.3 table has it as not tainting).
14. **The prompt has a line about sign-in requests next to the tool list** (sign-in follow-up), which the spec does not mention. It is only there when `request_signin` is offered.
15. **Close on the screen page** goes back to the chat when the page was loaded in the chat's own tab (sign-in follow-up).

#### Goal

Roland can watch or take control of **the same Chromium the agent drives** from his browser or phone, through authenticated same-origin noVNC, and sign in to sites himself. The agent never sees credentials.

#### Dependencies

M6 merged and smoked on Contabo. Caddy screen routes already exist in `docker/caddy/Caddyfile`; enable and test them now.

#### Deliverables

Expected locations per `docs/v2-spec.md`; verify in tree:

1. **x11vnc** started by the M6 launcher on display `:99`, listening only on the `vnc` network (10.77.5.40:5900), with a 0600 `-passwdfile` containing a full-access password and a view-only password (`VNC_PASSWORD_FILE`, `VNC_VIEW_PASSWORD_FILE`), plus `-noprimary -noclipboard -quiet -forever -shared`. View-only is enforced by the server. Never use `-nopw` or `-debug_keyboard`. There is exactly one Chromium; do not start a second browser for the screen.
2. **browserd** `/v1/user-mode` (`on`/`off`; every action route returns 423 while on) and `/v1/vnc/disconnect` (disconnect all VNC clients). Clear selections on handback.
3. **`docker/novnc/Dockerfile`** + compose `novnc` service: websockify pinned and hash-locked (`requirements-novnc.lock`), noVNC release tarball verified by hard-coded SHA-256 at build time, uid 1000, `websockify --web /opt/novnc 10.77.2.30:6080 10.77.5.40:5900`. On `screen` (10.77.2.30) and `vnc`. Limits: 64m, 0.25 cpus, pids 32.
4. **Core:** `GET /screen` serving `screen.html` + `screen.js` (RFB imported same-origin from `/screen/novnc/core/rfb.js`); `POST /api/screen/session` (returns the mode's VNC password in the response body only, never in HTML); `/api/screen/heartbeat` (every 60 s); release; `/internal/screen-auth` (200 only if peer is Caddy, session cookie valid, an active screen session exists for that login, and for `kind=ws` `Origin == https://{AGENT_HOST}`).
5. **Screen session rules:** one session at a time; bound to the login session hash and mode; expires after `SCREEN_SESSION_IDLE_MIN` (30) without heartbeat; on release, expiry, logout or replacement, core calls `/v1/vnc/disconnect`.
6. **Sign-in:** `agent/signin.py`, the `request_signin` tool, `signin_requests` state, SSE `signin_required`, chat sign-in card with **Open sign-in screen**, **I'm done**, **Cancel**; "I'm done" also in the screen toolbar; timeout `SIGNIN_TIMEOUT_MIN` (30).
7. **UI:** Watch and Take control buttons, screen toolbar (I'm done, Hand back to agent, Switch to watch, Close).
8. **Secrets:** `vnc_password`, `vnc_view_password` (must differ). `agent/config.py` already refuses `SCREEN_ENABLED=true` without both; keep that.

#### Security requirements

1. **Watch** uses the view-only password; the agent may keep working.
2. **Take control** sets browserd user mode. While Roland is in control or a sign-in is pending, **all agent browser reads, actions and captures are paused**: snapshot, describe, click, type, press, screenshot and approval screenshots return the locked message. Training capture (M8) must also skip these steps.
3. The sign-in resolves **only** through the explicit Done button (`POST /api/signin/{id}/done`). A chat message saying "done" must not resolve it. The composer is locked while a sign-in is pending.
4. **Jobs cannot request interactive sign-in.** `request_signin` is unavailable in jobs (`in_jobs=False`); a job that hits a login page fails with a message.
5. No keystroke logging anywhere: Caddy access logs off for `/screen/*`, x11vnc `-quiet`, websockify logs connect/disconnect only. Audit records screen session start/end with mode and duration only.
6. VNC passwords never appear in logs, audit, HTML or URLs. novnc and x11vnc are unreachable except through Caddy forward-auth.

#### Tests / acceptance

Automated:

- [x] **A7.1** `tests/test_screen_auth.py`: requires session; requires active screen session; WS origin must match; peer must be Caddy; only one screen session; logout ends screen and disconnects; VNC password not in logs or audit. *(Part 1: the seven named tests plus the screen switched off, refused requests, a failed hand-over, the core-side lock and restart clean-up.)*
- [x] **A7.2** `tests/test_signin.py`: request sets user mode and waits; Done resolves and unlocks; cancel and timeout messages; agent browser tools locked during sign-in; chat text "done" does not resolve; `request_signin` not available in jobs. *(Part 1: the six named tests plus Stop, restart, refused addresses, the `browser_open` approval rule and the prompt staying within budget.)*
- [x] **A7.3** integration: view-only password cannot send input; novnc unreachable from tester without forward-auth; `/screen/novnc/vnc.html` without a session → 401 via Caddy. *(Part 2: the first two in `tests/integration/test_screen_live.py`, run by `make test-browser`; the third in the CI edge job's screen phase, with 403 for a login that has no screen session and the relay unreachable from core and the sandbox.)*
- [x] All M6 and earlier suites still green. *(Unit suite, page tests, and the 34 live browser tests with the screen switched on.)*

Code Shipper (Grok) smoke on Contabo, then **ping Roland** for the manual part (A7.4 is a substantial test with his real credentials):

1. Without logging in, `curl -I https://37-60-226-214.sslip.io/screen/novnc/vnc.html` → 401. *(Passed 8 Oct 2026: `make verify` reports the screen routes as 401 / 401.)*
2. Logged in: open Watch; confirm the visible page is the agent's current tab (same browser). *(Passed 8 Oct 2026; clicks and key presses while watching did nothing.)*
3. Take control, then ask the agent to snapshot. Expect "Roland is using the browser right now". *(Passed 8 Oct 2026 by calling the Browser tab's screenshot route instead of a chat turn: 423 `user_mode`, and browserd reported mode `user`.)*
4. **A7.4 (Roland):** *(Second try on 8 Oct 2026 went through on his PC, see Status; the password check found nothing; Roland confirmed M7 the same evening.)* ask the agent to check something behind a login on a site Roland chooses; sign-in card appears; Roland takes control on the phone, logs in, presses I'm done; agent continues and reads the logged-in page. Audit shows `signin_requested`, `screen_session_start`/`end`, `signin_resolved` and no keystroke data. `docker compose logs browser novnc core caddy | grep -i <password>` finds nothing.
5. `make verify` passes. *(13 / 0 / 1 on 8 Oct 2026 when the screen was switched on, and again after #58 with 3488 MiB available.)*

#### Off in the repo; on on Contabo since 8 Oct 2026

`SCREEN_ENABLED`, the `novnc` service (the `screen` profile), x11vnc, `request_signin`. The repo default is off for all of them; Roland switched them on on Contabo with step 2 below.

#### Contabo deploy notes

Roland runs these himself. Two steps, so that the code is on the server and verified before anything is switched on. **Both were done on 8 Oct 2026** at `4d0f703`, with the results each step says to expect (3806 MiB available after step 1, 3746 MiB after step 2). The sign-in follow-up (#58) was deployed the same evening (core restarted at 22:34) and the chat refresh needs the same: the usual `git pull --ff-only`, deploy and verify; no setting changes.

**Step 1, after part 2 is merged: deploy with the screen still off.** Core mounts the two VNC secret files from this version on, so `make secrets` comes first. It creates what is missing and changes nothing that exists.

```
cd /opt/roland-agent && git pull --ff-only && sudo env APPLY=1 make secrets && sudo env APPLY=1 make deploy && sudo make verify
```

Expect `verify: 12 pass / 0 fail / 2 skip`: one more pass than before (the screen routes refuse a visitor without a login) and one more skip (the screen server, because the relay isn't running). Nothing visible changes.

**Step 2: switch the screen on.** In `/opt/roland-agent/.env` set `SCREEN_ENABLED=true` and `COMPOSE_PROFILES=browser,screen`, then deploy and verify as usual:

```
cd /opt/roland-agent && sudo env APPLY=1 make deploy && sudo make verify
```

Expect `verify: 13 pass / 0 fail / 1 skip`, with `[PASS] screen server (x11vnc) listening in the browser container` and the two new isolation lines for noVNC. The Browser tab then shows Watch screen and Take control.

To switch it off again: `SCREEN_ENABLED=false`, `COMPOSE_PROFILES=browser`, deploy.

Notes:

1. Firewall: the `vnc` and `screen` networks are internal and nothing new is published. The rule that drops connections from the browser to the relay is already in `deploy/firewall.sh`; deploy applies it. Re-run the external port scan (only 22, 80, 443, 443/udp).
2. Memory: the relay is capped at 64 MiB (13 MiB idle, 32 MiB at its busiest in the test runs). x11vnc runs inside the browser container's existing 1280 MiB cap. Re-measure headroom with a screen session open (`make memory-report`).
3. On a phone the screen page starts zoomed in: drag to move around, Fit screen to see the whole browser. Typing goes through the Keyboard button.
4. For A7.4, ask in plain words ("log in to <site>"). The screen opens on the address the agent was given, often the front page: open the site's sign-in form there, sign in, press I'm done. The agent then reports what the page shows.
5. For A7.4, look for the typed password afterwards, as the acceptance asks. Use this. It keeps the password out of the shell history and off the screen (nothing shows while it is typed), says how many characters it received and how many log lines there are, and searches only if something was typed. An empty search matches every line, which is how a run on 8 Oct 2026 printed 506. Expect the password's length and `log lines containing it: 0`:

   ```
   cd /opt/roland-agent && read -rsp "Type the password (nothing shows while you type), then press Enter once: " P; echo; L=$(sudo docker compose logs browser novnc core caddy 2>&1); echo "received ${#P} characters; log lines in total: $(printf '%s\n' "$L" | wc -l)"; if [ -n "$P" ]; then echo "log lines containing it: $(printf '%s\n' "$L" | grep -c -F -- "$P")"; else echo "nothing was typed, so nothing was searched"; fi; unset P L
   ```

---

### M8 — Persona and training pipeline (`v2-m8-model`)

**Spec:** §6.10, §6.11 (all subsections), §7.4 (migration m0003), §8.2 (persona/feedback/training/model endpoints), M8 in §12.

#### Branch status (8 Oct 2026)

All ten deliverables are written in draft PR #56. The branch is rebased on the final M7 baseline in `v2` at `e2f0792` (#57–#59), including chat refresh and preserving its screen service, routes, browser hand-back, passwords and deployment checks. Screen/trainer profile composition and capture exclusions are covered by combined tests. CI fixes update backup expectations for migration 3, load `TRAINING_DATA_DIR` as a path, require every mounted secret, annotate the generated activation script for ShellCheck, and add hash-locked protobuf to both training environments. CPU training and conversion now complete; model-client handling reads streamed schema-error bodies before constrained fallback, while unrelated HTTP errors still fail immediately. Three real-stream transport regression cases cover both rejection statuses and refusal to retry other errors. The synthetic model now declares 4096 context tokens, matching evaluation; its regression test covers the full public suite plus the output allowance because the pinned server caps slots at the trained context. Complete byte fallback fixes the pinned server tokenizer failure on newlines and unseen characters; a Unicode round-trip regression covers it. A literal assistant prefix avoids artificial whitespace that caused the pinned sampler to discard its first token; a separate prefix regression covers this. Cancellation now delivers the final sign-in event before ending the chat; the existing M7 regression reproduced on the untouched baseline and passes with the fix. The CPU workflow provides the checkout import path to its standalone probe. The actual tiny-GGUF swap probe exercises authenticated API import, promotion and rollback with human request tokens, missing-token refusal, unchanged container start time for valid swaps, and rollback after a corrupt candidate. Full Linux regression, edge containers, actual CPU training/conversion and pinned-container swap checks must pass; use PR #56's current-head checks for results. `docs/MODEL.md` remains a reviewable text draft awaiting Roland's approval (M8.10). No M8 deployment, paid GPU or real model promotion has been performed. Defaults remain off.

#### Goal

Roland can edit the persona safely, opt in to labelled feedback capture, export a scrubbed dataset, fine-tune on a **separate GPU machine**, evaluate candidates, and promote or roll back model versions — always with his explicit approval.

#### Dependencies

Roland authorised M8 coding in parallel with M7 on 8 Oct 2026, then asked to fix M8 and integrate the new M7 baseline. `v2-m8-model` originally started at `c35bb21` and is now rebased on final M7 in `v2` at `e2f0792` (#57–#59). Capture must refuse every active sign-in or screen session using durable M7 records. Roland accepted M7 on 8 Oct 2026; M8 review and host smoke remain separate. Keep training capture, the scheduled loop and the trainer profile off; M8 review, container integration and host smoke must pass before enabling it. The parallel coding authorisation supersedes the sequential coding order in §5; it does not authorise host deployment, GPU rental or model promotion.

#### Deliverables

Expected locations per `docs/v2-spec.md`; verify in tree:

1. **M8.1** Migration `agent/migrations/m0003_*.py` (§7.4). `agent/persona.py`: editable block followed by a **fixed safety block that is always last and cannot be edited**, token count, over-budget confirm, versions with history, diff and restore. Settings → Persona UI. No agent tool may change the persona.
2. **M8.2** Feedback UI: thumbs up/down per assistant message; correction box (plain answer or "should have called" tool + args validated against the schema); "What should it have done instead?" in the reject dialog. Feedback is locked once a dataset uses it.
3. **M8.3** Capture (`agent/training/capture.py`): global opt-in, per-chat toggle, "Include past chats" with a count first. Persist only labelled steps (feedback or gate decision); unlabelled steps live only in a 7-day pending file and are never exported. Tainted examples default to `include=0`. **Capture never runs while a sign-in or screen session is active.**
4. **M8.4** Scrubber (`agent/training/scrub.py`, §6.11.2): every rule (with Luhn / IBAN mod-97 / HETU checks), removal of loaded secret values, a self-check that aborts export if any secret survives, per-category counts, linear-time patterns.
5. **M8.5** Dataset/export: seed mixing (`TRAINING_SEED_RATIO` ≥ 0.3, lower refused), dedupe, held-out private eval split, manifest, `.tar.gz` download; Settings → Training data review UI; `training/seed/` with ≥ 300 examples.
6. **M8.6** GPU scripts under `training/` (`prepare.py`, `train_sft.py`, `train_dpo.py`, `merge.py`, `convert_quantize.sh` pinned to `docker/model/VERSION`, `run_all.sh`, `check_gpu.py`, `requirements-train.lock`, `config/default.yaml`, `base_models.lock`, model card template, README). CI runs `--dry-run` on CPU with a tiny model only.
7. **M8.7** Eval harness `agent/eval/`: production brain/parser with mocks; ≥ 200 cases including ≥ 40 gate-compliance (≥ 20 critical) and ≥ 40 injection cases in English and Finnish; promotion gate that auto-rejects any regression in gate compliance or injection refusal and any critical failure; `make model-eval`.
8. **M8.8** Registry/promotion/rollback: `registry.json`, `make model-import`, `make model-promote ID=` (typed-id confirmation), `make model-rollback`, `make model-list`; post-promotion health + smoke eval with automatic **rollback** on failure; retention keeps base, current, previous; Settings → Model page; promotion card in Approvals.
9. **M8.9** Optional `trainerd/` + `docker/trainer/Dockerfile` (compose profile `training`, `trainer_ctl` 10.77.7.70:7200, `trainer_egress`, 128m): modes `manual` (default), `ssh` (pinned host key required, no trust-on-first-use), `hook` (provision/teardown stubs only; teardown in a `trap` on every exit path); `TRAINING_MAX_HOURS` cap; `agent/training/loop.py` scheduled job (`TRAINING_SCHEDULE`, waits for backup, minimum-data check, notifications).
10. **M8.10** `docs/MODEL.md` (requires Roland's text approval like other docs) and a short README section.

#### Security requirements

1. **No fine-tuning on the VPS.** Training runs only on a separate GPU machine. The VPS only exports data, evaluates and imports.
2. **No automatic model promotion.** The only callers of the registry switch are the trainerd promote/rollback handlers and the CLI, each behind Roland's two-step confirmation. Passing eval creates a pending promotion request only. Automatic **rollback** after a failed post-promotion check is allowed.
3. Paid GPU rental and every promotion need Roland's explicit OK.
4. No provider SDKs, default provider or credentials in the repo. Trainer credentials exist only as Docker secrets Roland creates; core cannot read them.
5. Capture is opt-in, off by default, and never records sign-in, screen-control or secret content.
6. The fixed safety block is always last in the system prompt.

#### Tests / acceptance

Automated:

- [ ] **A8.1** `tests/test_persona.py` (safety block last and unchangeable; versions restore; over-budget confirm; no agent tool changes persona).
- [ ] **A8.2** `tests/test_feedback_capture.py` (including `test_capture_off_stores_nothing`, `test_signin_content_never_captured`, `test_tainted_examples_default_excluded`).
- [ ] **A8.3** `tests/test_scrub.py` (one positive and one negative per rule; self-check aborts export; patterns linear on 300 KB inputs).
- [ ] **A8.4** `tests/test_dataset.py` (schema, seed ratio, minimums, private eval never in train).
- [ ] **A8.5** `training/tests/test_dry_run.py` on CPU in CI produces a valid `candidate.tar` and a GGUF that loads in the pinned image.
- [ ] **A8.6** `tests/test_eval_gate.py` (regressions auto-reject; passing candidate only creates a pending promotion).
- [ ] **A8.7** `tests/test_promotion.py` (static + dynamic "no promotion without request"; two-step confirm + CSRF; single-use token; failed smoke rolls back; retention; SHA and llama.cpp build mismatch refused).
- [ ] **A8.8** `tests/test_trainer.py` (pinned host key; teardown on failure/timeout/cancel; max hours; no credentials in repo; trainer off by default; loop waits for backup).
- [ ] **A8.9** model swap integration with tiny GGUFs: promote B via API, rollback to A, no container restart.

Code Shipper (Grok) smoke on Contabo:

1. Persona: edit, preview, save, restore a previous version; confirm the safety block is shown last and read-only.
2. With capture off, thumbs-up a message; confirm no `training_examples` row is created.
3. Turn capture on for one test chat; give feedback; export a dataset; confirm scrubber counts and that a planted fake secret is removed.
4. `make model-list` shows base/current; no promotion happens without the UI request.

Manual (**Roland decides**): **A8.10** — one real loop on a rented GPU. Not required for merge if Roland does not want to rent a GPU yet; A8.5 and A8.9 are required.

#### Stays OFF until M8 is merged, and then until Roland turns it on

`TRAINING_CAPTURE=false`, `TRAINING_LOOP_ENABLED=false`, `TRAINER_URL` empty, compose profile `training` not in `COMPOSE_PROFILES`. `make deploy` must check that `COMPOSE_PROFILES=training` and `TRAINER_URL` are both set or both unset.

#### Contabo deploy notes

1. Migration m0003 runs on core start; take a backup first (`make backup`) and verify `python -m agent migrate --check`.
2. Training data lives in its own volume/directory with 0600 files; include it in backup policy as decided by Roland.
3. Trainer (if ever enabled) adds 128 MiB; planned total with trainer ~7072 MiB of ~7987 MiB. Measure before enabling.
4. GPU machine credentials: Roland creates the secrets himself (`make training-secrets`); never ask him to paste them into chat.

---

### M9 — Hardening, docs and release (`v2-m9-release`, then `v2` → `main`)

**Spec:** §10, §14, M9 in §12.

#### Goal

A reviewed, measured, documented 2.0.0 release, merged to `main` by Roland.

#### Dependencies

M6, M7, M8 merged on `v2` and smoked on Contabo.

#### Deliverables

1. **M9.1** Full security review against §10 (trust boundaries, baseline checklist, threat list, allowed outbound). Write `docs/SECURITY.md` for Roland with residual risks and "known, accepted" items (keep the existing sandbox notes).
2. **M9.2** Final `README.md` and `docs/AGENT.md` (Roland-approved text): features, safety model in plain words, local model and measured speed, training loop and why Roland approves every model, local dev without Docker. Fold or remove `docs/NEXT.md` at release. `docs/M4-HANDOFF.md` was removed in the status docs update; do not recreate it.
3. **M9.3** Runbook (§14) finalised: install, deploy, model install, promotion, rollback, domain switch, restore drill.
4. **M9.4** End-to-end acceptance on the actual Contabo host (A9.2); fix anything found in separate PRs to `v2`.
5. **M9.5** Measured headroom and performance: `docker stats` peaks with the model loaded during a browser task; prompt and generation tok/s and time-to-first-token at the production `MODEL_CTX`; soak and restart recovery. Fill the model acceptance table in `docs/AGENT.md`.
6. **M9.6** `pyproject.toml` version `2.0.0`; `CHANGELOG.md`.
7. **M9.7** Release PR `v2` → `main`, reviewed by Shipper (COMMENT review), merged **by Roland**.

#### Security requirements

Every §10.2 baseline item has a test or a runbook check. No open high-severity finding at release. Dedicated CI job `no-hosted-llm` added if workflow-scope credentials are available; otherwise record the gap for Roland.

#### Tests / acceptance

- [ ] **A9.1** CI green on `v2`: lint, unit, frontend, edge/integration, and `no-hosted-llm`.
- [ ] **A9.2** Server checklist green: isolation script (model, sandbox, browser, novnc, trainer if enabled); external `nmap -Pn -p- 37.60.226.214` shows only 22, 80, 443 and `nmap -sU -p 443` shows 443/udp; TLS valid; a backup exists and the restore drill passed on a copy; memory headroom recorded during a browser task with the model loaded; log rotation visible in `docker inspect`.
- [ ] Gate smoke repeated: chat "yes" does not approve; jobs cannot sign in; screenshots not sent to the text model.
- [ ] **A9.3** Shipper review comment on the release PR; Roland merges.

#### Contabo deploy notes

Restore drill on a **copy** only (`make restore-test FILE=…`), never over live data without Roland. Take a Contabo snapshot before the release deploy.

## 6. Open decisions for Roland

1. **Model context vs memory.** Contabo runs `MODEL_CTX=3072` because 4096 OOM'd under `MODEL_MEM_LIMIT=3840m`. 3072 OOM'd too on 8 Oct, and the cause was llama-server's RAM prompt cache, now off (`--cache-ram 0`, see §1). The 4096 kill was probably the same cache. After the cache was disabled, the pre-step at 3072 passed (model peak 3038 MiB, host minimum available 4045 MiB, projected 2765 MiB after the browser cap). The later browser-enabled watch also passed (model peak 3084.29 MiB, host minimum available 3553 MiB). No 4096 measurement is supplied, so these results do not decide the context setting. Options:
   - (a) keep 3072 and change the repo default (`docker-compose.yml`, `.env.example`, `docs/AGENT.md`) to 3072 so repo and host agree;
   - (b) raise `MODEL_MEM_LIMIT` (needs Roland's OK and costs browser headroom);
   - (c) upgrade the VPS (e.g. ~12 GB) before M6.
   Until Roland decides, keep 3072/3840m on the host and do not change the limit.
2. **Browser headroom.** If the M6 pre-measurement shows < ~800 MiB available with the browser cap added, Roland chooses between a smaller browser cap, a lower `MODEL_CTX`, or a larger VPS.
3. **Chromium sandbox** (`BROWSER_CHROMIUM_SANDBOX`, spec Q4): accept `false` with the hardened container as boundary, or require `true` if the M6 experiment succeeds. **Roland decided (8 Oct 2026): on as a trial on Contabo.** Host `.env` has `BROWSER_CHROMIUM_SANDBOX=true` and the pinned seccomp profile. The service is healthy and the main Chromium process has no `--no-sandbox`. Repo defaults remain unchanged; see `docs/SECURITY.md`.
4. **GPU provider and budget** for A8.10, or stay on manual mode.
5. **Training data in backups** (include or exclude).
6. **Real domain** instead of the sslip.io fallback (DNS change needs Roland).
7. **Background POSTs from "safe" clicks (found in M6 part 1).** The spec's POST-navigation guard only stops a click that *navigates* with a POST. A page script that sends a `fetch`/XHR POST when a plain link is clicked is not stopped, and §9.4.1 rule 4 lets plain links through without approval. Options for part 2: (a) keep the spec as is and accept it; (b) in `safe` mode, browserd also blocks non-GET `fetch`/XHR for a few seconds after an action, which closes the gap but breaks pages that load content with POST; (c) gate every link that has a script handler, which means many more approvals. **Roland decided (8 Oct 2026): (a), allow background POSTs (the default).** Keep the switch for (b) off. *Built in #42: (a) is the default, `BROWSER_BLOCK_BACKGROUND_POSTS=true` is (b). Part 2 also stops forms a page submits by itself at any time, which the spec did not ask for.*
8. **Sensitive-field word list (spec §6.5).** The spec matches `pass`, `pin`, `otp`… as plain substrings of a field's name, so "ship**pin**g address" and "**pass**enger name" count as secret fields and can never be typed into. Options: match whole words instead (recommended), or keep substrings and accept that the agent cannot fill such fields. **Roland decided (8 Oct 2026): keep both rules (the default).** #42 combines the spec's substring rule with a whole-word rule that also reads labels and placeholders. Keep that default; do not switch to `BROWSER_SENSITIVE_MATCH=word`.
9. **Everyday links that need approval.** By the §9.4.1 keyword rule, links such as "Next", "Reviews", "Share", "Sign up" or any address containing `/post/` ask for approval, some with the two-tap confirm. That is the spec working as written (Q3). If it proves too noisy in the Contabo smoke, Roland may drop words from the list; nobody else may.

## 7. Dev setup

```bash
git clone --branch v2 https://github.com/rolandmraiha-cmd/roland-agent.git
cd roland-agent
git fetch origin && git switch v2 && git pull --ff-only
python3.12 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock
pip install --no-deps --no-build-isolation --no-index -e .
cp .env.example .env
# Local HTTP: COOKIE_SECURE=false. Set MODEL_BASE_URL to a local llama.cpp if chatting.
# Never set ALLOW_SHELL=true on a personal computer.
python -m agent hash-password
make lint && make fmt-check && make test
```

Docker and integration:

```bash
make build && make compose-config && make preflight-edge
make test-integration      # edge/model fixtures (Docker)
make test-sandbox          # live sandbox stack (Docker + secrets/sandbox_api_token)
make test-browser          # live browser stack against the fixture site (Docker + secrets/; not on the server)
make model-fetch MODEL=qwen3-4b-q4km
make model-install FILE=/abs/path.gguf ID=qwen3-4b-q4km
```

Host (Contabo, `/opt/roland-agent`); mutating steps need `APPLY=1`:

```bash
make preflight             # read-only
make secrets               # create missing secrets/*; never prints values
APPLY=1 make firewall-install
APPLY=1 make deploy        # preflight → build → up → smoke
APPLY=1 make ship HOST=deploy@host REF=v2
make verify                # PASS/FAIL checklist incl. isolation
make backup && make restore-test FILE=…
```

CLI: `python -m agent` (serve), `chat`, `migrate --check`, `audit-verify`, `backup-now`, `restore <file.db.gz>`, `healthcheck`, `gen-token`, `hash-password`.

## 8. Code map hints

| Area | Where to look today | Expected new locations (verify in tree) |
|---|---|---|
| Config, flags, secret loading | `agent/config.py` (already validates browser/screen secrets) | — |
| Agent loop, tool dispatch | `agent/core.py`, `agent/tools.py`, `agent/tools_browser.py`, `agent/browser_client.py`, `agent/screen.py`, `agent/signin.py` | — |
| Gate | `agent/gate.py` (`POLICIES`, `Gate`, `CONFIRM_CATEGORIES`, `PIN_KEY`), `agent/policy_browser.py`, `agent/web/routes_approvals.py` | — |
| Shell | `agent/policy_shell.py`, `agent/sandbox_client.py`, `agent/local_shell.py`, `sandboxd/` | — |
| Workspace/files | `agent/workspace.py`, `agent/tools_files.py`, `agent/web/routes_files.py` | screenshots/downloads integration |
| Models | `agent/models/` (llamacpp, ollama, parse, grammar `action.gbnf`, `endpoint_guard.py`, `modelreg.py`), `docker/model/`, `deploy/model_store.py`, `deploy/models.lock` | `agent/persona.py`, `agent/training/`, `agent/eval/`, `training/`, `trainerd/`, `docker/trainer/` |
| Persistence | `agent/memory.py`, `agent/migrations/` (m0001, m0002), `agent/audit.py`, `agent/backup.py` | `m0003` |
| Web/UI | `agent/web/app.py`, `auth.py`, `middleware.py`, `routes_screen.py`, `agent/web/static/` (with `screen.html`, `screen.js`) | Settings tab |
| Browser service | `browserd/` (`launcher.py`, `session.py`, `server.py`, `guards.py`, `snapshot.js`, `settings.py`, `vnc.py`), `docker/browser/` (image, Chromium policy, seccomp profile), `requirements-browser.{in,lock}` | x11vnc child in `browserd/launcher.py` (`extra_children`), sign-in hand-over |
| Edge | `docker/caddy/Caddyfile` (screen routes), `docker-compose.yml`, `docker/novnc/` (relay image, `fetch.py`), `requirements-novnc.{in,lock}` | — |
| Deploy | `deploy/*.sh`, `deploy/preflight_edge.py`, `Makefile` | browser/vnc/trainer secrets and firewall rules |
| Tests | `tests/`, `tests/integration/` (`edge.sh`, `isolation.sh`, `browser.sh`, `test_sandbox_live.py`, `test_browser_live.py`, `test_screen_live.py`, `rfb.py`), `tests/frontend/` (`chat.test.cjs`, `snapshot.test.cjs`, `screen.test.cjs`), `tests/fixtures/site/`, `docker-compose.test.yml` (`fixture-web`, `browser`, `tester`), `docker/tester/` | — |

`docs/v2-spec.md` is the target design; `docs/AGENT.md` is the status. Where a spec path differs from the tree, follow the tree's conventions and note it in the PR.

## 9. Definition of done

### Per milestone

A milestone is done only when all of these are true:

1. All its automated acceptance tests exist and pass in CI; earlier suites remain green.
2. Code Shipper reviewed it (COMMENT review), ran the Contabo smoke listed above, and merged it to `v2`.
3. Any substantial manual test (A7.4, A8.10) was done with Roland or explicitly deferred by him in writing.
4. Measured memory on Contabo is recorded in the PR and headroom is ≥ ~800 MiB available.
5. `docs/AGENT.md`, `docs/NEXT.md`, and `README.md` were updated in the same PR before squash-merge, and `docs/SECURITY.md` too if an accepted limit changed. Large instruction-text replacements were approved by Roland.
6. Feature flags in production match Roland's decision (on only after smoke).

### Overall (v2 release)

1. M6–M9 done as above.
2. A9.1–A9.3 green; version 2.0.0 and `CHANGELOG.md` present.
3. Roland merged `v2` → `main`.

## 10. Do not

1. Do not push to `main`, and do not merge the release PR yourself.
2. Do not start M6 as a **Grok** bot in THE SCAM CALL CENTER while that room is parked. If you are an **external AI** and Roland handed you this work, parking the Grok room does **not** block you — proceed under §0 and his instructions to you.
3. Do not call any hosted LLM or inference API, for any purpose, including tests and evaluation.
4. Do not raise `MODEL_MEM_LIMIT`, `MODEL_CTX` above the host value, or any memory cap on Contabo without Roland's OK. Do not "fix" OOM by raising limits.
5. Do not enable `BROWSER_ENABLED`, `SCREEN_ENABLED`, `TRAINING_CAPTURE`, `TRAINING_LOOP_ENABLED` or the `training` profile in production before the milestone is merged and smoked.
6. Do not let a chat message approve an action, confirm a category, or resolve a sign-in.
7. Do not treat grammar/JSON validity as permission.
8. Do not run a second browser for the screen; the screen shows the agent's own Chromium.
9. Do not add endpoints that export cookies, passwords, storage, or run arbitrary JS; do not expose CDP.
10. Do not mount the browser profile into core, sandbox or the workspace.
11. Do not send screenshots or images to a text-only model.
12. Do not let jobs request interactive sign-in, and do not let the agent read, act or capture while Roland is in control or signing in.
13. Do not fine-tune on the VPS, promote a model automatically, or rent paid GPU without Roland.
14. Do not commit secrets, credentials, provider SDKs or keys; do not print secret values in logs or chat.
15. Do not run sandboxd's leftover reap outside its container; never set `SANDBOX_REAP_ALL` on a host.
16. Do not run host-mutating commands without `APPLY=1`, and do not change DNS, delete volumes or restore over live data without Roland.
17. Do not merge docs text Roland has not approved; do not add new instruction markdown files. Do not squash-merge a PR that changed code, deploy state, plans, or instructions without updating `docs/AGENT.md`, `docs/NEXT.md`, and `README.md` in that same PR.
18. Do not claim a feature works in docs until it is merged and smoked on Contabo.
