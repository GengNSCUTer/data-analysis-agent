#!/usr/bin/env bash
set -Eeuo pipefail

# Produce a full bf16 Base/Adapter pair sequentially on one guarded GPU. The
# QLoRA counterpart uses another screen and GPU, so both evaluations can run
# in parallel without mixing precision contracts or contending on one card.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PAIR_RUN_DIRECTORY="${PAIR_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-coverage26-pair-v1-20260825}"
BASE_RUN_DIRECTORY="${BASE_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-coverage26-base-spider-dev-v1-20260825}"
ADAPTER_RUN_DIRECTORY="${ADAPTER_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-coverage26-adapter-spider-dev-v1-20260825}"
ADAPTER_DIR="${ADAPTER_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-coverage26-v1-20260825/adapter_final}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-3}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-10863af0-8588-7625-5609-640ba794f64b}"
LOG_FILE="${PAIR_RUN_DIRECTORY}/screen-run.log"

if [[ ! -f "${ADAPTER_DIR}/adapter_model.safetensors" ]]; then
  echo "[error] adapter is missing: ${ADAPTER_DIR}/adapter_model.safetensors" >&2
  exit 2
fi
if [[ -e "${PAIR_RUN_DIRECTORY}/completed" || -e "${BASE_RUN_DIRECTORY}/official-test-suite/official-evaluator-evidence.json" || -e "${ADAPTER_RUN_DIRECTORY}/official-test-suite/official-evaluator-evidence.json" ]]; then
  echo "[error] final evaluation evidence already exists; choose a new run directory" >&2
  exit 2
fi

mkdir -p "${PAIR_RUN_DIRECTORY}"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[comparison] bf16 base versus bf16 lora adapter"
echo "[base_run_directory] ${BASE_RUN_DIRECTORY}"
echo "[adapter_run_directory] ${ADAPTER_RUN_DIRECTORY}"

RUN_LABEL=base \
RUN_DIRECTORY="${BASE_RUN_DIRECTORY}" \
BASE_WEIGHT_MODE=bf16_lora \
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES}" \
PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE}" \
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID}" \
  bash "${ROOT}/scripts/start_post_training_base_adapter_comparison_screen.sh"

RUN_LABEL=adapter \
RUN_DIRECTORY="${ADAPTER_RUN_DIRECTORY}" \
ADAPTER_DIR="${ADAPTER_DIR}" \
BASE_WEIGHT_MODE=bf16_lora \
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
