from __future__ import annotations

from scripts.post_training.data.materialize_olist_pilot_v1_sft import _query_spec_id


def test_query_spec_identity_supports_admission_and_runtime_record_shapes() -> None:
    assert _query_spec_id({"query_spec": {"query_spec_id": "qs-admission"}}) == "qs-admission"
    assert _query_spec_id({"query_spec_id": "qs-runtime"}) == "qs-runtime"
    assert _query_spec_id({"query_spec": {}}) is None
