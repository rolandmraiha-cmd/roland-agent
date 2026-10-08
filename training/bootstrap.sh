#!/usr/bin/env bash
# Run only on Roland's separate GPU machine (or the CPU dry-run CI).
set -euo pipefail
train_commit=$(sed -n 's/^commit=//p' docker/model/VERSION)
[[ $train_commit =~ ^[a-f0-9]{40}$ ]] || exit 2
python -m venv .training-venv
source .training-venv/bin/activate
pip install --require-hashes --extra-index-url https://download.pytorch.org/whl/cu128 -r training/requirements-train.lock
git init .llama.cpp
git -C .llama.cpp remote add origin https://github.com/ggml-org/llama.cpp.git
git -C .llama.cpp fetch --depth 1 origin "$train_commit"
git -C .llama.cpp checkout --detach FETCH_HEAD
cmake -S .llama.cpp -B .llama.cpp/build -DGGML_CUDA=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF
cmake --build .llama.cpp/build --target llama-server llama-quantize -j 2
echo "Activate .training-venv and set LLAMA_CPP_DIR before running training/run_all.sh."
