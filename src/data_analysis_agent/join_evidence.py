"""Build bounded, count-only checks of aggregation inputs, never business answers.

The original SQL is already policy checked. Checks use the same reader/snapshot.
Unknown row identities, weighted expressions and unmergeable derived relations
are reported as uncovered; they are not evidence that the join is safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlglot import exp, parse_one
from sqlglot.optimizer.merge_subqueries import merge_subqueries
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.optimizer.qualify_columns import qualify_columns
from sqlglot.errors import OptimizeError


@dataclass(frozen=True)
class JoinProbe:
    name: str
    sql: str
    table: str
    aggregate: str
    mode: str = 'identity'


def _allocation_weight(aggregate, sources, required):
    """Recognize only multiplicative allocation of one declared amount column."""
    value = aggregate.this
    allowed = (exp.Mul, exp.Div, exp.Paren, exp.Cast, exp.Nullif, exp.Column, exp.Identifier, exp.DataType, exp.Literal)
    if not isinstance(aggregate, exp.Sum) or not all(isinstance(n, allowed) for n in value.walk()):
        return None
    candidates = [c for c in value.find_all(exp.Column)
                  if isinstance(sources.get(c.table), exp.Table)
                  and f'{sources[c.table].name}.{c.name}' in required]
    if len(candidates) != 1:
        return None
    column = candidates[0]
    weight = value.copy().transform(lambda n: exp.Literal.number(1) if n == column else n)
    return column.table, weight


def _contributing_scopes(root):
    visited = set()

    def visit(scope):
        if id(scope) in visited:
            return
        visited.add(id(scope))
        children = [source for _, source in scope.selected_sources.values() if isinstance(source, Scope)]
        children.extend(scope.subquery_scopes)
        children.extend(scope.set_operation_scopes)
        for child in children:
            yield from visit(child)
        yield scope

    yield from visit(root)


def _unique_keys(source, row_keys):
    """Keys that survive a simple aggregate boundary, not guessed fact IDs."""
    if isinstance(source, exp.Table):
        keys = row_keys.get(source.name)
        return set(keys) if keys else None
    if not isinstance(source, Scope) or not isinstance(source.expression, exp.Select):
        return None
    select = source.expression
    group = select.args.get('group')
    if group:
        if any(group.args.get(k) for k in ('rollup', 'cube', 'grouping_sets')):
            return None
        keys = set()
        for item in group.expressions:
            matches = [p.alias_or_name for p in select.selects if p.unalias() == item]
            if not matches:
                return None
            keys.add(matches[0])
        return keys
    if any(isinstance(n, exp.AggFunc) and n.find_ancestor(exp.Select) is select
           for p in select.selects for n in p.walk()):
        return set()  # A scalar aggregate has at most one row.
    if select.args.get('distinct'):
        return set(select.named_selects)
    # Reused CTEs are intentionally not always flattened. A one-source
    # projection/filter still preserves a catalog key if every key column is
    # projected unchanged. No claim is made for joins, expressions or UNION.
    if not select.args.get('joins') and len(source.selected_sources) == 1:
        alias, (_, origin) = next(iter(source.selected_sources.items()))
        keys = _unique_keys(origin,row_keys)
        if keys is not None:
            projected = {p.alias_or_name for p in select.selects
                         if isinstance(p.unalias(),exp.Column) and p.unalias().name in keys
                         and p.unalias().table in {'',alias}}
            originals = {p.unalias().name for p in select.selects
                         if p.alias_or_name in projected and isinstance(p.unalias(),exp.Column)}
            if originals == keys and len(projected)==len(keys):
                return projected
    return None


def _unverified_fanout(scope: Scope, owners: set[str], row_keys) -> bool:
    """Can every joined source be reached on its complete unique key?

    This only discharges multiplicity, not metric correctness. In particular
    grouping a CTE by order_id preserves a key while raw payment rows do not.
    """
    sources = {alias: source for alias, (_, source) in scope.selected_sources.items()}
    known = owners & sources.keys()
    if not known:
        return True
    pairs = []
    for join in scope.expression.args.get('joins') or ():
        on = join.args.get('on')
        if on is None or any(isinstance(n, exp.Or) for n in on.walk()):
            continue
        pairs.extend((eq.this, eq.expression) for eq in on.find_all(exp.EQ)
                     if isinstance(eq.this, exp.Column) and isinstance(eq.expression, exp.Column))
    for start in known:
        reached = {start}
        while True:
            added = set()
            for alias, source in sources.items():
                if alias in reached:
                    continue
                keys = _unique_keys(source, row_keys)
                matched = set()
                for left, right in pairs:
                    for target, origin in ((left, right), (right, left)):
                        if target.table == alias and origin.table in reached:
                            matched.add(target.name)
                if keys is not None and keys <= matched:
                    added.add(alias)
            if len(reached) == len(sources):
                return False
            if not added:
                break
            reached.update(added)
    return True


def _unsupported_query(tree, reason, row_keys):
    root = build_scope(tree)
    high_risk = False
    if root:
        for scope in _contributing_scopes(root):
            if not isinstance(scope.expression, exp.Select) or not scope.expression.args.get('joins'):
                continue
            for projection in scope.expression.selects:
                for aggregate in projection.find_all(exp.Sum):
                    owners = {c.table for c in aggregate.find_all(exp.Column)}
                    high_risk |= _unverified_fanout(scope, owners, row_keys)
    return [], [{'reason': reason, 'high_risk': high_risk}]


def summarize_join_evidence(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate diagnostics; false rejects require independent safe labels."""
    statuses = dict.fromkeys(('checked', 'amplified', 'partial', 'not_applicable'), 0)
    covered = planned = rejected = applicable = safe = false_rejects = 0
    elapsed = []
    for record in records:
        evidence = record['evidence']
        statuses[evidence['status']] += 1
        applicable += evidence['status'] != 'not_applicable'
        covered += evidence['covered_aggregates']
        planned += evidence['planned_checks'] + evidence['uncovered_aggregates']
        rejected += bool(evidence['rejected'])
        elapsed.append(float(evidence['elapsed_ms']))
        if record.get('expected_safe') is True:
            safe += 1
            false_rejects += bool(evidence['rejected'])
    elapsed.sort()
    return {
        'attempts': len(records), 'statuses': statuses,
        'aggregate_coverage_ratio': covered / planned if planned else None,
        'diagnostic_rejection_rate': rejected / applicable if applicable else None,
        'labeled_safe_queries': safe, 'false_rejection_rate': false_rejects / safe if safe else None,
        'mean_elapsed_ms': sum(elapsed) / len(elapsed) if elapsed else None,
        'p95_elapsed_ms': elapsed[max(0, (len(elapsed) * 95 + 99) // 100 - 1)] if elapsed else None,
    }


def build_join_probes(
    sql: str,
    *,
    row_keys: Mapping[str, Sequence[str]],
    source_grains: Mapping[str, str],
    metric_grains: Mapping[str, str],
    column_schema: Mapping[str, object] | None = None,
    allocation_sources: Mapping[str, Sequence[str]] | None = None,
) -> tuple[list[JoinProbe], list[dict[str, Any]]]:
    """Find a repeated fact identity within one aggregate's actual grouping.

    Counting distinct identities within each group permits membership in
    different groups. COUNT(DISTINCT ...) and SUM(DISTINCT ...) are invariant
    to repeated inputs and do not need this duplicate-input check.
    """
    tree = parse_one(sql, read="postgres")
    if not tree.find(exp.Join):
        return [], []
    if any(with_.args.get("recursive") for with_ in tree.find_all(exp.With)):
        return _unsupported_query(tree, 'recursive_query', row_keys)
    if column_schema is not None:
        try:
            tree = qualify_columns(tree, schema=dict(column_schema), dialect="postgres")
        except OptimizeError:
            return _unsupported_query(tree, 'qualification_not_supported', row_keys)
    # Flatten ordinary projections/CTEs, retaining aggregate boundaries.
    tree = merge_subqueries(tree)
    root = build_scope(tree)
    if root is None:
        return [], []
    probes, uncovered = [], []
    global_with = tree.args.get("with_")
    for scope_index, scope in enumerate(_contributing_scopes(root)):
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
                if aggregate.find_ancestor(exp.Select) is not select:
                    continue
                name = f"scope_{scope_index}_{len(probes)}"
                owners = {column.table for column in aggregate.find_all(exp.Column)}

                def uncovered_aggregate(reason, aggregate=aggregate, projection=projection, scope=scope, owners=owners):
                    uncovered.append({
                        'reason': reason, 'aggregate': aggregate.key,
                        'metric': projection.alias_or_name,
                        'high_risk': isinstance(aggregate, exp.Sum)
                        and _unverified_fanout(scope, owners, row_keys),
                    })

                if aggregate.find_ancestor(exp.Window):
                    uncovered_aggregate('window_aggregate_not_supported')
                    continue

                if isinstance(aggregate.this, exp.Distinct):
                    continue
                if not isinstance(aggregate, (exp.Sum, exp.Avg, exp.Count)):
                    uncovered_aggregate('aggregate_not_supported')
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
                    uncovered_aggregate('aggregate_scope_not_supported')
                    continue
                columns = list(aggregate.find_all(exp.Column))
                owners = {column.table for column in columns}
                # Multiple value sources may be an intentional allocation;
                # repeated rows alone cannot prove its weighted sum is wrong.
                allocation = None
                if len(owners) > 1:
                    allocation = _allocation_weight(
                        aggregate, sources, (allocation_sources or {}).get(projection.alias_or_name, ()),
                    )
                    if allocation is None:
                        uncovered_aggregate('weighted_or_multiple_sources')
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
                if allocation:
                    alias = allocation[0]
                elif isinstance(aggregate, exp.Count) and len(fact_aliases) == 1:
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
                    uncovered_aggregate('source_identity_unavailable')
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
                        uncovered_aggregate('unresolved_group_expression')
                        group_expressions = []
                        break
                    group_expressions.append(group.copy())
                if not group_expressions and select.args.get("group") and select.args["group"].expressions:
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
                if allocation:
                    # A valid payment-share split may repeat a fact per
                    # payment, but total allocated weight must not exceed 1.
                    # Check all returned groups together, not each payment
                    # type in isolation. This is not a proof of the formula.
                    check.set('expressions', [exp.alias_(k.copy(), f'daa_fact_{i}')
                                              for i, k in enumerate(key_columns)] + [
                        exp.alias_(exp.Sum(this=allocation[1]), 'daa_weight'),
                    ])
                    check.set('group', exp.Group(expressions=[k.copy() for k in key_columns]))
                    check.set('having', None)
                check = check.limit(1)
                if allocation:
                    check.set('limit', None)
                # Identity grouping is an internal relational stage, just as
                # in the already-admitted business SQL. Keep it in a CTE so
                # analyst Policy sees only the two counts at the output; no
                # admin role or sensitive-projection exception is necessary.
                cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
                diagnostic_name = "daa_join_inputs"
                while diagnostic_name in cte_names:
                    diagnostic_name += "_next"
                ctes = [cte.copy() for cte in global_with.expressions] if global_with else []
                ctes.append(exp.CTE(
                    this=check,
                    alias=exp.TableAlias(this=exp.to_identifier(diagnostic_name)),
                ))
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
                ).from_(diagnostic_name)
                probe.set("with_", exp.With(expressions=ctes))
                if allocation:
                    probe.set('expressions', [
                        exp.alias_(exp.Count(this=exp.Case(ifs=[exp.If(
                            this=exp.GT(this=exp.column('daa_weight'), expression=exp.Literal.number('1.0000001')),
                            true=exp.Literal.number(1),
                        )])), 'input_rows'),
                        exp.alias_(exp.Count(this=exp.Star()), 'distinct_facts'),
                    ])
                probes.append(
                    JoinProbe(
                        name, probe.sql(dialect="postgres"), source.name, aggregate.key,
                        'allocation' if allocation else 'identity',
                    )
                )
    return probes, uncovered
