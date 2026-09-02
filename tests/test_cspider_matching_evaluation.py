from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from data_analysis_agent.sqlite_benchmark import normalize_cspider_validation_cases
from scripts.post_training.evaluation.run_sqlite_benchmark import main as sqlite_main
from scripts.post_training.evaluation.run_spider_bounded_denotation_audit import (
    main as denotation_main,
)
from scripts.post_training.evaluation.verify_cspider_matching_generation import (
    MatchingGenerationError,
    main as verify_main,
    verify_matching_generation,
)

pytest.importorskip("torch", reason="generation utilities run in the isolated QLoRA environment")

from scripts.post_training.inference.generate_post_training_text_to_sql import (
    GenerationInputError,
    require_dev_cases_without_gold,
    sha256_file,
    validate_cspider_validation_assets,
)


class QueryForbiddenCase(dict[str, object]):
    def get(self, key: str, default: object = None) -> object:
        if key == "query":
            raise AssertionError("CSpider gold SQL must not be read before the denotation phase")
        return super().get(key, default)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_cspider_assets(tmp_path: Path) -> tuple[Path, Path, Path]:
    cases_path = tmp_path / "dev.json"
    tables_path = tmp_path / "tables.json"
    _write_json(
        cases_path,
        [
            {
                "db_id": "shop",
                "question": "secret question must not become a report artifact",
                "query": "SELECT secret_gold_sql",
            }
        ],
    )
    _write_json(tables_path, [{"db_id": "shop", "table_names_original": ["metrics"]}])
    manifest_path = tmp_path / "acquisition-manifest.json"
    _write_json(
        manifest_path,
        {
            "dataset": {"id": "cspider"},
            "splits": {"dev": {"role": "validation_only", "record_count": 1}},
            "source_files": {
                "dev.json": sha256_file(cases_path),
                "tables.json": sha256_file(tables_path),
            },
        },
    )
    return cases_path, tables_path, manifest_path


