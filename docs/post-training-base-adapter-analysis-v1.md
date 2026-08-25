# Initial Base/Adapter Diagnostic v1

## Scope and Evidence

This document analyzes the completed `Qwen/Qwen2.5-Coder-1.5B` Base versus
first LoRA Adapter comparison. It does not modify the production Vanna,
FastAPI, PostgreSQL or trusted Text-to-SQL runtime.

The reproducible analyzer is
[`scripts/analyze_post_training_comparison.py`](../scripts/analyze_post_training_comparison.py).
It reads the paired external SQLite diagnostic reports and emits only aggregate
counts, distributions and bounded case IDs. It never writes question text, SQL,
database/table/column identifiers, raw SQLite errors or result rows. Its first
external report is:

```text
/disk2/gengnan/data-analysis-agent-data/experiments/
qwen25coder15b-base-adapter-pair-v1-20260825/analysis-v1/safe-comparison.json
SHA-256: cbfe9e5f5b521ed2a98f491568c252f0b8c8fb59f12a4a350fdc654b07903009
```

The report consumes the already frozen Base and Adapter normalized SQLite
diagnostic reports, whose SHA-256 values are respectively
`433cf3a7a4d358547a0bfa72ad239ee594297eedf76a9ad1d17c7102315d071b` and
`346bead6b027b8fd043b1433f666daa5c3a23aad6607ce372f42794d2ba7d82a`.

This remains an internal comparison for the fixed 2020-01 Spider mirror and
Test Suite assets. It is not a current Spider leaderboard result and does not
measure production business-metric correctness.

## Transition Evidence

SQLite execution state changed as follows:

| Base state -> Adapter state | Cases | Interpretation boundary |
| --- | ---: | --- |
| executed -> executed | 578 | Still only single-database executability. |
| executed -> execution error | 228 | Concrete execution regression. |
| executed -> policy rejected | 25 | Concrete syntax/policy regression. |
| execution error -> executed | 87 | Execution recovery only; not semantic correctness. |
| policy rejected -> executed | 1 | Execution recovery only; not semantic correctness. |
| execution error -> execution error | 108 | Persistent failure. |
| execution error -> policy rejected | 4 | Failure mode changed, not improved. |
| policy rejected -> execution error | 3 | Failure mode changed, not improved. |

There are 253 previously executable candidates that became an error or policy
rejection, versus 88 candidates that became executable. The net is the observed
loss of 165 executed candidates. Across the 20 development databases, none
improved in executed-candidate count; 17 regressed and 3 were unchanged. This
is broad enough that it should not be attributed to one database snapshot.

## Error and Output Drift

| Safe category | Base | Adapter | Delta |
| --- | ---: | ---: | ---: |
| no such column | 180 | 289 | +109 |
| no such table | 5 | 46 | +41 |
| policy parse failure | 3 | 29 | +26 |
| ambiguous column | 11 | 2 | -9 |
| aggregate misuse | 3 | 2 | -1 |
| multi-statement policy rejection | 1 | 0 | -1 |

The higher-level categories hide an important shift. Base had 16 qualified
missing-column references and 164 unqualified/other ones; Adapter had 254
qualified references and 35 unqualified/other ones. Adapter parse failures
contained 21 missing-expression patterns and 8 tokenizer/quote patterns; 15 of
the 29 stopped below the 256-token cap, so truncation alone cannot explain the
new parse failures. This is evidence of broken schema/alias grounding and SQL
construction, not proof of a particular root cause.

The Adapter did improve raw completion presentation: 1,033/1,034 completions
are direct query-shaped, while the Base had 806 section continuations that the
shared normalizer safely removed. It also reached the 256-token cap only 43
times versus Base's 342. Those improvements must not be read as SQL quality:
after the same normalizer, Adapter's median candidate length is 158 characters
and p95 is 576, compared with Base's 87 and 236. The Adapter is therefore not
merely emitting shorter final SQL; its normalized queries are commonly longer
and more error-prone despite fewer raw generation tokens.

## Bounded Manual Audit

A deliberately small, non-random changed-case audit inspected three
Base-executed -> Adapter-failed cases and four error -> Adapter-executed cases
against their external Spider question/schema/gold evidence after generation
had completed. Raw text remains external and is not reproduced here.

The regression sample showed three distinct patterns: an unnecessary join with
an invalid key reference, an invented/undefined table-alias chain, and a
malformed predicate after an otherwise executable Base output. The four
apparent execution recoveries did not satisfy the requested result semantics in
this audit: they respectively used an incorrect join/grain, omitted requested
output fields, or changed the requested aggregate/measure. Thus the audit is a
useful warning that the 88 execution recoveries cannot be reported as semantic
improvements. It is not a random sample and must not be converted into a
semantic-accuracy percentage.

## What Is Confirmed vs Hypothesized

Confirmed facts:

- The adapter changed completion stop and presentation behavior substantially.
- The paired SQLite and Test Suite aggregates regressed under a fixed contract.
- The 8-step run used global batch size 4, so it processed at most 32 sample
  exposures from 102 train rows. Trainer state recorded epoch `0.313725...`;
  this was not even one full training-data pass.
- Validation loss `0.466989` was teacher-forced token loss on 26
  schema-disjoint validation rows. It did not predict dev generation quality.

Plausible but unconfirmed explanations:

- A partial data pass may have shifted LoRA weights toward a narrow subset of
  SQL patterns before the model observed enough schema diversity.
- Constant learning rate `2e-4`, rank 16 and the small sample set may be an
  unstable adaptation regime for unseen Spider schemas.
- The SFT target/EOS behavior appears to suppress Base-style prompt
  continuation, but that formatting gain may be coupled with harmful changes to
  alias and schema-linking behavior.

The available evidence does **not** isolate any one of these hypotheses. In
particular, it does not justify blaming QLoRA in general, adding more repair
attempts, patching Spider identifiers, or entering DPO/GRPO.

## Next Smallest Experiment

Keep the current Base and 8-step Adapter as immutable controls. Before scaling
data or changing the production agent, run one SFT-coverage ablation with the
same model, prompt, data split, LoRA configuration, optimizer, constant learning
rate and decode contract, changing only total training horizon from 8 to 26
optimizer steps. With effective batch size 4, 26 steps is the smallest horizon
that covers the 102 training rows approximately once.

The new adapter must again run the full 1,034-case Base/Adapter protocol and
this aggregate analyzer. Its decision gate is non-regression on SQLite and Test
Suite before any claim of semantic improvement. If it still regresses, stop
hyperparameter scaling and audit/expand training-data diversity and prompt
targets under the existing holdout protocol. DPO/GRPO remains out of scope until
an SFT configuration can at least avoid this controlled regression and there is
trustworthy semantic or execution-feedback supervision.
