#!/usr/bin/env bash
# Evaluate installed versions sequentially; restore production inference on every exit.
set -euo pipefail
# shellcheck source=deploy/common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"
require_apply 'pause production model inference for evaluation'
cd -- "$deploy_repo"
[[ ${ID:-} =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || die 'Set ID to an installed model version'
eval_project=${MODEL_PROJECT:-roland-agent}
[[ $eval_project =~ ^[a-z0-9][a-z0-9_-]{0,100}$ ]] || die 'Invalid MODEL_PROJECT'
eval_current=$(compose exec -T core python -c 'import json; print(json.load(open("/models/registry.json"))["current"])')
[[ $eval_current =~ ^[a-z0-9][a-z0-9._-]{0,63}$ ]] || die 'Invalid current version'
eval_image=$(sed -n 's/^image=//p' docker/model/VERSION)
[[ $eval_image =~ ^ghcr.io/ggml-org/llama.cpp:server-b[0-9]+@sha256:[a-f0-9]{64}$ ]] || die 'Invalid pinned image'
docker volume inspect "${eval_project}_models" "${eval_project}_training-data" >/dev/null
docker build -t roland-agent/trainer:local -f docker/trainer/Dockerfile .
eval_name="${eval_project}-offline-eval"
if docker container inspect "$eval_name" >/dev/null 2>&1; then die 'An evaluation container already exists'; fi
eval_restore=0
eval_cleanup() {
    docker rm -f "$eval_name" >/dev/null 2>&1 || true
    if [[ $eval_restore == 1 ]]; then compose start model || true; fi
}
trap eval_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
echo 'Pausing production inference; current and candidate will be measured sequentially.'
eval_restore=1
compose stop model
for eval_version in "$eval_current" "$ID"; do
    docker run -d --name "$eval_name" --read-only --cap-drop ALL --security-opt no-new-privileges \
        --user 1000:1000 --memory 3840m --memory-swap 3840m --cpus 3 --pids-limit 128 \
        --network "${eval_project}_model" --ip 10.77.6.60 --tmpfs /tmp:size=16m \
        --mount "type=volume,source=${eval_project}_models,target=/models,readonly" \
        --mount "type=bind,source=$deploy_repo/secrets/model_server_token,target=/run/secrets/model_server_token,readonly" \
        --entrypoint /app/llama-server "$eval_image" --model "/models/versions/$eval_version/model.gguf" \
        --host 10.77.6.60 --port 8080 --api-key-file /run/secrets/model_server_token \
        --ctx-size 4096 --parallel 1 --threads 3 --cache-ram 0 --jinja --no-webui --no-slots >/dev/null
    eval_ready=0
    for _ in {1..150}; do
        if docker exec "$eval_name" curl --noproxy '*' -fsS --max-time 2 http://10.77.6.60:8080/health >/dev/null 2>&1; then eval_ready=1; break; fi
        sleep 2
    done
    [[ $eval_ready == 1 ]] || die 'Evaluation model did not become healthy'
    docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 \
        --memory 256m --memory-swap 256m --cpus 1 --pids-limit 64 --network "${eval_project}_model" \
        --mount "type=volume,source=${eval_project}_training-data,target=/training-data" \
        --mount "type=bind,source=$deploy_repo/secrets/model_server_token,target=/run/secrets/model_server_token,readonly" \
        -e MODEL_SERVER_TOKEN_FILE=/run/secrets/model_server_token roland-agent/trainer:local \
        python -m agent.eval run --server http://10.77.6.60:8080 --out "/training-data/eval-$eval_version.json" --version "$eval_version"
    docker rm -f "$eval_name" >/dev/null
done
echo 'Reports saved in training-data; production model is being restored.'
