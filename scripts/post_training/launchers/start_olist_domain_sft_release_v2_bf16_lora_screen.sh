#!/usr/bin/env bash
set -Eeuo pipefail

# Formal Olist domain-release v2 SFT. The release contains one deterministically
# selected Chinese form per QuerySpec (five reviewed forms stay as overlays),
# and all checkpoints/logs remain outside the Git worktree.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TRAIN_PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"

# AGENTS.md mapping: CUDA_VISIBLE_DEVICES=0 exposes physical nvidia-smi GPU 2
# as process-local cuda:0 (RTX 4090). Do not change only one identifier.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-2}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
SPLIT_DIR="${SPLIT_DIR:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-release-v2-20260909/sft-release-surface-random-v2}"
# Retry v3 changes only the physical-GPU selection after v2 was stopped at
# step 11: CUDA_VISIBLE_DEVICES=2 maps to a busy physical 3090 on this host.
# The micro-batch remains one and accumulation keeps the effective batch four.
RUN_DIR="${RUN_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-domain-sft-release-v2-bf16-lora-2epoch-retry-v3-20260909}"

for required_path in "${TRAIN_PYTHON}" "${MODEL_DIR}" "${SPLIT_DIR}/train.jsonl" "${SPLIT_DIR}/validation.jsonl" "${SPLIT_DIR}/split_audit.json"; do
  [[ -e "${required_path}" ]] || { echo "[error] required path does not exist: ${required_path}" >&2; exit 2; }
done
[[ ! -e "${RUN_DIR}/sft_smoke.json" ]] || { echo "[error] final evidence already exists; choose a new RUN_DIR" >&2; exit 2; }

mkdir -p "${RUN_DIR}"
exec > >(tee -a "${RUN_DIR}/screen-run.log") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] olist_domain_sft_release_v2_bf16_lora_2epoch"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
echo "[split_dir] ${SPLIT_DIR}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

"${TRAIN_PYTHON}" -m scripts.post_training.training.run_post_training_sft_smoke \
  --model-dir "${MODEL_DIR}" \
  --train-jsonl "${SPLIT_DIR}/train.jsonl" \
  --validation-jsonl "${SPLIT_DIR}/validation.jsonl" \
  --split-audit "${SPLIT_DIR}/split_audit.json" \
  --output-dir "${RUN_DIR}" \
  --max-seq-length 3072 \
  --num-train-epochs 2 \
  --seed 20260909 \
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
  --base-weight-mode bf16_lora \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}" \
  --experiment-label olist_domain_sft_release_v2_bf16_lora_2epoch

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
