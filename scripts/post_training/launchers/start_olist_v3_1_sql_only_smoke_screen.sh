#!/usr/bin/env bash
# Run the frozen Olist v3.1 SQL-only LoRA engineering smoke in a screen session.
# The full 3,000/750 release is intentionally retained: this is a one-step
# lifecycle/longest-sequence smoke, not a quality evaluation or data subset.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-2}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-2}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52}"
MIN_FREE_MIB="${MIN_FREE_MIB:-19000}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
SFT_DIR="${SFT_DIR:-/disk2/gengnan/data-analysis-agent-data/evals/olist-v3-balanced-release-v1.1/sft-release-v1.1}"
PREFLIGHT_DIR="${PREFLIGHT_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-v3_1-sql-only-preflight-v1-20260915}"
PREFLIGHT_SHA256="${PREFLIGHT_SHA256:-ff5a71ec50bf96c9b47ac1a601999e1c909475c38eabf4fad74876f83b460944}"
RUN_DIR="${RUN_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-v3_1-sql-only-bf16-lora-smoke-v1-20260915}"

for required_path in \
  "${PYTHON}" \
  "${MODEL_DIR}/download_manifest.json" \
  "${SFT_DIR}/train.jsonl" \
  "${SFT_DIR}/validation.jsonl" \
  "${SFT_DIR}/split_audit.json" \
  "${PREFLIGHT_DIR}/preflight.json"; do
  [[ -e "${required_path}" ]] || { echo "[error] missing required path: ${required_path}" >&2; exit 2; }
done
[[ ! -e "${RUN_DIR}" ]] || { echo "[error] run directory already exists: ${RUN_DIR}" >&2; exit 2; }
[[ "$(sha256sum "${PREFLIGHT_DIR}/preflight.json" | awk '{print $1}')" == "${PREFLIGHT_SHA256}" ]] || {
  echo "[error] CPU preflight fingerprint drifted" >&2
  exit 2
}

read -r gpu_used gpu_total < <(
  nvidia-smi -i "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
    --query-gpu=memory.used,memory.total --format=csv,noheader,nounits \
    | awk -F ',' '{gsub(/ /, "", $1); gsub(/ /, "", $2); print $1, $2}'
)
gpu_free=$((gpu_total - gpu_used))
(( gpu_free >= MIN_FREE_MIB )) || {
  echo "[error] GPU ${PHYSICAL_NVIDIA_SMI_DEVICE} has ${gpu_free} MiB free; requires ${MIN_FREE_MIB} MiB" >&2
  exit 2
}

mkdir -p "${RUN_DIR}"
exec > >(tee -a "${RUN_DIR}/screen-run.log") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] olist_v3_1_sql_only_bf16_lora_smoke_v1"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
echo "[preflight_sha256] ${PREFLIGHT_SHA256}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.training.run_post_training_sft_smoke \
  --model-dir "${MODEL_DIR}" \
  --train-jsonl "${SFT_DIR}/train.jsonl" \
  --validation-jsonl "${SFT_DIR}/validation.jsonl" \
  --split-audit "${SFT_DIR}/split_audit.json" \
  --output-dir "${RUN_DIR}" \
  --max-seq-length 3072 \
  --max-steps 1 \
  --seed 20260914 \
  --learning-rate 0.0001 \
  --weight-decay 0.01 \
  --per-device-train-batch-size 1 \
  --gradient-accumulation-steps 4 \
  --per-device-eval-batch-size 1 \
  --evaluation-steps 1 \
  --save-steps 1 \
  --logging-steps 1 \
  --lora-r 16 \
  --lora-alpha 32 \
  --lora-dropout 0.05 \
  --base-weight-mode bf16_lora \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}" \
  --experiment-label olist_v3_1_sql_only_bf16_lora_smoke_v1

# Recreate the Base in a fresh process, attach the validation-best adapter,
# and run one validation forward pass.  This checks loadability separately
# from Trainer's in-memory best-model reload.
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.training.validate_post_training_adapter \
  --model-dir "${MODEL_DIR}" \
  --adapter-dir "${RUN_DIR}/adapter_best" \
  --validation-jsonl "${SFT_DIR}/validation.jsonl" \
  --output-dir "${RUN_DIR}" \
  --sample-index 0 \
  --max-seq-length 3072 \
  --base-weight-mode bf16_lora \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}"

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
