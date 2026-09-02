#!/usr/bin/env bash
set -Eeuo pipefail

# Canonical CSpider two-epoch bf16 LoRA training launcher. It owns no training
# logic; the versioned manifest records the immutable comparison contract.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TRAIN_PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
# Logical CUDA 1 maps to physical nvidia-smi GPU 3 on this host. Keep all three
# identifiers together so a caller must opt in explicitly when selecting another card.
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
export CUDA_VISIBLE_DEVICES
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-3}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-10863af0-8588-7625-5609-640ba794f64b}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
SPLIT_DIRECTORY="${SPLIT_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/cspider/cspider-1.0-official-2026-09-01/prepared/official-splits-length1536-v1}"
RUN_DIRECTORY="${RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-cspider-bf16-lora-length1536-full2epoch-v1-20260902}"

for required_path in "${TRAIN_PYTHON}" "${MODEL_DIR}" "${SPLIT_DIRECTORY}/train.jsonl" "${SPLIT_DIRECTORY}/validation.jsonl" "${SPLIT_DIRECTORY}/split_audit.json" "${SPLIT_DIRECTORY}/exclusions/train-validation-length.jsonl"; do
  if [[ ! -e "${required_path}" ]]; then
    echo "[error] required path does not exist: ${required_path}" >&2
    exit 2
  fi
done
if [[ -e "${RUN_DIRECTORY}/sft_smoke.json" ]]; then
  echo "[error] final evidence already exists; choose a new RUN_DIRECTORY" >&2
  exit 2
fi

mkdir -p "${RUN_DIRECTORY}"
exec > >(tee -a "${RUN_DIRECTORY}/screen-run.log") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] cspider_bf16_lora_length1536_full2epoch_v1"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
echo "[split_directory] ${SPLIT_DIRECTORY}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

"${TRAIN_PYTHON}" "${ROOT}/scripts/run_post_training_sft_smoke.py" \
  --model-dir "${MODEL_DIR}" \
  --train-jsonl "${SPLIT_DIRECTORY}/train.jsonl" \
  --validation-jsonl "${SPLIT_DIRECTORY}/validation.jsonl" \
  --split-audit "${SPLIT_DIRECTORY}/split_audit.json" \
  --output-dir "${RUN_DIRECTORY}" \
  --max-seq-length 1536 \
  --num-train-epochs 2 \
  --seed 20260902 \
  --learning-rate 0.0001 \
  --weight-decay 0.01 \
  --per-device-train-batch-size 4 \
  --gradient-accumulation-steps 1 \
  --per-device-eval-batch-size 4 \
  --evaluation-steps 536 \
  --save-steps 536 \
  --logging-steps 25 \
  --lora-r 16 \
  --lora-alpha 32 \
  --lora-dropout 0.05 \
  --base-weight-mode bf16_lora \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}" \
  --experiment-label cspider_bf16_lora_length1536_full2epoch_v1

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
