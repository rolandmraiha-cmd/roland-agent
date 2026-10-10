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

# Restore-test uses the built core image and two disposable volumes only. No Compose
# services, production secrets, networks, browser profile or live database are mounted.
suffix=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:10])')
project=${deploy_project}-restore-test-$suffix
work=$(mktemp -d)
volumes=()
cleanup() {
    local failed=0 volume
    for volume in "${volumes[@]}"; do
        docker volume rm "$volume" >/dev/null 2>&1 || failed=1
    done
    rm -rf -- "$work"
    if ((failed)); then
        printf 'restore-test cleanup failed; inspect disposable volumes for %s\n' "$project" >&2
        return 1
    fi
}
trap cleanup EXIT

# The protected directory keeps this temporary copy private on the host. Binding just
# the file lets uid 1000 read it even when the original backup is owned by root.
cp -- "$file" "$work/backup.db.gz"
chmod 0444 -- "$work/backup.db.gz"
for name in agent-data backups; do
    volume=${project}_$name
    docker volume create --label "com.docker.compose.project=$project" \
        --label "com.docker.compose.volume=$name" "$volume" >/dev/null
    volumes+=("$volume")
done

run_copy() {
    docker run --rm \
        --user 1000:1000 --network none \
        --read-only --cap-drop ALL --security-opt no-new-privileges \
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
        --memory 640m --memory-swap 640m --cpus 1 --pids-limit 128 \
        --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 \
        -v "${project}_agent-data:/data" \
        -v "${project}_backups:/backups" \
        -v "$work/backup.db.gz:/input/backup.db.gz:ro" \
        -e DATA_DIR=/data -e BACKUP_DIR=/backups -e WORKSPACE_DIR=/tmp \
        roland-agent/core:local "$@"
}
printf 'restore-test project=%s\n' "$project"
run_copy python -m agent restore /input/backup.db.gz
run_copy python -c '
import sqlite3
from pathlib import Path
from agent.audit import verify_file
from agent.migrations import inspect_version, latest_version
with sqlite3.connect("file:/data/agent.db?mode=ro", uri=True) as db:
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise SystemExit("restore-test integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise SystemExit("restore-test foreign key check failed")
    if db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]:
        raise SystemExit("restore-test retained login sessions")
if inspect_version(Path("/data/agent.db")) != latest_version():
    raise SystemExit("restore-test schema is not current")
if not verify_file(Path("/data/agent.db"))["ok"]:
    raise SystemExit("restore-test audit check failed")
print("integrity, foreign keys, schema, audit and expired sessions: PASS")
'
cleanup
trap - EXIT
printf 'restore-test PASS (disposable volumes removed; live data untouched)\n'
