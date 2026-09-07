#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${TRAIN_PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python}"
export PYTHONPATH="${ROOT}:${PYTHONPATH:-}"

EVAL_DIR="${EVAL_DIR:-/disk2/gengnan/data-analysis-agent-data/evals/olist-domain-sft-medium-v1}"
SPLIT_DIR="${SPLIT_DIR:-${EVAL_DIR}/sft-splits-length3072-v1}"
RUN_DIR="${RUN_DIR:-/disk2/gengnan/data-analysis-agent-data/experiments/qwen25coder15b-olist-medium-base-adapter-finaltest-v1-20260906}"
OUTPUT="${OUTPUT:-${RUN_DIR}/analysis/gold-denotation-audit.json}"

TEST_JSONL="${TEST_JSONL:-${SPLIT_DIR}/final_evaluation_only/in_domain_test.jsonl}"
RUNTIME_CANDIDATES="${RUNTIME_CANDIDATES:-${EVAL_DIR}/runtime-prompts-20260904-v1/runtime_candidates.jsonl}"
SPLIT_AUDIT="${SPLIT_AUDIT:-${SPLIT_DIR}/split_audit.json}"

for required_path in "${PYTHON}" "${TEST_JSONL}" "${RUNTIME_CANDIDATES}" "${SPLIT_AUDIT}" \
  "${RUN_DIR}/base/safe-report.json" "${RUN_DIR}/adapter/safe-report.json" \
  "${RUN_DIR}/base/raw-candidates.jsonl" "${RUN_DIR}/adapter/raw-candidates.jsonl" \
  "${RUN_DIR}/completed"; do
  [[ -e "${required_path}" ]] || { echo "[error] required path does not exist: ${required_path}" >&2; exit 2; }
done
[[ ! -e "${OUTPUT}" ]] || { echo "[error] output already exists: ${OUTPUT}" >&2; exit 2; }

mkdir -p "$(dirname "${OUTPUT}")"
exec > >(tee -a "${RUN_DIR}/gold-denotation-audit-screen.log") 2>&1
echo "[start] $(date --iso-8601=seconds)"
echo "[experiment] olist_medium_gold_denotation_audit_v1"

"${PYTHON}" -m scripts.post_training.evaluation.run_olist_medium_gold_denotation_audit \
  --test-jsonl "${TEST_JSONL}" \
  --runtime-candidates "${RUNTIME_CANDIDATES}" \
  --split-audit "${SPLIT_AUDIT}" \
  --base-safe-report "${RUN_DIR}/base/safe-report.json" \
  --adapter-safe-report "${RUN_DIR}/adapter/safe-report.json" \
  --base-raw-candidates "${RUN_DIR}/base/raw-candidates.jsonl" \
  --adapter-raw-candidates "${RUN_DIR}/adapter/raw-candidates.jsonl" \
  --completed-marker "${RUN_DIR}/completed" \
  --output "${OUTPUT}"

echo "[exit_code] 0"
echo "[finish] $(date --iso-8601=seconds)"
