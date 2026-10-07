# roland-agent — NEXT: implementation handoff for M6 → M9

> Snapshot: 7 Oct 2026. Integration branch `v2`, docs base **`a27b5ff`** (#37; M5 code `98971cc`) plus the M6 fixture site (#38) and **M6 part 1** (#39: core-side browser code, dormant). Confirm the tip with `git log origin/v2 -1` before coding.
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

## 3. Current production state (7 Oct 2026)

| Item | Value |
|---|---|
| Repository | https://github.com/rolandmraiha-cmd/roland-agent |
| Integration branch | `v2`. Deployed on Contabo: **`a27b5ff`** (#37; M5 code `98971cc`, "M5: Workspace and files", #32). M6 part 1 sits on top and is not deployed |
| `main` | Untouched since v1; do not push until M9 |
| Live URL | https://37-60-226-214.sslip.io/ |
| Host | Contabo VPS, Ubuntu 24.04, ~4 vCPU / ~8 GB RAM, IPv4 `37.60.226.214` |
| Code path | `/opt/roland-agent` |
| Workspace | `/srv/roland-agent/workspace` (bind-mounted as `/workspace` in core and sandbox) |
| Healthy services | `caddy`, `core`, `model`, `sandbox` |
| Host `.env` | `MODEL_CTX=3072`, `MODEL_MEM_LIMIT=3840m` |
| Repo defaults | `docker-compose.yml` and `.env.example` still default `MODEL_CTX` to 4096 |
| Shell | On, via the sandbox (`ALLOW_SHELL=true`, `SHELL_BACKEND=sandbox`) |
| Off | Browser, screen, training (compose sets the flags to `"false"`) |
| App version | `0.1.0` in `pyproject.toml` (bump to `2.0.0` in M9) |

`MODEL_CTX` was lowered from 4096 to 3072 on Contabo after the model container was OOM-killed at 4096 with the 3840m limit. Do not "fix" this by raising memory.

Shipped milestones on `v2`: v1 (#1–#6), M0, M1a, M1, M2 web/edge/model/providers/deploy, **M3 gate (#29, #30)**, **M4 sandbox (#31)**, **M5 workspace and files (#32; A5.4 green on Contabo)**. See `docs/AGENT.md` §3 for the full table.

## 4. Architecture already live

### 4.1 Services (see `docker-compose.yml`)

| Service | Role | Networks (IP) | mem_limit |
|---|---|---|---|
| `caddy` | Only service with published ports: 80/tcp, 443/tcp, 443/udp. TLS (ACME), reverse proxy to core. Screen routes (`/screen/websockify`, `/screen/novnc/*` with forward-auth to `/internal/screen-auth`) already exist in `docker/caddy/Caddyfile` but have no backend yet. | `public` 10.77.0.2, `edge` 10.77.1.2, `screen` 10.77.2.2 | 96m |
| `core` | FastAPI app, agent loop, gate, tools, SQLite (`/data`), backups (`/backups`), workspace (`/workspace`). Only accepts peer 10.77.1.2. | `edge` 10.77.1.10, `sandbox_ctl` 10.77.3.10, `model` 10.77.6.10, `core_egress` | 640m |
| `model` | llama.cpp server (digest-pinned, `docker/model/VERSION`), read-only weights, bearer `model_server_token`, no egress, no published port. | `model` 10.77.6.60 | `${MODEL_MEM_LIMIT:-3840m}` |
| `sandbox` | `sandboxd` on 10.77.3.20:7000; peer 10.77.3.10 + Bearer `sandbox_api_token`; output cap 64 KiB, timeout ≤ 300 s, concurrency 2; container-gated leftover reap. | `sandbox_ctl` 10.77.3.20, `sandbox_egress` 10.77.11.20 | 1g |

All services are `read_only`, `cap_drop: [ALL]`, `no-new-privileges`, uid 1000, with `memswap_limit == mem_limit`.

Reserved, unused networks already declared: `browser_ctl` 10.77.4.0/24, `vnc` 10.77.5.0/24, `trainer_ctl` 10.77.7.0/24, `browser_egress` 10.77.12.0/24, `trainer_egress` 10.77.13.0/24, plus `screen` (attached to caddy only today).

Config caps today: 3840 + 640 + 96 + 1024 = **5600 MiB**. Config limits are not proof of headroom; real usage must be measured.

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

#### Status: part 1 of 2 done (7 Oct 2026)

M6 is split into two PRs to `v2`.

- **Part 1, core side (#39): done, dormant.** `agent/policy_browser.py`, `agent/browser_client.py`, `agent/tools_browser.py`, the browser policies in `agent/gate.py`, the screenshot on approval cards, and the tests for A6.1 and A6.2. Nothing is switched on: without `browserd` there is nothing to talk to, `BROWSER_ENABLED` stays `false`, and `agent/__main__.py` still refuses it.
- **Part 2, the browser service: not started.** Deliverables 1–3, 5, 6, 8 and 9 below, the browser part of `docker-compose.test.yml`, A6.3–A6.5, the Contabo pre-step and smoke. Part 2 removes the "not implemented yet" refusal in `agent/__main__.py` for the browser (keep it for the screen until M7).
- **Fixture site (deliverable 7): done in #38.** `tests/fixtures/site/server.py`, its loopback tests, and the `fixture-web` service on the internal `fixtures` test network. Part 2 attaches the `browser` service to that network and sets `BROWSER_ALLOW_PRIVATE_HOSTS=fixture-web` there.

#### Goal

The agent can drive one persistent, headed Chromium through a private `browserd` service. Consequential actions are gated by code. Only text and accessibility output reaches the default text-only model.

#### Dependencies

1. M3 gate and M5 workspace on `v2` (done).
2. **Pre-step (mandatory, before enabling on Contabo):** record measured memory on the live host with `caddy`, `core`, `model`, `sandbox` running and the model under load (`docker stats`, `free -m`). Put the numbers in the PR. If adding the planned browser cap (1280 MiB) would leave less than ~800 MiB available, stop and raise the open decision in §6 with Roland. Do not enable the browser to "see what happens".

#### Deliverables

Expected locations per `docs/v2-spec.md`; verify in tree (deliverables 4 and 7 exist; the rest do not yet):

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
7. **Fixture site** `tests/fixtures/site/` and `fixture-web` service in `docker-compose.test.yml` (§13.2–13.3): order form, injection page, SPA div-POST, prefilled password, login + `/whoami`, download.
   The fixture site exists with unit tests and the internal `fixture-web` service; browser integration remains pending.
8. **Chromium sandbox experiment** (`BROWSER_CHROMIUM_SANDBOX`, default `false`): try `true` with a pinned seccomp profile; report the result in the PR. Do not weaken host AppArmor to make it work.
9. **UI:** Browser tab showing status, current URL/title and a thumbnail. Vanilla JS, `textContent` only; keep function names used by `tests/frontend/chat.test.cjs`.

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
9. **Approvals are bound to more than the fingerprint.** Core re-reads the element before an approved action and compares every fact the classifier used and the full page address (contract item 10). An approved upload is bound to the file's SHA-256, so a file rewritten while the card waits is not sent. `browser_type` is GATED `form_submit` for a field whose typing already tried to submit a form. These came from the automated reviews of #39.

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
- [ ] **A6.3** `tests/integration/test_browser_live.py` against the fixture site: place-order needs approval (0 POSTs before, exactly 1 after); injection page cannot trigger delete; SPA div POST blocked in safe mode; password value never in snapshot; profile persists across browser restart; downloads land in workspace; no cookie/eval endpoints; private-IP navigation blocked.
- [ ] **A6.4** isolation script: from `browser`, 10.77.4.10:8080 and 10.77.1.10:8080 unreachable.
- [ ] Existing suites still green: `make lint`, `make test`, `make test-integration`.

Code Shipper (Grok) smoke on Contabo (after deploy with `BROWSER_ENABLED=true`):

1. `docker compose ps` shows `caddy`, `core`, `model`, `sandbox`, `browser` healthy.
2. In chat: "Open https://example.com and tell me the page title." Expect a correct answer with no approval card.
3. Ask the agent to submit a form on a harmless public test page. Expect an approval card with a screenshot; reject it; confirm nothing was submitted.
4. Type "yes" in chat while a card is pending. Expect the composer to be locked or the text to have no effect on the approval.
5. **A6.5:** run a ~10-minute browsing task; record `docker stats` peaks and `free -m`. Browser stays ≤ 1280 MiB, no OOM kill in `dmesg`/`docker inspect`, host available memory ≥ 800 MiB. Record in the PR.
6. `make verify` passes, including isolation.

#### Stays OFF until M6 is merged and smoked

`BROWSER_ENABLED`. Until M7: `SCREEN_ENABLED`, x11vnc listening, `novnc`, `request_signin`.

#### Contabo deploy notes

1. `make secrets` to create `secrets/browser_api_token` (uid 1000, 0400). Never print it.
2. `APPLY=1 make firewall-install` to apply the browser rules before starting the service.
3. Build the browser image when the model is idle (builds need ~1–1.5 GB temporarily).
4. Keep `MODEL_CTX=3072` and `MODEL_MEM_LIMIT=3840m`. If headroom fails, stop and ask Roland (§6). Do not lower other services' limits silently.
5. Take a Contabo snapshot before the first browser deploy.

---

### M7 — Screen and sign-in (`v2-m7-screen`)

**Spec:** §6.6, §6.7, §6.8, §8.2 (screen/sign-in endpoints), §8.4 (`/v1/user-mode`, `/v1/vnc/disconnect`), M7 in §12.

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

- [ ] **A7.1** `tests/test_screen_auth.py`: requires session; requires active screen session; WS origin must match; peer must be Caddy; only one screen session; logout ends screen and disconnects; VNC password not in logs or audit.
- [ ] **A7.2** `tests/test_signin.py`: request sets user mode and waits; Done resolves and unlocks; cancel and timeout messages; agent browser tools locked during sign-in; chat text "done" does not resolve; `request_signin` not available in jobs.
- [ ] **A7.3** integration: view-only password cannot send input; novnc unreachable from tester without forward-auth; `/screen/novnc/vnc.html` without a session → 401 via Caddy.
- [ ] All M6 and earlier suites still green.

Code Shipper (Grok) smoke on Contabo, then **ping Roland** for the manual part (A7.4 is a substantial test with his real credentials):

1. Without logging in, `curl -I https://37-60-226-214.sslip.io/screen/novnc/vnc.html` → 401.
2. Logged in: open Watch; confirm the visible page is the agent's current tab (same browser).
3. Take control, then ask the agent to snapshot. Expect "Roland is using the browser right now".
4. **A7.4 (Roland):** ask the agent to check something behind a login on a site Roland chooses; sign-in card appears; Roland takes control on the phone, logs in, presses I'm done; agent continues and reads the logged-in page. Audit shows `signin_requested`, `screen_session_start`/`end`, `signin_resolved` and no keystroke data. `docker compose logs browser novnc core caddy | grep -i <password>` finds nothing.
5. `make verify` passes.

#### Stays OFF until M7 is merged and smoked

`SCREEN_ENABLED`, the `novnc` service, x11vnc, `request_signin`.

#### Contabo deploy notes

1. `make secrets` for `vnc_password` and `vnc_view_password`.
2. Firewall: `vnc` and `screen` networks internal; nothing new published. Re-run the external port scan (only 22, 80, 443, 443/udp).
3. Memory: novnc adds 64 MiB. Re-measure headroom with browser + screen session active.

---

### M8 — Persona and training pipeline (`v2-m8-model`)

**Spec:** §6.10, §6.11 (all subsections), §7.4 (migration m0003), §8.2 (persona/feedback/training/model endpoints), M8 in §12.

#### Goal

Roland can edit the persona safely, opt in to labelled feedback capture, export a scrubbed dataset, fine-tune on a **separate GPU machine**, evaluate candidates, and promote or roll back model versions — always with his explicit approval.

#### Dependencies

M7 merged (capture must respect sign-in/screen state). Split into several PRs (suggested: persona + feedback; capture + scrubber + export; training scripts + eval; registry + promotion; trainerd + loop).

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

1. **Model context vs memory.** Contabo runs `MODEL_CTX=3072` because 4096 OOM'd under `MODEL_MEM_LIMIT=3840m`. Options:
   - (a) keep 3072 and change the repo default (`docker-compose.yml`, `.env.example`, `docs/AGENT.md`) to 3072 so repo and host agree;
   - (b) raise `MODEL_MEM_LIMIT` (needs Roland's OK and costs browser headroom);
   - (c) upgrade the VPS (e.g. ~12 GB) before M6.
   Until Roland decides, keep 3072/3840m on the host and do not change the limit.
2. **Browser headroom.** If the M6 pre-measurement shows < ~800 MiB available with the browser cap added, Roland chooses between a smaller browser cap, a lower `MODEL_CTX`, or a larger VPS.
3. **Chromium sandbox** (`BROWSER_CHROMIUM_SANDBOX`, spec Q4): accept `false` with the hardened container as boundary, or require `true` if the M6 experiment succeeds.
4. **GPU provider and budget** for A8.10, or stay on manual mode.
5. **Training data in backups** (include or exclude).
6. **Real domain** instead of the sslip.io fallback (DNS change needs Roland).
7. **Background POSTs from "safe" clicks (found in M6 part 1).** The spec's POST-navigation guard only stops a click that *navigates* with a POST. A page script that sends a `fetch`/XHR POST when a plain link is clicked is not stopped, and §9.4.1 rule 4 lets plain links through without approval. Options for part 2: (a) keep the spec as is and accept it; (b) in `safe` mode, browserd also blocks non-GET `fetch`/XHR for a few seconds after an action, which closes the gap but breaks pages that load content with POST; (c) gate every link that has a script handler, which means many more approvals. Until Roland decides, part 2 builds (a) and leaves a switch for (b).
8. **Sensitive-field word list (spec §6.5).** The spec matches `pass`, `pin`, `otp`… as plain substrings of a field's name, so "ship**pin**g address" and "**pass**enger name" count as secret fields and can never be typed into. Options: match whole words instead (recommended), or keep substrings and accept that the agent cannot fill such fields. Decide before `snapshot.js` is written.
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
| Agent loop, tool dispatch | `agent/core.py`, `agent/tools.py`, `agent/tools_browser.py`, `agent/browser_client.py` | `agent/signin.py` |
| Gate | `agent/gate.py` (`POLICIES`, `Gate`, `CONFIRM_CATEGORIES`, `PIN_KEY`), `agent/policy_browser.py`, `agent/web/routes_approvals.py` | — |
| Shell | `agent/policy_shell.py`, `agent/sandbox_client.py`, `agent/local_shell.py`, `sandboxd/` | — |
| Workspace/files | `agent/workspace.py`, `agent/tools_files.py`, `agent/web/routes_files.py` | screenshots/downloads integration |
| Models | `agent/models/` (llamacpp, ollama, parse, grammar `action.gbnf`, `endpoint_guard.py`, `modelreg.py`), `docker/model/`, `deploy/model_store.py`, `deploy/models.lock` | `agent/persona.py`, `agent/training/`, `agent/eval/`, `training/`, `trainerd/`, `docker/trainer/` |
| Persistence | `agent/memory.py`, `agent/migrations/` (m0001, m0002), `agent/audit.py`, `agent/backup.py` | `m0003` |
| Web/UI | `agent/web/app.py`, `auth.py`, `middleware.py`, `agent/web/static/` | `screen.html`, `screen.js`, Browser/Settings tabs |
| Edge | `docker/caddy/Caddyfile` (screen routes present), `docker-compose.yml` (reserved networks) | `docker/browser/`, `browserd/`, `docker/novnc/` |
| Deploy | `deploy/*.sh`, `deploy/preflight_edge.py`, `Makefile` | browser/vnc/trainer secrets and firewall rules |
| Tests | `tests/`, `tests/integration/` (`edge.sh`, `isolation.sh`, `test_sandbox_live.py`), `tests/frontend/chat.test.cjs`, `docker-compose.test.yml` | `tests/fixtures/site/`, `fixture-web` |

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
