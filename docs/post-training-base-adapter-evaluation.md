# Qwen 1.5B Base vs Adapter Evaluation Protocol

## Goal

The first QLoRA SFT smoke showed that the data, optimizer, checkpoint and
adapter-reload path works. It did not show that the adapter produces better
Text-to-SQL. This protocol is the next gate: compare the frozen
`Qwen/Qwen2.5-Coder-1.5B` base with the saved LoRA adapter while changing only
whether the adapter is loaded.

This is an offline SQLite research evaluation. The product remains Vanna plus
PostgreSQL with the server-owned Catalog, AST Policy, reader role,
ResultContract, ResultValidator and ChartContract. Neither model receives a
production credential or makes a safety decision.

## Frozen Comparison Contract

Both runs must use the following invariant contract:

| Dimension | Frozen value |
| --- | --- |
| Base weights | `Qwen/Qwen2.5-Coder-1.5B`, revision `df3ce67c0e24480f20468b6ef2894622d69eb73b` |
| Model loading | 4-bit NF4, double quantization, bf16 compute, single visible RTX 4090 |
| Adapter difference | Base run has no adapter; adapter run loads only the 74 MB LoRA adapter from the SFT smoke |
| Prompt | `spider-sft-schema-question-sql-v1`, shared by corpus construction and inference |
| Inputs | Native ordered Spider dev question plus table metadata; schema DDL values only, no database rows |
| Decode | Greedy (`do_sample=false`, `num_beams=1`), seed 42, max input 1536, max new tokens 256 |
| Candidate contract | Exactly one external `candidate_index=0` JSONL record per `spider_dev:00000` through `spider_dev:01033` |
| Diagnostic | Same read-only SQLite policy, 5-second statement limit and 1,000-row cap |
| Official boundary | Same unmodified Test Suite evaluator commit and test-suite database root |

The generator deliberately reads only `db_id` and `question` from Spider dev
for inference. It does not inspect its `query` field. Gold SQL is read only
after all predictions are complete, inside the existing official evaluator
bridge, where it is written outside Git and represented in evidence only by
hashes.

## Why This Is Fair

The SFT candidate builder and the inference runner call the same
`spider_sft_format` module. That module renders the `### SQLite schema`,
`### Question` and `### SQL` boundary exactly once. The comparison therefore
does not mix a training template with a different inference prompt. Loading the
LoRA adapter is the intended independent variable; model revision, quantization,
inputs, order, decoding and evaluators are controlled variables.

The prior Ollama `qwen2.5-coder:3b` internal reference remains useful as a
separate historical baseline, but it cannot be used as the direct SFT effect
comparison because it differs in model size, weight distribution, inference
engine and prompt contract.

## Run Procedure

Run the base and adapter jobs sequentially on logical CUDA `1`, physical
`nvidia-smi` GPU `3`. Check `nvidia-smi` immediately before launch and do not
interrupt other processes. The pair driver creates distinct external directories
and executes base generation, diagnostics and the Test Suite bridge before it
starts the adapter member; the two models therefore never contend for the same
GPU.

```bash
cd /disk2/gengnan/data-analysis-agent
screen -dmS daa-qwen15b-base-adapter-v1 \
  bash scripts/start_post_training_base_adapter_comparison_pair_screen.sh
```

The pair driver stops before adapter generation if the base member fails. The
scripts record launch parameters and `nvidia-smi` summary in external logs; they
reject output directories that already contain final evidence, preventing an
accidental blend of two runs.

## Preflight Result

The 1,034 native dev prompts were tokenized with the frozen Qwen tokenizer
before launch. The longest is 365 tokens, so none exceed the 1,536-token input
budget. A two-case controlled smoke also completed for both members. Under the
same 256-token generation limit, the base generated 512 tokens in total and
its two candidates were rejected by the SQLite policy; the adapter generated 12
tokens and both candidates executed under that same policy. This verifies the
comparison plumbing and confirms that the adapter changes generation behavior.
It does not measure Text-to-SQL accuracy: two cases cannot establish a general
quality delta, and execution remains weaker than SQL intent or semantic
correctness.

## What To Compare

Record each run's prediction coverage, JSONL SHA-256, generated token count,
generation time, peak memory, SQLite policy/execution categories and raw
unmodified Test Suite output hash. Compare the same error categories, especially
`no_such_column`, `no_such_table`, `ambiguous_column`, policy rejection and
timeout. If Test Suite output is structurally comparable, report it only as an
internal result for the selected 2020-01 Spider mirror and its fixed asset
combination, not as a current official leaderboard score.

Do not infer quality from SFT loss. A meaningful conclusion needs a complete
base/adapter delta plus manual inspection of a sampled set of changed outcomes:
did the adapter improve schema linking and preserve SQL semantics, or merely
make more queries executable? Execution alone does not validate SQL intent,
join multiplicity, aggregation grain or business metric meaning.

## Learning Checkpoint

For an interview, describe this as a controlled ablation. The independent
variable is adapter loading; controlled variables are base revision, prompt,
decode, data, database snapshot and evaluator. The dependent variables include
execution diagnostics, Test Suite result, latency and manually reviewed failure
categories. This is stronger than saying “validation loss fell,” because it
tests behavior at generation time rather than only teacher-forced target-token
fit.
