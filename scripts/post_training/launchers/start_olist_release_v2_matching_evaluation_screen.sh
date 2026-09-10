#!/usr/bin/env bash
set -Eeuo pipefail

# Compare the frozen Olist release-v2 Base and final LoRA adapter on the exact
# 600 primary surface forms selected for the final test.  Generation reads no
# Gold SQL; raw candidates and reports remain outside Git.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
DATA_ROOT="/disk2/gengnan/data-analysis-agent-data"

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-3}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-10863af0-8588-7625-5609-640ba794f64b}"
MODEL_DIR="${MODEL_DIR:-${DATA_ROOT}/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
SPLIT_DIR="${SPLIT_DIR:-${DATA_ROOT}/evals/olist-domain-sft-release-v2-20260909/sft-release-surface-random-v2}"
RUNTIME_CANDIDATES="${RUNTIME_CANDIDATES:-${DATA_ROOT}/evals/olist-domain-sft-release-v2-20260909/runtime-prompts-v2/runtime_candidates.jsonl}"
ADAPTER_DIR="${ADAPTER_DIR:-${DATA_ROOT}/experiments/qwen25coder15b-olist-domain-sft-release-v2-bf16-lora-2epoch-retry-v3-20260909/adapter_final}"
RUN_DIR="${RUN_DIR:-${DATA_ROOT}/experiments/qwen25coder15b-olist-domain-sft-release-v2-base-adapter-finaltest-v1-20260909}"

for required_path in "${PYTHON}" "${MODEL_DIR}" "${ADAPTER_DIR}/adapter_model.safetensors" "${SPLIT_DIR}/final_evaluation_only/in_domain_test.jsonl" "${SPLIT_DIR}/validation.jsonl" "${SPLIT_DIR}/split_audit.json" "${RUNTIME_CANDIDATES}"; do
  [[ -e "${required_path}" ]] || { echo "[error] required path does not exist: ${required_path}" >&2; exit 2; }
done
[[ ! -e "${RUN_DIR}/completed" ]] || { echo "[error] evaluation already completed; choose a new RUN_DIR" >&2; exit 2; }
mkdir -p "${RUN_DIR}"
exec > >(tee -a "${RUN_DIR}/screen-run.log") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] olist_release_v2_matching_base_adapter_finaltest_v1"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.training.validate_post_training_adapter \
  --model-dir "${MODEL_DIR}" --adapter-dir "${ADAPTER_DIR}" \
  --validation-jsonl "${SPLIT_DIR}/validation.jsonl" --output-dir "${RUN_DIR}/adapter-reload" \
  --sample-index 0 --max-seq-length 3072 --base-weight-mode bf16_lora \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" --expected-gpu-uuid "${EXPECTED_GPU_UUID}"

COMMON=(--test-jsonl "${SPLIT_DIR}/final_evaluation_only/in_domain_test.jsonl" --runtime-candidates "${RUNTIME_CANDIDATES}" --split-audit "${SPLIT_DIR}/split_audit.json" --model-dir "${MODEL_DIR}" --evaluation-suite olist_domain_sft_release_v2 --max-input-tokens 3072 --max-new-tokens 768 --seed 20260909 --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" --expected-gpu-uuid "${EXPECTED_GPU_UUID}")
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.run_olist_medium_matching_evaluation --run-label base --output-dir "${RUN_DIR}/base" "${COMMON[@]}"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.run_olist_medium_matching_evaluation --run-label adapter --adapter-dir "${ADAPTER_DIR}" --output-dir "${RUN_DIR}/adapter" "${COMMON[@]}"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.analyze_olist_candidate_sql_evaluation --base-report "${RUN_DIR}/base/safe-report.json" --adapter-report "${RUN_DIR}/adapter/safe-report.json" --output "${RUN_DIR}/analysis/safe-comparison.json"
touch "${RUN_DIR}/completed"
echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
