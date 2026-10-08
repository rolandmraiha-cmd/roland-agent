#!/usr/bin/env bash
# A4.4/A6.4/A7.3: lateral targets must be blocked from the sandbox and browser.
# Usage:
#   isolation.sh            # CI / local compose stack (no host firewall checks)
#   isolation.sh --server   # Contabo after firewall (§14.8): also blocks SSH and checks egress
# The stack probed is the production compose file. A test stack names its own files:
#   ISOLATION_COMPOSE_FILES="docker-compose.yml docker-compose.test.yml" isolation.sh
set -euo pipefail
# Acceptance probes never consume the caller's terminal or piped input.
exec </dev/null

mode=ci
if [[ ${1:-} == --server ]]; then
    mode=server
elif [[ ${1:-} == --ci || -z ${1:-} ]]; then
    mode=ci
elif [[ ${1:-} == --help || ${1:-} == -h ]]; then
    printf 'Usage: isolation.sh [--ci|--server]\n'
    exit 0
else
    printf 'Unknown argument: %s\n' "$1" >&2
    exit 2
fi

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd -- "$ROOT"

need_docker() {
    command -v docker >/dev/null 2>&1 || { printf 'docker not available\n' >&2; exit 1; }
}

compose_args=()
read -r -a compose_files <<< "${ISOLATION_COMPOSE_FILES:-docker-compose.yml}"
for compose_file in "${compose_files[@]}"; do
    compose_args+=(-f "$compose_file")
done

compose() {
    timeout --kill-after=2s 15s docker compose "${compose_args[@]}" "$@"
}

# Stop the probe inside the container: stopping Docker's client alone may leave
# curl running there. The outer deadline also bounds a stuck Docker exec client.
sandbox_probe() {
    local seconds=$1
    shift
    timeout --kill-after=2s "$((seconds + 5))s" docker compose "${compose_args[@]}" exec -T --interactive=false sandbox \
        timeout --kill-after=1s "${seconds}s" "$@"
}

# Only curl's DNS, connection and connect-timeout errors show a blocked target.
# HTTP errors, missing commands and deadline expiry must fail verification.
must_fail() {
    local label=$1 status=0
    shift
    printf 'check: %s\n' "$label"
    sandbox_probe 5 "$@" >/dev/null 2>&1 || status=$?
    case $status in
        6 | 7 | 28) ;;
        *)
            printf 'FAIL isolation: %s (probe exit %s; expected blocked connection)\n' "$label" "$status" >&2
            return 1
            ;;
    esac
    printf 'ok fail: %s\n' "$label"
}

must_succeed() {
    local label=$1
    shift
    printf 'check: %s\n' "$label"
    if ! sandbox_probe 15 "$@" >/dev/null 2>&1; then
        printf 'FAIL isolation: %s unexpectedly failed\n' "$label" >&2
        return 1
    fi
    printf 'ok allow: %s\n' "$label"
}

# Browser images need only Python, not curl. A distinct blocked exit code keeps
# missing Python, Docker errors and a hung probe from looking like isolation.
browser_must_fail() {
    local label=$1 host=$2 port=$3 status=0
    printf 'check: %s\n' "$label"
    timeout --kill-after=2s 5s docker compose "${compose_args[@]}" exec -T --interactive=false browser python3 -c '
import socket, sys
try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3):
        pass
except OSError:
    sys.exit(42)
sys.exit(0)
' "$host" "$port" >/dev/null 2>&1 || status=$?
    if [[ $status != 42 ]]; then
        printf 'FAIL isolation: %s (probe exit %s; expected blocked connection)\n' "$label" "$status" >&2
        return 1
    fi
    printf 'ok fail: %s\n' "$label"
}

browser_must_succeed() {
    local label=$1 host=$2 port=$3
    printf 'check: %s\n' "$label"
    if ! timeout --kill-after=2s 15s docker compose "${compose_args[@]}" exec -T --interactive=false browser python3 -c '
import socket, sys
with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=10):
    pass
