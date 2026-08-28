#!/usr/bin/env bash
set -Eeuo pipefail

# Evaluate the 26-step QLoRA adapter against the already frozen matching
# 4-bit Base. Reusing that Base does not change the comparison variable.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PAIR_RUN_DIRECTORY="${PAIR_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-qlora-coverage26-pair-v1-20260825}"
BASE_RUN_DIRECTORY="${BASE_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-base-spider-dev-v1-20260825}"
ADAPTER_RUN_DIRECTORY="${ADAPTER_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-qlora-coverage26-adapter-spider-dev-v1-20260825}"
ADAPTER_DIR="${ADAPTER_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-qlora-coverage26-v1-20260825/adapter_final}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-2}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52}"
LOG_FILE="${PAIR_RUN_DIRECTORY}/screen-run.log"

for required_path in "${BASE_RUN_DIRECTORY}/sqlite-diagnostics.json" "${BASE_RUN_DIRECTORY}/official-test-suite/official-evaluator-evidence.json" "${ADAPTER_DIR}/adapter_model.safetensors"; do
  if [[ ! -f "${required_path}" ]]; then
    echo "[error] required comparison artifact does not exist: ${required_path}" >&2
    exit 2
  fi
done
if [[ -e "${PAIR_RUN_DIRECTORY}/completed" || -e "${ADAPTER_RUN_DIRECTORY}/official-test-suite/official-evaluator-evidence.json" ]]; then
  echo "[error] final evaluation evidence already exists; choose a new run directory" >&2
  exit 2
fi

mkdir -p "${PAIR_RUN_DIRECTORY}"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[comparison] qlora_coverage26 adapter versus frozen qlora base"
echo "[base_run_directory] ${BASE_RUN_DIRECTORY}"
echo "[adapter_run_directory] ${ADAPTER_RUN_DIRECTORY}"

RUN_LABEL=adapter \
RUN_DIRECTORY="${ADAPTER_RUN_DIRECTORY}" \
ADAPTER_DIR="${ADAPTER_DIR}" \
BASE_WEIGHT_MODE=qlora_4bit \
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES}" \
PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE}" \
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID}" \
  bash "${ROOT}/scripts/start_post_training_base_adapter_comparison_screen.sh"

PYTHONPATH="${ROOT}/src:${ROOT}" \
  /disk2/gengnan/conda_envs/data-analysis-agent/bin/python "${ROOT}/scripts/analyze_post_training_comparison.py" \
    --base-report "${BASE_RUN_DIRECTORY}/sqlite-diagnostics.json" \
    --adapter-report "${ADAPTER_RUN_DIRECTORY}/sqlite-diagnostics.json" \
    --output "${PAIR_RUN_DIRECTORY}/safe-comparison.json"

touch "${PAIR_RUN_DIRECTORY}/completed"
echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
