from __future__ import annotations

from pathlib import Path

from data_analysis_agent.metric_context import OLIST_V3_WORKSPACE
from data_analysis_agent.olist_queryspec import QuerySpec, QueryTime, WorkspacePin
from data_analysis_agent.semantic_catalog import CatalogLoader
from scripts.post_training.data.build_olist_v3_surface_variants import (
    EXPECTED_ROWS,
    VARIANTS_PER_SEED,
    load_admitted_records,
    render_variants,
)


ADMISSION_DIR = Path(
    "/disk2/gengnan/data-analysis-agent-data/evals/olist-v3-balanced-release-v1/admission-20260914"
)


def test_v3_surface_input_requires_the_passing_hash_bound_full_admission() -> None:
    manifest, records = load_admitted_records(ADMISSION_DIR)

    assert manifest["checks"]["status"] == "pass"
    assert len(records) == EXPECTED_ROWS
    assert all(record["admission_status"] == "admitted" for record in records)


def test_v3_surface_forms_use_catalog_terms_and_preserve_query_time_dimension() -> None:
    catalog = CatalogLoader(workspace=OLIST_V3_WORKSPACE).load()
    spec = QuerySpec.create_validated(
        workspace=WorkspacePin.current(OLIST_V3_WORKSPACE),
        metric_ids=("unique_customer_count", "approval_latency_days"),
        result_shape="state_grouped",
        time=QueryTime("absolute_range", "2017-01-01", "2017-04-01"),
        dimension="customer_state",
    )

    variants = render_variants(spec, "surface-test", catalog)

    assert len(variants) == VARIANTS_PER_SEED
    assert len({variant["question"] for variant in variants}) == VARIANTS_PER_SEED
    assert all("2017-01-01至2017-04-01" in variant["question"] for variant in variants)
    assert all("客户州" in variant["question"] for variant in variants)
    assert {variant["variant_id"] for variant in variants} == {
        f"surface-test-v{index}" for index in range(1, 6)
    }