' "$host" "$port" >/dev/null 2>&1; then
        printf 'FAIL isolation: %s unexpectedly failed\n' "$label" >&2
        return 1
    fi
    printf 'ok allow: %s\n' "$label"
}

need_docker

if ! running=$(compose ps --status running --services 2>/dev/null); then
    printf 'FAIL isolation: cannot list running Compose services\n' >&2
    exit 1
fi
is_running() {
    grep -qx -- "$1" <<< "$running"
}

if ! is_running sandbox; then
    printf 'sandbox service is not running; start the stack first\n' >&2
    exit 1
fi

# A refused or dropped connection only shows isolation when something is listening at the
# other end. On the server every target must be up, so a stopped core or model fails the run
# instead of passing it. In a partial test stack such probes are skipped, not counted.
target_up() {
    local service=$1
    if is_running "$service"; then
        return 0
    fi
    if [[ $mode == server ]]; then
        printf 'FAIL isolation: %s is not running, so a blocked connection to it proves nothing\n' "$service" >&2
        exit 1
    fi
    printf 'SKIP probes of %s: it is not running\n' "$service"
    return 1
}

if target_up core; then
    must_fail "curl core by name" curl -fsS --connect-timeout 3 http://core:8080/healthz
    must_fail "curl core sandbox_ctl IP" curl -fsS --connect-timeout 3 http://10.77.3.10:8080/healthz
    must_fail "curl core edge IP" curl -fsS --connect-timeout 3 http://10.77.1.10:8080/
fi
# The browser is optional, so without it this probe is skipped in both modes.
if is_running browser; then
    must_fail "curl browserd" curl -fsS --connect-timeout 3 http://10.77.4.40:7100/healthz
else
    printf 'SKIP sandbox to browserd: browser service is not running\n'
fi
# The screen server (M7) listens here while the screen is switched on. The sandbox is on
# neither screen network, so it must not get to it or to the relay in front of it.
must_fail "curl x11vnc" curl -fsS --connect-timeout 3 http://10.77.5.40:5900
if is_running novnc; then
    must_fail "curl noVNC from the sandbox" curl -fsS --connect-timeout 3 http://10.77.2.30:6080/
    must_fail "curl noVNC vnc address from the sandbox" curl -fsS --connect-timeout 3 http://10.77.5.30:6080/
else
    printf 'SKIP sandbox to noVNC: novnc service is not running\n'
fi

if [[ $mode == server ]]; then
    must_fail "curl link-local metadata" curl -fsS --connect-timeout 3 http://169.254.169.254/
    must_fail "curl gateway ssh" curl -fsS --connect-timeout 3 http://10.77.11.1:22
    must_fail "curl public ssh" curl -fsS --connect-timeout 3 http://37.60.226.214:22
    must_succeed "curl example.com" curl -fsS --connect-timeout 10 https://example.com
fi

if is_running browser; then
    if target_up core; then
        browser_must_fail "browser to core browser_ctl IP" 10.77.4.10 8080
        browser_must_fail "browser to core edge IP" 10.77.1.10 8080
    fi
    browser_must_fail "browser to sandboxd" 10.77.3.20 7000
    if target_up model; then
        browser_must_fail "browser to model" 10.77.6.60 8080
    fi
    # The browser shares the vnc network with the relay, but connections only go one way:
    # the relay listens on its other address only, and the firewall drops browser -> relay.
    if is_running novnc; then
        browser_must_fail "browser to noVNC on the vnc network" 10.77.5.30 6080
        browser_must_fail "browser to noVNC on the screen network" 10.77.2.30 6080
    fi
    if [[ $mode == server ]]; then
        browser_must_fail "browser to link-local metadata" 169.254.169.254 80
        browser_must_fail "browser to gateway ssh" 10.77.12.1 22
        browser_must_fail "browser to public ssh" 37.60.226.214 22
        browser_must_succeed "browser to example.com:443" example.com 443
    fi
else
    printf 'SKIP browser isolation: browser service is not running\n'
fi

printf 'isolation.sh (%s): all checks passed\n' "$mode"
