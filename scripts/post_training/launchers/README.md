# Post-Training Launchers

This directory contains the supported `screen` entry points for long-running
offline post-training work. Models, checkpoints, raw candidates, database rows
and logs remain outside Git under `/disk2/gengnan/data-analysis-agent-data/`.

## Current Multi-Schema Evaluation Protocol

New Olist + TheLook Base/Adapter evaluations must use one of these launchers:

| Model family | Supported launcher | Terminal output |
| --- | --- | --- |
| Qwen2.5-Coder-1.5B bf16 LoRA | `start_qwen25_multischema_v3_1_matching_evaluation_screen.sh` | `terminal-evaluation-summary.json` |

Every supported run has the same required sequence:

```text
Olist Base governed generation
-> Olist Adapter governed generation
-> Olist runtime comparison
-> Olist post-generation Gold denotation
-> TheLook Base generation
-> TheLook Adapter generation
-> TheLook post-generation Gold denotation
-> cross-suite terminal summary
-> completed marker
```

The final marker is never written after generation alone. A missing Olist or
TheLook Gold report makes the launcher fail before completion. Olist
`ResultContract valid` and Gold denotation are deliberately reported as
different metrics; the former is not semantic accuracy.

Historical Olist medium/release-v2 and TheLook v1/v2 launchers have been
removed from this directory. Their immutable external reports remain evidence
for the old experiments, but they are not supported entry points for new runs.

Do not launch a training or full evaluation run without completing the
learning-review gate in `AGENTS.md` and explicitly confirming the experiment.
New final-test launchers must require an existing `adapter_best` selected using
the frozen validation rule. `adapter_final` is a diagnostic artifact only and
must never be the default final-test target.
