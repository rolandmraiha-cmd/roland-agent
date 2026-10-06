#!/usr/bin/env bash
# Shared helpers for deploy/*.sh. Source only; do not execute.
# shellcheck shell=bash
set -euo pipefail

deploy_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export deploy_comment=roland-agent
export deploy_project=${COMPOSE_PROJECT_NAME:-roland-agent}

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

require_apply() {
    local action=$1
    if [[ ${APPLY:-0} != 1 ]]; then
        die "Refusing $action without APPLY=1 (destructive or host-mutating step)."
    fi
}

repo_root() {
    printf '%s\n' "$deploy_repo"
}

have_docker() {
    command -v docker >/dev/null 2>&1
}

compose() {
    docker compose --project-directory "$deploy_repo" -f "$deploy_repo/docker-compose.yml" "$@"
}

is_truthy() {
    case ${1:-} in
        1 | true | TRUE | yes | YES | on | ON) return 0 ;;
        *) return 1 ;;
    esac
}
