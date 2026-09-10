#!/usr/bin/env bash
set -Eeuo pipefail

# Formal Olist Release v2 SFT for Qwen3.5-4B Instruct + bf16 LoRA.
# The launcher fails before model allocation when the shared physical GPU has
# insufficient free memory. It never stops or changes any existing process.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TRAIN_PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qwen35/bin/python}"

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}:${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

# AGENTS.md mapping: CUDA_VISIBLE_DEVICES=0 -> nvidia-smi GPU 2 (RTX 4090).
PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-2}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52}"
# A short-sequence smoke peaked at 14,034 MiB reserved; a 2,498-token
# validation sample required 2,310 MiB more. Keep a conservative 17 GiB
# availability gate for the formal 3,072-token contract, rather than forcing
# an OOM on a GPU already shared by another owner.
MIN_FREE_MIB="${MIN_FREE_MIB:-17408}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen3.5-4b-instruct-851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a}"
SPLIT_DIR="${SPLIT_DIR:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/sft-release-surface-random-v2}"
LAYOUT_AUDIT="${LAYOUT_AUDIT:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/qwen35-olist-sft-layout-audit-v1.json}"
RUN_DIR="${RUN_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen35-4b-olist-domain-sft-v1-20260910}"
LOG_FILE="${LOG_FILE:-${RUN_DIR}.screen.log}"

for required_path in "${TRAIN_PYTHON}" "${MODEL_DIR}" "${SPLIT_DIR}/train.jsonl" "${SPLIT_DIR}/validation.jsonl" "${SPLIT_DIR}/split_audit.json" "${LAYOUT_AUDIT}"; do
  [[ -e "${required_path}" ]] || { echo "[error] required path does not exist: ${required_path}" >&2; exit 2; }
done
[[ ! -e "${RUN_DIR}" ]] || { echo "[error] run directory already exists; choose a new RUN_DIR" >&2; exit 2; }

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
  echo "[error] physical GPU ${PHYSICAL_NVIDIA_SMI_DEVICE} has ${free_mib} MiB free; formal Qwen3.5 run requires at least ${MIN_FREE_MIB} MiB" >&2
  exit 3
fi
mkdir -p "$(dirname "${LOG_FILE}")"

exec > >(tee -a "${LOG_FILE}") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] qwen35_4b_olist_domain_sft_v1"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
echo "[free_mib] ${free_mib}"
echo "[run_dir] ${RUN_DIR}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

"${TRAIN_PYTHON}" "${ROOT}/scripts/post_training/training/run_qwen35_olist_instruct_sft.py" \
  --model-dir "${MODEL_DIR}" \
  --train-jsonl "${SPLIT_DIR}/train.jsonl" \
  --validation-jsonl "${SPLIT_DIR}/validation.jsonl" \
  --split-audit "${SPLIT_DIR}/split_audit.json" \
  --layout-audit "${LAYOUT_AUDIT}" \
  --output-dir "${RUN_DIR}" \
  --max-seq-length 3072 \
  --num-train-epochs 2 \
  --seed 20260910 \
  --learning-rate 0.0001 \
  --weight-decay 0.01 \
  --per-device-train-batch-size 1 \
  --gradient-accumulation-steps 4 \
  --per-device-eval-batch-size 1 \
  --evaluation-steps 150 \
  --save-steps 300 \
  --logging-steps 10 \
  --lora-r 16 \
  --lora-alpha 32 \
  --lora-dropout 0.05 \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}" \
  --experiment-label qwen35_4b_olist_domain_sft_v1

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
