# Post-Training Launchers

These launchers own long-running, detached `screen` workflows for offline
post-training experiments. They expect all models, raw data, checkpoints and
logs outside the Git working tree. The root-level `scripts/start_*.sh` files
remain compatibility entry points for historical commands and documents.

Do not launch a training or full evaluation run without first completing the
learning-review gate in `AGENTS.md` and explicitly confirming the experiment.

`start_post_training_cspider_base_adapter_evaluation_screen.sh` is the CSpider
validation-only pair launcher. It runs Base then Adapter sequentially on the
same guarded GPU, requires the matching-generation verifier before SQLite
diagnostics, and intentionally never invokes Spider Test Suite or CSpider final
test assets.
