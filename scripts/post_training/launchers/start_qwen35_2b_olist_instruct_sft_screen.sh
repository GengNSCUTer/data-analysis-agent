#!/usr/bin/env bash
set -Eeuo pipefail

# Formal Olist Release v2 SFT for Qwen3.5-2B Instruct + bf16 LoRA.
# It runs in a dedicated screen session and never stops or changes existing
# processes. A real longest-sequence smoke establishes the conservative gate.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TRAIN_PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qwen35/bin/python}"

# AGENTS.md mapping: CUDA_VISIBLE_DEVICES=3 -> physical nvidia-smi GPU 1.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-3}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
# The successful 2,874-token smoke reserved 14,694 MiB. This allocator
# setting does not change model/training semantics; it reduces fragmentation
# risk on the small remaining shared-GPU margin.
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
export PYTHONPATH="${ROOT}:${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-1}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-2fce35f7-4f80-b803-74bb-12b9cea0b9f0}"
MIN_FREE_MIB="${MIN_FREE_MIB:-15360}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen3.5-2b-instruct-15852e8c16360a2fea060d615a32b45270f8a8fc}"
SPLIT_DIR="${SPLIT_DIR:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/sft-release-surface-random-v2}"
LAYOUT_AUDIT="${LAYOUT_AUDIT:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/qwen35-2b-olist-sft-layout-audit-v1.json}"
RUN_DIR="${RUN_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen35-2b-olist-domain-sft-v1-20260910}"
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
  echo "[error] physical GPU ${PHYSICAL_NVIDIA_SMI_DEVICE} has ${free_mib} MiB free; 2B formal run requires at least ${MIN_FREE_MIB} MiB" >&2
  exit 3
fi
mkdir -p "$(dirname "${LOG_FILE}")"

exec > >(tee -a "${LOG_FILE}") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] qwen35_2b_olist_domain_sft_v1"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
echo "[free_mib] ${free_mib}"
echo "[pytorch_cuda_alloc_conf] ${PYTORCH_CUDA_ALLOC_CONF}"
echo "[run_dir] ${RUN_DIR}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

"${TRAIN_PYTHON}" "${ROOT}/scripts/post_training/training/run_qwen35_olist_instruct_sft.py" \
  --model-dir "${MODEL_DIR}" \
  --expected-model-id Qwen/Qwen3.5-2B \
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
  --experiment-label qwen35_2b_olist_domain_sft_v1

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
