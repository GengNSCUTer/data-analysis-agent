"""Training contract tests; the runtime environment deliberately has no torch."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")

from scripts.post_training.training.run_post_training_sft_smoke import (
    CausalSqlCollator,
    CausalSqlDataset,
    FinalAdapterSnapshotCallback,
    RUNTIME_PROMPT_VERSION,
    RUNTIME_RELEASE_VERSION,
    load_rows,
    required_max_seq_length,
    select_smoke_rows,
    sha256_file,
    split_prompt_and_target,
    validate_split_audit,
)


class CharacterTokenizer:
    eos_token_id = 999
    pad_token_id = 999

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [ord(char) for char in text]}


def sample(split="train", workspace="olist", index=0):
    prompt = "### Question\n统计订单数。\n### SQL"
    sql = f"SELECT {index};"
    return {
        "sample_id": f"{workspace}-{split}-{index}",
        "split": {"name": split},
        "workspace_key": workspace,
        "prompt_format_version": RUNTIME_PROMPT_VERSION,
        "rendered_prompt": prompt,
        "candidate_sql": sql,
        "training_text": prompt + "\n" + sql,
        "token_length": len(prompt + "\n" + sql) + 1,
        "admission_status": "admitted",
        "training_length_status": "within_frozen_contract",
        "execution_evidence": {
            "transaction_read_only": True, "policy_state": "allowed",
            "query_plan_guard": "passed", "result_state": "valid",
            "execution_truncated": False, "denotation": "ordered_denotation_match",
        },
    }


@pytest.fixture
def release(tmp_path):
    paths = []
    for split in ("train", "validation"):
        path = tmp_path / f"{split}.jsonl"
        path.write_text(json.dumps(sample(split), ensure_ascii=False) + "\n")
        paths.append(path)
    asset = "src/data_analysis_agent/candidate_sql_generator.py"
    root = Path(__file__).resolve().parents[1]
    manifest = {
        "release_version": RUNTIME_RELEASE_VERSION,
        "prompt_format_version": RUNTIME_PROMPT_VERSION,
        "not_a_formal_training_release": False,
        "checks": {
            "status": "pass", "source_and_split_preserved": True,
            "runtime_derived_inputs": True, "readonly_gold_comparison": True,
            "train_validation_identity_audit": "pass", "final_test_read": False,
            "silent_truncation": False,
        },
        "tokenizer": {"max_seq_length": 4096, "silent_truncation": False},
        "outputs": {name: {"rows": 1, "sha256": sha256_file(path)}
                    for name, path in zip(("train", "validation"), paths)},
        "split_audit": {name: {"rows": 1, "by_workspace": {"olist": 1},
                               "sequence_tokens": {"max": sample(name)["token_length"]}}
                        for name in ("train", "validation")},
        "admitted": 2,
        "execution_summary": {"result_states": {"valid": 2},
                              "denotation": {"ordered_denotation_match": 2}},
        "runtime_asset_sha256": {asset: sha256_file(root / asset)},
    }
    return manifest, paths


def test_manifest_accepts_admitted_counts_and_binds_file_hashes(release):
    manifest, paths = release
    validate_split_audit(manifest, *paths)
    assert required_max_seq_length(manifest) == 4096
    paths[0].write_text("tampered\n")
    with pytest.raises(ValueError, match="file hash drift"):
        validate_split_audit(manifest, *paths)


@pytest.mark.parametrize(("section", "field", "value", "message"), [
    ("checks", "final_test_read", True, "admission/isolation"),
    ("checks", "silent_truncation", True, "admission/isolation"),
    ("checks", "runtime_derived_inputs", False, "admission/isolation"),
    ("tokenizer", "max_seq_length", 3072, "length contract"),
    ("runtime_asset_sha256", "src/data_analysis_agent/candidate_sql_generator.py", "0" * 64, "asset hash drift"),
])
def test_manifest_rejects_contract_drift(release, section, field, value, message):
    manifest, paths = release
    manifest[section][field] = value
    with pytest.raises(ValueError, match=message):
        validate_split_audit(manifest, *paths)


@pytest.mark.parametrize("field", ["transaction_read_only", "denotation", "result_state", "policy_state", "execution_truncated"])
def test_loader_rejects_unverified_runtime_rows(tmp_path, field):
    row = sample()
    row["execution_evidence"][field] = "invalid"
    path = tmp_path / "train.jsonl"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="runtime admission evidence"):
        load_rows(path, "train")


def test_runtime_prefix_is_exact_and_completion_includes_separator_and_eos():
    row = sample()
    tokenizer = CharacterTokenizer()
    prompt, target = split_prompt_and_target(row)
    assert prompt == row["rendered_prompt"]
    assert target == "\n" + row["candidate_sql"]
    example = CausalSqlDataset([row], tokenizer, 4096)[0]
    count = len(prompt)
    assert example["input_ids"].tolist() == tokenizer(row["training_text"])["input_ids"] + [999]
    assert example["labels"][:count].tolist() == [-100] * count
    assert example["labels"][count:].tolist() == tokenizer(target)["input_ids"] + [999]
    assert example["attention_mask"].tolist() == [1] * row["token_length"]


def test_over_old_cap_is_retained_and_eos_is_distinct_from_masked_padding():
    short = sample()
    long = copy.deepcopy(short)
    long["rendered_prompt"] = "x" * 3200 + "\n### SQL"
    long["training_text"] = long["rendered_prompt"] + "\n" + long["candidate_sql"]
    long["token_length"] = len(long["training_text"]) + 1
    dataset = CausalSqlDataset([short, long], CharacterTokenizer(), 4096)
    assert dataset.stats["max_sequence_tokens"] > 3072
    batch = CausalSqlCollator(999)([dataset[0], dataset[1]])
    eos = short["token_length"] - 1
    assert batch["labels"][0, eos] == 999
    assert batch["attention_mask"][0, eos] == 1
    assert torch.all(batch["input_ids"][0, eos + 1:] == 999)
    assert torch.all(batch["labels"][0, eos + 1:] == -100)
    assert torch.all(batch["attention_mask"][0, eos + 1:] == 0)
    with pytest.raises(ValueError, match="refuse to truncate"):
        CausalSqlDataset([long], CharacterTokenizer(), 3072)
    long["token_length"] -= 1
    with pytest.raises(ValueError, match="recorded token length drift"):
        CausalSqlDataset([long], CharacterTokenizer(), 4096)


def test_smoke_is_reproducible_longest_per_workspace_and_preserves_source():
    rows = [sample(workspace=w, index=i) for w in ("olist", "thelook") for i in (1, 10, 100)]
    original = copy.deepcopy(rows)
    selected = select_smoke_rows(rows, 2)
    assert selected == select_smoke_rows(list(reversed(rows)), 2)
    assert [row["workspace_key"] for row in selected] == ["olist", "thelook"] * 2
    assert {row["sample_id"].rsplit("-", 1)[1] for row in selected} == {"100", "10"}
    assert rows == original
    with pytest.raises(ValueError, match="at least two"):
        select_smoke_rows(rows, 1)


def test_final_step_is_evaluated_and_saved_even_off_cadence(tmp_path):
    model = SimpleNamespace(save_pretrained=lambda _: None)
    callback = FinalAdapterSnapshotCallback(tmp_path, evaluate_final_step=True)
    control = SimpleNamespace(should_evaluate=False, should_save=False)
    callback.on_step_end(None, SimpleNamespace(global_step=7, max_steps=7), control, model=model)
    assert control.should_evaluate and control.should_save
    assert callback.captured_step == 7
