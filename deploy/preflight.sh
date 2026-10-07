#!/usr/bin/env bash
# Full host preflight for M2 deploy baseline (§14.1 / M2.5).
# Read-only by default. Writes AGENT_HOST into .env only with APPLY=1 when nested
# Compose defaults are unsupported. Never prints secret values.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

errors=0
warns=0
fail() { printf 'FAIL: %s\n' "$*" >&2; errors=$((errors + 1)); }
warn() { printf 'WARN: %s\n' "$*" >&2; warns=$((warns + 1)); }
ok() { printf 'OK: %s\n' "$*"; }

version_ge() {
    # Compare dotted versions: version_ge 28.0.0 28 → 0
    python3 - "$1" "$2" <<'PY'
import sys
def parts(v):
    v = v.lstrip("v").split("+", 1)[0].split("-", 1)[0]
    out = []
    for p in v.split("."):
        out.append(int(p) if p.isdigit() else 0)
    return out
a, b = parts(sys.argv[1]), parts(sys.argv[2])
n = max(len(a), len(b))
a += [0] * (n - len(a)); b += [0] * (n - len(b))
sys.exit(0 if a >= b else 1)
PY
}

# --- Docker / Compose ---
if ! have_docker; then
    fail "docker CLI not found"
else
    if docker info >/dev/null 2>&1; then
        engine=$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)
        if [[ -n $engine ]] && version_ge "$engine" "28.0.0"; then
            ok "Docker Engine $engine (>= 28)"
        else
            fail "Docker Engine >= 28 required (got: ${engine:-unknown})"
        fi
        compose_v=$(docker compose version --short 2>/dev/null || true)
        if [[ -n $compose_v ]] && version_ge "$compose_v" "2.33.1"; then
            ok "Compose $compose_v (>= 2.33.1)"
        else
            fail "Compose >= 2.33.1 required (got: ${compose_v:-unknown})"
        fi
        fw=$(docker info 2>/dev/null | grep -i -E 'firewall|iptables' || true)
        if echo "$fw" | grep -qi 'nftables'; then
            warn "Docker firewall backend looks like nftables; use deploy/firewall.sh --nft"
        elif sudo iptables -t filter -L DOCKER-USER -n >/dev/null 2>&1; then
            ok "DOCKER-USER chain present (iptables backend)"
        else
            warn "Could not confirm DOCKER-USER; check firewall backend before make firewall"
        fi
    else
        warn "Docker daemon not reachable from this environment (OK for unit/CI hosts)"
    fi
fi

# --- ufw (informational) ---
if command -v ufw >/dev/null 2>&1; then
    if sudo -n ufw status >/dev/null 2>&1; then
        status=$(sudo -n ufw status verbose 2>/dev/null | head -20 || true)
        if echo "$status" | grep -qE '\b22\b' && echo "$status" | grep -qE '\b80\b' && echo "$status" | grep -qE '\b443\b'; then
            ok "ufw mentions 22/80/443"
        else
            warn "ufw present; confirm 22, 80, 443/tcp and 443/udp are allowed"
        fi
    else
        warn "ufw present but passwordless sudo unavailable for status check"
    fi
else
    warn "ufw not installed (confirm host firewall separately)"
fi

# --- secrets mode/uid ---
secrets_dir=$deploy_repo/secrets
env_file=$deploy_repo/.env
required_secrets=(model_server_token agent_password_hash)
browser_enabled=$(python3 - "$env_file" <<'PY'
import pathlib, shlex, sys

value = ""
path = pathlib.Path(sys.argv[1])
if path.is_file():
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw = line.partition("=")
        if separator and key.strip() == "BROWSER_ENABLED":
            try:
                parts = shlex.split(raw, comments=True)
            except ValueError:
                parts = []
            value = parts[0] if len(parts) == 1 else ""
print("true" if value.lower() in {"true", "1", "yes", "on"} else "false")
PY
)
if [[ $browser_enabled == true ]]; then
    required_secrets+=(browser_api_token)
fi
if [[ ! -d $secrets_dir ]]; then
    fail "secrets/ directory missing (run make secrets && make hash-password)"
