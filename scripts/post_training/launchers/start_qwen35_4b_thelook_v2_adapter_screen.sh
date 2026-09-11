#!/usr/bin/env bash
set -Eeuo pipefail

# Generate only the 4B LoRA Adapter side of the frozen TheLook v2 comparison.
# The already frozen 4B Base output is deliberately reused.  Gold SQL,
# PostgreSQL, ResultContract and denotation are later stages, never generation
# inputs.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
EVAL_PYTHON="${EVAL_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qwen35/bin/python}"
DATA_ROOT="${DATA_ROOT:-/disk2/gengnan/data-analysis-agent-data}"

# AGENTS.md mapping: logical CUDA device 2 is physical nvidia-smi GPU 0.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-2}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}:${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-0}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-24fc79b9-0cb5-8a75-17de-188e04e0690b}"
MIN_FREE_MIB="${MIN_FREE_MIB:-12288}"
EVAL_DIR="${EVAL_DIR:-${DATA_ROOT}/evals/thelook-cross-schema-final-test-v2-20260910}"
MODEL_DIR="${MODEL_DIR:-${DATA_ROOT}/models/qwen3.5-4b-instruct-851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a}"
ADAPTER_DIR="${ADAPTER_DIR:-${DATA_ROOT}/experiments/qwen35-4b-olist-domain-sft-v1-20260911-3090/adapter_final}"
RUN_ROOT="${RUN_ROOT:-${DATA_ROOT}/experiments/qwen35-4b-thelook-v2-adapter-v1-20260911}"
OUT_DIR="${OUT_DIR:-${RUN_ROOT}/adapter-generation}"
LOG_FILE="${LOG_FILE:-${RUN_ROOT}/adapter-generation.log}"

for path in "${EVAL_PYTHON}" "${EVAL_DIR}/cases.jsonl" "${EVAL_DIR}/manifest.json" "${MODEL_DIR}" "${ADAPTER_DIR}/adapter_model.safetensors" "${ADAPTER_DIR}/adapter_config.json"; do
  [[ -e "${path}" ]] || { echo "[error] required path does not exist: ${path}" >&2; exit 2; }
done
[[ ! -e "${OUT_DIR}" ]] || { echo "[error] adapter output directory already exists: ${OUT_DIR}" >&2; exit 2; }

read -r observed_uuid used_mib total_mib < <(
  nvidia-smi --id="${PHYSICAL_NVIDIA_SMI_DEVICE}" \
    --query-gpu=uuid,memory.used,memory.total --format=csv,noheader,nounits \
    | awk -F ', ' '{print $1, $2, $3}'
)
[[ "${observed_uuid}" == "${EXPECTED_GPU_UUID}" ]] || {
  echo "[error] physical GPU UUID guard failed: expected ${EXPECTED_GPU_UUID}, got ${observed_uuid}" >&2
  exit 2
}
free_mib=$((total_mib - used_mib))
if (( free_mib < MIN_FREE_MIB )); then
  echo "[error] physical GPU ${PHYSICAL_NVIDIA_SMI_DEVICE} has ${free_mib} MiB free; requires ${MIN_FREE_MIB} MiB" >&2
  exit 3
fi
mkdir -p "$(dirname "${LOG_FILE}")"

exec > >(tee -a "${LOG_FILE}") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] qwen35_4b_thelook_v2_adapter_generation_v1"
echo "[generation_scope] adapter_only; no_gold_no_postgres"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
echo "[free_mib] ${free_mib}"
echo "[output_dir] ${OUT_DIR}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

"${EVAL_PYTHON}" "${ROOT}/scripts/post_training/evaluation/run_qwen35_4b_thelook_v2_adapter_generation.py" \
  --cases-jsonl "${EVAL_DIR}/cases.jsonl" \
  --manifest "${EVAL_DIR}/manifest.json" \
  --model-dir "${MODEL_DIR}" \
  --adapter-dir "${ADAPTER_DIR}" \
  --output-dir "${OUT_DIR}" \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}"

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
