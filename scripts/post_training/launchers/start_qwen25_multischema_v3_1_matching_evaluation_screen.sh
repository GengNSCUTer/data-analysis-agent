#!/usr/bin/env bash
set -Eeuo pipefail

# Evaluate the best checkpoint from the 2026-09-16 Qwen2.5-Coder-1.5B
# multi-schema SQL-only SFT run.  This is intentionally a fresh external run:
# the generation runners never load Gold SQL, while the dataset-specific
# comparison commands run only after both Base and Adapter outputs are frozen.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
DATA_ROOT="${DATA_ROOT:-/disk2/gengnan/data-analysis-agent-data}"

# Logical CUDA 0 is physical nvidia-smi GPU 2 (RTX 4090); see AGENTS.md.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-2}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-129ba5d7-5a0a-745d-5a49-11dc7967bb52}"
MODEL_DIR="${MODEL_DIR:-${DATA_ROOT}/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
ADAPTER_DIR="${ADAPTER_DIR:-${DATA_ROOT}/experiments/qwen25coder15b-multischema-sql-only-sft-v1-20260916/adapter_best}"
TRAINING_SPLIT_DIR="${TRAINING_SPLIT_DIR:-${DATA_ROOT}/evals/multi-schema-ecommerce-sql-only-v1-r1}"
OLIST_SPLIT_DIR="${OLIST_SPLIT_DIR:-${DATA_ROOT}/evals/olist-v3-balanced-release-v1.1/sft-release-v1.1}"
OLIST_RUNTIME_CANDIDATES="${OLIST_RUNTIME_CANDIDATES:-${DATA_ROOT}/evals/olist-v3-balanced-release-v1.1/runtime-prompts-20260914-v3_1/runtime_candidates.jsonl}"
THELOOK_EVAL_DIR="${THELOOK_EVAL_DIR:-${DATA_ROOT}/evals/thelook-cross-schema-final-test-v3-20260915-r3}"
RUN_DIR="${RUN_DIR:-${DATA_ROOT}/experiments/qwen25coder15b-multischema-v3_1-olist-thelook-base-adapter-v1-20260916}"

for required_path in \
  "${PYTHON}" \
  "${MODEL_DIR}/download_manifest.json" \
  "${ADAPTER_DIR}/adapter_model.safetensors" \
  "${ADAPTER_DIR}/adapter_config.json" \
  "${TRAINING_SPLIT_DIR}/validation.jsonl" \
  "${OLIST_SPLIT_DIR}/final_evaluation_only/in_domain_test.jsonl" \
  "${OLIST_SPLIT_DIR}/split_audit.json" \
  "${OLIST_RUNTIME_CANDIDATES}" \
  "${THELOOK_EVAL_DIR}/cases.jsonl" \
  "${THELOOK_EVAL_DIR}/manifest.json"; do
  [[ -e "${required_path}" ]] || { echo "[error] required path does not exist: ${required_path}" >&2; exit 2; }
done

[[ ! -e "${RUN_DIR}" ]] || { echo "[error] RUN_DIR already exists; choose a new run directory: ${RUN_DIR}" >&2; exit 2; }
mkdir -p "${RUN_DIR}"
exec > >(tee -a "${RUN_DIR}/screen-run.log") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] qwen25coder15b_multischema_v3_1_olist_thelook_matching_v1"
echo "[run_dir] ${RUN_DIR}"
echo "[gpu] logical=${CUDA_VISIBLE_DEVICES}; physical=${PHYSICAL_NVIDIA_SMI_DEVICE}; expected_uuid=${EXPECTED_GPU_UUID}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

# First prove that the selected best adapter reloads against the frozen combined
# validation set.  This does not generate any final-test candidates.
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.training.validate_post_training_adapter \
  --model-dir "${MODEL_DIR}" \
  --adapter-dir "${ADAPTER_DIR}" \
  --validation-jsonl "${TRAINING_SPLIT_DIR}/validation.jsonl" \
  --output-dir "${RUN_DIR}/adapter-reload" \
  --sample-index 0 \
  --max-seq-length 3072 \
  --base-weight-mode bf16_lora \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}"

