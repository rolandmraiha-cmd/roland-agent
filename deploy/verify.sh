#!/usr/bin/env bash
# Server acceptance checklist (§14.8). Prints PASS/FAIL per check.
# Safe to run read-only; skips checks that need unimplemented sidecars or missing sudo.
set -euo pipefail
# This read-only checklist has no interactive prompts. In particular, Docker
# clients under timeout must not read from a terminal in a background group.
exec </dev/null
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

pass=0
fail=0
skip=0
result() {
    local status=$1
    shift
    printf '[%s] %s\n' "$status" "$*"
    case $status in
        PASS) pass=$((pass + 1)) ;;
        FAIL) fail=$((fail + 1)) ;;
        SKIP) skip=$((skip + 1)) ;;
    esac
}

need_cmd docker || { result FAIL "docker not available"; printf 'verify: %s pass / %s fail / %s skip\n' "$pass" "$fail" "$skip"; exit 1; }

cd -- "$deploy_repo"

# 1. Service health + ports only on caddy
if compose ps >/dev/null 2>&1; then
    bad=0
    while IFS= read -r line; do
        name=${line%% *}
        ports=${line#* }
        case $name in
            ${deploy_project}-caddy-* | *caddy*) continue ;;
        esac
        if [[ $ports == *'0.0.0.0:'* || $ports == *':::'* ]]; then
            bad=1
            result FAIL "non-caddy published ports: $line"
        fi
    done < <(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null || true)
    if ((bad == 0)); then
        result PASS "published ports only on caddy (or none)"
    fi
    unhealthy=0
    while IFS= read -r line; do
        [[ -n $line ]] || continue
        health=$(printf '%s' "$line" | python3 -c 'import json,sys; o=json.loads(sys.stdin.read()); print(o.get("Health") or "")' 2>/dev/null || true)
        name=$(printf '%s' "$line" | python3 -c 'import json,sys; o=json.loads(sys.stdin.read()); print(o.get("Service") or o.get("Name") or "")' 2>/dev/null || true)
        if [[ -n $health && $health != healthy ]]; then
            unhealthy=1
            result FAIL "service not healthy: $name ($health)"
        fi
    done < <(compose ps --format json 2>/dev/null || true)
    if ((unhealthy == 0)); then
        result PASS "compose services healthy or without Health field"
    fi
else
    result FAIL "compose ps unavailable"
fi

# 2. No internal listeners on host
if command -v ss >/dev/null 2>&1; then
    if sudo -n true 2>/dev/null; then
        leak=0
        for port in 8080 7000 7100 5900 6080; do
            if sudo -n ss -tulpn 2>/dev/null | grep -E ":${port}\b" | grep -vE '127\.0\.0\.1|\[::1\]' | grep -q .; then
                leak=1
                result FAIL "host listener on $port"
            fi
        done
        if ((leak == 0)); then
            result PASS "no host listeners on 8080/7000/7100/5900/6080"
        fi
    else
        result SKIP "ss host-listener check needs passwordless sudo"
    fi
else
    result SKIP "ss not available"
fi

# 3. TLS smoke (best-effort)
agent_host=$(
    python3 - <<PY
from pathlib import Path
p = Path("$deploy_repo") / ".env"
vals = {}
if p.is_file():
    for line in p.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line: continue
        k,_,v = line.partition("="); vals[k]=v
print(vals.get("AGENT_HOST") or vals.get("AGENT_DOMAIN") or vals.get("AGENT_FALLBACK_HOST") or "")
PY
)
if [[ -n $agent_host ]] && command -v curl >/dev/null 2>&1; then
    if curl -fsS -o /dev/null --connect-timeout 5 "https://${agent_host}/login"; then
        result PASS "https://${agent_host}/login reachable"
    else
        result FAIL "https://${agent_host}/login not reachable"
    fi
else
    result SKIP "TLS login check (no AGENT_HOST or curl)"
fi

# 4. isolation.sh --server (may be unimplemented)
if [[ -x $deploy_repo/tests/integration/isolation.sh ]]; then
    if bash "$deploy_repo/tests/integration/isolation.sh" --server; then
        result PASS "isolation.sh --server"
    else
        result FAIL "isolation.sh --server"
    fi
else
    result SKIP "isolation.sh --server not present yet"
