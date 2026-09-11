#!/usr/bin/env bash
set -Eeuo pipefail

# Full protected Qwen2.5-Coder-1.5B-Instruct baseline: first generation without
# Gold/database access, then the independently fail-closed post-evaluator.  It
# is intentionally separate from the historical Base/Adapter matching launcher.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
EVAL_PYTHON="${EVAL_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qwen35/bin/python}"
DATA_ROOT="${DATA_ROOT:-/disk2/gengnan/data-analysis-agent-data}"

# AGENTS.md mapping: logical CUDA device 3 is physical nvidia-smi GPU 1 (3090).
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-3}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}:${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-1}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-2fce35f7-4f80-b803-74bb-12b9cea0b9f0}"
# bf16 1.5B inference with the 4096/768 capacity contract needs materially less
# memory than training, but leave a 7 GiB free-memory floor for the shared GPU.
MIN_FREE_MIB="${MIN_FREE_MIB:-7168}"
EVAL_DIR="${EVAL_DIR:-${DATA_ROOT}/evals/thelook-cross-schema-final-test-v2-20260910}"
MODEL_DIR="${MODEL_DIR:-${DATA_ROOT}/models/qwen2.5-coder-1.5b-instruct-2e1fd397ee46e1388853d2af2c993145b0f1098a}"
RUN_ROOT="${RUN_ROOT:-${DATA_ROOT}/experiments/qwen25coder15b-instruct-thelook-v2-baseline-v1-20260911}"
GENERATION_DIR="${GENERATION_DIR:-${RUN_ROOT}/generation}"
EVALUATION_DIR="${EVALUATION_DIR:-${RUN_ROOT}/execution-evaluation}"
LOG_FILE="${LOG_FILE:-${RUN_ROOT}.screen.log}"

for required_path in \
  "${EVAL_PYTHON}" \
  "${EVAL_DIR}/cases.jsonl" \
  "${EVAL_DIR}/manifest.json" \
  "${MODEL_DIR}/config.json" \
  "${MODEL_DIR}/model.safetensors" \
  "${MODEL_DIR}/download_manifest.json"; do
  [[ -e "${required_path}" ]] || {
    echo "[error] required path does not exist: ${required_path}" >&2
    exit 2
  }
done
[[ ! -e "${RUN_ROOT}" ]] || {
  echo "[error] run root already exists: ${RUN_ROOT}" >&2
  exit 2
}

read -r observed_uuid used_mib total_mib < <(
  nvidia-smi --id="${PHYSICAL_NVIDIA_SMI_DEVICE}" \
    --query-gpu=uuid,memory.used,memory.total --format=csv,noheader,nounits \
    | awk -F ', ' '{print $1, $2, $3}'
)
[[ "${observed_uuid}" == "${EXPECTED_GPU_UUID}" ]] || {
  echo "[error] physical GPU UUID guard failed: expected ${EXPECTED_GPU_UUID}, got ${observed_uuid}" >&2
  exit 2
}
free_mib=$((total_mib - used_mib))
if (( free_mib < MIN_FREE_MIB )); then
  echo "[error] physical GPU ${PHYSICAL_NVIDIA_SMI_DEVICE} has ${free_mib} MiB free; requires ${MIN_FREE_MIB} MiB" >&2
  exit 3
fi
mkdir -p "$(dirname "${LOG_FILE}")"

exec > >(tee -a "${LOG_FILE}") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] qwen25coder15b_instruct_thelook_v2_baseline_v1"
echo "[generation_scope] no_gold_no_postgres"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
echo "[free_mib] ${free_mib}"
echo "[run_root] ${RUN_ROOT}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total,utilization.gpu --format=csv,noheader

"${EVAL_PYTHON}" "${ROOT}/scripts/post_training/evaluation/run_qwen25coder15b_instruct_thelook_v2_baseline_generation.py" \
  --cases-jsonl "${EVAL_DIR}/cases.jsonl" \
  --manifest "${EVAL_DIR}/manifest.json" \
  --model-dir "${MODEL_DIR}" \
  --output-dir "${GENERATION_DIR}" \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}"

echo "[generation_complete] $(date --iso-8601=seconds)"
echo "[post_evaluation_scope] verified_generation_then_policy_reader_contract_gold"
"${EVAL_PYTHON}" "${ROOT}/scripts/post_training/evaluation/evaluate_qwen25coder15b_instruct_thelook_v2_baseline.py" \
  --cases-jsonl "${EVAL_DIR}/cases.jsonl" \
  --manifest "${EVAL_DIR}/manifest.json" \
  --model-dir "${MODEL_DIR}" \
  --safe-report "${GENERATION_DIR}/safe-report.json" \
  --completions "${GENERATION_DIR}/raw-completions.jsonl" \
  --output-dir "${EVALUATION_DIR}"

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
