# QLoRA/SFT Environment

## Purpose

`data-analysis-agent-qlora` is an isolated Conda environment for local
Text-to-SQL QLoRA/SFT experiments. It is deliberately separate from the Python
3.12 PostgreSQL + Vanna runtime, so training dependencies, CUDA packages and
future model libraries cannot modify the running product environment.

The initial authorization gate has now been completed for one external Spider
train-only engineering smoke. It may use the Spider train targets outside Git
after the documented provenance, execution, split and holdout checks. It must
not use Spider dev gold SQL or the protected v2 holdout for SFT, LoRA, DPO,
GRPO, prompt examples or synthetic rewrites.

## Frozen Compatibility Decision

The host reports NVIDIA driver `535.54.03` and CUDA capability `12.2`. The
environment therefore uses Python 3.11 and the PyTorch CUDA 12.1 wheel
`torch==2.5.1+cu121`, rather than CUDA 12.4 wheels that require a newer shared
driver. The direct training-library versions are in
[`infra/post_training/requirements-qlora-v1.in`](../infra/post_training/requirements-qlora-v1.in);
the full environment contract is
[`evals/manifests/qlora_environment_v1.yaml`](../evals/manifests/qlora_environment_v1.yaml).

Future GPU processes must set `CUDA_VISIBLE_DEVICES=1`, which is logical CUDA
device `1` and physical `nvidia-smi` GPU `3` (RTX 4090) on this host. Each task
must recheck occupancy immediately before launch. This convention does not grant
permission to interrupt an existing process on any GPU.

## Planned Validation

After the environment is created, verify the Python version, installed direct
dependencies, `pip check`, and a minimal CUDA visibility probe. The probe must
not load a model or run training. Record the exact resulting versions and any
known CUDA-library limitations before adding data or a base model.

## Bootstrap Result

The environment was created at
`/disk2/gengnan/conda_envs/data-analysis-agent-qlora` on 2026-08-24. Its direct
requirements are frozen in
[`requirements-qlora-v1.in`](../infra/post_training/requirements-qlora-v1.in)
and its resolved pip packages are frozen in
[`requirements-qlora-v1.lock`](../infra/post_training/requirements-qlora-v1.lock).
The pip install log is an external operational artifact at
`/disk2/gengnan/data-analysis-agent-data/experiments/qlora-environment-v1-20260824/`.

| Check | Result |
| --- | --- |
| Python | `3.11.15` |
| PyTorch | `2.5.1+cu121` |
| accelerate / bitsandbytes | `1.3.0` / `0.45.1` |
| transformers / PEFT / TRL | `4.48.3` / `0.14.0` / `0.15.2` |
| datasets | `3.2.0` |
| pytest / pytest-asyncio | `9.1.1` / `1.4.0` |
| `pip check` | passed |
| CUDA availability | true |
| Visible CUDA devices | 1 |
| Process-local device `cuda:0` | RTX 4090, UUID `GPU-10863af0-8588-7625-5609-640ba794f64b` |
| Resolved physical GPU | `nvidia-smi` GPU `3`, selected via logical `CUDA_VISIBLE_DEVICES=1` |

The probe queried device metadata only. It did not load model weights, allocate
a training batch or start a training process. The temporary CUDA context exited
after validation; no long-running QLoRA process remains.

`pytest` and `pytest-asyncio` are deliberately frozen as training-environment
test dependencies. The repository's pytest configuration loads the async
plugin even though the QLoRA dataset tests are synchronous; recording both
packages avoids a misleading situation where those tests only run with
`--noconftest` on one developer machine.

## Training Boundary

Every follow-up experiment must use an explicitly licensed base model and a
separately audited corpus following
[`docs/post-training-data-protocol.md`](post-training-data-protocol.md). The 60 cases in
[`evals/manifests/post_training_holdout_v1.yaml`](../evals/manifests/post_training_holdout_v1.yaml)
remain permanently excluded from SFT, LoRA, preference optimization, prompt
examples and synthetic rewrites.

## First QLoRA Forward Smoke

On 2026-08-25, the environment completed a **forward-only** smoke using the
Apache-2.0 `Qwen/Qwen2.5-Coder-1.5B` revision
`df3ce67c0e24480f20468b6ef2894622d69eb73b`. The model and its per-file SHA-256
manifest remain outside Git under
`/disk2/gengnan/data-analysis-agent-data/models/`; the 128 Spider train-only
candidates and their hash remain under the external experiments directory.

The smoke selected one execution-checked train candidate, tokenized its schema,
question and target SQL into 172 tokens, masked all 125 prompt tokens, and kept
47 SQL/EOS tokens supervised. It loaded frozen base weights in 4-bit NF4 with
double quantization and bf16 compute, then attached LoRA (`r=16`, `alpha=32`,
dropout `0.05`) to attention and MLP projections. The adapter exposed
18,464,768 of 907,081,216 parameters (2.035625%). The forward loss was finite
at `0.884486`; peak allocated GPU memory was 2,085,800,448 bytes on logical
CUDA `1` / physical GPU `3` (RTX 4090).

No `backward()`, optimizer update, checkpoint write or adapter save occurred.
The reproducible contract is
[`evals/manifests/post_training_forward_smoke_v1.yaml`](../evals/manifests/post_training_forward_smoke_v1.yaml).
This validates loading, label masking and memory feasibility for a minimal
batch; it does not establish training quality or a post-training improvement.

## First QLoRA SFT Smoke

The first actual adapter optimization ran on 2026-08-25, after a deterministic
external split of 128 execution-checked Spider train candidates. The split uses
Spider `db_id` as the primary group, so one database schema cannot occur in both
sets: 102 train rows across 66 schemas and 26 validation rows across 19 schemas.
The normalized SQL-shape intersection was also empty. The v2 60-case project
holdout was not read or used.

The single RTX 4090 run used Qwen 1.5B base weights frozen in 4-bit NF4,
LoRA `r=16/alpha=32/dropout=0.05`, bf16 compute, `batch_size=1`, gradient
accumulation 4, `max_seq_length=1536`, and eight optimizer steps. Its peak
allocated/reserved memory was 3,557,137,920 / 4,003,463,168 bytes. It saved a
74 MB LoRA adapter and an external resumable checkpoint; a fresh PEFT reload of
the adapter on a validation sample returned a finite loss. RTX 40-series
Accelerate initialization requires `NCCL_P2P_DISABLE=1` and
`NCCL_IB_DISABLE=1` even for this single-GPU process; the runner sets and
records them explicitly.

The recorded train loss `0.556203` and validation loss `0.466989` are average
next-token cross-entropy over this tiny, short run. They are useful to detect
broken labels, NaNs or OOM, but cannot be called SQL Execution Accuracy, Test
Suite Accuracy, semantic correctness or a baseline improvement. The full
evidence is in
[`evals/manifests/post_training_sft_smoke_v1.yaml`](../evals/manifests/post_training_sft_smoke_v1.yaml).

The external evidence can be rechecked without printing raw training rows or
SQL by running `python scripts/verify_post_training_sft_artifacts.py` from the
repository root in the QLoRA environment. The verifier checks the split audit,
both JSONL files, SFT evidence, adapter and reload evidence against the
manifest, and rejects any artifact placed under the Git working tree.
