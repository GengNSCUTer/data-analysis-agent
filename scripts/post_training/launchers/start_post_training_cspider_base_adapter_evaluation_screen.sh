#!/usr/bin/env bash
set -Eeuo pipefail

# Run one complete CSpider validation comparison on the same GPU sequentially.
# This launcher is deliberately separate from Spider Test Suite infrastructure:
# CSpider final test is never referenced or read here.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
QLORA_PYTHON="${QLORA_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
EVAL_PYTHON="${EVAL_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent/bin/python}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
export CUDA_VISIBLE_DEVICES
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-3}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-10863af0-8588-7625-5609-640ba794f64b}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
ADAPTER_DIR="${ADAPTER_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-cspider-bf16-lora-length1536-full2epoch-v1-20260902/adapter_final}"
CSPIDER_ROOT="${CSPIDER_ROOT:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/cspider/cspider-1.0-official-2026-09-01/extracted}"
CASES="${CASES:-${CSPIDER_ROOT}/dev.json}"
TABLES_JSON="${TABLES_JSON:-${CSPIDER_ROOT}/tables.json}"
ACQUISITION_MANIFEST="${ACQUISITION_MANIFEST:-${CSPIDER_ROOT}/acquisition-manifest.json}"
DATABASE_ROOT="${DATABASE_ROOT:-${CSPIDER_ROOT}/database}"
PAIR_RUN_DIRECTORY="${PAIR_RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-cspider-bf16-lora-full2epoch-pair-v1-20260902}"
BASE_RUN_DIRECTORY="${BASE_RUN_DIRECTORY:-${PAIR_RUN_DIRECTORY}/base}"
ADAPTER_RUN_DIRECTORY="${ADAPTER_RUN_DIRECTORY:-${PAIR_RUN_DIRECTORY}/adapter}"
LOG_FILE="${PAIR_RUN_DIRECTORY}/screen-run.log"
EXPECTED_CASE_COUNT=1034
PROMPT_FORMAT_VERSION="spider-sft-schema-question-sql-v2"

for required_path in "${QLORA_PYTHON}" "${EVAL_PYTHON}" "${MODEL_DIR}" "${ADAPTER_DIR}/adapter_model.safetensors" "${CASES}" "${TABLES_JSON}" "${ACQUISITION_MANIFEST}" "${DATABASE_ROOT}"; do
  if [[ ! -e "${required_path}" ]]; then
    echo "[error] required path does not exist: ${required_path}" >&2
    exit 2
  fi
done
if [[ -e "${PAIR_RUN_DIRECTORY}/completed" || -e "${BASE_RUN_DIRECTORY}/generation_evidence.json" || -e "${ADAPTER_RUN_DIRECTORY}/generation_evidence.json" ]]; then
  echo "[error] output directory already has frozen generation artifacts; choose a new PAIR_RUN_DIRECTORY" >&2
  exit 2
fi

mkdir -p "${PAIR_RUN_DIRECTORY}"
exec > >(tee -a "${LOG_FILE}") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[contract] cspider_bf16_lora_2epoch_v1"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

generate_member() {
  local run_label="$1"
  local run_directory="$2"
  local args=(
    "${QLORA_PYTHON}" "${ROOT}/scripts/post_training/inference/generate_post_training_text_to_sql.py"
    --model-dir "${MODEL_DIR}"
    --run-label "${run_label}"
    --dataset cspider_validation
    --cases "${CASES}"
    --tables-json "${TABLES_JSON}"
    --cspider-acquisition-manifest "${ACQUISITION_MANIFEST}"
    --output-dir "${run_directory}"
    --max-input-tokens 1536
    --max-new-tokens 256
    --seed 42
    --base-weight-mode bf16_lora
    --prompt-format-version "${PROMPT_FORMAT_VERSION}"
    --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}"
    --expected-gpu-uuid "${EXPECTED_GPU_UUID}"
  )
  if [[ "${run_label}" == "adapter" ]]; then
    args+=(--adapter-dir "${ADAPTER_DIR}")
  fi
  "${args[@]}"
}

generate_member base "${BASE_RUN_DIRECTORY}"
generate_member adapter "${ADAPTER_RUN_DIRECTORY}"

"${EVAL_PYTHON}" "${ROOT}/scripts/post_training/evaluation/verify_cspider_matching_generation.py" \
  --base-evidence "${BASE_RUN_DIRECTORY}/generation_evidence.json" \
  --adapter-evidence "${ADAPTER_RUN_DIRECTORY}/generation_evidence.json" \
  --base-predictions "${BASE_RUN_DIRECTORY}/predictions.jsonl" \
  --adapter-predictions "${ADAPTER_RUN_DIRECTORY}/predictions.jsonl" \
  --expected-case-count "${EXPECTED_CASE_COUNT}" \
  --output "${PAIR_RUN_DIRECTORY}/matching-generation-verification.json"

for run_label in base adapter; do
  run_directory="${BASE_RUN_DIRECTORY}"
  if [[ "${run_label}" == "adapter" ]]; then run_directory="${ADAPTER_RUN_DIRECTORY}"; fi
  "${EVAL_PYTHON}" "${ROOT}/scripts/post_training/evaluation/run_sqlite_benchmark.py" \
    --dataset cspider_validation \
    --cases "${CASES}" \
    --database-root "${DATABASE_ROOT}" \
    --predictions "${run_directory}/predictions.jsonl" \
    --dataset-version cspider-1.0-official-2026-09-01-dev-validation \
    --model-id qwen2.5-coder-1.5b-cspider-comparison \
    --model-version bf16-lora-"${run_label}" \
    --prompt-version "${PROMPT_FORMAT_VERSION}" \
    --output "${run_directory}/sqlite-diagnostics.json"
done

"${EVAL_PYTHON}" "${ROOT}/scripts/post_training/evaluation/analyze_post_training_comparison.py" \
  --base-report "${BASE_RUN_DIRECTORY}/sqlite-diagnostics.json" \
  --adapter-report "${ADAPTER_RUN_DIRECTORY}/sqlite-diagnostics.json" \
  --max-new-tokens 256 \
  --output "${PAIR_RUN_DIRECTORY}/sqlite-paired-analysis.json"

# This is intentionally the CSpider dev label file and runs only after both
# candidates are frozen. No Spider Test Suite or CSpider final-test input is used.
"${EVAL_PYTHON}" "${ROOT}/scripts/post_training/evaluation/run_spider_bounded_denotation_audit.py" \
  --dataset-id cspider_validation \
  --base-report "${BASE_RUN_DIRECTORY}/sqlite-diagnostics.json" \
  --adapter-report "${ADAPTER_RUN_DIRECTORY}/sqlite-diagnostics.json" \
  --audit-cases "${CASES}" \
  --database-root "${DATABASE_ROOT}" \
  --output "${PAIR_RUN_DIRECTORY}/bounded-denotation-audit.json"

touch "${PAIR_RUN_DIRECTORY}/completed"
echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
