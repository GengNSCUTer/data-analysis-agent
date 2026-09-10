#!/usr/bin/env bash
set -Eeuo pipefail

# One optimizer-update lifecycle smoke for Qwen3.5-2B Instruct + bf16 LoRA.
# It runs inside its own screen session and never changes other GPU processes.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TRAIN_PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qwen35/bin/python}"

# AGENTS.md mapping: CUDA_VISIBLE_DEVICES=3 exposes physical GPU 1 as cuda:0.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-3}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}:${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-1}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-2fce35f7-4f80-b803-74bb-12b9cea0b9f0}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen3.5-2b-instruct-15852e8c16360a2fea060d615a32b45270f8a8fc}"
SPLIT_DIR="${SPLIT_DIR:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/sft-release-surface-random-v2}"
LAYOUT_AUDIT="${LAYOUT_AUDIT:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/qwen35-2b-olist-sft-layout-audit-v1.json}"
RUN_DIR="${RUN_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen35-2b-olist-instruct-sft-smoke-v1-20260910}"
LOG_FILE="${LOG_FILE:-${RUN_DIR}.screen.log}"
SMOKE_SAMPLE_SELECTION="${SMOKE_SAMPLE_SELECTION:-shortest_sequence}"

for required_path in "${TRAIN_PYTHON}" "${MODEL_DIR}" "${SPLIT_DIR}/train.jsonl" "${SPLIT_DIR}/validation.jsonl" "${SPLIT_DIR}/split_audit.json" "${LAYOUT_AUDIT}"; do
  [[ -e "${required_path}" ]] || { echo "[error] required path does not exist: ${required_path}" >&2; exit 2; }
done
[[ ! -e "${RUN_DIR}" ]] || { echo "[error] run directory already exists; choose a new RUN_DIR" >&2; exit 2; }
mkdir -p "$(dirname "${LOG_FILE}")"

exec > >(tee -a "${LOG_FILE}") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] qwen35_2b_olist_instruct_bf16_lora_smoke_v1"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
echo "[smoke_sample_selection] ${SMOKE_SAMPLE_SELECTION}"
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
  --max-steps 1 \
  --max-train-samples 1 \
  --max-validation-samples 1 \
  --sample-selection "${SMOKE_SAMPLE_SELECTION}" \
  --seed 20260910 \
  --learning-rate 0.0001 \
  --weight-decay 0.01 \
  --per-device-train-batch-size 1 \
  --gradient-accumulation-steps 1 \
  --per-device-eval-batch-size 1 \
  --evaluation-steps 1 \
  --save-steps 1 \
  --logging-steps 1 \
  --lora-r 16 \
  --lora-alpha 32 \
  --lora-dropout 0.05 \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}" \
  --experiment-label qwen35_2b_olist_instruct_bf16_lora_smoke_v1

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
