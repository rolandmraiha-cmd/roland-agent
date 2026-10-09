# Security notes (roland-agent)

M9 review, 9 October 2026: the trust boundaries and §10 baseline below describe the
current implementation and its tests. The host still runs `7ef26de`; M9 host acceptance
is pending. This is not a claim that an approval gate makes arbitrary websites safe.
The existing accepted sandbox/browser/screen limits are retained below.

Update this file in the same PR, before squash-merge, whenever an accepted limit changes. Do not leave that note only in chat.


## Trust boundaries and data

| Component/data | Trust and authority | Persistent access |
|---|---|---|
| Roland's authenticated browser | The sole human principal; approves exact cards and model versions | Host-bound login cookie |
| Caddy | Trusted TLS/proxy; forwards only configured routes | TLS keys/certificates |
| Core | Trusted policy/auth/approval authority | SQLite, audit, backups, workspace, internal tokens and screen passwords; model read-only |
| Model output, web/file/shell text, tainted facts | Untrusted suggestions/data; never grant authority | May enter chat/history after bounded processing |
| Sandbox | Untrusted arbitrary commands | Shared workspace and its own token; no core DB/secrets or Docker socket |
| Browser | Can be compromised by a page; cookies are high-value | Its own profile, browser workspace corner, own token and VNC passwords |
| noVNC relay | Carries screen traffic, no decision authority | No secret/profile/data volume |
| Model runtime | Parser/runtime boundary; no inference egress | Read-only weights, own token |
| Optional trainer | Trusted for writes after a human-bound request; off for release | Model registry, scrubbed datasets, user-provided SSH/provider secrets |
| Rented training machine/candidate | Third-party hardware and untrusted artifacts | Receives only deliberately transferred scrubbed data; never inference fallback |

Containers share the host kernel. A host/root/Docker compromise defeats these boundaries
and exposes databases, backups, secrets and signed-in cookies. Disk encryption and off-site
backups are not implemented. Browser profile, models, secrets and training exports have
separate recovery needs; DB/workspace snapshots do not include them.

## Baseline verification (§10.2)

Every row names implementation evidence and a test or host check. Unit tests do not prove
that Contabo firewall rules, TLS, ownership or load are correct; repeat the runbook checklist
at the actual release commit. Missing acceptance evidence stays pending.

| Baseline | Enforcement / evidence |
|---|---|
| Secrets from env/private files, not images/git | `Config._secret`, `repr=False`, compose mounts, ignore files; `test_config.py`, `test_compose_policy.py`, `test_audit.py`; host preflight mode/uid checks |
| Core cannot be read through same-uid `/proc`; sidecars receive minimal env | `harden_process`, Dockerfiles and launcher/exec env builders; `test_m0_cli.py`, `test_sandboxd.py`, browser launcher/live process checks |
| Route auth, peers, websocket and screen auth | ASGI peer/auth middleware and Caddy forward-auth; `test_web_v2.py::test_every_route_requires_auth`, `test_screen_auth.py`, edge/live screen tests |
| CSRF/Origin on mutations and websocket | Session-bound CSRF header and strict Origin; `test_web.py`, `test_web_v2.py`, `test_screen_auth.py`; forwarded origin/peer regressions |
| Brute-force limits and audit | Argon2id, per-IP/IPv6-prefix lockout, bounded serialized login checks and global slowdown; `test_web.py`, login audit tests |
| Private session cookies/expiry | Hashed tokens, Secure/HttpOnly/SameSite=Strict, host prefix in production, idle+absolute expiry/password invalidation; web/auth tests |
| CSP/headers/HSTS | Plain-text rendering, no inline executable content, Caddy security headers; frontend/web and edge tests |
| Injection resistance and taint | Tool-output envelope, marker stripping, fixed safety block, taint escalation and gated facts/overwrites/commands; core/gate/policy/persona/feedback tests |
| Human-only approvals, no job self-approval or interactive sign-in | Exact argument/page fingerprints, expiry, one-use DB update, required timed second tap; `test_gate.py`, scheduler/sign-in/browser tool tests |
| Owned local inference only | Host/IP allowlist, DNS pin, redirects/proxies refused, internal model network; `test_model_endpoint.py`, `test_no_hosted_llm.py`; dedicated CI job plus host model egress probe |
| No automatic model promotion | Purpose/version/digest-bound human token, evaluation gate, atomic switch/journal and rollback; `test_promotion.py`, `test_eval_gate.py`, `test_trainer.py`, actual trained-GGUF CI probe |
| Container/network/resource/log limits | Compose policy tests, pinned images, dropped caps/read-only/non-root/no-new-privileges, firewall; host isolation, exact log inspect, loaded memory/restart watch |
| Supply-chain pins | Hash-locked Python packages, image digests and full action SHAs; config/model-store/compose policy tests; least-privilege CI `contents: read` |

