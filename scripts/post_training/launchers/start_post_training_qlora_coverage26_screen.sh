#!/usr/bin/env bash
set -Eeuo pipefail

# One controlled 26-step QLoRA run. All raw training data, checkpoints,
# adapters and logs remain outside the Git worktree.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
QLORA_PYTHON="${QLORA_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export CUDA_VISIBLE_DEVICES
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-2}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52}"
RUN_DIRECTORY="${RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-qlora-coverage26-v1-20260825}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
SPLIT_DIRECTORY="${SPLIT_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/spider-sft-split-v1-20260825}"
LOG_FILE="${RUN_DIRECTORY}/screen-run.log"

for required_path in "${QLORA_PYTHON}" "${MODEL_DIR}" "${SPLIT_DIRECTORY}/train.jsonl" "${SPLIT_DIRECTORY}/validation.jsonl" "${SPLIT_DIRECTORY}/split_audit.json"; do
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
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] qlora_coverage26"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

"${QLORA_PYTHON}" "${ROOT}/scripts/run_post_training_sft_smoke.py" \
  --model-dir "${MODEL_DIR}" \
  --train-jsonl "${SPLIT_DIRECTORY}/train.jsonl" \
  --validation-jsonl "${SPLIT_DIRECTORY}/validation.jsonl" \
  --split-audit "${SPLIT_DIRECTORY}/split_audit.json" \
  --output-dir "${RUN_DIRECTORY}" \
  --max-seq-length 1536 \
  --max-steps 26 \
  --seed 20260825 \
  --learning-rate 0.0002 \
  --gradient-accumulation-steps 4 \
  --lora-r 16 \
  --lora-alpha 32 \
  --lora-dropout 0.05 \
  --base-weight-mode qlora_4bit \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}" \
  --experiment-label qlora_coverage26_v1

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
