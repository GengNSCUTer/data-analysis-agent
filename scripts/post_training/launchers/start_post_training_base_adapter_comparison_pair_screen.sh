#!/usr/bin/env bash
set -Eeuo pipefail

# Run the paired Qwen 1.5B comparison sequentially on the same selected GPU.
# The base member must finish before adapter generation begins, avoiding GPU
# contention and preserving the one-variable comparison contract.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PAIR_RUN_DIRECTORY="${PAIR_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-base-adapter-pair-v1-20260825}"
BASE_RUN_DIRECTORY="${BASE_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-base-spider-dev-v1-20260825}"
ADAPTER_RUN_DIRECTORY="${ADAPTER_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-adapter-spider-dev-v1-20260825}"
BASE_REUSE_GENERATION="${BASE_REUSE_GENERATION:-0}"
LOG_FILE="${PAIR_RUN_DIRECTORY}/screen-run.log"

if [[ -e "${PAIR_RUN_DIRECTORY}/completed" ]]; then
  echo "[error] paired comparison has already completed; choose a new PAIR_RUN_DIRECTORY" >&2
  exit 2
fi
mkdir -p "${PAIR_RUN_DIRECTORY}"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[base_run_directory] ${BASE_RUN_DIRECTORY}"
echo "[adapter_run_directory] ${ADAPTER_RUN_DIRECTORY}"
echo "[base_reuse_generation] ${BASE_REUSE_GENERATION}"

RUN_LABEL=base RUN_DIRECTORY="${BASE_RUN_DIRECTORY}" REUSE_GENERATION="${BASE_REUSE_GENERATION}" \
  bash "${ROOT}/scripts/start_post_training_base_adapter_comparison_screen.sh"

RUN_LABEL=adapter RUN_DIRECTORY="${ADAPTER_RUN_DIRECTORY}" \
  bash "${ROOT}/scripts/start_post_training_base_adapter_comparison_screen.sh"

touch "${PAIR_RUN_DIRECTORY}/completed"
echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