else
    mode=$(stat -c '%a' -- "$secrets_dir" 2>/dev/null || echo missing)
    if [[ $mode == 700 ]]; then
        ok "secrets/ mode 0700"
    else
        fail "secrets/ must be mode 0700 (got $mode)"
    fi
    for name in "${required_secrets[@]}"; do
        path=$secrets_dir/$name
        if [[ ! -f $path ]]; then
            fail "missing secret file: $name"
            continue
        fi
        if [[ -L $path ]]; then
            fail "secret $name must not be a symlink"
            continue
        fi
        fmode=$(stat -c '%a' -- "$path")
        fuid=$(stat -c '%u' -- "$path")
        fsize=$(stat -c '%s' -- "$path")
        if [[ $fmode != 400 ]]; then
            fail "secret $name mode must be 0400 (got $fmode)"
        elif [[ $fuid != 1000 ]]; then
            # Browser enabling always requires its production owner. Keep the
            # existing development warning for the other secrets.
            if [[ $name == browser_api_token || ${AGENT_ENV:-} == production || ${STRICT_SECRETS:-0} == 1 ]]; then
                fail "secret $name uid must be 1000 (got $fuid); run make secrets APPLY=1"
            else
                warn "secret $name uid is $fuid (production expects 1000); run make secrets APPLY=1 on the host"
            fi
        else
            ok "secret $name mode 0400 uid 1000"
        fi
        if [[ $fsize -eq 0 ]]; then
            fail "secret $name is empty"
        fi
    done
fi

# --- .env keys ---
if [[ ! -f $env_file ]]; then
    fail ".env missing (cp -n .env.example .env && chmod 600 .env)"
else
    emode=$(stat -c '%a' -- "$env_file" 2>/dev/null || echo missing)
    if [[ $emode == 600 || $emode == 400 ]]; then
        ok ".env mode $emode"
    else
        warn ".env mode is $emode (prefer 0600)"
    fi
    # Required non-secret keys that must exist as assignments (value may be empty).
    for key in AGENT_FALLBACK_HOST CADDY_TLS MODEL_PROVIDER MODEL_BASE_URL; do
        if grep -E "^${key}=" "$env_file" >/dev/null; then
            ok ".env has $key"
        else
            fail ".env missing $key"
        fi
    done
    # Refuse inline secret values that belong in files.
    for key in AGENT_PASSWORD_HASH MODEL_SERVER_TOKEN SANDBOX_API_TOKEN BROWSER_API_TOKEN VNC_PASSWORD VNC_VIEW_PASSWORD TRAINER_API_TOKEN; do
        if grep -E "^${key}=.+" "$env_file" >/dev/null; then
            fail ".env must not set $key directly; use secrets/ files"
        fi
    done
fi

