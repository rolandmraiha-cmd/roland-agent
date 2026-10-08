#!/usr/bin/env bash
# A6.3 and A7.3: the real browser service against the fixture site, in the compose test stack,
# with the screen switched on (x11vnc in the browser container, and the noVNC relay).
# Builds the images, starts the fixture site, the browser and the relay, runs the live tests,
# restarts the browser to check that a sign-in survives, and then looks at the containers
# from outside.
# Usage:
#   browser.sh                      # run everything, then remove the test stack
#   KEEP=1 browser.sh               # leave the test stack up afterwards
#   BUILD=0 browser.sh              # use the images that are already built
#   CHROMIUM_SANDBOX=1 browser.sh   # the same with Chromium's own sandbox switched on
# Needs Docker, a .env, and the files `make secrets` creates in secrets/.
# Not for the production host: the test stack uses the same private addresses.
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd -- "$ROOT"

die() {
    printf '%s\n' "$*" >&2
    exit 2
}

command -v docker >/dev/null 2>&1 || die "docker not available"
[[ -f .env ]] || die "no .env here; for a test run: cp -n .env.example .env"
[[ -s secrets/browser_api_token && -s secrets/sandbox_api_token ]] || die "missing secrets; run: make secrets"
[[ -s secrets/vnc_password && -s secrets/vnc_view_password ]] || die "missing screen passwords; run: make secrets"
if docker network inspect roland-agent_edge >/dev/null 2>&1; then
    die "the production stack is on this host; run the browser tests somewhere else"
fi

if [[ ${CHROMIUM_SANDBOX:-0} == 1 ]]; then
    export BROWSER_CHROMIUM_SANDBOX=true
    export BROWSER_SECCOMP=./docker/browser/seccomp-chromium.json
fi

compose=(docker compose -f docker-compose.yml -f docker-compose.test.yml)
tests=(python -m pytest -m integration -q -p no:cacheprovider tests/integration/test_browser_live.py)
screen_tests=(python -m pytest -m integration -q -p no:cacheprovider tests/integration/test_screen_live.py)

cleanup() {
    status=$?
    if [[ $status != 0 ]]; then
        "${compose[@]}" logs --no-color --tail 60 browser novnc fixture-web sandbox || true
    fi
    if [[ ${KEEP:-0} != 1 ]]; then
        "${compose[@]}" --profile browser --profile screen down --volumes --remove-orphans || true
    fi
    exit "$status"
}
trap cleanup EXIT

