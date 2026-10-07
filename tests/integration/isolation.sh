#!/usr/bin/env bash
# A4.4/A6.4: lateral targets must be blocked from the sandbox and browser.
# Usage:
#   isolation.sh            # CI / local compose stack (no host firewall checks)
#   isolation.sh --server   # Contabo after firewall (§14.8): also blocks SSH and checks egress
set -euo pipefail

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

compose() {
    docker compose -f docker-compose.yml "$@"
}

# Fail the probe if it succeeds or hangs past 5s. Success of curl = isolation failure.
must_fail() {
    local label=$1
    shift
    if timeout 5 docker compose -f docker-compose.yml exec -T sandbox "$@" >/dev/null 2>&1; then
        printf 'FAIL isolation: %s unexpectedly succeeded\n' "$label" >&2
        return 1
    fi
    printf 'ok fail: %s\n' "$label"
}

must_succeed() {
    local label=$1
    shift
    if ! timeout 15 docker compose -f docker-compose.yml exec -T sandbox "$@" >/dev/null 2>&1; then
        printf 'FAIL isolation: %s unexpectedly failed\n' "$label" >&2
        return 1
    fi
    printf 'ok allow: %s\n' "$label"
}

# Browser images need only Python, not curl. A distinct blocked exit code keeps
# missing Python, Docker errors and a hung probe from looking like isolation.
browser_must_fail() {
    local label=$1 host=$2 port=$3 status=0
    timeout 5 docker compose -f docker-compose.yml exec -T browser python3 -c '
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
    if ! timeout 15 docker compose -f docker-compose.yml exec -T browser python3 -c '
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

if ! docker compose -f docker-compose.yml ps --status running --services 2>/dev/null | grep -qx sandbox; then
    printf 'sandbox service is not running; start the stack first\n' >&2
    exit 1
fi

must_fail "curl core by name" curl -fsS --connect-timeout 3 http://core:8080/healthz
must_fail "curl core sandbox_ctl IP" curl -fsS --connect-timeout 3 http://10.77.3.10:8080/healthz
must_fail "curl core edge IP" curl -fsS --connect-timeout 3 http://10.77.1.10:8080/
must_fail "curl browserd" curl -fsS --connect-timeout 3 http://10.77.4.40:7100/healthz
must_fail "curl x11vnc" curl -fsS --connect-timeout 3 http://10.77.5.40:5900

if [[ $mode == server ]]; then
    must_fail "curl link-local metadata" curl -fsS --connect-timeout 3 http://169.254.169.254/
    must_fail "curl gateway ssh" curl -fsS --connect-timeout 3 http://10.77.11.1:22
    must_fail "curl public ssh" curl -fsS --connect-timeout 3 http://37.60.226.214:22
    must_succeed "curl example.com" curl -fsS --connect-timeout 10 https://example.com
fi

if compose ps --status running --services 2>/dev/null | grep -qx browser; then
    browser_must_fail "browser to core browser_ctl IP" 10.77.4.10 8080
    browser_must_fail "browser to core edge IP" 10.77.1.10 8080
    browser_must_fail "browser to sandboxd" 10.77.3.20 7000
    browser_must_fail "browser to model" 10.77.6.60 8080
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
