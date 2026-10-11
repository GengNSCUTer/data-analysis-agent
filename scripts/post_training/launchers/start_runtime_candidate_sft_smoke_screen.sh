#!/usr/bin/env bash
# Only the bounded runtime-v2 smoke. No full training or protected-test reads.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TRAIN_PYTHON=/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python
DATA_ROOT=/disk2/gengnan/data-analysis-agent-data
RELEASE_DIR="${DATA_ROOT}/evals/runtime-candidate-sql-v2-r1"
MODEL_DIR="${DATA_ROOT}/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b"
BATCH="${2:-${1:-2}}"
[[ "${BATCH}" == 1 || "${BATCH}" == 2 ]] || { echo "usage: bash $0 [1|2]" >&2; exit 2; }
RUN_DIR="${DATA_ROOT}/experiments/runtime-candidate-sft-smoke-20261011-b${BATCH}-r1"
LOG="${DATA_ROOT}/evals/runtime-candidate-sft-smoke-20261011-b${BATCH}-r1.log"
SESSION="daa-runtime-smoke-b${BATCH}-20261011"

if [[ "${1:-}" != --worker ]]; then
  [[ ! -e "${RUN_DIR}" && ! -e "${LOG}" ]] || { echo "[error] smoke artifacts already exist" >&2; exit 2; }
  screen -L -Logfile "${LOG}" -dmS "${SESSION}" bash "${BASH_SOURCE[0]}" --worker "${BATCH}"
  echo "[screen] ${SESSION}"
  echo "[log] ${LOG}"
  echo "[output] ${RUN_DIR}"
  exit 0
fi

# Logical CUDA 1 -> physical nvidia-smi 3, RTX 4090. Recheck occupancy
# inside screen immediately before loading; never stop another GPU process.
GPU_UUID=GPU-10863af0-8588-7625-5609-640ba794f64b
GPU_STATE="$(nvidia-smi -i "${GPU_UUID}" --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits)"
IFS=, read -r GPU_MEMORY GPU_UTILIZATION <<< "${GPU_STATE}"
if (( GPU_MEMORY > 1024 || GPU_UTILIZATION > 10 )); then
  echo "[error] physical GPU 3 is busy: ${GPU_STATE}" >&2
  exit 2
fi
export CUDA_VISIBLE_DEVICES=1
export PYTHONPATH="${ROOT}/src:${ROOT}"
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export NCCL_P2P_DISABLE=1
export NCCL_IB_DISABLE=1
cd "${ROOT}"
echo "[start] $(date --iso-8601=seconds); physical GPU 3; ${GPU_UUID}; batch ${BATCH}"
"${TRAIN_PYTHON}" -m scripts.post_training.training.run_post_training_sft_smoke \
  --model-dir "${MODEL_DIR}" \
  --train-jsonl "${RELEASE_DIR}/train.jsonl" \
  --validation-jsonl "${RELEASE_DIR}/validation.jsonl" \
  --split-audit "${RELEASE_DIR}/manifest.json" \
  --expected-release-manifest-sha256 5a850f34cbc2e3dd36d770ea9203e0cf7e1cda0ef4b7d73351c4b47138ff904d \
  --output-dir "${RUN_DIR}" --max-seq-length 4096 \
  --smoke-samples-per-workspace 4 --max-steps 2 \
  --seed 20261011 --learning-rate 0.0001 --weight-decay 0.01 \
  --per-device-train-batch-size "${BATCH}" --gradient-accumulation-steps "$((4 / BATCH))" \
  --per-device-eval-batch-size "${BATCH}" \
  --evaluation-steps 1 --save-steps 1 --logging-steps 1 \
  --lora-r 16 --lora-alpha 32 --lora-dropout 0.05 --base-weight-mode bf16_lora \
  --physical-nvidia-smi-device 3 --expected-gpu-uuid "${GPU_UUID}" \
  --experiment-label "runtime_candidate_sft_smoke_b${BATCH}_20261011"
echo "[finish] $(date --iso-8601=seconds); exit 0"
