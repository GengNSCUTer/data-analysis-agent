"""Offline, traceable migration of train/validation into question-derived inputs.

Only the data repair step can consult the source Spec. It writes an explicit
question edit first, then reruns the shared runtime builder from that question.
This module does not execute SQL, load a model, or read a protected test.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any, Mapping, Sequence

from sqlglot import exp, parse
from sqlglot.optimizer.scope import traverse_scope

from .candidate_sql_generator import RUNTIME_CANDIDATE_SQL_PROMPT_VERSION
from .query_plan import QueryPlan
from .query_time import DATE_RANGE, EXCLUSIVE_END
from .runtime_alignment_audit import compare_source_semantics, replay_single_turn
from .semantic_catalog import CatalogRetriever
from .workspace import WorkspaceProfile

MATERIALIZER_VERSION = "runtime-candidate-materializer-v1"
GRAIN_NAMES = {"day": "日", "week": "周", "month": "月", "quarter": "季度", "year": "年"}


class RuntimeMaterializationError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def repair_source_question(
    source: Mapping[str, Any], retriever: CatalogRetriever, workspace: WorkspaceProfile
) -> tuple[str, list[str]]:
    if source.get("split") not in ({"name": "train"}, {"name": "validation"}):
        raise RuntimeMaterializationError("source_must_be_train_or_validation")
    question = source["question"]
    original = replay_single_turn(question, retriever, workspace=workspace)
    spec = source["query_spec"]
    changes = []
    match = DATE_RANGE.search(question)
    bounds = spec["time"]
    if match and not EXCLUSIVE_END.search(question):
        if (match["start"], match["end"]) != (bounds["start"], bounds["end_exclusive"]):
            raise RuntimeMaterializationError("source_date_literals_disagree")
        # Do not override an explicitly inclusive statement with an exclusive
        # label. Only repair the old generator's unmarked endpoint ambiguity.
        if any(term in question for term in ("含结束日", "包括结束日", "包含结束日", "含首尾")):
            raise RuntimeMaterializationError("explicit_inclusive_question_conflicts_with_gold")
        question = question[:match.end()] + "（含开始日，不含结束日）" + question[match.end():]
        changes.append("clarify_exclusive_end")
    grain = bounds.get("grain")
    if grain and QueryPlan._time_grain(question) is None:
        if grain not in GRAIN_NAMES:
            raise RuntimeMaterializationError("unsupported_source_grain")
        question += f" 请按{GRAIN_NAMES[grain]}汇总时间序列。"
        changes.append("clarify_time_grain")
    if original["route_reason"] in {"catalog_mention_without_speech_act", "catalog_metric_definition"}:
        question = "数据查询：" + question + " 请输出上述指标的查询结果。"
        changes.append("clarify_data_query_action")
    return question, changes


def _one_select(sql: str) -> exp.Select:
    statements = [node for node in parse(sql, read="postgres") if node is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise RuntimeMaterializationError("gold_must_be_one_select")
    return statements[0]


def reorder_gold_projection(sql: str, columns: Sequence[str]) -> str:
    """Reorder only final projections; reject ordinal references that would drift."""
    statement = _one_select(sql)
    projections = statement.expressions
    names = [expression.alias_or_name for expression in projections]
    if (len(set(names)) != len(names) or len(set(columns)) != len(columns)
            or Counter(names) != Counter(columns)):
        raise RuntimeMaterializationError("gold_projection_columns_disagree")
    if names != list(columns):
        for clause_name in ("order", "group", "distinct"):
            clause = statement.args.get(clause_name)
            if clause is not None and any(literal.is_int for literal in clause.find_all(exp.Literal)):
                raise RuntimeMaterializationError("outer_ordinal_reference_cannot_be_reordered")
    by_name = dict(zip(names, projections, strict=True))
    statement.set("expressions", [by_name[name] for name in columns])
    return statement.sql(dialect="postgres") + ";"


def catalog_dependency_evidence(
    sql: str, replay: Mapping[str, Any], retriever: CatalogRetriever, workspace: WorkspaceProfile
) -> dict[str, Any]:
    """Resolve physical table/column references within each SQL scope.

    A CTE output is not a physical column. Nested scopes are inspected separately
    so an inner source cannot borrow a same-named column from another table.
    """
    selected = replay["selected_columns"]
    dependencies: dict[str, set[str]] = {}
    available_edges = {
        frozenset((join.from_table, join.to_table))
        for join in retriever.catalog.joins if join.join_id in replay["selected_joins"]
    }
    for scope in traverse_scope(_one_select(sql)):
        physical = {alias: source for alias, source in scope.sources.items() if isinstance(source, exp.Table)}
        for table in physical.values():
            if table.db != workspace.analytics_schema or table.catalog:
                raise RuntimeMaterializationError("gold_reads_outside_analytics")
            if table.name not in selected:
                raise RuntimeMaterializationError("gold_table_missing_from_runtime_catalog")
            dependencies.setdefault(table.name, set())
        for column in scope.columns:
            if column.table:
                table = physical.get(column.table)
                if table is None:
                    continue  # A CTE/subquery output; its own scope is checked.
            else:
                candidates = [table for table in physical.values()
                              if column.name in workspace.allowed_columns.get(table.name, ())]
                if not candidates:
                    continue
                if len(candidates) != 1:
                    raise RuntimeMaterializationError("ambiguous_physical_gold_column")
                table = candidates[0]
            if column.name not in selected[table.name]:
                raise RuntimeMaterializationError("gold_column_missing_from_runtime_catalog")
            dependencies[table.name].add(column.name)
        for projection in scope.expression.expressions:
            if isinstance(projection, exp.Star) or isinstance(projection, exp.Column) and projection.is_star:
                if physical:
                    raise RuntimeMaterializationError("physical_gold_star_not_supported")
        for join in scope.expression.args.get("joins") or []:
            on = join.args.get("on")
            if on is None:
                if isinstance(join.this, exp.Table) and join.this.alias_or_name in physical:
                    raise RuntimeMaterializationError("physical_join_without_catalog_condition")
                continue
            for equality in on.find_all(exp.EQ):
                left, right = equality.this, equality.expression
                if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                    continue
                if left.table in physical and right.table in physical:
                    pair = frozenset((physical[left.table].name, physical[right.table].name))
                    if len(pair) > 1 and pair not in available_edges:
                        raise RuntimeMaterializationError("gold_join_missing_from_runtime_catalog")
    if not dependencies:
        raise RuntimeMaterializationError("gold_has_no_physical_dependency")
    return {table: sorted(columns) for table, columns in sorted(dependencies.items())}


def materialize_runtime_candidate_row(
    source: Mapping[str, Any], retriever: CatalogRetriever, workspace: WorkspaceProfile
) -> dict[str, Any]:
    if source.get("admission_status") != "admitted" or source.get("prompt_format_version") != "multi-schema-sql-only-v1":
        raise RuntimeMaterializationError("source_is_not_frozen_admitted_v1")
    parent_sql = source["candidate_sql"]
    if text_sha256(parent_sql) != source["canonical_gold_sql_sha256"]:
        raise RuntimeMaterializationError("source_gold_hash_drift")
    for field in ("catalog_version", "dataset_version", "metric_version", "policy_version"):
        if source["query_spec"]["workspace"][field] != getattr(workspace, field):
            raise RuntimeMaterializationError("source_workspace_version_drift")
    question, changes = repair_source_question(source, retriever, workspace)
    replay = replay_single_turn(question, retriever, workspace=workspace)
    mismatches = set(compare_source_semantics(replay, source["query_spec"])) - {"result_column_order_mismatch"}
    if mismatches:
        raise RuntimeMaterializationError("runtime_semantics:" + ",".join(sorted(mismatches)))
    sql = reorder_gold_projection(parent_sql, replay["plan"]["required_result_columns"])
    dependencies = catalog_dependency_evidence(sql, replay, retriever, workspace)
    retained = {key: source[key] for key in (
        "sample_id", "workspace_key", "split", "query_spec", "query_spec_id",
        "scenario_family_id", "program_id", "risk_tags", "primary_variant_id",
    ) if key in source}
    prompt = replay["candidate_prompt"]
    return {
        **retained, "workspace_id": workspace.workspace_id,
        "prompt_format_version": RUNTIME_CANDIDATE_SQL_PROMPT_VERSION,
        "materializer_version": MATERIALIZER_VERSION, "question": question,
        "query_plan": replay["plan"], "result_contract": replay["result_contract"],
        "rendered_prompt": prompt, "candidate_sql": sql,
        "training_text": prompt + "\n" + sql, "admission_status": "pending_execution",
        "provenance": {
            "parent_question": source["question"], "question_changes": changes,
            "parent_question_sha256": text_sha256(source["question"]),
            "question_sha256": text_sha256(question), "parent_gold_sql_sha256": text_sha256(parent_sql),
            "gold_sql_sha256": text_sha256(sql), "candidate_prompt_sha256": text_sha256(prompt),
            "parent_prompt_sha256": text_sha256(source["rendered_prompt"]),
            "source_row_sha256": text_sha256(json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            "physical_dependencies": dependencies,
        },
    }
