#!/usr/bin/env bash
# Host commands use one-off containers; the long-running trainer profile is optional.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
cd -- "$deploy_repo"
train_command=${1:?Use list, import, promote, rollback or eval}
case $train_command in
    import|promote|rollback|eval) require_apply "model $train_command" ;;
    list) ;;
    *) die "Unsupported model operation" ;;
esac
if [[ $train_command == eval ]]; then
    exec bash "$deploy_repo/deploy/model-eval.sh"
fi
if [[ $train_command == list ]]; then
    compose exec -T core python -m agent.models.cli list
    exit
fi
train_file_mount=()
if [[ $train_command == import ]]; then
    [[ -n ${FILE:-} && $FILE != *','* && $FILE != *$'\n'* ]] || die "Set FILE to a candidate.tar"
    train_path=$(readlink -e -- "$FILE")
    [[ -f $train_path && $train_path != *','* && $train_path != *$'\n'* ]] || die "Invalid candidate path"
    train_file_mount=(-v "$train_path:/input/candidate.tar:ro")
fi
train_tty=()
[[ $train_command == promote || $train_command == rollback ]] && train_tty=(-i)
docker build -t roland-agent/trainer:local -f docker/trainer/Dockerfile .
train_project=${MODEL_PROJECT:-roland-agent}
[[ $train_project =~ ^[a-z0-9][a-z0-9_-]{0,100}$ ]] || die "Invalid MODEL_PROJECT"
docker volume inspect "${train_project}_models" "${train_project}_agent-data" >/dev/null
docker run --rm "${train_tty[@]}" --read-only --cap-drop ALL --security-opt no-new-privileges \
    --user 1000:1000 --memory 256m --memory-swap 256m --cpus 1 --pids-limit 64 \
    --network "${train_project}_model" --tmpfs /tmp:rw,nosuid,nodev,size=16m \
    --mount "type=volume,source=${train_project}_models,target=/models" \
    --mount "type=volume,source=${train_project}_agent-data,target=/data" \
    --mount "type=bind,source=$deploy_repo/secrets/model_server_token,target=/run/secrets/model_server_token,readonly" \
    "${train_file_mount[@]}" -e "APPLY=${APPLY:-0}" -e "ID=${ID:-}" -e "FORCE=${FORCE:-0}" \
    -e FILE=/input/candidate.tar -e MODEL_SERVER_TOKEN_FILE=/run/secrets/model_server_token \
    roland-agent/trainer:local python -m agent.models.cli "$train_command"
