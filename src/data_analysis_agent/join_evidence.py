"""Build bounded, count-only checks of aggregation inputs, never business answers.

The original SQL is already policy checked. Checks use the same reader/snapshot.
Unknown row identities, weighted expressions and unmergeable derived relations
are reported as uncovered; they are not evidence that the join is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from sqlglot import exp, parse_one
from sqlglot.optimizer.merge_subqueries import merge_subqueries
from sqlglot.optimizer.scope import build_scope
from sqlglot.optimizer.qualify_columns import qualify_columns
from sqlglot.errors import OptimizeError


@dataclass(frozen=True)
class JoinProbe:
    name: str
    sql: str
    table: str
    aggregate: str


def build_join_probes(
    sql: str,
    *,
    row_keys: Mapping[str, Sequence[str]],
    source_grains: Mapping[str, str],
    metric_grains: Mapping[str, str],
    column_schema: Mapping[str, object] | None = None,
) -> tuple[list[JoinProbe], list[dict[str, str]]]:
    """Find a repeated fact identity within one aggregate's actual grouping.

    Counting distinct identities within each group permits membership in
    different groups. COUNT(DISTINCT ...) and SUM(DISTINCT ...) are invariant
    to repeated inputs and do not need this duplicate-input check.
    """
    tree = parse_one(sql, read="postgres")
    if any(with_.args.get("recursive") for with_ in tree.find_all(exp.With)):
        return [], [{"reason": "recursive_query"}]
    if column_schema is not None:
        try:
            tree = qualify_columns(tree, schema=dict(column_schema), dialect="postgres")
        except OptimizeError:
            return [], [{"reason": "qualification_not_supported"}]
    # Flatten ordinary projections/CTEs, retaining aggregate boundaries.
    tree = merge_subqueries(tree)
    root = build_scope(tree)
    if root is None:
        return [], []
    probes, uncovered = [], []
    global_with = tree.args.get("with_")
    for scope_index, scope in enumerate(root.traverse()):
        select = scope.expression
        if not isinstance(select, exp.Select) or not select.args.get("joins"):
            continue
        sources = {
            alias: source
            for alias, (_, source) in scope.selected_sources.items()
            if isinstance(source, exp.Table)
        }
        seen = set()
        for projection in select.selects:
            for aggregate in projection.find_all(exp.AggFunc):
                if aggregate.find_ancestor(
                    exp.Select
                ) is not select or aggregate.find_ancestor(exp.Window):
                    continue
                name = f"scope_{scope_index}_{len(probes)}"
                if isinstance(aggregate.this, exp.Distinct):
                    continue
                if not isinstance(aggregate, (exp.Sum, exp.Avg, exp.Count)):
                    uncovered.append(
                        {
                            "reason": "aggregate_not_supported",
                            "aggregate": aggregate.key,
                        }
                    )
                    continue
                if (
                    scope.is_correlated_subquery
                    or select.args.get("having")
                    or select.args.get("qualify")
                    or select.args.get("group")
                    and any(
                        select.args["group"].args.get(k)
                        for k in ("rollup", "cube", "grouping_sets")
                    )
                ):
                    uncovered.append(
                        {
                            "reason": "aggregate_scope_not_supported",
                            "aggregate": aggregate.key,
                        }
                    )
                    continue
                columns = list(aggregate.find_all(exp.Column))
                owners = {column.table for column in columns}
                # Multiple value sources may be an intentional allocation;
                # repeated rows alone cannot prove its weighted sum is wrong.
                if len(owners) > 1:
                    uncovered.append(
                        {
                            "reason": "weighted_or_multiple_sources",
                            "aggregate": aggregate.key,
                        }
                    )
                    continue
                grain = metric_grains.get(projection.alias_or_name)
                fact_aliases = (
                    [
                        alias
                        for alias, source in sources.items()
                        if source_grains.get(source.name) == grain
                    ]
                    if grain
                    else []
                )
                if isinstance(aggregate, exp.Count) and len(fact_aliases) == 1:
                    # COUNT(order_id) can legitimately count item rows; its
                    # intended fact grain comes from the metric, not the FK.
                    alias = fact_aliases[0]
                elif len(owners) == 1 and next(iter(owners)):
                    alias = next(iter(owners))
                elif len(fact_aliases) == 1:
                    alias = fact_aliases[0]
                else:
                    alias = ""
                source = sources.get(alias)
                keys = row_keys.get(source.name, ()) if source is not None else ()
                if not keys:
                    uncovered.append(
                        {
                            "reason": "source_identity_unavailable",
                            "aggregate": aggregate.key,
                        }
                    )
                    continue
                group_expressions = []
                aliases = {p.alias: p.unalias() for p in select.selects if p.alias}
                for group in select.args.get("group", exp.Group()).expressions:
                    if isinstance(group, exp.Literal) and group.is_int:
                        group = select.selects[int(group.this) - 1].unalias()
                    elif (
                        isinstance(group, exp.Column)
                        and not group.table
                        and group.name in aliases
                    ):
                        group = aliases[group.name]
                    elif isinstance(group, exp.Column) and not group.table:
                        uncovered.append(
                            {
                                "reason": "unresolved_group_expression",
                                "aggregate": aggregate.key,
                            }
                        )
                        group_expressions = []
                        break
                    group_expressions.append(group.copy())
                if not group_expressions and select.args.get("group") and select.args["group"].expressions:
                    continue
                identities = {
                    (a, key)
                    for a, table in sources.items()
                    for key in row_keys.get(table.name, ())
                }
                if any(
                    (c.table, c.name) in identities
                    or not c.table
                    and any(c.name == key for _, key in identities)
                    for group in group_expressions
                    for c in group.find_all(exp.Column)
                ):
                    # The business query may legitimately preaggregate by a
                    # private identity. Do not expose that grouping in a
                    # diagnostic or weaken Policy to run it.
                    uncovered.append(
                        {
                            "reason": "identity_group_preaggregation",
                            "aggregate": aggregate.key,
                        }
                    )
                    continue
                value = aggregate.this
                predicate = (
                    aggregate.parent.expression
                    if isinstance(aggregate.parent, exp.Filter)
                    else None
                )
                signature = (
                    alias,
                    tuple(g.sql() for g in group_expressions),
                    value.sql(),
                    predicate.sql() if predicate else None,
                )
                if signature in seen:
                    continue
                seen.add(signature)
                key_columns = [exp.column(key, table=alias) for key in keys]
                check = select.copy()
                for clause in (
                    "with_",
                    "order",
                    "limit",
                    "offset",
                    "distinct",
                    "windows",
                ):
                    check.set(clause, None)
                identity = (
                    key_columns[0].copy()
                    if len(key_columns) == 1
                    else exp.Tuple(expressions=[k.copy() for k in key_columns])
                )
                distinct = exp.Count(this=exp.Distinct(expressions=[identity]))
                check.set(
                    "expressions",
                    [
                        exp.alias_(exp.Count(this=exp.Star()), "input_rows"),
                        exp.alias_(distinct.copy(), "distinct_facts"),
                    ],
                )
                check.set(
                    "group",
                    exp.Group(expressions=group_expressions)
                    if group_expressions
                    else None,
                )
                check.set(
                    "having",
                    exp.Having(
                        this=exp.GT(
                            this=exp.Count(this=exp.Star()), expression=distinct.copy()
                        )
                    ),
                )
                # Ignore NULL-extended rows from outer joins: they have no
                # contributing source fact. No identity values leave this SQL.
                check = check.where(
                    *(
                        exp.Not(this=exp.Is(this=k.copy(), expression=exp.Null()))
                        for k in key_columns
                    )
                )
                if not isinstance(value, exp.Star):
                    check = check.where(
                        exp.Not(this=exp.Is(this=value.copy(), expression=exp.Null()))
                    )
                if predicate is not None:
                    check = check.where(
                        predicate.this.copy()
                        if isinstance(predicate, exp.Where)
                        else predicate.copy()
                    )
                check = check.limit(1)
                probe = exp.select(
                    *[
                        exp.alias_(
                            exp.Coalesce(
                                this=exp.Max(this=exp.column(field)),
                                expressions=[exp.Literal.number(0)],
                            ),
                            field,
                        )
                        for field in ("input_rows", "distinct_facts")
                    ]
                ).from_(check.subquery("duplicate_inputs"))
                if global_with is not None:
                    probe.set("with_", global_with.copy())
                probes.append(
                    JoinProbe(
                        name, probe.sql(dialect="postgres"), source.name, aggregate.key
                    )
                )
    return probes, uncovered
