#!/usr/bin/env bash
# Idempotent workspace loop filesystem (hard quota). Never reformats an existing image.
# Usage: workspace-fs.sh [SIZE_GB]
# Without APPLY=1: report planned actions and exit 0 if already correct, else exit 2.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

size_gb=${1:-${WORKSPACE_SIZE_GB:-10}}
if ! [[ $size_gb =~ ^[1-9][0-9]?$ ]]; then
    die "SIZE_GB must be an integer 1–99 (got: $size_gb)"
fi

base=${WORKSPACE_BASE:-/srv/roland-agent}
img=$base/workspace.img
mnt=${WORKSPACE_HOST_DIR:-$base/workspace}
label=ra-workspace
fstab_line="$img $mnt ext4 loop,nodev,nosuid,noatime 0 2"
subdirs=(browser/downloads browser/uploads screenshots uploads .trash .uploads-tmp .sandbox-home)

planned=()
note() { planned+=("$*"); printf '%s\n' "$*"; }

if [[ ! -d $base ]]; then
    note "mkdir -p $base"
fi
if [[ ! -f $img ]]; then
    note "fallocate -l ${size_gb}G $img && mkfs.ext4 -F -m 0 -L $label $img"
elif [[ -f $img ]]; then
    note "keep existing image $img (never reformat)"
fi
if [[ ! -d $mnt ]]; then
    note "mkdir -p $mnt"
fi
if ! findmnt -n "$mnt" >/dev/null 2>&1; then
    note "mount loop image at $mnt"
fi
if [[ -f /etc/fstab ]] && ! grep -Fqx "$fstab_line" /etc/fstab 2>/dev/null; then
    # Also accept an uncommented line whose $1 equals img (field equality; no regex).
    if ! awk -v img="$img" '$1 == img { found=1; exit } END { exit !found }' /etc/fstab; then
        note "append fstab entry for $img"
    fi
fi
note "chown 1000:1000 $mnt and ensure subdirs"

if ! is_truthy "${APPLY:-0}"; then
    if findmnt -n "$mnt" >/dev/null 2>&1 && [[ -f $img ]]; then
        printf 'workspace filesystem already present (dry-run). Set APPLY=1 to reconcile ownership/subdirs.\n'
        exit 0
    fi
    die "Workspace filesystem not fully applied. Re-run with APPLY=1 (requires sudo)."
fi

require_apply "workspace filesystem changes"
need_cmd sudo
need_cmd fallocate
need_cmd mkfs.ext4
need_cmd findmnt

sudo mkdir -p -- "$base" "$mnt"
if [[ ! -f $img ]]; then
    sudo fallocate -l "${size_gb}G" -- "$img"
    sudo mkfs.ext4 -F -m 0 -L "$label" -- "$img"
else
    printf 'existing image kept (no reformat): %s\n' "$img"
fi

if [[ -f /etc/fstab ]] && ! grep -Fqx "$fstab_line" /etc/fstab; then
    if ! awk -v img="$img" '$1 == img { found=1; exit } END { exit !found }' /etc/fstab; then
        printf '%s\n' "$fstab_line" | sudo tee -a /etc/fstab >/dev/null
    fi
fi

if ! findmnt -n "$mnt" >/dev/null 2>&1; then
    sudo mount -- "$mnt"
fi

sudo chown 1000:1000 -- "$mnt"
for rel in "${subdirs[@]}"; do
    sudo -u '#1000' mkdir -p -- "$mnt/$rel"
done
printf 'workspace filesystem ready at %s (%sG image)\n' "$mnt" "$size_gb"
