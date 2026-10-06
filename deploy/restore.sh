#!/usr/bin/env bash
# Restore helpers.
#   restore.sh FILE=...           → production restore via core (refuses if serving lock held)
#   restore.sh --test FILE=...    → restore-test into a temporary container/volume, then delete
# Destructive; requires APPLY=1. Never prints secret values.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

mode=restore
while [[ $# -gt 0 ]]; do
    case $1 in
        --test) mode='test' ;;
        --help|-h)
            printf 'Usage: FILE=/path/to.bak.db.gz restore.sh [--test]\n'
            exit 0
            ;;
        *) die "Unknown argument: $1" ;;
    esac
    shift
done

file=${FILE:?FILE= path to a .db.gz backup is required}
[[ -f $file ]] || die "Backup file not found: $file"
require_apply "$mode restore"

need_cmd docker
cd -- "$deploy_repo"

if [[ $mode == restore ]]; then
    printf 'Restoring into running project data (core must be stopped / lock free)\n'
    compose stop core || true
    compose run --rm --no-deps \
        -v "$(readlink -f -- "$file"):/input/backup.db.gz:ro" \
        core python -m agent restore /input/backup.db.gz
    printf 'restore complete; start with make up or make deploy\n'
    exit 0
fi

# restore-test: disposable project
suffix=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:10])')
project=${deploy_project}-restore-test-$suffix
work=$(mktemp -d)
cleanup() {
    docker compose -p "$project" -f "$deploy_repo/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf -- "$work"
}
trap cleanup EXIT

mkdir -p -- "$work/workspace" "$work/secrets"
chmod 0700 -- "$work/workspace" "$work/secrets"
# Minimal secrets so compose can start core for checks; values are disposable.
python3 - <<PY
import secrets, pathlib
root = pathlib.Path("$work/secrets")
(root / "model_server_token").write_text(secrets.token_urlsafe(32))
(root / "agent_password_hash").write_text("not-a-real-hash-for-restore-test")
for p in root.iterdir():
    p.chmod(0o400)
PY
cp -n -- "$deploy_repo/.env.example" "$work/.env"
chmod 0600 -- "$work/.env"
# Point workspace and use internal TLS / localhost.
python3 - "$work/.env" "$work/workspace" <<'PY'
import pathlib, sys
env, ws = pathlib.Path(sys.argv[1]), sys.argv[2]
lines = []
overrides = {
    "AGENT_DOMAIN": "localhost",
    "CADDY_TLS": "internal",
    "WORKSPACE_HOST_DIR": ws,
}
keys = set(overrides)
for line in env.read_text().splitlines():
    k = line.partition("=")[0]
    if k in keys:
        continue
    lines.append(line)
lines.extend(f"{k}={v}" for k, v in overrides.items())
env.write_text("\n".join(lines) + "\n")
PY

# Seed agent-data by running restore in a one-off core with a fresh volume.
printf 'restore-test project=%s\n' "$project"
# Create volumes via a short compose config resolve + volume create labels.
docker volume create --label "com.docker.compose.project=$project" --label com.docker.compose.volume=agent-data "${project}_agent-data" >/dev/null
docker volume create --label "com.docker.compose.project=$project" --label com.docker.compose.volume=backups "${project}_backups" >/dev/null

docker run --rm \
    --user 1000:1000 \
    --network none \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    -v "${project}_agent-data:/data" \
    -v "${project}_backups:/backups" \
    -v "$(readlink -f -- "$file"):/input/backup.db.gz:ro" \
    -v "$deploy_repo/secrets/model_server_token:/run/secrets/model_server_token:ro" \
    -e DATA_DIR=/data -e BACKUP_DIR=/backups -e WORKSPACE_DIR=/tmp \
    -e AGENT_PASSWORD_HASH_FILE=/run/secrets/agent_password_hash \
    -e MODEL_SERVER_TOKEN_FILE=/run/secrets/model_server_token \
    -v "$work/secrets/agent_password_hash:/run/secrets/agent_password_hash:ro" \
    roland-agent/core:local \
    python -m agent restore /input/backup.db.gz

docker run --rm \
    --user 1000:1000 \
    --network none \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m \
    -v "${project}_agent-data:/data" \
    -v "${project}_backups:/backups" \
    -v "$work/secrets/agent_password_hash:/run/secrets/agent_password_hash:ro" \
    -v "$work/secrets/model_server_token:/run/secrets/model_server_token:ro" \
    -e DATA_DIR=/data -e BACKUP_DIR=/backups -e WORKSPACE_DIR=/tmp \
    -e AGENT_PASSWORD_HASH_FILE=/run/secrets/agent_password_hash \
    -e MODEL_SERVER_TOKEN_FILE=/run/secrets/model_server_token \
    roland-agent/core:local \
    bash -c 'python -c "import sqlite3; c=sqlite3.connect(\"/data/agent.db\"); print(c.execute(\"PRAGMA integrity_check\").fetchone()[0])" \
      && python -m agent migrate --check \
      && python -m agent audit-verify'

printf 'restore-test PASS (temporary volume will be deleted)\n'
