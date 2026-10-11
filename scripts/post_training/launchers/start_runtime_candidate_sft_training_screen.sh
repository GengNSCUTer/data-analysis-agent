#!/usr/bin/env bash
# Frozen runtime-candidate v2, fresh Base -> two epochs; no final-test reads.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TRAIN_PYTHON=/disk2/gengnan/conda_envs/data-analysis-agent-qlora/bin/python
DATA_ROOT=/disk2/gengnan/data-analysis-agent-data
RELEASE_DIR="${DATA_ROOT}/evals/runtime-candidate-sql-v2-r1"
MODEL_DIR="${DATA_ROOT}/models/qwen2.5-coder-1.5b-base-df3ce67c0e24480f20468b6ef2894622d69eb73b"
SMOKE="${DATA_ROOT}/experiments/runtime-candidate-sft-smoke-20261011-b2-r1/sft_smoke.json"
RUN_DIR="${DATA_ROOT}/experiments/runtime-candidate-qwen25coder15b-bf16-lora-2epoch-20261011-r1"
LOG="${DATA_ROOT}/evals/runtime-candidate-qwen25coder15b-sft-20261011-r1.log"
LAUNCH_MANIFEST="${DATA_ROOT}/evals/runtime-candidate-qwen25coder15b-sft-20261011-r1-launch.json"
EXIT_MANIFEST="${DATA_ROOT}/evals/runtime-candidate-qwen25coder15b-sft-20261011-r1-exit.json"
SESSION=daa-runtime-sft-20261011-r1
GPU_UUID=GPU-10863af0-8588-7625-5609-640ba794f64b
RELEASE_SHA=5a850f34cbc2e3dd36d770ea9203e0cf7e1cda0ef4b7d73351c4b47138ff904d

COMMAND=("${TRAIN_PYTHON}" -m scripts.post_training.training.run_post_training_sft_smoke
  --model-dir "${MODEL_DIR}"
  --train-jsonl "${RELEASE_DIR}/train.jsonl"
  --validation-jsonl "${RELEASE_DIR}/validation.jsonl"
  --split-audit "${RELEASE_DIR}/manifest.json"
  --expected-release-manifest-sha256 "${RELEASE_SHA}"
  --output-dir "${RUN_DIR}" --max-seq-length 4096 --num-train-epochs 2
  --seed 20261011 --learning-rate 0.0001 --weight-decay 0.01
  --per-device-train-batch-size 2 --gradient-accumulation-steps 2
  --per-device-eval-batch-size 2
  --evaluation-steps 250 --save-steps 250 --logging-steps 10
  --lora-r 16 --lora-alpha 32 --lora-dropout 0.05 --base-weight-mode bf16_lora
  --physical-nvidia-smi-device 3 --expected-gpu-uuid "${GPU_UUID}"
  --experiment-label runtime_candidate_qwen25coder15b_bf16_lora_2epoch_20261011_r1)

# Read-only command inspection also lets tests verify the actual launched args.
if [[ "${1:-}" == --print-command ]]; then
  printf '%s\n' "${COMMAND[@]}"
  exit 0
fi
[[ $# == 0 || ( $# == 1 && "$1" == --worker ) ]] || { echo "usage: bash $0 [--print-command]" >&2; exit 2; }

expect_hash() {
  local actual unused
  read -r actual unused < <(sha256sum "$1")
  [[ "${actual}" == "$2" ]] || { echo "[error] hash drift: $1" >&2; exit 2; }
}
expect_hash "${RELEASE_DIR}/manifest.json" "${RELEASE_SHA}"
expect_hash "${SMOKE}" 79854c528ed8ffad6d34431f17228e2b428c10217bec0a35bfe58d418bb5fee2
expect_hash "${MODEL_DIR}/download_manifest.json" 484d1e01b35f19c6c764d2801239c20ca17da4270a76869825bffc08022829c1

[[ ! -e "${RUN_DIR}" && ! -e "${LAUNCH_MANIFEST}" && ! -e "${EXIT_MANIFEST}" ]] || {
  echo "[error] formal-run artifacts already exist; refusing duplicate launch or overwrite" >&2; exit 2;
}
if [[ "${1:-}" != --worker ]]; then
  [[ ! -e "${LOG}" ]] || { echo "[error] launch log already exists" >&2; exit 2; }
  screen -L -Logfile "${LOG}" -dmS "${SESSION}" bash "${BASH_SOURCE[0]}" --worker
  echo "[screen] ${SESSION}"
  echo "[log] ${LOG}"
  echo "[output] ${RUN_DIR}"
  exit 0
fi

# Recheck GPU inside screen just before the training process. Logical CUDA 1
# maps to physical nvidia-smi 3. Never stop another process to make room.
GPU_STATE="$(nvidia-smi -i "${GPU_UUID}" --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits)"
IFS=, read -r GPU_MEMORY GPU_UTILIZATION <<< "${GPU_STATE}"
if (( GPU_MEMORY > 1024 || GPU_UTILIZATION > 10 )); then
  echo "[error] physical GPU 3 is busy: ${GPU_STATE}" >&2; exit 2;
fi
export CUDA_VISIBLE_DEVICES=1
export PYTHONPATH="${ROOT}/src:${ROOT}"
export PYTHONUNBUFFERED=1
export TOKENIZERS_PARALLELISM=false
export NCCL_P2P_DISABLE=1
export NCCL_IB_DISABLE=1
cd "${ROOT}"

# Save the exact argv and code hashes before training; no raw samples/secrets.
"${TRAIN_PYTHON}" - "${LAUNCH_MANIFEST}" "${BASH_SOURCE[0]}" "${SMOKE}" "${COMMAND[@]}" <<'PY'
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

launcher = Path(sys.argv[2]).resolve()
trainer = Path('scripts/post_training/training/run_post_training_sft_smoke.py').resolve()
payload = {
    'status': 'launching', 'started_at': datetime.now(timezone.utc).isoformat(),
    'argv': sys.argv[4:], 'cuda_visible_devices': os.environ['CUDA_VISIBLE_DEVICES'],
    'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
    'code_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (launcher, trainer)},
    'smoke_evidence': sys.argv[3], 'expected_optimizer_steps': 2990,
    'final_test_read': False, 'smoke_adapter_used_for_training': False,
}
with Path(sys.argv[1]).open('x', encoding='utf-8') as handle:
    json.dump(payload, handle, ensure_ascii=False, indent=2)
    handle.write('\n')
print(json.dumps(payload, ensure_ascii=False), flush=True)
PY

set +e
"${COMMAND[@]}"
EXIT_CODE=$?
set -e
"${TRAIN_PYTHON}" - "${EXIT_MANIFEST}" "${EXIT_CODE}" <<'PY'
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

payload = {'exit_code': int(sys.argv[2]), 'finished_at': datetime.now(timezone.utc).isoformat()}
with Path(sys.argv[1]).open('x', encoding='utf-8') as handle:
    json.dump(payload, handle, indent=2)
    handle.write('\n')
print(json.dumps(payload), flush=True)
PY
echo "[finish] $(date --iso-8601=seconds); exit ${EXIT_CODE}"
exit "${EXIT_CODE}"
