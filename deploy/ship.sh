#!/usr/bin/env bash
# From a laptop: fetch/checkout REF on HOST and run make deploy (§14.3).
# Usage: ship.sh   (env: HOST=deploy@host REF=v2)
# Requires APPLY=1. Does not manage DNS or ACME.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_apply "ship (remote deploy over SSH)"
host=${HOST:?HOST is required, e.g. HOST=deploy@37.60.226.214}
ref=${REF:-v2}
remote_dir=${REMOTE_DIR:-/opt/roland-agent}

need_cmd ssh
printf 'shipping %s to %s:%s\n' "$ref" "$host" "$remote_dir"
ssh -o BatchMode=yes "$host" "set -euo pipefail; cd -- $(printf '%q' "$remote_dir") && git fetch && git checkout $(printf '%q' "$ref") && git pull --ff-only && APPLY=1 make deploy"
printf 'ship complete\n'
