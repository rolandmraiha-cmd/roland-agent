# Operate and release roland-agent 2.0.0

This is the operational runbook for the implementation, rather than the spec's reference
commands. Commands assume Bash, `/opt/roland-agent`, Docker Engine 28+ with Compose
2.33.1+, and passwordless sudo for `deploy`. Contabo currently has Ubuntu 24.04, four
shared vCPUs, about 7.8 GB usable RAM and 2 GB swap at `37.60.226.214`.

**Status, 10 October 2026:** 2.0.0 accepted for Roland's single-user deployment at
`7ce7c2c`: verify 12/0/2, schema 3 current, valid 604-row audit, idle RAM 3820 MiB.
The complete 768 MiB file passed full size/SHA-256/ZIP CRC checks. The 180-second watch
recorded core sampled peak 87.99/640 MiB, minimum host available 3817 MiB and zero OOM
kills or restarts across all services. All seven jobs passed at that exact head.
Full M9 restore/load/soak/external-port/TLS and restart checks at `bb76263` and browser
smoke at `7b3de9d` retain their measured scopes in [the acceptance note](releases/m9-acceptance-2026-10-09.md).
Extra feature branches and temporary cleanup workflows are removed; v2 is retained.
Roland authorized the release with documented follow-ups; [PR #66](https://github.com/rolandmraiha-cmd/roland-agent/pull/66)
records final documentation-head checks/review and main merge status. CHANGELOG is dated
10 October. The final paperwork changes only documentation; no further runtime rebuild
or repeated full restore/load/soak is needed for it.
Only Roland merges the final `v2` → `main` PR unless he explicitly delegates that merge.
External implementers need Roland's explicit instruction before SSH/deploy; this runbook
does not grant it.

## Preserve the current host configuration

| Setting | Current host value |
|---|---|
| `MODEL_CTX` | `3072` |
| `MODEL_MEM_LIMIT` | `3840m` |
| `MODEL_THREADS`, `MAX_TOOL_STEPS` | `3`, `6` |
| `BROWSER_ENABLED`, `SCREEN_ENABLED` | `true`, `true` |
| `COMPOSE_PROFILES` | `browser,screen` |
| `BROWSER_CHROMIUM_SANDBOX` | `true` (Roland's accepted trial) |
| `BROWSER_SECCOMP` | `./docker/browser/seccomp-chromium.json` |
| `BROWSER_BLOCK_BACKGROUND_POSTS` | `false` |
| `BROWSER_SENSITIVE_MATCH` | `both` |
| `TRAINING_CAPTURE`, `TRAINING_LOOP_ENABLED` | `false`, `false` |
| `TRAINER_URL` | empty; `training` profile absent |

Do not replace an existing `.env` with the example. The example's 4096-token context is not
an accepted host change. Memory/context increases, DNS changes, model promotion, GPU
rental and restore over live data require Roland's explicit decision. Training setup and
known phone-access issues stay deferred until after 2.0.0.

## Fresh host installation

Skip this section on the existing Contabo installation. Keep an SSH session open during
firewall work and take a provider snapshot before adding a service.

1. Install Docker/Compose, git, Python 3, make, curl and host firewall tooling. Verify
   `docker info` reports seccomp/AppArmor and check the firewall backend. Host ingress
   must allow SSH 22/tcp, HTTP 80/tcp and HTTPS 443/tcp plus 443/udp only. Check both
   ufw and the provider firewall; the project's helper does not replace host ingress rules.
2. Use a read-only GitHub deploy key. Clone the approved `v2` branch to
   `/opt/roland-agent`; use `main` only after Roland merges the release.
3. Run `cp -n .env.example .env && chmod 600 .env`. Set ACME email and the served name.
   The fallback is `37-60-226-214.sslip.io`; a real domain can be set later. For this VPS
   explicitly use `MODEL_CTX=3072`, `MODEL_MEM_LIMIT=3840m`. Keep all optional features
   off until their host smoke is complete. Production Compose overrides core security
   settings, including secure cookies and trusted proxy `10.77.1.2`.
4. Run `sudo env APPLY=1 make secrets`, then `sudo env APPLY=1 make hash-password`.
   Secrets are uid 1000, mode 0400; the directory is 0700. Use a password of at least
   16 characters. No hosted-model key is needed.
5. Create the quota filesystem with `sudo env APPLY=1 make workspace-fs
   WORKSPACE_SIZE_GB=10`. The script refuses to reformat an existing image. Confirm
   `/srv/roland-agent/workspace` is mounted, owned by uid 1000, with nodev/nosuid.
6. Install the firewall helper: `sudo env APPLY=1 make firewall-install`. It detects
   iptables versus nftables; explicit `--iptables`/`--nft` options exist. Verify the service
   and rules using `systemctl status roland-agent-firewall` and the relevant backend.
   If the backend is unclear, stop before exposing the stack.
7. Install the owned model as below, then deploy and complete the host checklist.

Every Compose service sets `json-file` rotation (`10m`, three files). On a fresh host also
merge these defaults into `/etc/docker/daemon.json`, retaining other keys:

```json
{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"},"live-restore":true}
```

Restarting Docker to apply daemon settings is a planned host operation. Test firewall
reapplication afterwards. No Docker socket is mounted into an app container.

## Install the local model

```bash
cd /opt/roland-agent
sudo make model-fetch MODEL=qwen3-4b-q4km
sudo make model-list
```

`model-fetch` builds a one-off installer, downloads only the catalogue's pinned model file
and verifies its size/SHA-256. The running model has no egress. The first installation sets
`qwen3-4b-q4km-base` as current. It does not silently replace an existing current model.
Catalogue licence/notice/card and versions are retained in the `models` volume.

For a file copied to the server, use the **catalogue ID**, not a version directory name:

```bash
sudo make model-install FILE=/absolute/path/model.gguf ID=qwen3-4b-q4km
```

Unlisted files are refused by this installer. Fine-tuned candidates use `model-import` as
below. Keep at least 15 GB free for builds, backups and model versions; inspect disk space
before a new model. No GPU or fine-tuning runs on this VPS.

## Update and deploy

Take a Contabo snapshot before the release deploy. Confirm the approved remote tip and
that local changes will be retained; do not reset the host checkout to resolve differences.

```bash
(
set -e
cd /opt/roland-agent
deploy_git_status=$(sudo git -c safe.directory="$PWD" status --porcelain)
if [ -n "$deploy_git_status" ]; then
    printf 'STOP: local changes exist:\n%s\n' "$deploy_git_status"
    exit 1
fi
git fetch origin
git switch v2
git pull --ff-only
git log -1 --oneline
# Confirm this is the approved commit before deploying.
sudo env APPLY=1 make deploy
sudo make verify
sudo docker compose exec -T core python -m agent migrate --check
sudo docker compose exec -T core python -m agent audit-verify
)
```

The privileged Git status is read-only: the deploy user cannot inspect the protected
`secrets/` directory, while ordinary fetch/switch/pull preserve deploy ownership of Git files.
Keep secrets at directory 0700 and files 0400. On an older checkout that predates the
`.env.*` exclusions, existing `.env.before-m8` and `.env.before-screen` backups can be
retained by adding those exact names to `.git/info/exclude` before the clean check:

```bash
cd /opt/roland-agent
printf '%s\n' '/.env.before-m8' '/.env.before-screen' >> .git/info/exclude
```

Do not hide other changes. The 10 October deploy used this fix and retained both backups.
Use `git switch main` and `git pull --ff-only` instead of the `v2` lines after the release
merge if following the released branch. The documentation-only checkpoint `6a02480`
needed no rebuild. The subsequent streaming fix has now been built and deployed at
`7ce7c2c`; its large-file check must use those new containers.

Deploy preflights the host, records the checkout in `.deploy/`, takes an online backup if
core is running, builds pinned images, reapplies the installed firewall, starts services,
waits for health and checks HTTPS/schema. No missing firewall helper is accepted by default.
A successful deploy is followed by `verify`; its exit code does not replace manual checks.
The version command below confirms the installed package, not deployment acceptance:

```bash
sudo docker compose exec -T core python -c 'from importlib.metadata import version; print(version("roland-agent"))'
```

## Backups and restore drill on a copy

The scheduler takes an online SQLite snapshot nightly at 03:30 Europe/Helsinki. Defaults
keep fourteen dailies and eight weekly database snapshots. A workspace archive, when under
its configured cap, retains three days and excludes trash/upload staging/cache. Confirm
actual config and backup results. Local backups share the host's failure domain; no off-site
backup exists by default. Browser profile, model weights, `.env`/secrets and the separate
training-data volume are not included in a DB/workspace backup.

`make backup` prints the saved paths without contents. Copy a selected DB backup out of
the container before passing it to `restore-test`; `/backups/...` is a container path.
Run the following after the reviewed M9 code is built on the host:

```bash
cd /opt/roland-agent
sudo install -d -m 0700 .release-evidence
sudo make backup
backup_name=$(sudo docker compose exec -T core python -c 'from pathlib import Path; print(max(Path("/backups/db").glob("agent-*.db.gz"), key=lambda p: p.stat().st_mtime).name)')
sudo docker compose cp "core:/backups/db/$backup_name" ".release-evidence/$backup_name"
sudo env APPLY=1 FILE="$PWD/.release-evidence/$backup_name" make restore-test
```

The drill mounts only a private staging copy and two randomly named disposable volumes,
uses the built core image with no network, and never starts Compose services. It runs the
real restore, checks SQLite integrity/foreign keys, current schema, the audit chain and
expired login sessions, then removes its disposable volumes. No production secret, live
database, workspace or browser profile is mounted. A corrupt backup or failed cleanup fails
the command. Keep the PASS output, exact checkout and backup name as acceptance evidence.
Repeat quarterly and before a release that changes persistence.

**Live restore is a separate operation.** After Roland chooses a backup and downtime,
`sudo env APPLY=1 FILE=/absolute/host/backup.db.gz make restore` stops core and restores
its production database, preserving the old DB/sidecars. Then run deploy/verify and log in
again. Migrations are forward-only. Do not restore older code onto a newer schema without
checking compatibility. Workspace recovery and browser/profile recovery need their own
chosen copies; the database drill does not prove those restores.

## Model import, human promotion and rollback

Training remains off through this release. These commands are for a later explicitly
requested model operation, not a release acceptance requirement:

```bash
sudo make model-list
sudo env APPLY=1 make model-import FILE=/absolute/path/candidate.tar
sudo env APPLY=1 make model-promote ID=EXACT_CANDIDATE_VERSION
sudo env APPLY=1 make model-rollback ID=EXACT_PREVIOUS_OR_BASE_VERSION
```

Import checks archive safety, hashes, the pinned build, baseline and evaluation coverage;
it never switches the model. Promotion/rollback require a typed full version ID. UI switching
requires the optional trainer configured by Roland and a two-step, one-use human request.
Scheduling never supplies that authority. Failed post-switch checks roll back; the durable
journal recovers interrupted switches. Keep base/current/previous versions. `FORCE=1` can
override an evaluation rejection via an audited host confirmation; it cannot bypass integrity.
See [MODEL.md](MODEL.md) before use. Decide training-data storage/backup policy before any
capture or transfer; a scrubber does not remove every identifying sentence.

## Domain switch, after Roland chooses a domain

1. Create an A record for the chosen name pointing at `37.60.226.214`. Add AAAA only if
   IPv6 and its firewall are configured and verified.
2. Change `AGENT_DOMAIN` in the existing `.env`. If `AGENT_HOST` was set explicitly,
   update it too; it overrides the domain/fallback calculation.
3. Deploy with `APPLY=1`, check certificate/hostname validation with ordinary curl, then
   repeat login, screen Origin and external port tests for the new name. Everyone logs in
   again because the secure host cookie belongs to the old name.

A domain may help the reported mobile-data NXDOMAIN problem, but that cause has not
been established. This change and phone-screen troubleshooting are deferred after 2.0.0.

## M9 server acceptance: record each result

Create `.release-evidence/` mode 0700; it is excluded from git and Docker builds. Do not
record passwords, cookie values, tokens, account page contents or raw form inputs.
Collect results at the actual deployed M9 commit with training still off.

| Check | Command / evidence | Passing condition |
|---|---|---|
| Host services, ports, TLS, backups, rotation | `sudo make verify` | No FAIL; explain every SKIP. All required services running and healthy; backup is a nonempty file; logs `json-file/10m/3` |
| Schema/audit | `python -m agent migrate --check` and `audit-verify` via `docker compose exec -T core` | Version 3 is current; audit `ok: true` |
| Isolation | `sudo bash tests/integration/isolation.sh --server` | Sandbox/browser cannot reach core, model, screen/relay or trainer if enabled; model egress fails in verify |
| External TCP ports | From a different machine: `nmap -Pn -p- 37.60.226.214` | Only 22, 80, 443 open; retain dated output |
| External UDP | From that machine: `sudo nmap -sU -p 443 37.60.226.214` | Record open/open\|filtered and confirm the Caddy UDP mapping; UDP silence alone is inconclusive |
| Certificate | `curl -v -o /dev/null -w 'HTTP %{http_code}\n' https://37-60-226-214.sslip.io/login` | Valid chain/hostname without `-k`; GET login 200 |
| Actual log settings | `sudo docker inspect --format '{{json .HostConfig.LogConfig}}' $(sudo docker compose ps -q)` | Every service has `json-file`, `max-size=10m`, `max-file=3` |
| Browser/screen sockets | Command below with browser and screen enabled | Exactly browserd and VNC on their intended IPv4 addresses; no IPv6 wildcard VNC listener |
| Restore | Drill above | PASS on the selected copy; live state retained |
| Browser-task load | `sudo env WATCH=600 make memory-report` while running a browsing task | At least 800 MiB host available throughout, no restart/OOM; record all service peaks and minimum headroom |
| Idle headroom | `free -m` with loaded model and all intended services | At least 1200 MiB available |
| CPU speed | `sudo docker compose exec -T core python -m agent.models.benchmark --samples 3` | Record each prompt/generation tok/s and TTFT; no invented threshold or extrapolation |
| Recovery/soak | Steps below | State survives; no stale approval/sign-in authority and no restarts/OOM during watch |
| Human workflow | Steps below | Chat/file/approval/screen work; chat “yes” does not approve |

The login route accepts GET; `curl -I` sends HEAD and returns 405. On Windows, use
`curl.exe -v -o NUL -w "HTTP %{http_code}\n" https://37-60-226-214.sslip.io/login`.

The benchmark uses a fixed public prompt, 128 output tokens, no tools, no prompt caching,
three sequential calls and the configured local-only transport. It prints server token rates,
time to the first nonempty text chunk and total time; it does not print generated text or
credentials. It stays quiet until all samples finish, then prints the results together.
These synthetic rates are not an estimate of a full tool task. Run during a quiet
period; a benchmark competes with chats for CPU. Record production `MODEL_CTX=3072` and
model version alongside results. It never changes context, limits or the serving model.

The live CI found that `-no6` alone left LibVNCServer listening on IPv6. After deploying
the `-rfbportv6 0` fix, check the actual browser socket tables too (Ubuntu's little-endian
address representation below). This reads socket metadata without connecting or logging keys:

```bash
sudo docker compose exec -T browser python -c '
from pathlib import Path
found = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    path = Path(table)
    if not path.exists():
        continue
    for line in path.read_text().splitlines()[1:]:
        fields = line.split()
        if fields[3] == "0A" and not fields[1].startswith("0B00007F:"):
            found.add(fields[1])
if found != {"28044D0A:1BBC", "28054D0A:170C"}:
    raise SystemExit("FAIL: unexpected browser/screen listener")
print("PASS: browserd/VNC only on intended IPv4 addresses")
'
```

For the loaded watch, start the memory report, then in the UI ask the agent to open/read an
ordinary public page and summarise it. Approve any presented card yourself. The command
samples memory, start times and cgroup kill counters. Record model/browser/core/novnc peaks,
host minimum and task outcome in AGENT's release table. The earlier M6 watch is context,
not an M9 pass. Leave a further 30-minute idle/task watch (`WATCH=1800`) for a short soak;
record the duration. This is not proof of indefinite 24/7 reliability.

With Roland's planned restart/downtime, first record a fact/chat/file and the current model,
then `sudo docker compose restart core browser model novnc` and wait for health. Re-login
if required, verify schema/audit and the same fact/chat/file/model; the browser's signed-in
profile should persist. Pending approvals/sign-ins/screen sessions must not acquire fresh
authority across the restart. Repeat chat and a harmless file read afterwards. Retain only
counts/status, not private contents. Do not restart Docker itself for this check.

Human smoke: send a chat; run `uname -a` through the sandbox; upload/download a harmless
fixture file; request an action needing approval, type “yes” and confirm nothing executes,
then reject its card and confirm there is no repeated card in that turn. Watch must not
control the browser; Take control pauses agent calls; Hand back restores them. Jobs cannot
request sign-in. Screenshots are shown to Roland rather than the text model (automated
regression tests cover both rules). Test on available wifi/desktop; the specifically deferred
mobile-data and intermittent phone-screen faults are not made new release prerequisites.

## Review and final release order

1. Review M9 implementation/docs in `v2-m9-release` → `v2`; Roland reviews the proposed
   README/master text before it is merged. Shipper leaves a COMMENT review (shared GitHub identity).
2. Merge reviewed M9 changes into `v2`. A9.1 requires **all** CI jobs green on that actual
   integration tip: lint, unit, frontend, no-hosted-llm, edge/restore, browser integration and
   the separate CPU training/model-switch workflow. Record URLs/commit, not an older PR run.
3. Roland/Shipper deploy that tip and complete this checklist. Record measured values and
   remaining accepted/deferred items in AGENT/NEXT/README in one follow-up PR. Fix any
   newly found release blocker in a separate reviewed PR to `v2`; no open high-severity issue.
4. Only then open the single release PR `v2` → `main`, obtain Shipper's COMMENT review,
   and have Roland merge it (A9.3). Fold/archive NEXT's completed implementation plan into
   AGENT's history at that release, retaining the deferred work. Mark the changelog released
   only when the release is actually accepted. Never push straight to `main`.

## Routine recovery and troubleshooting

`make logs S=core` shows capped logs; avoid copying complete logs with private tool results
into a public issue. `make ps`, `make preflight` and `make verify` report operational status.
Changing the password with the guarded hash-password/deploy flow invalidates old sessions.
Token rotation is a deliberate replacement of named secret files followed by deploy, not a
blanket deletion of `secrets/`. Back up and plan downtime first.

| Symptom | Check |
|---|---|
| Login cannot resolve on mobile data | Compare DNS on wifi/mobile; the existing NXDOMAIN issue is deferred. Do not change DNS speculatively |
| TLS or 502 failure | Caddy/core health and capped logs, certificate name, ingress 80/443, bind `10.77.1.10:8080` |
| Screen unavailable/black | Browser and relay health, active login/screen session, Origin, `SCREEN_ENABLED`, relay profile; never publish VNC/CDP to diagnose |
| Model unavailable or OOM | Image/weight hashes, registry current, health, restart count and cgroup memory events; retain 3072/3840m and `--cache-ram 0` |
| Shell unavailable | Sandbox health, peer/token permissions and firewall. Do not enable the local backend on production |
| Backup/drill fails | Disk space, nonempty backup path copied to host, built core image, private file permissions and disposable-volume cleanup |
| Need code rollback | Inspect the timestamp+SHA fields in `.deploy/history`, choose an approved compatible SHA and deploy it. Check schema compatibility before changing code |

Updates to images/packages are reviewed changes with new digest/hash pins, not automatic
runtime downloads. Restore drills and host security updates are scheduled operational work.
