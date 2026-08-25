#!/usr/bin/env bash
set -Eeuo pipefail

# Generate and evaluate exactly one member of the frozen Qwen 1.5B comparison.
# Invoke once with RUN_LABEL=base and once with RUN_LABEL=adapter in separate,
# sequential screen sessions. All raw predictions, SQL diagnostics and official
# evaluator inputs stay outside Git.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QLORA_PYTHON="${QLORA_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
EVAL_PYTHON="${EVAL_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent/bin/python}"
RUN_LABEL="${RUN_LABEL:?set RUN_LABEL=base or RUN_LABEL=adapter}"
REUSE_GENERATION="${REUSE_GENERATION:-0}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-1}"
export CUDA_VISIBLE_DEVICES
export NCCL_P2P_DISABLE="${NCCL_P2P_DISABLE:-1}"
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export PYTHONPATH="${ROOT}/src:${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

MODEL_DIR="${MODEL_DIR:-/disk2/gengnan/data-analysis-agent-data/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b}"
ADAPTER_DIR="${ADAPTER_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-sft-smoke-v1-20260825/adapter_final}"
CASES="${CASES:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-kaggle-mirror-v1-2020-01-27/extracted/spider/dev.json}"
TABLES_JSON="${TABLES_JSON:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-kaggle-mirror-v1-2020-01-27/extracted/spider/tables.json}"
DATABASE_ROOT="${DATABASE_ROOT:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-kaggle-mirror-v1-2020-01-27/extracted/spider/database}"
EVALUATOR_ROOT="${EVALUATOR_ROOT:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/sources/test-suite-sql-eval}"
EVALUATOR_COMMIT="${EVALUATOR_COMMIT:-e97acc546ecbee8fa27fa8dbf025ef61493a876c}"
TEST_SUITE_DATABASE_ROOT="${TEST_SUITE_DATABASE_ROOT:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/test-suite-databases-official-2020-12-27/database}"
RUN_DIRECTORY="${RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-${RUN_LABEL}-spider-dev-v1-20260825}"
LOG_FILE="${RUN_DIRECTORY}/screen-run.log"

if [[ "${RUN_LABEL}" != "base" && "${RUN_LABEL}" != "adapter" ]]; then
  echo "[error] RUN_LABEL must be base or adapter" >&2
  exit 2
fi
for required_path in "${QLORA_PYTHON}" "${EVAL_PYTHON}" "${MODEL_DIR}" "${CASES}" "${TABLES_JSON}" "${DATABASE_ROOT}" "${EVALUATOR_ROOT}" "${TEST_SUITE_DATABASE_ROOT}"; do
  if [[ ! -e "${required_path}" ]]; then
    echo "[error] required path does not exist: ${required_path}" >&2
    exit 2
  fi
done
if [[ "${RUN_LABEL}" == "adapter" && ! -f "${ADAPTER_DIR}/adapter_model.safetensors" ]]; then
  echo "[error] adapter run needs adapter_model.safetensors: ${ADAPTER_DIR}" >&2
  exit 2
fi
if [[ "${REUSE_GENERATION}" != "0" && "${REUSE_GENERATION}" != "1" ]]; then
  echo "[error] REUSE_GENERATION must be 0 or 1" >&2
  exit 2
fi
if [[ -e "${RUN_DIRECTORY}/official-evaluator-evidence.json" ]]; then
  echo "[error] run directory already contains official evaluator evidence; choose a new RUN_DIRECTORY" >&2
  exit 2
fi
if [[ "${REUSE_GENERATION}" == "1" ]]; then
  for required_artifact in "${RUN_DIRECTORY}/generation_evidence.json" "${RUN_DIRECTORY}/predictions.jsonl"; do
    if [[ ! -f "${required_artifact}" ]]; then
      echo "[error] REUSE_GENERATION=1 requires ${required_artifact}" >&2
      exit 2
    fi
  done
elif [[ -e "${RUN_DIRECTORY}/generation_evidence.json" || -e "${RUN_DIRECTORY}/predictions.jsonl" ]]; then
  echo "[error] run directory already contains generation artifacts; set REUSE_GENERATION=1 or choose a new RUN_DIRECTORY" >&2
  exit 2
fi

mkdir -p "${RUN_DIRECTORY}"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[run_label] ${RUN_LABEL}"
echo "[run_directory] ${RUN_DIRECTORY}"
echo "[reuse_generation] ${REUSE_GENERATION}"
echo "[cuda_visible_devices] ${CUDA_VISIBLE_DEVICES}"
echo "[model_dir] ${MODEL_DIR}"
echo "[adapter_dir] ${ADAPTER_DIR}"
nvidia-smi --query-gpu=index,name,uuid,memory.used,memory.total --format=csv,noheader

generation_args=(
  "${QLORA_PYTHON}" "${ROOT}/scripts/generate_post_training_text_to_sql.py"
  --model-dir "${MODEL_DIR}"
  --run-label "${RUN_LABEL}"
  --cases "${CASES}"
  --tables-json "${TABLES_JSON}"
  --output-dir "${RUN_DIRECTORY}"
  --max-input-tokens 1536
  --max-new-tokens 256
  --seed 42
)
if [[ "${RUN_LABEL}" == "adapter" ]]; then
  generation_args+=(--adapter-dir "${ADAPTER_DIR}")
fi
if [[ "${REUSE_GENERATION}" == "1" ]]; then
  echo "[generation] reusing existing prediction JSONL and generation evidence"
else
  "${generation_args[@]}"
fi

"${EVAL_PYTHON}" "${ROOT}/scripts/run_sqlite_benchmark.py" \
  --dataset spider_dev \
  --cases "${CASES}" \
  --database-root "${DATABASE_ROOT}" \
  --predictions "${RUN_DIRECTORY}/predictions.jsonl" \
  --dataset-version spider-spider-1.0-kaggle-mirror-v1-2020-01-27-dev \
  --model-id qwen2.5-coder-1.5b-qlora-comparison \
  --model-version "${RUN_LABEL}" \
  --prompt-version spider-sft-schema-question-sql-v1 \
  --output "${RUN_DIRECTORY}/sqlite-diagnostics.json"

"${EVAL_PYTHON}" "${ROOT}/scripts/run_official_spider_test_suite.py" \
  --cases "${CASES}" \
  --predictions "${RUN_DIRECTORY}/predictions.jsonl" \
  --evaluator-root "${EVALUATOR_ROOT}" \
  --evaluator-commit "${EVALUATOR_COMMIT}" \
  --test-suite-database-root "${TEST_SUITE_DATABASE_ROOT}" \
  --output-directory "${RUN_DIRECTORY}/official-test-suite"

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
