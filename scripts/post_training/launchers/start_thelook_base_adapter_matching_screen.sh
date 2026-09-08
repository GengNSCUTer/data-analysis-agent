#!/usr/bin/env bash
set -euo pipefail

# Start matching generation only.  Evaluation is intentionally a separate
# command and must not run until both raw candidate files have 206 rows.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DATA_ROOT="${DATA_ROOT:-/disk2/gengnan/data-analysis-agent-data}"
EVAL_DIR="${DATA_ROOT}/evals/thelook-cross-schema-final-test-v1-20260908"
MODEL_DIR="${DATA_ROOT}/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b"
ADAPTER_DIR="${DATA_ROOT}/experiments/qwen25coder15b-olist-medium-bf16-lora-full2epoch-v1-20260904/adapter_final"
RUN_ROOT="${DATA_ROOT}/experiments/qwen25coder15b-thelook-matching-v1-20260908"
PHYSICAL_DEVICE="${PHYSICAL_DEVICE:?set physical nvidia-smi device (3 or another explicitly free GPU)}"
GPU_UUID="${GPU_UUID:?set expected GPU UUID from nvidia-smi}"
RUN_LABEL="${1:?usage: $0 base|adapter}"

case "$RUN_LABEL" in
  base) GPU_ENV="${BASE_CUDA_VISIBLE_DEVICES:?set BASE_CUDA_VISIBLE_DEVICES}"; OUT="${RUN_ROOT}/base"; ADAPTER_ARGS=() ;;
  adapter) GPU_ENV="${ADAPTER_CUDA_VISIBLE_DEVICES:?set ADAPTER_CUDA_VISIBLE_DEVICES}"; OUT="${RUN_ROOT}/adapter"; ADAPTER_ARGS=(--adapter-dir "$ADAPTER_DIR") ;;
  *) echo "run label must be base or adapter" >&2; exit 2 ;;
esac

mkdir -p "${RUN_ROOT}/logs"
exec env CUDA_VISIBLE_DEVICES="$GPU_ENV" PYTHONUNBUFFERED=1 \
  /disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python \
  "$ROOT/scripts/post_training/evaluation/run_thelook_matching_evaluation.py" \
  --cases-jsonl "$EVAL_DIR/cases.jsonl" \
  --manifest "$EVAL_DIR/manifest.json" \
  --model-dir "$MODEL_DIR" \
  --run-label "$RUN_LABEL" \
  "${ADAPTER_ARGS[@]}" \
  --output-dir "$OUT" \
  --physical-nvidia-smi-device "$PHYSICAL_DEVICE" \
  --expected-gpu-uuid "$GPU_UUID" \
  2>&1 | tee "${RUN_ROOT}/logs/${RUN_LABEL}.log"