OLIST_COMMON=(
  --test-jsonl "${OLIST_SPLIT_DIR}/final_evaluation_only/in_domain_test.jsonl"
  --runtime-candidates "${OLIST_RUNTIME_CANDIDATES}"
  --split-audit "${OLIST_SPLIT_DIR}/split_audit.json"
  --model-dir "${MODEL_DIR}"
  --evaluation-suite olist_v3_1_in_domain_final_test
  --max-input-tokens 3072
  --max-new-tokens 768
  --seed 20260916
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}"
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}"
)

echo "[stage] Olist Base generation + governed execution"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.run_olist_medium_matching_evaluation \
  --run-label base --output-dir "${RUN_DIR}/olist/base" "${OLIST_COMMON[@]}"

echo "[stage] Olist Adapter generation + governed execution"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.run_olist_medium_matching_evaluation \
  --run-label adapter --adapter-dir "${ADAPTER_DIR}" --output-dir "${RUN_DIR}/olist/adapter" "${OLIST_COMMON[@]}"

echo "[stage] Olist aggregate comparison"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.analyze_olist_candidate_sql_evaluation \
  --base-report "${RUN_DIR}/olist/base/safe-report.json" \
  --adapter-report "${RUN_DIR}/olist/adapter/safe-report.json" \
  --output "${RUN_DIR}/olist/analysis/safe-comparison.json"

# Gold is allowed only after both matching Olist generation sides and their
# immutable safe reports exist. This marker is deliberately narrower than the
# final experiment marker written after both data suites are fully audited.
touch "${RUN_DIR}/olist/generation-completed"
echo "[stage] Olist post-generation Gold/denotation comparison"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.run_olist_medium_gold_denotation_audit \
  --test-jsonl "${OLIST_SPLIT_DIR}/final_evaluation_only/in_domain_test.jsonl" \
  --runtime-candidates "${OLIST_RUNTIME_CANDIDATES}" \
  --split-audit "${OLIST_SPLIT_DIR}/split_audit.json" \
  --base-safe-report "${RUN_DIR}/olist/base/safe-report.json" \
  --adapter-safe-report "${RUN_DIR}/olist/adapter/safe-report.json" \
  --base-raw-candidates "${RUN_DIR}/olist/base/raw-candidates.jsonl" \
  --adapter-raw-candidates "${RUN_DIR}/olist/adapter/raw-candidates.jsonl" \
  --completed-marker "${RUN_DIR}/olist/generation-completed" \
  --output "${RUN_DIR}/olist/gold-denotation-audit.json"

THELOOK_COMMON=(
  --cases-jsonl "${THELOOK_EVAL_DIR}/cases.jsonl"
  --manifest "${THELOOK_EVAL_DIR}/manifest.json"
  --model-dir "${MODEL_DIR}"
  --max-input-tokens 3072
  --max-new-tokens 768
  --seed 20260916
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}"
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}"
)

echo "[stage] TheLook Base generation"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.run_thelook_matching_evaluation \
  --run-label base --output-dir "${RUN_DIR}/thelook/base" "${THELOOK_COMMON[@]}"

echo "[stage] TheLook Adapter generation"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.run_thelook_matching_evaluation \
  --run-label adapter --adapter-dir "${ADAPTER_DIR}" --output-dir "${RUN_DIR}/thelook/adapter" "${THELOOK_COMMON[@]}"

# This is the first and only stage allowed to load the frozen Gold SQL.
echo "[stage] TheLook post-generation Gold/denotation comparison"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.evaluate_thelook_matching_outputs \
  --cases-jsonl "${THELOOK_EVAL_DIR}/cases.jsonl" \
  --manifest "${THELOOK_EVAL_DIR}/manifest.json" \
  --base-dir "${RUN_DIR}/thelook/base" \
  --adapter-dir "${RUN_DIR}/thelook/adapter" \
  --output-dir "${RUN_DIR}/thelook/evaluation"

echo "[stage] cross-suite terminal summary"
PYTHONPATH="${ROOT}" "${PYTHON}" -m scripts.post_training.evaluation.summarize_multischema_matching_evaluation \
  --olist-safe-comparison "${RUN_DIR}/olist/analysis/safe-comparison.json" \
  --olist-gold-audit "${RUN_DIR}/olist/gold-denotation-audit.json" \
  --thelook-evaluation "${RUN_DIR}/thelook/evaluation/evaluation-report.json" \
  --output "${RUN_DIR}/terminal-evaluation-summary.json"

touch "${RUN_DIR}/completed"
echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
