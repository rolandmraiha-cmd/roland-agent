#!/usr/bin/env bash
# Prompt twice and write secrets/agent_password_hash. Never print the hash or password.
# Overwrite requires FORCE=1. Ownership apply requires APPLY=1.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

secrets_dir=${SECRETS_DIR:-$deploy_repo/secrets}
target=$secrets_dir/agent_password_hash
min_len=${MIN_PASSWORD_LENGTH:-16}

mkdir -p -- "$secrets_dir"
chmod 0700 -- "$secrets_dir" || true

if [[ -e $target || -L $target ]] && ! is_truthy "${FORCE:-0}"; then
    die "Refusing to overwrite existing agent_password_hash without FORCE=1"
fi
if [[ -e $target || -L $target ]]; then
    require_apply "overwrite of existing agent_password_hash"
fi

read -r -s -p "New password for the chat page: " pw
printf '\n'
if ((${#pw} < min_len)); then
    unset pw
    die "Use at least ${min_len} characters."
fi
read -r -s -p "Same again: " pw2
printf '\n'
if [[ $pw != "$pw2" ]]; then
    unset pw pw2
    die "The passwords didn't match."
fi
unset pw2

hash=$(
    cd -- "$deploy_repo" && PASSWORD="$pw" python3 - <<'PY'
import os
from agent.web.auth import hash_password
print(hash_password(os.environ["PASSWORD"]), end="")
PY
)
unset pw

tmp=$(mktemp --tmpdir="$secrets_dir" .hash.XXXXXX)
printf '%s' "$hash" >"$tmp"
unset hash
chmod 0400 -- "$tmp"
mv -f -- "$tmp" "$target"
printf 'wrote secrets/agent_password_hash (mode 0400); value not printed\n'

if is_truthy "${APPLY:-0}"; then
    require_apply "chown of agent_password_hash"
    need_cmd sudo
    sudo chown 1000:1000 -- "$target"
    sudo chmod 0400 -- "$target"
    printf 'ownership applied: uid 1000\n'
fi
