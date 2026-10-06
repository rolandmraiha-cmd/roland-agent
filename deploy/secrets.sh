#!/usr/bin/env bash
# Create missing private secret files expected by Compose. Never print values.
# Idempotent: existing files are left untouched. Overwrite requires FORCE=1.
# Ownership uid 1000 / mode 0400 is applied only with APPLY=1 (typically sudo).
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

secrets_dir=${SECRETS_DIR:-$deploy_repo/secrets}
token_names=(
    model_server_token
    trainer_api_token
    sandbox_api_token
    browser_api_token
)
mkdir -p -- "$secrets_dir"
chmod 0700 -- "$secrets_dir" || true

write_secret() {
    local path=$1 value=$2
    if [[ -e $path || -L $path ]]; then
        if is_truthy "${FORCE:-0}"; then
            require_apply "overwrite of existing secret $(basename -- "$path")"
            # Write via temp + rename; never echo the value.
            local tmp
            tmp=$(mktemp --tmpdir="$secrets_dir" .secret.XXXXXX)
            printf '%s' "$value" >"$tmp"
            chmod 0400 -- "$tmp"
            mv -f -- "$tmp" "$path"
        else
            printf 'keep existing %s\n' "$(basename -- "$path")"
            return 0
        fi
    else
        umask 077
        local tmp
        tmp=$(mktemp --tmpdir="$secrets_dir" .secret.XXXXXX)
        printf '%s' "$value" >"$tmp"
        chmod 0400 -- "$tmp"
        if [[ -e $path || -L $path ]]; then
            rm -f -- "$tmp"
            printf 'keep existing %s\n' "$(basename -- "$path")"
            return 0
        fi
        mv -- "$tmp" "$path"
        printf 'created %s\n' "$(basename -- "$path")"
    fi
}

gen_token() {
    python3 -c 'import secrets; print(secrets.token_urlsafe(32), end="")'
}

gen_vnc() {
    python3 -c 'import secrets,string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(8)), end="")'
}

for name in "${token_names[@]}"; do
    write_secret "$secrets_dir/$name" "$(gen_token)"
done

# VNC passwords must differ.
vnc_a=$(gen_vnc)
vnc_b=$(gen_vnc)
while [[ $vnc_a == "$vnc_b" ]]; do
    vnc_b=$(gen_vnc)
done
write_secret "$secrets_dir/vnc_password" "$vnc_a"
write_secret "$secrets_dir/vnc_view_password" "$vnc_b"
unset vnc_a vnc_b

# agent_password_hash is created by make hash-password, not here.
if [[ ! -e $secrets_dir/agent_password_hash && ! -L $secrets_dir/agent_password_hash ]]; then
    printf 'note: secrets/agent_password_hash missing; run make hash-password\n'
fi

if is_truthy "${APPLY:-0}"; then
    require_apply "chown/chmod of secrets for container uid 1000"
    need_cmd sudo
    sudo chown 1000:1000 -- "$secrets_dir"/*
    sudo chmod 0400 -- "$secrets_dir"/*
    sudo chmod 0700 -- "$secrets_dir"
    printf 'ownership applied: uid 1000 mode 0400 (dir 0700)\n'
else
    printf 'secrets present. Set APPLY=1 to chown uid 1000 / chmod 0400 on the host.\n'
fi
