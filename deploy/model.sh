#!/usr/bin/env bash
# Host-initiated one-off installer. Runtime model containers have no egress.
set -euo pipefail
model_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
model_project=${MODEL_PROJECT:-roland-agent}
if [[ ! $model_project =~ ^[a-z0-9][a-z0-9_-]{0,100}$ ]]; then
    echo "Invalid MODEL_PROJECT." >&2
    exit 2
fi
model_args=()
model_mount=()
case ${1:-} in
    fetch)
        model_args=(fetch --model "${MODEL:-qwen3-4b-q4km}")
        ;;
    install)
        if [[ -z ${FILE:-} || -z ${ID:-} || $FILE == *','* || $FILE == *$'\n'* ]]; then
            echo "model-install requires FILE and ID (a models.lock catalogue id)." >&2
            exit 2
        fi
        model_source=$(readlink -e -- "$FILE")
        if [[ $model_source == *','* || $model_source == *$'\n'* ]]; then
            echo "Source paths must not contain mount-option separators." >&2
            exit 2
        fi
        [[ -f $model_source ]] || exit 2
        model_mount=(--mount "type=bind,source=$model_source,target=/input/model.gguf,readonly")
        model_args=(install --model "$ID" --file /input/model.gguf)
        ;;
    *) echo "Use fetch or install." >&2; exit 2 ;;
esac
docker build -t roland-agent/model-installer:local -f "$model_repo/docker/model-installer/Dockerfile" "$model_repo"
model_suffix=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
if ! docker volume inspect "${model_project}_models" >/dev/null 2>&1; then
    docker volume create --label "com.docker.compose.project=$model_project" \
        --label com.docker.compose.volume=models "${model_project}_models" >/dev/null
fi
model_run_name="$model_project-model-$1-$model_suffix"
if [[ $1 == fetch ]]; then
    model_network="$model_project-model-fetch-$model_suffix"
    docker network create --driver bridge "$model_network" >/dev/null
    model_cleanup() { docker network rm "$model_network" >/dev/null || true; }
    trap model_cleanup EXIT
    model_network_args=(--network "$model_network")
else
    # Local FILE installs need no egress.
    model_network_args=(--network none)
fi
docker run --rm --name "$model_run_name" "${model_network_args[@]}" \
    --user 1000:1000 --read-only --cap-drop ALL --security-opt no-new-privileges \
    --memory 128m --memory-swap 128m --cpus 1 --pids-limit 64 \
    --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --env "MODEL_INSTALL_TEST_ONLY=${MODEL_INSTALL_TEST_ONLY:-false}" \
    --mount "type=volume,source=${model_project}_models,target=/models" \
    "${model_mount[@]}" roland-agent/model-installer:local "${model_args[@]}"
