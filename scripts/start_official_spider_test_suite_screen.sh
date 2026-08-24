#!/usr/bin/env bash
set -Eeuo pipefail

# Run the full official bridge outside Git. Override variables only when using
# another frozen prediction/evaluator release; do not point output into the repo.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/disk2/gengnan/conda_envs/data-analysis-agent/bin/python}"
CASES="${CASES:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/spider/spider-1.0-kaggle-mirror-v1-2020-01-27/extracted/spider/dev.json}"
PREDICTIONS="${PREDICTIONS:-/disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-full-v1-20260824/predictions.jsonl}"
EVALUATOR_ROOT="${EVALUATOR_ROOT:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/sources/test-suite-sql-eval}"
EVALUATOR_COMMIT="${EVALUATOR_COMMIT:-e97acc546ecbee8fa27fa8dbf025ef61493a876c}"
TEST_SUITE_DATABASE_ROOT="${TEST_SUITE_DATABASE_ROOT:-/disk2/gengnan/data-analysis-agent-data/text-to-sql/test-suite-databases-official-2020-12-27/database}"
RUN_DIRECTORY="${RUN_DIRECTORY:-/disk2/gengnan/data-analysis-agent-data/experiments/spider-qwen25coder3b-test-suite-v1-20260824}"
LOG_FILE="${RUN_DIRECTORY}/screen-run.log"

# Keep the pinned evaluator checkout clean: its imports otherwise create
# __pycache__ files next to the external source tree.
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

mkdir -p "${RUN_DIRECTORY}"
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "[start] $(date --iso-8601=seconds)"
echo "[run_directory] ${RUN_DIRECTORY}"
echo "[cases] ${CASES}"
echo "[predictions] ${PREDICTIONS}"
echo "[evaluator_root] ${EVALUATOR_ROOT}"
echo "[evaluator_commit] ${EVALUATOR_COMMIT}"
echo "[test_suite_database_root] ${TEST_SUITE_DATABASE_ROOT}"

for required_path in "${CASES}" "${PREDICTIONS}" "${EVALUATOR_ROOT}" "${TEST_SUITE_DATABASE_ROOT}"; do
  if [[ ! -e "${required_path}" ]]; then
    echo "[error] required path does not exist: ${required_path}" >&2
    exit 2
  fi
done

"${PYTHON}" - <<'PY'
from nltk import word_tokenize

assert word_tokenize("SELECT COUNT(*) FROM items")
PY
echo "[nltk] tokenizer smoke passed"

if [[ -e "${RUN_DIRECTORY}/gold.txt" || -e "${RUN_DIRECTORY}/predictions.txt" || \
      -e "${RUN_DIRECTORY}/official-evaluator-output.txt" || \
      -e "${RUN_DIRECTORY}/official-evaluator-evidence.json" ]]; then
  echo "[error] run directory already contains evaluator artifacts; choose a new RUN_DIRECTORY" >&2
  exit 2
fi

set +e
"${PYTHON}" "${ROOT}/scripts/run_official_spider_test_suite.py" \
  --cases "${CASES}" \
  --predictions "${PREDICTIONS}" \
  --evaluator-root "${EVALUATOR_ROOT}" \
  --evaluator-commit "${EVALUATOR_COMMIT}" \
  --test-suite-database-root "${TEST_SUITE_DATABASE_ROOT}" \
  --output-directory "${RUN_DIRECTORY}"
status=$?
set -e

echo "[exit_code] ${status}"
echo "[finish] $(date --iso-8601=seconds)"
exit "${status}"
