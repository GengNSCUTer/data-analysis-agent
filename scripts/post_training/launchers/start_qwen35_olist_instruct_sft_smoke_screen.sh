#!/usr/bin/env bash
set -Eeuo pipefail

# One optimizer-update lifecycle smoke for Qwen3.5-4B Instruct + bf16 LoRA.
# This script itself runs inside a separately created screen session.  It
# neither discovers nor changes other users' GPU processes.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TRAIN_PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qwen35/bin/python}"

# Host-specific mapping in AGENTS.md: this exposes physical nvidia-smi GPU 2
# as process-local cuda:0.  The Python entry verifies the UUID before loading.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}:${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-2}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen3.5-4b-instruct-851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a}"
SPLIT_DIR="${SPLIT_DIR:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/sft-release-surface-random-v2}"
LAYOUT_AUDIT="${LAYOUT_AUDIT:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/qwen35-olist-sft-layout-audit-v1.json}"
RUN_DIR="${RUN_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen35-4b-olist-instruct-sft-smoke-v2-20260910}"
LOG_FILE="${LOG_FILE:-${RUN_DIR}.screen.log}"

for required_path in "${TRAIN_PYTHON}" "${MODEL_DIR}" "${SPLIT_DIR}/train.jsonl" "${SPLIT_DIR}/validation.jsonl" "${SPLIT_DIR}/split_audit.json" "${LAYOUT_AUDIT}"; do
  [[ -e "${required_path}" ]] || { echo "[error] required path does not exist: ${required_path}" >&2; exit 2; }
done
[[ ! -e "${RUN_DIR}" ]] || { echo "[error] run directory already exists; choose a new RUN_DIR" >&2; exit 2; }
mkdir -p "$(dirname "${LOG_FILE}")"

exec > >(tee -a "${LOG_FILE}") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] qwen35_4b_olist_instruct_bf16_lora_smoke_v1"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
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
  --max-steps 1 \
  --max-train-samples 1 \
  --max-validation-samples 1 \
  --sample-selection shortest_sequence \
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
  --experiment-label qwen35_4b_olist_instruct_bf16_lora_smoke_v1

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