# --- AGENT_HOST nested defaults ---
compute_agent_host() {
    local domain fallback
    domain=$(grep -E '^AGENT_DOMAIN=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- || true)
    fallback=$(grep -E '^AGENT_FALLBACK_HOST=' "$env_file" 2>/dev/null | head -1 | cut -d= -f2- || true)
    fallback=${fallback:-37-60-226-214.sslip.io}
    if [[ -n $domain ]]; then
        printf '%s\n' "$domain"
    else
        printf '%s\n' "$fallback"
    fi
}

nested_ok=0
if have_docker && docker compose version >/dev/null 2>&1; then
    # Probe whether Compose expands nested ${A:-${B}} without error.
    probe=$(mktemp)
    cat >"$probe" <<'YAML'
services:
  probe:
    image: alpine:3.20
    environment:
      AGENT_HOST: ${AGENT_DOMAIN:-${AGENT_FALLBACK_HOST:-37-60-226-214.sslip.io}}
YAML
    if AGENT_DOMAIN='' AGENT_FALLBACK_HOST=nested-probe.example docker compose -f "$probe" config 2>/dev/null | grep -q 'nested-probe.example'; then
        nested_ok=1
        ok "Compose supports nested AGENT_HOST defaults"
    else
        warn "Compose nested defaults unsupported or probe failed"
    fi
    rm -f -- "$probe"
fi

if [[ -f $env_file ]]; then
    current_host=$(grep -E '^AGENT_HOST=' "$env_file" | head -1 | cut -d= -f2- || true)
    desired=$(compute_agent_host)
    if [[ -z $current_host ]]; then
        if ((nested_ok)); then
            ok "AGENT_HOST empty; Compose nested default will supply $desired"
        else
            if is_truthy "${APPLY:-0}"; then
                require_apply "write AGENT_HOST into .env"
                if grep -qE '^AGENT_HOST=' "$env_file"; then
                    # Replace empty assignment without printing unrelated values.
                    python3 - "$env_file" "$desired" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
host = sys.argv[2]
lines = path.read_text().splitlines()
out = []
for line in lines:
    if line.startswith("AGENT_HOST="):
        out.append(f"AGENT_HOST={host}")
    else:
        out.append(line)
path.write_text("\n".join(out) + "\n")
PY
                else
                    printf 'AGENT_HOST=%s\n' "$desired" >>"$env_file"
                fi
                ok "wrote AGENT_HOST=$desired into .env (nested defaults unsupported)"
            else
                fail "AGENT_HOST empty and nested defaults unsupported; re-run with APPLY=1 to write it"
            fi
        fi
    else
        ok "AGENT_HOST is set in .env"
    fi
fi

# --- disk / swap / workspace ---
avail_kb=$(df -Pk "$deploy_repo" | awk 'NR==2{print $4}')
avail_gb=$((avail_kb / 1024 / 1024))
if ((avail_gb >= 15)); then
    ok "free disk ${avail_gb}G (>= 15G)"
else
    fail "free disk ${avail_gb}G (< 15G required)"
fi

if swapon --show --noheadings 2>/dev/null | grep -q .; then
    ok "swap configured"
else
    warn "no swap detected (Contabo target expects ~2G)"
fi

workspace=${WORKSPACE_HOST_DIR:-/srv/roland-agent/workspace}
if [[ -d $workspace ]]; then
    if findmnt -n "$workspace" >/dev/null 2>&1; then
        ok "workspace mounted at $workspace"
    else
        warn "workspace directory exists but is not a separate mount: $workspace"
    fi
    wuid=$(stat -c '%u' -- "$workspace" 2>/dev/null || echo missing)
    wmode=$(stat -c '%a' -- "$workspace" 2>/dev/null || echo missing)
    if [[ $wuid == 1000 ]]; then
        ok "workspace uid 1000"
    else
        warn "workspace uid is $wuid (expected 1000)"
    fi
    if [[ $wmode == 700 ]]; then
        ok "workspace mode 0700"
    else
        warn "workspace mode is $wmode (expected 0700)"
    fi
else
    warn "workspace missing at $workspace (run make workspace-fs APPLY=1 on the host)"
fi

# --- no other container publishing ports ---
if have_docker && docker info >/dev/null 2>&1; then
    conflict=0
    while IFS= read -r line; do
        name=${line%% *}
        ports=${line#* }
        [[ $ports == *:* ]] || continue
        case $name in
            ${deploy_project}-caddy-* | *roland-agent*caddy*) continue ;;
        esac
        # Allow our own caddy; flag anything else publishing host ports.
        if [[ $ports == *'0.0.0.0:'* || $ports == *':::'* || $ports == *'0.0.0.0'* ]]; then
            fail "other container publishes host ports: $line"
            conflict=1
        fi
    done < <(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null || true)
    if ((conflict == 0)); then
        ok "no conflicting published container ports"
    fi
fi

# --- edge policy (when Docker + secrets + .env allow) ---
if have_docker && [[ -f $env_file ]] && [[ -f $secrets_dir/model_server_token ]]; then
    if python3 "$deploy_repo/deploy/preflight_edge.py"; then
        ok "preflight_edge policy checks passed"
    else
        fail "preflight_edge policy checks failed"
    fi
else
    warn "skipping preflight_edge (docker/.env/secrets not ready)"
fi

printf '\npreflight summary: %s failure(s), %s warning(s)\n' "$errors" "$warns"
if ((errors > 0)); then
    exit 1
fi
exit 0
