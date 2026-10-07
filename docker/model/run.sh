#!/usr/bin/env bash
# No downloads, model promotion, shell commands from the model or Docker socket.
set -euo pipefail

if [[ ${1:-} == healthcheck ]]; then
    exec curl --noproxy '*' -fsS --max-time 5 http://10.77.6.60:8080/health >/dev/null
fi
if [[ $# != 0 ]]; then
    echo "Unsupported model supervisor command." >&2
    exit 2
fi
MODEL_ROOT=${MODEL_ROOT:-/models}
MODEL_SERVER_BIN=${MODEL_SERVER_BIN:-/app/llama-server}
MODEL_SERVER_TOKEN_FILE=${MODEL_SERVER_TOKEN_FILE:-/run/secrets/model_server_token}
MODEL_CTX=${MODEL_CTX:-5120}
MODEL_THREADS=${MODEL_THREADS:-3}
if [[ ! $MODEL_CTX =~ ^[1-9][0-9]{0,3}$ || $MODEL_CTX -gt 6144 || ! $MODEL_THREADS =~ ^[1-3]$ ]]; then
    echo "Require MODEL_CTX between 1 and 6144 and MODEL_THREADS between 1 and 3." >&2
    exit 2
fi
if [[ ! -f $MODEL_SERVER_TOKEN_FILE || ! -s $MODEL_SERVER_TOKEN_FILE || -L $MODEL_SERVER_TOKEN_FILE ]]; then
    echo "Required model token file is missing, empty or a symlink." >&2
    exit 2
fi
if [[ $(stat -c %s -- "$MODEL_SERVER_TOKEN_FILE") -gt 256 ]]; then
    echo "Invalid model token file format." >&2
    exit 2
fi
model_token=$(< "$MODEL_SERVER_TOKEN_FILE")
model_token=${model_token%$'\r'}
if [[ ! $model_token =~ ^[A-Za-z0-9_-]{32,128}$ ]]; then
    echo "Model token must be one 32–128 character URL-safe token." >&2
    exit 2
fi
unset model_token
MODEL_ROOT=$(readlink -e -- "$MODEL_ROOT")
model_child=
model_sleep=
stop_child() {
    if [[ -n $model_child ]]; then
        kill -TERM "$model_child" 2>/dev/null || true
        model_deadline=$((SECONDS + 25))
        while kill -0 "$model_child" 2>/dev/null && (( SECONDS < model_deadline )); do
            sleep 0.1
        done
        kill -KILL "$model_child" 2>/dev/null || true
        wait "$model_child" 2>/dev/null || true
        model_child=
    fi
}
cleanup() {
    [[ -z $model_sleep ]] || kill "$model_sleep" 2>/dev/null || true
    stop_child
}
trap cleanup EXIT
trap 'exit 0' TERM INT

verify_current() {
    if [[ ! -L $MODEL_ROOT/current ]]; then
        echo "No installed current model symlink." >&2
        return 1
    fi
    model_target=$(readlink -e -- "$MODEL_ROOT/current") || return 1
    model_id=${model_target##*/}
    if [[ ! $model_id =~ ^[a-z0-9][a-z0-9._-]{0,119}$ || $model_target != "$MODEL_ROOT/versions/$model_id" ]]; then
        echo "Current model must be a direct version directory under models/versions." >&2
        return 1
    fi
    for model_file in model.gguf model.sha256; do
        if [[ ! -f $model_target/$model_file || -L $model_target/$model_file ]]; then
            echo "Missing or symlinked model integrity file." >&2
            return 1
        fi
    done
    model_expected=$(cat -- "$model_target/model.sha256")
    if [[ ! $model_expected =~ ^[a-f0-9]{64}$ ]]; then
        echo "Invalid model SHA-256 record." >&2
        return 1
    fi
    model_actual=$(sha256sum -- "$model_target/model.gguf")
    if [[ ${model_actual%% *} != "$model_expected" ]]; then
        echo "Model SHA-256 mismatch; refusing to serve it." >&2
        return 1
    fi
}
start_child() {
    verify_current
    model_active=$model_target
    "$MODEL_SERVER_BIN" --model "$model_active/model.gguf" \
        --host 10.77.6.60 --port 8080 --api-key-file "$MODEL_SERVER_TOKEN_FILE" \
        --ctx-size "$MODEL_CTX" --parallel 1 --threads "$MODEL_THREADS" --threads-batch "$MODEL_THREADS" \
        --batch-size 512 --ubatch-size 256 --flash-attn off --load-mode none \
        --cache-type-k f16 --cache-type-v f16 --jinja --no-webui --no-agent --no-ui-mcp-proxy \
        --no-slots --cache-reuse 256 &
    model_child=$!
}
start_child
model_check=$((SECONDS + 15))
while true; do
    if ! kill -0 "$model_child" 2>/dev/null; then
        echo "Model server exited; container restart policy will retry." >&2
        exit 1
    fi
    if (( SECONDS >= model_check )); then
        model_next=$(readlink -e -- "$MODEL_ROOT/current") || exit 1
        if [[ $model_next != "$model_active" ]]; then
            stop_child
            start_child
        fi
        model_check=$((SECONDS + 15))
    fi
    sleep 1 &
    model_sleep=$!
    wait "$model_sleep"
    model_sleep=
done
