#!/usr/bin/env bash
# Deploy the current tree on this host (§14.8). Stops on first failure.
# Requires APPLY=1. Does not touch DNS/ACME beyond existing .env. No Contabo assumptions.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_apply "deploy (build/up/firewall)"
need_cmd docker
need_cmd git
need_cmd curl

cd -- "$deploy_repo"

printf '==> preflight\n'
bash "$deploy_repo/deploy/preflight.sh"

printf '==> record HEAD\n'
mkdir -p -- "$deploy_repo/.deploy"
head=$(git rev-parse HEAD)
printf '%s\n' "$head" >"$deploy_repo/.deploy/last"
printf '%s %s\n' "$(date -Is)" "$head" >>"$deploy_repo/.deploy/history"
chmod 0644 -- "$deploy_repo/.deploy/last" "$deploy_repo/.deploy/history" || true
printf 'recorded %s\n' "$head"

printf '==> backup if core running\n'
if compose ps --status running --services 2>/dev/null | grep -qx core; then
    compose exec -T core python -m agent backup-now
else
    printf 'core not running; skip backup-now\n'
fi

printf '==> build\n'
build_args=()
if is_truthy "${PULL:-0}"; then
    build_args+=(--pull)
fi
compose build "${build_args[@]+"${build_args[@]}"}"

printf '==> firewall\n'
fw=/usr/local/sbin/roland-agent-firewall
if [[ -x $fw ]]; then
    sudo "$fw"
else
    warn_fw="firewall helper not installed at $fw; run: sudo make firewall APPLY=1"
    if is_truthy "${REQUIRE_FIREWALL:-1}"; then
        die "$warn_fw"
    else
        printf 'WARN: %s\n' "$warn_fw" >&2
    fi
fi

printf '==> up\n'
compose up -d --remove-orphans

printf '==> wait for healthy (<= 180s)\n'
deadline=$((SECONDS + 180))
unhealthy=1
while ((SECONDS < deadline)); do
    mapfile -t lines < <(compose ps --format json 2>/dev/null || true)
    if ((${#lines[@]} == 0)); then
        sleep 2
        continue
    fi
    unhealthy=0
    for line in "${lines[@]}"; do
        [[ -n $line ]] || continue
        health=$(printf '%s' "$line" | python3 -c 'import json,sys; o=json.loads(sys.stdin.read()); print(o.get("Health") or o.get("State") or "")')
        service=$(printf '%s' "$line" | python3 -c 'import json,sys; o=json.loads(sys.stdin.read()); print(o.get("Service") or o.get("Name") or "")')
        case $health in
            healthy|running) ;;
            *)
                unhealthy=1
                printf '  waiting: %s health=%s\n' "$service" "$health"
                ;;
        esac
    done
    if ((unhealthy == 0)); then
        printf 'all services healthy/running\n'
        break
    fi
    sleep 3
done
if ((unhealthy != 0)); then
    die "services not healthy within 180s"
fi

# Resolve AGENT_HOST for smoke checks.
agent_host=$(
    python3 - <<PY
from pathlib import Path
env = Path("$deploy_repo") / ".env"
vals = {}
for line in env.read_text().splitlines():
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    vals[k] = v
host = vals.get("AGENT_HOST") or vals.get("AGENT_DOMAIN") or vals.get("AGENT_FALLBACK_HOST") or "127.0.0.1"
print(host)
PY
)

printf '==> smoke checks against https://%s\n' "$agent_host"
curl_opts=(-fsS)
curl_code_opts=(-sS)
caddy_tls=$(grep -E '^CADDY_TLS=' "$deploy_repo/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)
if [[ ${caddy_tls:-acme} == internal ]]; then
    curl_opts+=(-k)
    curl_code_opts+=(-k)
fi
curl "${curl_opts[@]}" -o /dev/null "https://${agent_host}/login"
code=$(curl "${curl_code_opts[@]}" -o /dev/null -w '%{http_code}' "https://${agent_host}/healthz" || true)
if [[ $code != 404 ]]; then
    die "expected /healthz → 404, got $code"
fi
compose exec -T core python -m agent migrate --check | tee /tmp/roland-agent-migrate-check.txt >/dev/null
grep -q 'up to date' /tmp/roland-agent-migrate-check.txt

printf '\nDeploy OK: https://%s/\n' "$agent_host"
