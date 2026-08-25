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

### Candidate Normalization

The generated JSONL retains the raw completion for audit. Before either the
read-only SQLite diagnostic or the official bridge consumes it, both runs apply
the same conservative candidate normalizer. It removes only unambiguous
presentation wrappers: an opening SQL code fence, `SQLQuery:`/`SQL:` prefix,
and a later Markdown heading such as `### Answer` or `### Explanation`. It does
not repair SQL syntax, table or column names, joins, predicates, literals, or
aggregation semantics. A malformed normalized candidate is still recorded as a
policy rejection or evaluator error.

This is part of the fixed evaluation input contract, not a post-hoc quality
repair. The already generated Base raw JSONL and the future Adapter raw JSONL
are normalized by exactly the same code immediately before their two
evaluators. For the official evaluator's one-line input format, legitimate SQL
`--` line comments are converted to equivalent block comments before newline
folding, so their comment scope is not broadened. Model answer tables and
explanations are removed before this step rather than misclassified as SQL
comments.

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

### Recovery After Generation

`generation_evidence.json` and the complete prediction JSONL are immutable
inputs once a member's generation has completed. If an interruption occurs
after those two artifacts exist but before SQLite diagnostics or the Test Suite
bridge finishes, do not regenerate the member. Resume the pair explicitly with
`BASE_REUSE_GENERATION=1`; the base member verifies that both artifacts exist,
records the reuse decision in its external log, then reruns diagnostics and the
official bridge before the adapter member begins.

```bash
cd /disk2/gengnan/data-analysis-agent
BASE_REUSE_GENERATION=1 screen -dmS daa-qwen15b-base-adapter-recovery-v1 \
  bash scripts/start_post_training_base_adapter_comparison_pair_screen.sh
```

The recovery mode is deliberately opt-in. It rejects a missing generation
artifact, an invalid reuse flag, or a directory that already has official
evaluator evidence. This preserves the original base/adapter comparison
contract and avoids treating a partially written run as complete. A malformed
model SQL candidate is a policy-rejected data point, not a batch-level server
failure: the SQLite diagnostic runner records it and continues evaluating later
cases.

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

## Base Recovery Result

The Base raw prediction JSONL completed before the recovery work and remains
unchanged: 1,034/1,034 ordered primary candidates, SHA-256
`8978b8ab0734069f4975810ba19e6221db4cfeb2545e357778f12cfcca29cc0e`,
128,957 generated tokens, and 4,631,685 ms generation time on logical CUDA `1`
/ physical GPU `3` (RTX 4090). The initial unnormalized diagnostic is retained
outside Git as a pre-normalization audit artifact with SHA-256
`320f68dee858dd71409998ab03c555bdb5521a9dbc1115c448957e0b37a101ef`; it is
not comparable to the Adapter because Markdown answer sections were still
being treated as part of the SQL candidate.

Under the frozen shared normalizer, Base SQLite diagnostics have complete
coverage: 831 executed, 4 policy rejected, 199 execution errors and 0 timeouts.
The normalized report SHA-256 is
`433cf3a7a4d358547a0bfa72ad239ee594297eedf76a9ad1d17c7102315d071b`.
The four policy rejections are genuine malformed or non-SQL candidates; the
dominant execution failures are unresolved schema links such as nonexistent
columns, not server faults.

The unmodified pinned Test Suite evaluator completed all 1,034 cases. Its raw
internal output reports execution values `0.748 / 0.448 / 0.241 / 0.094 / 0.427`
for easy / medium / hard / extra / all. Evidence SHA-256 values are prediction
input `207585672ec91bd9aa455f525a3e1d89736d9415fd77ba88679178a5315936d9` and raw
output `ddec7e34daee62b2f5bbe767407cde979c9f4e0bc5a168a1ca9de95b10b39f5e`.
This is an internal result for the frozen 2020-01 mirror and Test Suite asset
combination, not a current official Spider leaderboard result. It must not be
compared with the 831 SQLite executions as though they were the same metric.
The Adapter generation is running under the same normalizer and evaluator
contract; no QLoRA quality conclusion is possible until it completes.

## Learning Checkpoint

For an interview, describe this as a controlled ablation. The independent
variable is adapter loading; controlled variables are base revision, prompt,
decode, data, database snapshot and evaluator. The dependent variables include
execution diagnostics, Test Suite result, latency and manually reviewed failure
categories. This is stronger than saying “validation loss fell,” because it
tests behavior at generation time rather than only teacher-forced target-token
fit.
