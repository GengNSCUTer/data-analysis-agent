# Olist English Candidate-Prompt Transfer v1

## Purpose

This experiment tests whether changing only the natural-language question seen
by the offline candidate SQL generator changes the Olist transfer outcome of
the frozen Spider SFT v2 bf16 LoRA adapter. It does not test end-to-end English
workspace support and does not alter the Vanna/SiliconFlow default.

## Controlled Contract

The experiment selects the same twelve `answerable` database cases from the
permanent `post_training_holdout_v1.yaml`. The current Chinese source question
continues to construct the server-owned Semantic Catalog selection,
QuestionRouter decision, WorkingMemory, QueryPlan, ResultContract and SQL
audit metadata. Only `CandidateSqlContext.question` is replaced with a
versioned external English overlay.

The matching Chinese refresh and English run both used:

- `Qwen/Qwen2.5-Coder-1.5B` revision
  `df3ce67c0e24480f20468b6ef2894622d69eb73b`;
- the same bf16 Base and frozen Spider SFT v2 LoRA adapter;
- greedy decoding (`seed=42`, `max_new_tokens=256`), prompt version
  `olist-candidate-sql-v1`, PostgreSQL, no SQL repair and the same twelve IDs;
- current `SqlPolicy`, PostgreSQL readonly role, timeout/row limits and
  `ResultValidator`/`ResultContract` boundaries;
- one refreshed run per language on 2026-09-01 using logical CUDA `0`, mapped
  to physical `nvidia-smi` GPU `2` (RTX 4090).

The English overlay is external to Git and SHA-256 pinned in
`post_training_olist_business_adapter_english_prompt_v1.yaml` as
`e36539fa3999a2c28d3054690156e77f37c2a9e127c3036ea161a30f073333fd`.
Raw questions, candidate SQL, result rows and complete logs remain external.

## Separate Locale Preflight

As a distinct product-layer check, the English questions were also supplied
directly to the current Catalog and Router without loading either model. Only
8/12 were classified as answerable database requests, 4/12 preserved the
Chinese grounding metric set, and 8/12 preserved the requested-dimension count.
The current Catalog aliases and Router rules are primarily Chinese. This is a
multilingual semantic-layer compatibility finding, not evidence about the LoRA
adapter, which is why it is not part of the candidate-generator comparison.

## Fresh Base/Adapter Results

| Model condition | Candidate language | Generated | SqlPolicy accepted | PostgreSQL executed | ResultContract valid |
| --- | --- | ---: | ---: | ---: | ---: |
| bf16 Base | Chinese source question | 12/12 | 6 | 4 | 2 |
| bf16 Base | English prompt-only overlay | 12/12 | 6 | 2 | 1 |
| bf16 LoRA Adapter | Chinese source question | 12/12 | 6 | 2 | 0 |
| bf16 LoRA Adapter | English prompt-only overlay | 12/12 | 4 | 2 | 0 |

For the Base, English leaves generation and policy acceptance unchanged but
reduces executed candidates by two and contract-valid candidates by one. For
the Adapter, English reduces policy acceptance by two and leaves executed and
contract-valid outcomes unchanged at two and zero. Under neither language does
the Adapter produce a contract-valid candidate, so it remains excluded from the
production candidate-generator path.

## Evidence and Boundaries

The fresh cross-language safe-comparison SHA-256 values are:

- Base Chinese-to-English: `c304b6bb497c899ae885d4002884c91695289ad8feead3f7cdafce0be78bd1db`.
- Adapter Chinese-to-English: `7edc3d23fb6f4f0bdbb762ef85cb7869a845db8288f5b8e366b10542ec645440`.

`ResultContract valid` demonstrates that the returned columns and result-level
contract passed for this request. It is not a general business-semantic accuracy
score, does not establish production reliability, and does not show that the
English Catalog/Router path is ready. The small protected sample is a transfer
gate, not a benchmark ranking.

## Decision

Keep the current production default unchanged. Treat multilingual support as a
separate product capability: add bilingual metric/dimension aliases and
English-aware deterministic routing, then evaluate full end-to-end English
workspaces with an independently protected suite. Do not use this experiment
to claim that the current Spider LoRA adapter benefits English Olist SQL
generation.