def test_cspider_generation_inputs_and_asset_identity_fail_closed(tmp_path: Path) -> None:
    cases_path, tables_path, manifest_path = _write_cspider_assets(tmp_path)

    validate_cspider_validation_assets(
        cases_path=cases_path, tables_path=tables_path, manifest_path=manifest_path
    )
    protected = QueryForbiddenCase(
        db_id="shop", question="question", query="SELECT forbidden_gold_sql"
    )
    assert require_dev_cases_without_gold([protected]) == [("shop", "question")]

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["splits"]["dev"]["role"] = "test_only"
    _write_json(manifest_path, manifest)
    with pytest.raises(GenerationInputError, match="validation_only"):
        validate_cspider_validation_assets(
            cases_path=cases_path, tables_path=tables_path, manifest_path=manifest_path
        )

    manifest["splits"]["dev"]["role"] = "validation_only"
    _write_json(manifest_path, manifest)
    cases_path.write_text(cases_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(GenerationInputError, match="dev.json does not match"):
        validate_cspider_validation_assets(
            cases_path=cases_path, tables_path=tables_path, manifest_path=manifest_path
        )


def test_cspider_normalizer_does_not_read_gold_sql() -> None:
    cases = [QueryForbiddenCase(db_id="shop", question="unused", query="SELECT hidden_gold_sql")]

    normalized = normalize_cspider_validation_cases(cases)

    assert normalized[0].case_id == "cspider_validation:00000"
    assert normalized[0].database_path == "shop/shop.sqlite"


def test_cspider_sqlite_cli_keeps_question_and_gold_sql_out_of_report(tmp_path: Path) -> None:
    cases_path, _, _ = _write_cspider_assets(tmp_path)
    database_root = tmp_path / "database"
    database_path = database_root / "shop" / "shop.sqlite"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE metrics (value INTEGER)")
        connection.execute("INSERT INTO metrics VALUES (7)")
    predictions_path = tmp_path / "predictions.jsonl"
    predictions_path.write_text(
        json.dumps(
            {
                "case_id": "cspider_validation:00000",
                "candidate_sql": "SELECT value FROM metrics",
                "generated_tokens": 4,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "diagnostic.json"

    assert sqlite_main(
        [
            "--dataset", "cspider_validation", "--cases", str(cases_path),
            "--database-root", str(database_root), "--predictions", str(predictions_path),
            "--dataset-version", "cspider-fixture", "--model-id", "fixture",
            "--model-version", "base", "--prompt-version", "prompt-v2",
            "--output", str(output_path),
        ]
    ) == 0
    serialized = output_path.read_text(encoding="utf-8")
    assert "cspider_validation:00000" in serialized
    assert "secret question" not in serialized
    assert "secret_gold_sql" not in serialized


def _write_predictions(path: Path, case_count: int, *, reverse: bool = False) -> str:
    indices = list(range(case_count))
    if reverse:
        indices.reverse()
    path.write_text(
        "".join(
            json.dumps(
                {
                    "case_id": f"cspider_validation:{index:05d}",
                    "candidate_index": 0,
                    "candidate_sql": "SELECT forbidden_candidate_sql FROM hidden_database",
                }
            )
            + "\n"
            for index in indices
        ),
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _generation_evidence(
    *, run_label: str, prediction_hash: str, case_count: int, prompt: str = "prompt-v2"
) -> dict[str, object]:
    adapter: dict[str, object] = {"enabled": False}
    if run_label == "adapter":
        adapter = {
            "enabled": True,
            "adapter_config_sha256": "a" * 64,
            "adapter_model_sha256": "b" * 64,
            "adapter_model_bytes": 123,
        }
    return {
        "run_label": run_label,
        "model": {
            "id": "Qwen/Qwen2.5-Coder-1.5B",
            "revision": "revision",
            "download_manifest_sha256": "c" * 64,
            "base_weight_mode": "bf16_lora",
            "load_in_4bit": False,
            "quant_type": None,
            "double_quant": False,
            "compute_dtype": "bfloat16",
        },
        "adapter": adapter,
        "comparison_contract": {
            "prompt_format_version": prompt,
            "dataset": "cspider_validation",
            "case_id_prefix": "cspider_validation",
            "max_input_tokens": 1536,
            "cases_sha256": "d" * 64,
            "tables_sha256": "e" * 64,
            "cspider_acquisition_manifest_sha256": "f" * 64,
            "decode": {"do_sample": False, "num_beams": 1, "max_new_tokens": 256, "seed": 42},
            "gold_sql_read_for_generation": False,
            "raw_questions_or_prompts_written": False,
            "raw_database_rows_read": False,
        },
        "generation": {
            "native_case_count": case_count,
            "generated_this_invocation": case_count,
            "existing_prediction_case_count": 0,
            "max_cases": None,
            "prediction_jsonl_sha256": prediction_hash,
        },
    }


def test_matching_generation_cli_checks_full_order_and_emits_safe_report(tmp_path: Path) -> None:
    base_predictions = tmp_path / "base.jsonl"
    adapter_predictions = tmp_path / "adapter.jsonl"
    base_hash = _write_predictions(base_predictions, 2)
    adapter_hash = _write_predictions(adapter_predictions, 2)
    base_evidence = tmp_path / "base-evidence.json"
    adapter_evidence = tmp_path / "adapter-evidence.json"
    _write_json(base_evidence, _generation_evidence(run_label="base", prediction_hash=base_hash, case_count=2))
    _write_json(adapter_evidence, _generation_evidence(run_label="adapter", prediction_hash=adapter_hash, case_count=2))
    output_path = tmp_path / "matching.json"

    assert verify_main(
        [
            "--base-evidence", str(base_evidence), "--adapter-evidence", str(adapter_evidence),
            "--base-predictions", str(base_predictions), "--adapter-predictions", str(adapter_predictions),
            "--expected-case-count", "2", "--output", str(output_path),
        ]
    ) == 0
    serialized = output_path.read_text(encoding="utf-8")
    assert json.loads(serialized)["scope"]["matching_contract_verified_before_sqlite_diagnostics"] is True
    assert "forbidden_candidate_sql" not in serialized
    assert "hidden_database" not in serialized


def test_matching_generation_rejects_config_drift_and_nonmatching_case_order(tmp_path: Path) -> None:
    base_predictions = tmp_path / "base.jsonl"
    adapter_predictions = tmp_path / "adapter.jsonl"
    base_hash = _write_predictions(base_predictions, 2)
    adapter_hash = _write_predictions(adapter_predictions, 2, reverse=True)
    base = _generation_evidence(run_label="base", prediction_hash=base_hash, case_count=2)
    adapter = _generation_evidence(run_label="adapter", prediction_hash=adapter_hash, case_count=2, prompt="other")

    with pytest.raises(MatchingGenerationError, match="comparison field differs: prompt_format_version"):
        verify_matching_generation(
            base_evidence=base,
            adapter_evidence=adapter,
            base_predictions=base_predictions,
            adapter_predictions=adapter_predictions,
            expected_case_count=2,
        )

    adapter = _generation_evidence(run_label="adapter", prediction_hash=adapter_hash, case_count=2)
    with pytest.raises(MatchingGenerationError, match="adapter prediction case IDs"):
        verify_matching_generation(
            base_evidence=base,
            adapter_evidence=adapter,
            base_predictions=base_predictions,
            adapter_predictions=adapter_predictions,
            expected_case_count=2,
        )


def test_cspider_denotation_cli_uses_validation_namespace_and_writes_no_sql(tmp_path: Path) -> None:
    database_root = tmp_path / "database"
    database_path = database_root / "shop" / "shop.sqlite"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE metrics (value INTEGER)")
        connection.execute("INSERT INTO metrics VALUES (7)")
    report = {
        "records": [{
            "case_id": "cspider_validation:00000",
            "database_path": "shop/shop.sqlite",
            "execution": {"status": "executed", "final_sql": "SELECT value FROM metrics"},
        }]
    }
    base_path, adapter_path, cases_path, output_path = (
        tmp_path / "base.json", tmp_path / "adapter.json", tmp_path / "dev.json", tmp_path / "audit.json"
    )
    _write_json(base_path, report)
    _write_json(adapter_path, report)
    _write_json(cases_path, [{"db_id": "shop", "query": "SELECT value FROM metrics /* secret_gold_sql */"}])

    assert denotation_main(
        [
            "--dataset-id", "cspider_validation", "--base-report", str(base_path),
            "--adapter-report", str(adapter_path), "--audit-cases", str(cases_path),
            "--database-root", str(database_root), "--output", str(output_path),
        ]
    ) == 0
    serialized = output_path.read_text(encoding="utf-8")
    assert "cspider_validation:00000" in serialized
    assert "secret_gold_sql" not in serialized
    assert "SELECT value" not in serialized
