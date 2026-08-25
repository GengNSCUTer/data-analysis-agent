from __future__ import annotations

import json
from pathlib import Path

from scripts.split_post_training_candidates import main


def candidate(sample_id: str, database: str, shape: str) -> dict[str, object]:
    return {
        "sample_id": sample_id,
        "workspace_id": "spider_research",
        "split": {"name": "train", "group": f"{database}:digest"},
        "query_plan": {"sql_shape": shape},
        "execution_outcome": {"sqlite_readonly_explain": "pass"},
    }


def test_splitter_keeps_each_database_and_shape_in_one_split(
    monkeypatch, tmp_path: Path
) -> None:
    candidates_path = tmp_path / "candidates.jsonl"
    candidates_path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                candidate("spider_train:00000", "db_a", "shape_a"),
                candidate("spider_train:00001", "db_a", "shape_b"),
                candidate("spider_train:00002", "db_b", "shape_c"),
                candidate("spider_train:00003", "db_c", "shape_d"),
                candidate("spider_train:00004", "db_d", "shape_e"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    holdout_path = tmp_path / "holdout.yaml"
    holdout_path.write_text(
        "cases:\n  - case_id: data_001\n    forbidden_for_training: true\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "external-split"
    monkeypatch.setattr(
        "sys.argv",
        [
            "split_post_training_candidates.py",
            "--candidates-jsonl",
            str(candidates_path),
            "--holdout-manifest",
            str(holdout_path),
            "--output-dir",
            str(output_dir),
            "--validation-ratio",
            "0.4",
            "--seed",
            "17",
            "--generated-at",
            "2026-08-25T00:00:00Z",
        ],
    )

    assert main() == 0

    train_rows = [json.loads(line) for line in (output_dir / "train.jsonl").read_text().splitlines()]
    validation_rows = [
        json.loads(line) for line in (output_dir / "validation.jsonl").read_text().splitlines()
    ]
    train_databases = {row["split"]["group"].split(":")[0] for row in train_rows}
    validation_databases = {row["split"]["group"].split(":")[0] for row in validation_rows}
    assert train_databases.isdisjoint(validation_databases)
    assert {row["query_plan"]["sql_shape"] for row in train_rows}.isdisjoint(
        {row["query_plan"]["sql_shape"] for row in validation_rows}
    )
    assert {row["split"]["name"] for row in train_rows} == {"train"}
    assert {row["split"]["name"] for row in validation_rows} == {"validation"}

    audit = json.loads((output_dir / "split_audit.json").read_text())
    assert audit["checks"]["status"] == "pass"
    assert audit["checks"]["v2_holdout_used"] is False
