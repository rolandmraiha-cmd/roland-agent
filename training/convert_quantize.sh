#!/usr/bin/env bash
set -euo pipefail
train_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
train_source=${LLAMA_CPP_DIR:?Set LLAMA_CPP_DIR to the built, pinned llama.cpp checkout}
train_commit=$(sed -n 's/^commit=//p' "$train_repo/docker/model/VERSION")
[[ $(git -C "$train_source" rev-parse HEAD) == "$train_commit" ]] || { echo "llama.cpp pin differs" >&2; exit 2; }
[[ $# == 2 && -d $1 ]] || { echo "Use convert_quantize.sh MERGED_DIR OUTPUT_DIR" >&2; exit 2; }
mkdir -p -- "$2"
PYTHONPATH="$train_source/gguf-py${PYTHONPATH:+:$PYTHONPATH}" python "$train_source/convert_hf_to_gguf.py" "$1" --outfile "$2/model-f16.gguf" --outtype f16
"$train_source/build/bin/llama-quantize" "$2/model-f16.gguf" "$2/model.gguf" Q4_K_M
sha256sum -- "$2/model.gguf" | cut -d' ' -f1 > "$2/model.sha256"
rm -- "$2/model-f16.gguf"
