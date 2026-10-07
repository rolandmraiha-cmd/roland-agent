#!/usr/bin/env bash
# A4.4: from the sandbox container, lateral/metadata targets must fail within 5s.
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
must_fail "curl link-local metadata" curl -fsS --connect-timeout 3 http://169.254.169.254/

if [[ $mode == server ]]; then
    must_fail "curl gateway ssh" curl -fsS --connect-timeout 3 http://10.77.11.1:22
    must_fail "curl public ssh" curl -fsS --connect-timeout 3 http://37.60.226.214:22
    must_succeed "curl example.com" curl -fsS --connect-timeout 10 https://example.com
fi

printf 'isolation.sh (%s): all checks passed\n' "$mode"