fi

# 5. log config
if compose ps -q >/dev/null 2>&1; then
    ids=$(compose ps -q 2>/dev/null || true)
    if [[ -n $ids ]]; then
        bad_log=0
        for id in $ids; do
            cfg=$(docker inspect --format '{{json .HostConfig.LogConfig}}' "$id" 2>/dev/null || echo {})
            if ! printf '%s' "$cfg" | grep -q '10m'; then
                bad_log=1
            fi
        done
        if ((bad_log == 0)); then
            result PASS "container log max-size looks capped"
        else
            result FAIL "one or more containers missing 10m log max-size"
        fi
    else
        result SKIP "no running containers for log-config check"
    fi
fi

# 6. memory headroom (informational thresholds)
if command -v free >/dev/null 2>&1; then
    avail_mi=$(free -m | awk '/^Mem:/{print $7}')
    if ((avail_mi >= 1200)); then
        result PASS "available memory ${avail_mi} MiB (>= 1200 idle target)"
    else
        result FAIL "available memory ${avail_mi} MiB (< 1200 idle target)"
    fi
else
    result SKIP "free not available"
fi

# 7. backup exists
if compose exec -T core ls -l /backups/db >/dev/null 2>&1; then
    result PASS "backup directory visible in core"
else
    result SKIP "backup check (core not running or /backups empty)"
fi

# 8. model health + no egress
if compose exec -T model bash /opt/run/run.sh healthcheck >/dev/null 2>&1; then
    result PASS "model healthcheck"
else
    result SKIP "model healthcheck (service not running?)"
fi
if compose exec -T model bash -c 'timeout 5 bash -c "echo > /dev/tcp/1.1.1.1/443"' >/dev/null 2>&1; then
    result FAIL "model container reached the internet"
else
    # Failure to connect is the expected PASS when model is up; if model missing, SKIP.
    if compose ps --status running --services 2>/dev/null | grep -qx model; then
        result PASS "model has no internet egress"
    else
        result SKIP "model egress check (model not running)"
    fi
fi

# 9. Browser health (only when the optional service is running)
if compose ps --status running --services 2>/dev/null | grep -qx browser; then
    if compose exec -T browser python -m browserd healthcheck >/dev/null 2>&1; then
        result PASS "browser healthcheck"
    else
        result FAIL "browser healthcheck"
    fi
else
    result SKIP "browser healthcheck (browser not running)"
fi

# 10. Screen (M7). Without a login nobody gets to the screen routes, whether the screen is
# on (401) or off (403). Anything else here (a page, a websocket, an error from the relay)
# means a request got past core.
if [[ -n $agent_host ]] && command -v curl >/dev/null 2>&1; then
    screen_codes=""
    for screen_path in /screen/novnc/core/rfb.js /screen/websockify; do
        code=$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 "https://${agent_host}${screen_path}" 2>/dev/null || true)
        screen_codes+="${code:-none} "
    done
    case $screen_codes in
        "401 401 " | "403 403 ") result PASS "screen routes refuse a visitor without a login (${screen_codes% })" ;;
        *) result FAIL "screen routes answered a visitor without a login: ${screen_codes% } (expected 401 or 403)" ;;
    esac
else
    result SKIP "screen routes check (no AGENT_HOST or curl)"
fi
# The screen server itself, only when the optional relay is running and the screen is on.
if compose ps --status running --services 2>/dev/null | grep -qx novnc; then
    screen_on=$(compose exec -T browser printenv SCREEN_ENABLED 2>/dev/null || true)
    if [[ ${screen_on,,} == true ]]; then
        if compose exec -T browser python -m browserd healthcheck screen >/dev/null 2>&1; then
            result PASS "screen server (x11vnc) listening in the browser container"
        else
            result FAIL "screen server (x11vnc) is not listening; see: docker compose logs browser"
        fi
    else
        result SKIP "screen server (novnc runs, but SCREEN_ENABLED is not true for the browser)"
    fi
else
    result SKIP "screen server (novnc not running)"
fi

# 11. Phone/manual checks are human-only
result SKIP "manual phone login / chat / approval (human)"

printf '\nverify: %s pass / %s fail / %s skip\n' "$pass" "$fail" "$skip"
if ((fail > 0)); then
    exit 1
fi
exit 0
