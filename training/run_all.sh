#!/usr/bin/env bash
set -euo pipefail
train_repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$train_repo"
export HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
export PYTHONPATH="$train_repo${PYTHONPATH:+:$PYTHONPATH}"
exec python -m training.run_all "$@"
