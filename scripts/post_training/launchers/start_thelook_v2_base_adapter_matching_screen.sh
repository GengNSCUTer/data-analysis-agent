#!/usr/bin/env bash
set -euo pipefail

# This launcher performs generation only.  Run it inside a separately named
# screen session for `base` and `adapter`; do not invoke the verifier or the
# Gold audit until both safe reports and raw completion files are complete.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DATA_ROOT="${DATA_ROOT:-/disk2/gengnan/data-analysis-agent-data}"
EVAL_DIR="${DATA_ROOT}/evals/thelook-cross-schema-final-test-v2-20260910"
MODEL_DIR="${DATA_ROOT}/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b"
ADAPTER_DIR="${DATA_ROOT}/experiments/qwen25coder15b-olist-domain-sft-release-v2-bf16-lora-2epoch-retry-v3-20260909/adapter_final"
RUN_ROOT="${DATA_ROOT}/experiments/qwen25coder15b-thelook-v2-matching-v1-20260910"
PHYSICAL_DEVICE="${PHYSICAL_DEVICE:?set the explicit physical nvidia-smi device}"
GPU_UUID="${GPU_UUID:?set the expected GPU UUID from nvidia-smi}"
RUN_LABEL="${1:?usage: $0 base|adapter}"

case "$RUN_LABEL" in
  base)
    GPU_ENV="${BASE_CUDA_VISIBLE_DEVICES:?set BASE_CUDA_VISIBLE_DEVICES}"
    OUT="${RUN_ROOT}/base-generation"
    ADAPTER_ARGS=()
    ;;
  adapter)
    GPU_ENV="${ADAPTER_CUDA_VISIBLE_DEVICES:?set ADAPTER_CUDA_VISIBLE_DEVICES}"
    OUT="${RUN_ROOT}/adapter-generation"
    ADAPTER_ARGS=(--adapter-dir "$ADAPTER_DIR")
    ;;
  *)
    echo "run label must be base or adapter" >&2
    exit 2
    ;;
esac

mkdir -p "${RUN_ROOT}/logs"
exec env CUDA_VISIBLE_DEVICES="$GPU_ENV" PYTHONUNBUFFERED=1 \
  /disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python \
  "$ROOT/scripts/post_training/evaluation/run_thelook_v2_matching_generation.py" \
  --cases-jsonl "$EVAL_DIR/cases.jsonl" \
  --manifest "$EVAL_DIR/manifest.json" \
  --model-dir "$MODEL_DIR" \
  --run-label "$RUN_LABEL" \
  "${ADAPTER_ARGS[@]}" \
  --output-dir "$OUT" \
  --physical-nvidia-smi-device "$PHYSICAL_DEVICE" \
  --expected-gpu-uuid "$GPU_UUID" \
  2>&1 | tee "${RUN_ROOT}/logs/${RUN_LABEL}-generation.log"
