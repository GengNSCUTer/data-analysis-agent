#!/usr/bin/env bash
set -Eeuo pipefail

# Run the protected Olist transfer evaluation sequentially on one guarded GPU.
# The generated SQL and all reports stay in the selected external run directory.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QLORA_PYTHON="${QLORA_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
RUN_DIRECTORY="${RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-olist-transfer-v1-20260826}"
MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
ADAPTER_DIR="${ADAPTER_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-bf16-lora-spider-sft-v2-20260826/adapter_final}"
MANIFEST="${MANIFEST:-${ROOT}/evals/manifests/post_training_olist_business_adapter_evaluation_v1.yaml}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
PHYSICAL_NVIDIA_SMI_DEVICE="${PHYSICAL_NVIDIA_SMI_DEVICE:-3}"
EXPECTED_GPU_UUID="${EXPECTED_GPU_UUID:-GPU-10863af0-8588-7625-5609-640ba794f64b}"
LOG_FILE="${RUN_DIRECTORY}/screen-run.log"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'USAGE'
Usage: start_olist_candidate_sql_evaluation_screen.sh

Run the protected Base/Adapter Olist transfer evaluation in the current shell.
Set RUN_DIRECTORY to a new external directory before re-running a completed task.
Use `screen -dmS <name> bash scripts/start_olist_candidate_sql_evaluation_screen.sh`
to detach it from an SSH session.
USAGE
  exit 0
fi

if [[ -e "${RUN_DIRECTORY}/completed" ]]; then
  echo "[error] evaluation has already completed; choose a new RUN_DIRECTORY" >&2
  exit 2
fi
for required_path in "${QLORA_PYTHON}" "${MODEL_DIR}" "${ADAPTER_DIR}" "${MANIFEST}"; do
  if [[ ! -e "${required_path}" ]]; then
    echo "[error] required path does not exist: ${required_path}" >&2
    exit 2
  fi
done
if [[ ! -f "${ADAPTER_DIR}/adapter_model.safetensors" ]]; then
  echo "[error] adapter directory lacks adapter_model.safetensors: ${ADAPTER_DIR}" >&2
  exit 2
fi

mkdir -p "${RUN_DIRECTORY}"
exec > >(tee -a "${LOG_FILE}") 2>&1
export CUDA_VISIBLE_DEVICES
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

echo "[start] $(date --iso-8601=seconds)"
echo "[run_directory] ${RUN_DIRECTORY}"
echo "[manifest] ${MANIFEST}"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[physical_nvidia_smi_device] ${PHYSICAL_NVIDIA_SMI_DEVICE}"
echo "[expected_gpu_uuid] ${EXPECTED_GPU_UUID}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total --format=csv,noheader

"${QLORA_PYTHON}" "${ROOT}/scripts/run_olist_candidate_sql_evaluation.py" \
  --manifest "${MANIFEST}" \
  --model-dir "${MODEL_DIR}" \
  --run-label base \
  --output-dir "${RUN_DIRECTORY}/base" \
  --max-input-tokens 4096 \
  --max-new-tokens 256 \
  --seed 42 \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}"

"${QLORA_PYTHON}" "${ROOT}/scripts/run_olist_candidate_sql_evaluation.py" \
  --manifest "${MANIFEST}" \
  --model-dir "${MODEL_DIR}" \
  --run-label adapter \
  --adapter-dir "${ADAPTER_DIR}" \
  --output-dir "${RUN_DIRECTORY}/adapter" \
  --max-input-tokens 4096 \
  --max-new-tokens 256 \
  --seed 42 \
  --physical-nvidia-smi-device "${PHYSICAL_NVIDIA_SMI_DEVICE}" \
  --expected-gpu-uuid "${EXPECTED_GPU_UUID}"

"${QLORA_PYTHON}" "${ROOT}/scripts/analyze_olist_candidate_sql_evaluation.py" \
  --base-report "${RUN_DIRECTORY}/base/safe-report.json" \
  --adapter-report "${RUN_DIRECTORY}/adapter/safe-report.json" \
  --output "${RUN_DIRECTORY}/analysis/safe-comparison.json"

touch "${RUN_DIRECTORY}/completed"
echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