The Caddy build uses Amazon ECR Public's Docker Official Image copy, pinned to the exact
official `2.11.7-alpine` index. Registry bytes and the Linux/amd64 manifest were verified
against Docker's image records; it is the same VPS image as the earlier Docker Hub pin.
This avoids the observed anonymous Docker Hub pull limit without changing Caddy's version,
runtime policy or ownership. The compose policy test requires the mirror, version and digest.

M9 verification refuses an empty/stopped stack, missing health data, a loopback-published
internal port, empty backup directory, partial log limits and a Docker/probe error posing as
model isolation. The restore drill has no production mounts/secrets/network and validates
actual restored state. New negative tests are in `test_release_checks.py`; CI uses the real
core image for restore/corrupt-input checks. Neither script alone completes A9.2.

The new live browser CI caught an IPv6 wildcard VNC listener despite `-no6`. x11vnc and
LibVNCServer have separate listeners: `-rfbportv6 0` now disables the latter too. The live
socket-table assertion remains strict; the server must expose only its two expected IPv4
sockets inside the container. The fix still requires a reviewed deploy and host verification.
Upstream [argument handling](https://github.com/LibVNC/libvncserver/blob/master/src/libvncserver/cargs.c)
and [socket creation](https://github.com/LibVNC/libvncserver/blob/master/src/libvncserver/sockets.c)
show why disabling only x11vnc's listener was insufficient.

## Threat review (§10.3)

| Threats | Controls and residual risk |
|---|---|
| T1: injected buy/send/post/delete | Code gate is independent of model text; exact approval cards and timed confirmation. A model can still propose a misleading action and a human can approve it |
| T2: exfiltration through GET URLs | Tainted shell is gated, but `browser_open`/`fetch_url` can still send chat/workspace text in a GET URL without approval if its classifier allows the address. This is the spec's open T2 risk; M9 does not claim to close it. Local-only inference does not stop browser/web exfiltration |
| T3: container escape | Dropped caps, non-root, no-new-privileges, seccomp/AppArmor, no Docker socket, read-only roots and limits. Containers share the kernel; keep the host patched |
| T4: sandbox/browser → core | Separate control/egress networks, edge-only core listener, peer allowlist and host firewall. Verify actual rules after deploy/restart |
| T5: compromised Chromium | Browser container/profile isolated from core/model. Browser cookies, own token and VNC passwords would be exposed; keep only deliberately chosen site logins |
| T6: capturing passwords | Tools locked during control/sign-in, secret fields forbidden/redacted, screen bypasses core, no key/traffic logs, clipboard cleared. Credentials pasted into chat are outside that protection |
| T7: traversal and symlink races | Workspace openat/O_NOFOLLOW confinement. Production workspace is a separate filesystem; same-filesystem hard links remain an accepted local workspace limit |
| T8–T9: disk, CPU and RAM exhaustion | Hard workspace quota, retention/rotation, upload/output/context/concurrency caps and headroom checks. Shared CPU throughput varies; downloads can transiently overshoot their size limit |
| T10–T13: login, CSRF, XSS and spoofed proxy IP | Bounded login checks, strict cookie/Origin/CSRF/CSP, trusted direct proxy and rightmost untrusted hop. A stolen valid session has the owner's authority |
| T14–T15: approval replay or swapped target | One-use conditional update, exact args hash, expiry, timed confirmation and fresh page/element fingerprint. A DOM change between final check and click is still possible |
| T16: exposed screen/passwords | Private routes/networks, session+Origin, server-enforced view-only, disconnect on release, no key/traffic logs. Internal VNC is plaintext and browser compromise exposes its own screen secrets |
| T17–T19: regex abuse, audit secret leakage and memory poisoning | Bounded linear parsing/redaction, output caps, taint flags and gated memory. Exact-value/pattern redaction is not a general proof that arbitrary private prose is removed |
| T20–T23: backup theft, supply chain, fallback DNS and HTTP | Private local backups, reviewed pins, HTTPS/secure cookies and limited ingress. Host/storage compromise, upstream supply chain and sslip.io trust remain; domain/mobile-DNS work is deferred |
| T24–T28: training poisoning/drift/data leakage/artifact tampering/credentials | Opt-in labelled capture, scrubbing+self-check, held-out eval/seed replay, human approval, hashes and rollback; trainer off. Eval can miss subtle regressions, arbitrary personal prose can survive and a GPU provider can copy data |

Sensitive credentials typed in the screen are not intentionally stored in chat/audit/logs;
live fixture tests check that. Sites can still expose identifying text on the resulting page.
Do not paste passwords, payment details or live tokens into chat, shell prompts or feedback.
Avoid storing a browser login for an account whose exposure would be unacceptable.

## Allowed outbound connections (§10.4)

| From | Destination and purpose |
|---|---|
| Caddy | Certificate issuance/validation endpoints |
| Core | User-requested public web fetches; private model/sidecar control networks for features |
| Sandbox/browser | Public destinations needed for requested commands/browsing; local/private targets are firewall-restricted |
| Model | No internet egress; accepts authenticated core inference on its internal network |
| Optional trainer | Only user-configured training SSH/rsync and optional provisioning hooks; off for release |
| One-off model installer | Catalogue-pinned model weights from Hugging Face/CDN during an explicit fetch |
| Builds/CI | Pinned image/package/release sources and synthetic training fixtures; no real external site tasks in tests |

No runtime analytics, crash reporting or update checks are configured. Downloads are
untrusted input. Scraped content is not permission to open a connection, send data or change
a model. Installing packages/building containers necessarily contacts their configured
repositories; that is separate from inference.

## Known accepted/deferred items at 2.0.0

The detailed sandbox/browser/screen limits below remain accepted, including default
background POSTs, GET-side effects, Chromium sandbox default off (on as a Contabo trial),
page-owned inspection, VNC's eight-character/password protocol and the bounded whole-tab
POST approval window. No approval rule is weakened in M9.

By Roland's decision on 9 October, training-data storage/backup policy, capture/export host
smoke and a paid GPU run wait until after 2.0.0. Capture/weekly loop/trainer remain off.
The pipeline's acceptance is automated only for those paths. The phone's intermittent
screen opening and mobile-data NXDOMAIN/access problem are also deferred; their causes
are not established. Do not present those as fixed or require a GPU rental for release.

The final Contabo scan, TLS/isolation, backup restore, load/performance/soak and restart
record is still required. See [RUNBOOK](RUNBOOK.md). High-severity new findings are release
blockers, not newly accepted residual risk. Keep the serving limits and production flags
until Roland decides otherwise.

## Sandbox (M4)

- The shell classifier (`agent/policy_shell.py`) is a **usability filter**, not the security boundary. The boundary is the isolated `sandbox` container (§6.3), peer + Bearer auth on sandboxd, DOCKER-USER firewall rules, and gating every command once a run is tainted (or when `SHELL_APPROVAL=always`).
- **Known, accepted limit:** commands run as the same uid as sandboxd (1000), so a command can read the sandbox token file (`/run/secrets/sandbox_api_token`) or kill sandboxd. The token only authorises sandbox command execution (which the sandbox can already do). Killing sandboxd only causes a restart; healthcheck + `restart: unless-stopped` recover. Nothing in the sandbox reaches the core, its database, or core secrets.
- **Implementation note:** sandboxd's leftover-process reap (`/proc` SIGKILL except PID 1 and self) must run **only inside the container** (gate on `/.dockerenv` or `SANDBOX_REAP_ALL`). Running it on a host during in-process unit tests will kill the machine.

## Browser (M6)

What decides and what enforces: the gate in core decides which browser actions need Roland (`agent/policy_browser.py`, approvals bound to the element and the page). `browserd` enforces at run time, whatever the model or the page does: which addresses may be requested, that a form is only submitted during an approved action, that an element is still what was looked at, and that secret fields are never typed into or read. For addresses the host firewall is the layer that counts; the checks in `browserd` only know names.

- **Accepted limit: Chromium's own sandbox is off by default** (`BROWSER_CHROMIUM_SANDBOX=false`, spec Q4). The hardened container is then the boundary. A page that breaks out of Chromium's renderer with a browser exploit would have everything in the `browser` container: the signed-in profile (cookies), the browser API token and the files under `browser/` in the workspace. It would not have core, the database, the other secrets, the model or the sandbox, and the firewall still limits where it can connect. The token only lets it drive the browser it already controls. The sandbox can be switched on: `BROWSER_CHROMIUM_SANDBOX=true` with `BROWSER_SECCOMP=./docker/browser/seccomp-chromium.json`. That file is Playwright's published profile with one change (chroot no longer needs a capability, because the container drops them all). It ran with every other hardening setting in a dev container. **Roland decided on 8 Oct 2026 to keep it on as a Contabo trial:** the host has both settings above, all five services are healthy, and the main Chromium process has no `--no-sandbox`. The repo default remains off; M9's final host isolation verification is still pending (earlier M6 verification passed). Do not weaken host AppArmor to make it work.
- **Accepted limit: the page script runs where the page's own scripts run.** `browserd/snapshot.js` reads the page from inside it, so a page that rewrites the browser's built-in functions can make it report false facts about that page's own elements. `browserd` treats the report as page data (typed, cut, fingerprinted by `browserd` itself), and the address and POST guards do not depend on it. What a lying page gains is an unapproved click on its own button, which its own script could have made anyway; a form submission is still stopped.
- **Accepted limit: data sent in the background.** **Roland accepted option (a) on 8 Oct 2026: keep the default allowing background POSTs.** By default only a form submission (a POST that loads a page) is stopped outside an approved action. A page script that sends data with `fetch`, XHR or a WebSocket after a harmless-looking click is not (Roland's decision 7, retained in docs/NEXT.md §6). `BROWSER_BLOCK_BACKGROUND_POSTS=true` also stops `fetch`/XHR POSTs, and breaks many sites.
- **Accepted limit: sensitive-field matching.** Roland chose on 8 Oct 2026 to keep both default rules: substring matching on field names plus whole-word matching that also reads labels and placeholders (Roland's decision 8, retained in docs/NEXT.md §6). This can classify ordinary fields such as shipping address or passenger name as secret and forbid typing into them. Do not switch to `BROWSER_SENSITIVE_MATCH=word`.
- **Accepted limit: redirects and WebSockets are not seen by the request guard.** Playwright does not hand them to it. For those, Chromium itself is started with rules under which local names and private addresses written as numbers never resolve. A public name that points at a private address is stopped only by the firewall, as the spec says (§5.3).
- **Accepted limit: GET is never stopped at run time.** A link or a GET form that changes something on a site depends on the classifier alone (keywords, submit controls, default-deny).
- **Accepted limit: developer tools are not disabled by policy.** The spec's `DeveloperToolsAvailability=2` also switches off the pipe Playwright drives Chromium through, so it cannot be used. The agent has no key or address that opens them; `devtools://` and `chrome://` pages are blocked by policy and by `browserd`.
- **Not read at all:** frames from another site (a card form embedded from a payment provider, for example), and anything in user mode. While Roland has the browser, every `browserd` route except health, status and the mode switch answers 423. The status route still returns addresses and titles for Roland's own Browser tab; core never passes them to the model.
- **Downloads** are saved without asking into `browser/downloads` in the workspace, under plain file names. One file may be 200 MB (`BROWSER_MAX_DOWNLOAD_MB`); a larger one is stopped while it arrives, checked twice a second, so it can overshoot by what arrives in that time. They are untrusted content like anything else from the web.
- **Accepted limit: one approval covers its whole tab for a few seconds.** While an approved action is carried out (until 2 s without requests, 10 s at most), any form in that tab may be submitted, not only the one Roland saw. That is the spec's design. Other tabs are not covered. **Not possible at all:** a form that posts into a new tab; it is never sent, because the request cannot be tied to the tab that asked.
- **A page that submits a form by itself** (some single sign-on and payment hand-offs do) is stopped in agent mode unless it happens within an approved action. Such flows need Roland at the controls (M7).

## Screen and sign-in (M7)

What decides and what enforces: core decides who may see the screen (a valid login with an open screen session, asked by Caddy before every request) and whether the agent or Roland has the browser. x11vnc enforces view-only for the watching password, and browserd refuses the agent's calls while Roland has control or a sign-in waits; core refuses them too, before they reach browserd.

- **The VNC passwords are defence in depth, not the lock.** Classic VNC login compares eight characters and is not encrypted beyond its challenge. What keeps a stranger out is the login cookie (SameSite=strict), the screen session, the Origin check on the websocket, and that x11vnc and the relay are only reachable on internal networks, x11vnc only from the relay's address.
- **Accepted limit: the screen travels in the clear inside the server.** TLS ends at Caddy. From Caddy to the relay and from the relay to x11vnc the picture and Roland's keys are plain VNC traffic on two internal Docker networks. The relay holds no secret and logs connections only, but it does carry what Roland types on its way to the browser. Core never does.
- **Accepted limit: everything in the browser container runs as one user.** A page that took over Chromium (see the M6 limit on Chromium's sandbox) could read x11vnc's password file and watch or drive the screen from inside that container. It already controls that browser; it gains no way out, since the firewall drops connections from the browser to the relay and to core.
- **Nothing typed on the screen is logged or stored.** Caddy keeps no access log, x11vnc runs with `-quiet` and never with key logging, websockify never with `--traffic` or `--record`. Core audits a screen session's start and end with mode, duration and reason, and a sign-in with the site's name. Checked in the live tests: the typed password reached the test site and appeared in no log, no audit row and no database file.
- **The screen's clipboard is never sent to a viewer**, and is emptied when Roland hands the browser back. What a viewer with the full password pastes does arrive; a view-only viewer's paste is dropped by x11vnc.
- **While Roland has the browser he has all of it:** the address bar, new tabs, the browser's own menus. Chromium's policy still blocks its internal pages, the password manager and extensions. The agent's two-tab limit does not apply to him; the agent closes what it doesn't want afterwards.
- **Only the I'm done button ends a sign-in.** A chat message never does, and the composer is locked while one waits. The button only says Roland stopped: the agent is told that it proves nothing and is handed the page as it then is, read by core once the browser is back with the agent. If the sign-in form is still showing, it is told so. The page handed over is a snapshot like any other: values of password, code and card fields are never in it, and it marks the run as having seen untrusted content, so a shell command later in that run waits for approval.
- **A screen session ends** on release, 30 idle minutes, logout, a newer session or a restart of core. Each end replaces x11vnc, so a page left open cannot go on watching or typing. If browserd can't be reached at that moment, core keeps trying and the agent stays locked out meanwhile.