# The browser's /files is the workspace's browser folder. Both containers run as uid 1000.
workspace=$("${compose[@]}" config --format json | python3 -c '
import json, sys
mounts = json.load(sys.stdin)["services"]["tester"]["volumes"]
print(next(mount["source"] for mount in mounts if mount["target"] == "/workspace"))')
mkdir -p -- "$workspace/browser/downloads" "$workspace/browser/uploads"
if [[ $(stat -c '%u' -- "$workspace/browser/uploads") != 1000 ]]; then
    chown -R 1000:1000 -- "$workspace" 2>/dev/null || sudo -n chown -R 1000:1000 -- "$workspace" ||
        die "$workspace must belong to uid 1000 (sudo chown -R 1000:1000 $workspace)"
fi

if [[ ${BUILD:-1} != 0 ]]; then
    printf '==> build\n'
    "${compose[@]}" build fixture-web sandbox browser novnc tester
fi

printf '==> start the fixture site, the sandbox, the browser and the screen relay\n'
"${compose[@]}" up -d --wait --wait-timeout 180 fixture-web sandbox browser novnc

printf '==> live tests\n'
"${compose[@]}" run --rm --no-deps -e BROWSER_LIVE_REQUIRED=1 tester "${tests[@]}"

printf '==> live screen tests (A7.3)\n'
"${compose[@]}" run --rm --no-deps -e BROWSER_LIVE_REQUIRED=1 tester "${screen_tests[@]}"

# x11vnc lets in noVNC's address (and, in this test stack only, the tester's). Anyone else on
# the same network is dropped before it can log in, even with the right passwords.
vnc_network=$(docker inspect --format '{{ range $name, $_ := .NetworkSettings.Networks }}{{ $name }} {{ end }}' \
    "$("${compose[@]}" ps --quiet browser)" | tr ' ' '\n' | grep '_vnc$')
if docker run --rm --network "$vnc_network" --ip 10.77.5.99 --user 1000:1000 --read-only --cap-drop ALL \
    -v "$ROOT/tests/integration:/t:ro" -v "$ROOT/secrets:/s:ro" \
    --entrypoint python roland-agent/tester:local -c '
import sys
sys.path.insert(0, "/t")
import rfb
got_in = False
for name in ("vnc_password", "vnc_view_password"):
    try:
        with rfb.Rfb("10.77.5.40", 5900, open("/s/" + name).read().strip(), timeout=5):
            got_in = True
    except (rfb.RfbError, OSError):
        pass
sys.exit(1 if got_in else 0)'; then
    printf 'ok: an address that is not noVNC cannot log in to the screen server, even with the passwords\n'
else
    printf 'FAIL the screen server let in an address that is not noVNC\n' >&2
    exit 1
fi

# Memory after the tests. The counters include file cache (the Chromium program itself the
# first time it starts), which the kernel gives back when needed, so "programs" is the figure
# to compare with the 1280 MiB cap.
"${compose[@]}" exec -T browser python -c '
import pathlib
root = pathlib.Path("/sys/fs/cgroup")
old = (root / "memory/memory.stat").is_file()  # cgroup v1
stat_file, peak_file = ("memory/memory.stat", "memory/memory.max_usage_in_bytes") if old else ("memory.stat", "memory.peak")
names = ("total_rss", "total_shmem", "total_cache") if old else ("anon", "shmem", "file")
try:
    stat = dict(line.split() for line in (root / stat_file).read_text().splitlines())
    programs, shared, cache = (int(stat[name]) // 2**20 for name in names)
    print(f"memory after the tests: programs {programs} MiB, shared memory and /tmp {shared} MiB, "
          f"file cache {max(cache - shared, 0)} MiB")
    print(f"highest total so far, file cache included: {int((root / peak_file).read_text()) // 2**20} MiB")
except (OSError, ValueError, KeyError):
    print("memory: not reported by this kernel")'

printf '==> restart the browser; the profile must keep the sign-in\n'
"${compose[@]}" restart browser
"${compose[@]}" up -d --wait --wait-timeout 180 browser
"${compose[@]}" run --rm --no-deps -e BROWSER_LIVE_REQUIRED=1 -e BROWSER_LIVE_AFTER_RESTART=1 tester \
    "${tests[@]}" -k test_profile_persists_across_restart

printf '==> the container from outside\n'
"${compose[@]}" exec -T -e EXPECT_CHROMIUM_SANDBOX="${CHROMIUM_SANDBOX:-0}" browser python - <<'PY'
import os
import pathlib
import sys

problems = []


def listening(table: str) -> set[str]:
    found = set()
    try:
        lines = pathlib.Path(table).read_text().splitlines()[1:]
    except OSError:
        return found  # no IPv6 in this container
    for line in lines:
        fields = line.split()
        if fields[3] == "0A":  # LISTEN
            address, port = fields[1].rsplit(":", 1)
            found.add(f"{address}:{int(port, 16)}")
    return found


# 10.77.4.40 in the kernel's byte order, port 7100: browserd. 10.77.5.40, port 5900: the
# screen server, on the vnc network only. And nothing else, on IPv4 or IPv6.
# (0B00007F is 127.0.0.11, Docker's own name resolver inside every container.)
sockets = listening("/proc/net/tcp") | listening("/proc/net/tcp6")
sockets = {entry for entry in sockets if not entry.startswith("0B00007F:")}
if sockets != {"28044D0A:7100", "28054D0A:5900"}:
    problems.append(f"unexpected listening sockets: {sorted(sockets)}")

# The screen passwords are in x11vnc's private file and nowhere a process listing shows.
passwords = [pathlib.Path(f"/run/secrets/{name}").read_text().strip() for name in ("vnc_password", "vnc_view_password")]
password_file = pathlib.Path("/tmp/vnc.passwd")
if not password_file.is_file() or (password_file.stat().st_mode & 0o777) != 0o600:
    problems.append("x11vnc's password file is missing or not private")
screen_servers = 0

chromium = []
for folder in pathlib.Path("/proc").iterdir():
    if not folder.name.isdigit():
        continue
    try:
        command = (folder / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        owner = next(line for line in (folder / "status").read_text().splitlines() if line.startswith("Uid:"))
    except (OSError, StopIteration):
        continue  # the process ended while we looked
    if owner.split()[1:] != ["1000"] * 4:
        problems.append(f"process {folder.name} does not run as uid 1000")
    if any(password in command for password in passwords):
        problems.append(f"process {folder.name} has a screen password on its command line")
    if command.startswith("x11vnc "):
        screen_servers += 1
        try:
            environment = (folder / "environ").read_bytes()
        except OSError:
            environment = b""
        if b"TOKEN" in environment or b"/run/secrets" in environment or any(p.encode() in environment for p in passwords):
            problems.append("x11vnc was given the service's settings or a password in its environment")
        for flag in (" -nopw", " -passwd ", " -debug_keyboard", " -localhost", " -yesremote"):
            if flag in command + " ":
                problems.append(f"x11vnc runs with{flag}")
        for flag in (" -listen 10.77.5.40 ", " -no6 ", " -safer ", " -nocmds ", " -quiet", " -passwdfile /tmp/vnc.passwd "):
            if flag not in command + " ":
                problems.append(f"x11vnc runs without{flag}")
    if "/chrome " in command or command.rstrip().endswith("/chrome"):
        chromium.append((folder, command))
        try:
            environment = (folder / "environ").read_bytes()
        except OSError:
            environment = b""
        if b"TOKEN" in environment or b"/run/secrets" in environment:
            problems.append(f"Chromium process {folder.name} was given the service's token settings")

if screen_servers != 1:
    problems.append(f"expected one x11vnc process, found {screen_servers}")
if not chromium:
    problems.append("no Chromium process found")
main = [command for _, command in chromium if "--type=" not in command]
if len(main) != 1:
    problems.append(f"expected one main Chromium process, found {len(main)}")
else:
    if "--remote-debugging-pipe" not in main[0] or "--remote-debugging-port" in main[0]:
        problems.append("Chromium must be driven over a pipe, with no debugging port")
    if "--user-data-dir=/profile" not in main[0]:
        problems.append("Chromium does not use the profile volume")
    if "--host-resolver-rules=" not in main[0]:
        problems.append("Chromium was started without the private-address rules")
    sandboxed = "--no-sandbox" not in main[0]
    if sandboxed != (os.environ.get("EXPECT_CHROMIUM_SANDBOX") == "1"):
        problems.append(f"Chromium sandbox is {'on' if sandboxed else 'off'}, which is not what was asked for")

policy = pathlib.Path("/etc/opt/chrome_for_testing/policies/managed/roland-agent.json")
if not policy.is_file():
    problems.append("the managed policy file is missing")
try:
    pathlib.Path("/app/probe").write_text("x")
    problems.append("the container's own files are writable")
except OSError:
    pass

for problem in problems:
    print(f"FAIL {problem}", file=sys.stderr)
if problems:
    raise SystemExit(1)
print(f"ok: two listening sockets (browserd, and x11vnc on the vnc network), {len(chromium)} Chromium "
      "processes as uid 1000, pipe transport, policy file in place, read-only root, no screen password "
      "in any command line")
PY

# The relay: up, as uid 1000, with its one listening socket on the network Caddy uses.
"${compose[@]}" exec -T novnc python - <<'PY'
import os
import pathlib
import sys

problems = []
found = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        lines = pathlib.Path(table).read_text().splitlines()[1:]
    except OSError:
        continue
    for line in lines:
        fields = line.split()
        if fields[3] == "0A" and not fields[1].startswith("0B00007F:"):
            found.add(fields[1])
if found != {"1E024D0A:17C0"}:  # 10.77.2.30:6080
    problems.append(f"unexpected listening sockets in novnc: {sorted(found)}")
if os.geteuid() != 1000:
    problems.append("novnc does not run as uid 1000")
if any(name.startswith(("VNC", "BROWSER", "MODEL", "AGENT", "SANDBOX")) for name in os.environ):
    problems.append("novnc was given settings that are not its own")
if pathlib.Path("/run/secrets").exists() and any(pathlib.Path("/run/secrets").iterdir()):
    problems.append("novnc was given a secret")
served = sorted(path.name for path in pathlib.Path("/opt/novnc").iterdir())
if "core" not in served or "vendor" not in served or any(name.endswith(".html") for name in served):
    problems.append(f"novnc serves something other than the library: {served}")
try:
    pathlib.Path("/opt/novnc/core/probe.js").write_text("x")
    problems.append("the files novnc serves are writable")
except OSError:
    pass
for problem in problems:
    print(f"FAIL {problem}", file=sys.stderr)
if problems:
    raise SystemExit(1)
print("ok: the relay listens on the screen network only, as uid 1000, with no secret and no settings")
PY

# Neither container's log may hold a screen password (A7.4 asks the same on the server).
if "${compose[@]}" logs --no-color browser novnc 2>&1 | grep -q -F -e "$(cat secrets/vnc_password)" -e "$(cat secrets/vnc_view_password)"; then
    printf 'FAIL a screen password appears in a container log\n' >&2
    exit 1
fi
printf 'ok: no screen password in the browser or relay logs\n'

# A6.4 without the host firewall: the sandbox can't reach browserd, x11vnc or the relay, and
# the browser can't reach sandboxd or the relay, while all are up. Core and the model aren't
# part of this stack, so their probes are skipped here; on the server `make verify` runs them all.
ISOLATION_COMPOSE_FILES="docker-compose.yml docker-compose.test.yml" bash tests/integration/isolation.sh --ci

# The signed-in profile must be in the browser container and in no other.
for container in $("${compose[@]}" ps --all --quiet); do
    name=$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$container")
    mounts=$(docker inspect --format '{{ range .Mounts }}{{ .Destination }} {{ end }}' "$container")
    if [[ $name != browser && $mounts == *"/profile"* ]]; then
        printf 'FAIL %s mounts the browser profile\n' "$name" >&2
        exit 1
    fi
done
ports=$("${compose[@]}" ps --format '{{ .Publishers }}' browser)
if [[ $ports == *"0.0.0.0"* || $ports == *":::"* ]]; then
    printf 'FAIL the browser service publishes a port: %s\n' "$ports" >&2
    exit 1
fi
printf 'ok: profile volume only in the browser container, no published port\n'

# Nothing may have died along the way: no out-of-memory kill, no crash that the launcher
# or Docker quietly recovered from.
browser_container=$("${compose[@]}" ps --quiet browser)
state=$(docker inspect --format '{{ .State.OOMKilled }} {{ .RestartCount }}' "$browser_container")
if [[ $state != "false 0" ]]; then
    printf 'FAIL the browser container was killed or restarted by itself (OOMKilled RestartCount: %s)\n' "$state" >&2
    exit 1
fi
if "${compose[@]}" logs --no-color browser 2>&1 | grep -E 'exited with|next start in' >/dev/null; then
    printf 'FAIL a process inside the browser container died and was started again\n' >&2
    exit 1
fi
printf 'ok: no out-of-memory kill, no restart, no process died\n'

printf '==> memory now, after the restart\n'
docker stats --no-stream --format '{{ .Name }}  {{ .MemUsage }}  {{ .MemPerc }}' "$browser_container"

printf '\nbrowser stack: all checks passed\n'
