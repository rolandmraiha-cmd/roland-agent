#!/usr/bin/env bash
# CPU-only synthetic training and actual pinned-server integration. Never rents a GPU.
set -euo pipefail
[[ ${GITHUB_ACTIONS:-} == true ]] || { echo 'Restricted to disposable CI; see training/README.md for the separate GPU workflow.' >&2; exit 2; }
[[ -n ${LLAMA_CPP_DIR:-} ]] || { echo 'Set LLAMA_CPP_DIR to the reviewed CPU build.' >&2; exit 2; }
bash training/run_all.sh --dry-run --current-version ci-tiny-base --out .ci-training
python tests/integration/training_probe.py .ci-training/candidate.tar
