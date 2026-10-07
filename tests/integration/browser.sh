#!/usr/bin/env bash
# A6.3: the real browser service against the fixture site, in the compose test stack.
# Builds the images, starts the fixture site and the browser, runs the live tests, restarts
# the browser to check that a sign-in survives, and then looks at the container from outside.
# Usage:
#   browser.sh                      # run everything, then remove the test stack
#   KEEP=1 browser.sh               # leave the test stack up afterwards
#   BUILD=0 browser.sh              # use the images that are already built
#   CHROMIUM_SANDBOX=1 browser.sh   # the same with Chromium's own sandbox switched on
# Needs Docker, a .env, and secrets/browser_api_token (make secrets).
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
if docker network inspect roland-agent_edge >/dev/null 2>&1; then
    die "the production stack is on this host; run the browser tests somewhere else"
fi

if [[ ${CHROMIUM_SANDBOX:-0} == 1 ]]; then
    export BROWSER_CHROMIUM_SANDBOX=true
    export BROWSER_SECCOMP=./docker/browser/seccomp-chromium.json
fi

compose=(docker compose -f docker-compose.yml -f docker-compose.test.yml)
tests=(python -m pytest -m integration -q -p no:cacheprovider tests/integration/test_browser_live.py)

cleanup() {
    status=$?
    if [[ $status != 0 ]]; then
        "${compose[@]}" logs --no-color --tail 60 browser fixture-web || true
    fi
    if [[ ${KEEP:-0} != 1 ]]; then
        "${compose[@]}" --profile browser down --volumes --remove-orphans || true
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
    "${compose[@]}" build fixture-web browser tester
fi

printf '==> start the fixture site and the browser\n'
"${compose[@]}" up -d --wait --wait-timeout 180 fixture-web browser

printf '==> live tests\n'
"${compose[@]}" run --rm --no-deps -e BROWSER_LIVE_REQUIRED=1 tester "${tests[@]}"

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


# 10.77.4.40 in the kernel's byte order, port 7100: browserd, and nothing else.
# (0B00007F is 127.0.0.11, Docker's own name resolver inside every container.)
sockets = listening("/proc/net/tcp") | listening("/proc/net/tcp6")
sockets = {entry for entry in sockets if not entry.startswith("0B00007F:")}
if sockets != {"28044D0A:7100"}:
    problems.append(f"unexpected listening sockets: {sorted(sockets)}")

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
    if "/chrome " in command or command.rstrip().endswith("/chrome"):
        chromium.append((folder, command))
        try:
            environment = (folder / "environ").read_bytes()
        except OSError:
            environment = b""
        if b"TOKEN" in environment or b"/run/secrets" in environment:
            problems.append(f"Chromium process {folder.name} was given the service's token settings")

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
print(f"ok: one listening socket (browserd), {len(chromium)} Chromium processes as uid 1000, "
      "pipe transport, policy file in place, read-only root")
PY

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

printf '==> memory after the run (the cap is 1280 MiB)\n'
docker stats --no-stream --format '{{ .Name }}  {{ .MemUsage }}  {{ .MemPerc }}' \
    "$("${compose[@]}" ps --quiet browser)"

printf '\nbrowser stack: all checks passed\n'
