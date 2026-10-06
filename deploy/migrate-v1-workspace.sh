#!/usr/bin/env bash
# Copy v1 workspace files from the agent-data volume into WORKSPACE_HOST_DIR (M5.6 / §14.5).
# Idempotent; refuses to overwrite. Requires APPLY=1. Server is typically fresh — keep for upgrades.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_apply "migrate-v1-workspace"
need_cmd docker
workspace=${WORKSPACE_HOST_DIR:-/srv/roland-agent/workspace}
[[ -d $workspace ]] || die "Workspace missing: $workspace (run workspace-fs.sh first)"

vol=${deploy_project}_agent-data
if ! docker volume inspect "$vol" >/dev/null 2>&1; then
    die "Volume $vol not found"
fi

# Copy from /data/workspace inside the volume into the host bind mount, without overwrite.
docker run --rm \
    --user 1000:1000 \
    --network none \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,nosuid,nodev,size=16m \
    -v "$vol:/data:ro" \
    -v "$workspace:/workspace" \
    alpine:3.20 \
    sh -c 'if [ ! -d /data/workspace ]; then echo "no v1 workspace in volume"; exit 0; fi
           cd /data/workspace && find . -mindepth 1 -print0 | while IFS= read -r -d "" p; do
             target="/workspace/$p"
             if [ -e "$target" ]; then echo "skip existing $p"; continue; fi
             mkdir -p "$(dirname "$target")"
             cp -a "$p" "$target"
             echo "copied $p"
           done'

printf 'migrate-v1-workspace complete\n'
