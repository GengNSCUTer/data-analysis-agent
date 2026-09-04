#!/usr/bin/env bash
set -Eeuo pipefail

# One-step preflight for the frozen Olist Medium v1 bf16 LoRA contract.
# Full training is intentionally not launched by this script.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-3}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-10863af0-8588-7625-5609-640ba794f64b}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
SPLIT_DIR="${SPLIT_DIR:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-medium-v1/sft-splits-length3072-v1}"
RUN_DIR="${RUN_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-medium-bf16-lora-smoke-v1-20260904}"

for required_path in "${PYTHON}" "${MODEL_DIR}" "${SPLIT_DIR}/train.jsonl" "${SPLIT_DIR}/validation.jsonl" "${SPLIT_DIR}/split_audit.json"; do
  [[ -e "${required_path}" ]] || { echo "[error] required path does not exist: ${required_path}" >&2; exit 2; }
done
[[ ! -e "${RUN_DIR}/sft_smoke.json" ]] || { echo "[error] final evidence already exists; choose a new RUN_DIR" >&2; exit 2; }

mkdir -p "${RUN_DIR}"
exec > >(tee -a "${RUN_DIR}/screen-run.log") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] olist_medium_bf16_lora_1step_smoke_v1"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.training.run_post_training_sft_smoke \
  --model-dir "${MODEL_DIR}" \
  --train-jsonl "${SPLIT_DIR}/train.jsonl" \
  --validation-jsonl "${SPLIT_DIR}/validation.jsonl" \
  --split-audit "${SPLIT_DIR}/split_audit.json" \
  --output-dir "${RUN_DIR}" \
  --max-seq-length 3072 \
  --max-steps 1 \
  --seed 20260904 \
  --learning-rate 0.0001 \
  --weight-decay 0.01 \
  --per-device-train-batch-size 2 \
  --gradient-accumulation-steps 2 \
  --per-device-eval-batch-size 2 \
  --evaluation-steps 1 \
  --save-steps 1 \
  --lora-r 16 \
  --lora-alpha 32 \
  --lora-dropout 0.05 \
  --base-weight-mode bf16_lora \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}" \
  --experiment-label olist_medium_bf16_lora_1step_smoke_v1

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
