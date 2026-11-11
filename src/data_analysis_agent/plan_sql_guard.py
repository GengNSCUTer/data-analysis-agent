"""Reject candidate SQL that drops a server-owned query-plan rule.

The SQL model still writes the statement. Equality filters follow contributing
AST scopes; ranking and attribution checks are additional bounded checks, not
a proof of SQL equivalence or metric correctness.
"""

from __future__ import annotations

import re
from datetime import datetime
from fractions import Fraction
from typing import Any, Mapping

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.lineage import lineage
from sqlglot.errors import OptimizeError, SqlglotError


_LIMIT = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)
_TOP_N_DIMENSIONS = {"customer_city": 20, "seller_city": 20}
_TIME_PARTITION_HINT = re.compile(
    r"(?:time|date|day|week|month|quarter|year|period|timestamp)",
    re.IGNORECASE,
)


def sql_plan_mismatch(
    sql: str, plan: Mapping[str, Any] | None, *,
    metric_value_sources: Mapping[str, Mapping[str, Any]] | None = None,
    column_schema: Mapping[str, Any] | None = None,
    table_grains: Mapping[str, str] | None = None,
) -> str | None:
    """Return a safe reason for a detected, covered mismatch; otherwise None."""

    if not isinstance(sql, str) or not sql.strip() or not isinstance(plan, Mapping):
        return None
    expected_columns = plan.get('required_result_columns')
    if plan.get('query_plan_version') == 'query-plan-v2' and expected_columns:
        try:
            statement = parse_one(sql, read='postgres')
        except (ParseError, TokenError, RecursionError):
            statement = None
        if statement is not None:
            # Inspect only the final output. Order/entity aggregation CTEs may
            # retain internal keys and months, provided the final SELECT drops them.
            unexpected = [column.alias_or_name for column in statement.selects
                          if not column.is_star and column.alias_or_name not in expected_columns]
            if unexpected:
                return (f'SQL 多返回了计划外结果列：{", ".join(unexpected)}；'
                        f'最终只返回 {", ".join(expected_columns)}。'
                        '没有time或分组列时应重新按完整范围聚合，不沿用上轮月份/维度分组。')
    folded = sql.casefold()
    filters = plan.get("filters") or []
    if filters or plan.get("lookup_joins"):
        try:
            root = build_scope(parse_one(sql, read="postgres"))
        except (ParseError, TokenError, RecursionError):
            # SqlPolicy owns malformed SQL; this check owns plan omissions.
            return None
        if root is not None:
            dimensions = dict.fromkeys(item['dimension'] for item in filters)
            for dimension in dimensions:
                items = [item for item in filters if item['dimension'] == dimension]
                check = _FilterCheck(items)
                if not all(check.enforced(root, metric) for metric in plan.get("metric_ids", ()) or (None,)):
                    return f"SQL 遗漏或改变了已确认筛选：{dimension} IN {[item['value'] for item in items]!r}。请按计划中的来源字段保留条件。"
            mismatch = _lookup_join_mismatch(root, plan.get("lookup_joins") or [])
            if mismatch:
                return mismatch
    for warning in plan.get("warnings") or []:
        if filters:
            break
        if not isinstance(warning, str) or not warning.startswith("filter_customer_state:"):
            continue
        code = warning.split(":", 1)[1].casefold()
        if "customer_state" not in folded or code not in folded:
            return f"SQL 必须按客户州筛选 customer_state = '{code.upper()}'，分组和结果列仍遵循查询计划。"
    # v2 plans explicitly own ranking limits. TheLook city groupings must not
    # inherit the legacy Olist top-20 rule after preflight admitted a window.
    limits = {} if plan.get("query_plan_version") == "query-plan-v2" else dict(_TOP_N_DIMENSIONS)
    raw_top_n = plan.get("top_n")
    if isinstance(raw_top_n, Mapping):
        for key, value in raw_top_n.items():
            try:
                limits[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
    partition_dimensions = tuple(
        item for item in (plan.get("top_n_partition_dimensions") or ())
        if isinstance(item, str) and item
    )
    if partition_dimensions and limits:
        limit = min(limits.values())
        try:
            root = parse_one(sql, read="postgres")
        except (ParseError, TokenError, RecursionError):
            root = None
        rank_windows = [] if root is None else [
            node for node in root.find_all(exp.Window)
            if isinstance(node.this, (exp.RowNumber, exp.Rank, exp.DenseRank))
        ]
        if not rank_windows:
            return "分区 Top-N 的 SQL 必须使用窗口排名并按计划分区，不能用全局 LIMIT 代替。"
        if not any(_rank_partition_matches(window, partition_dimensions) for window in rank_windows):
            return f"分区 Top-N 缺少计划分区字段：{', '.join(partition_dimensions)}。"
        # Follow the rank used by a contributing WHERE, not any occurrence
        # inside NOT, OR TRUE, an unused CTE, or a differently partitioned rank.
        scope = build_scope(root)
        if scope is None or not _rank_bound_enforced(scope, partition_dimensions, limit):
            return f"分区 Top-N 必须在外层过滤排名不超过 {limit} 的行。"
    # The online aggregate runner owns the outer LIMIT: SqlPolicy replaces it
    # using this server plan. Rejecting a missing model-written LIMIT here is a
    # duplicate language-model repair, not an execution safety measure.
    ranking_dimensions = () if plan.get("result_budget") or partition_dimensions else plan.get("dimensions") or []
    for dimension in ranking_dimensions:
        limit = limits.get(str(dimension))
        if limit is None:
            continue
        # Equality on every grouping key already leaves at most one group.
        # Requiring a literal LIMIT here triggered an unnecessary repair for
        # a valid drilldown to one category, even though top-N is an upper bound.
        fixed_groups = {item["dimension"] for item in filters}
        if fixed_groups and not plan.get("time_grain") and set(plan["dimensions"]) <= fixed_groups:
            continue
        match = _LIMIT.search(sql)
        if match is None or int(match.group(1)) > limit:
            return f"按 {dimension} 汇总时 SQL 必须 LIMIT {limit} 或更少，只保留该指标最高的分组。"
    requirements = plan.get("attribution_requirements") or []
    if requirements:
        try:
            columns = {column.name.casefold() for column in parse_one(sql, read="postgres").find_all(exp.Column)}
        except (ParseError, TokenError, RecursionError):
            return None  # SqlPolicy owns malformed SQL.
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        mode = requirement.get("policy_mode")
        dimension = str(requirement.get("dimension") or "")
        if mode == "payment_share" and not {"payment_value", "payment_type"} <= columns:
            return "按支付方式拆金额时，SQL 必须用 payment_value 占比分摊，并按 payment_type 汇总。"
        if mode == "multi_membership" and dimension and dimension.casefold() not in columns:
            return f"SQL 必须包含分组列 {dimension}，并允许同一订单进入多个分组。"
    missing_groups = _unrequested_null_group_filter(sql, plan, column_schema or {})
    return (missing_groups
            or _output_grain_mismatch(sql, plan)
            or _metric_value_source_mismatch(sql, plan, metric_value_sources or {}, column_schema or {})
            or _metric_plan_field_mismatch(sql,plan,column_schema or {},table_grains or {}))


def _output_grain_mismatch(sql, plan):
    """Detect grouping by a preaggregated metric instead of reaggregating it.

    This catches the real user-total-as-state-total query, even when LIMIT 1
    hides duplicate states. It does not claim arbitrary SQL equivalence.
    """
    if plan.get('query_mode') != 'aggregate':
        return None
    try:
        root = build_scope(parse_one(sql, read='postgres'))
    except (ParseError, TokenError, RecursionError):
        return None
    if root is None:
        return None
    branches = root.set_operation_scopes if isinstance(root.expression, exp.SetOperation) else (root,)
    for branch in branches:
        selects = branch.expression.selects
        group = branch.expression.args.get('group')
        if group is None:
            continue
        for key in group.expressions:
            value = key
            if isinstance(key, exp.Literal) and key.is_int and 0 < int(key.this) <= len(selects):
                value = selects[int(key.this) - 1].unalias()
            for projection in selects:
                if projection.alias_or_name not in plan.get('metric_ids', ()):
                    continue
                if value == projection.unalias() or (isinstance(value, exp.Column)
                        and not value.table and value.name == projection.alias_or_name):
                    return (f'指标 {projection.alias_or_name} 被用作最终分组键；应按计划完整维度重新聚合，'
                            '不能把用户/订单等实体的预聚合金额直接当作州或品类总额。')
    return None


def _metric_plan_field_mismatch(sql, plan, schema, table_grains):
    """Catch omitted status fields, wrong bucket lineage and raw-row means.

    Predicate *presence* is necessary, not a proof that every CASE, denominator
    or Boolean formula implements the Catalog. Opaque lineage remains outside
    these bounded checks instead of rejecting every complex query.
    """
    metrics = plan.get('metric_plans') or ()
    if not metrics:
        return None
    try:
        scope = build_scope(parse_one(sql,read='postgres'))
    except (ParseError,TokenError,RecursionError):
        return None
    if scope is None:
        return None
    if plan.get('time_grain'):
        branches = scope.set_operation_scopes if isinstance(scope.expression, exp.SetOperation) else (scope,)
        for branch in branches:
            for projection in branch.expression.selects:
                if projection.alias_or_name != 'time':
                    continue
                value = projection.unalias()
                invalid = isinstance(value, exp.Null)
                if isinstance(value, exp.Literal) and value.is_string:
                    try:
                        datetime.fromisoformat(value.this)
                    except ValueError:
                        invalid = True
                if invalid:
                    return ('计划要求可验证的时间桶，time不能是非日期期间文本或NULL；'
                            '请按计划time_grain返回事实时间分组，期间合计和增长率由服务器计算。')
    for metric in metrics:
        metric_id = metric['metric_id']
        for condition in metric.get('default_filters', ()):
            required = parse_one(condition,read='postgres')
            predicate = required.this if isinstance(required,exp.Not) else required
            if not isinstance(predicate,(exp.EQ,exp.In)):
                continue
            values = predicate.expressions if isinstance(predicate,exp.In) else [predicate.expression]
            if not values or not all(isinstance(value,exp.Literal) and value.is_string for value in values):
                continue
            columns = list(predicate.find_all(exp.Column))
            if len(columns) == 1 and not _MetricFieldPresence(columns[0].table,columns[0].name).enforced(scope,metric_id):
                return f'指标 {metric_id} 的 SQL 遗漏了 Catalog 默认状态口径字段 {columns[0].sql()}，请在实际指标计算路径保留对应条件。'
        if table_grains and metric.get('grain'):
            try:
                output = lineage(metric_id,sql,schema=dict(schema),dialect='postgres')
            except (SqlglotError,OptimizeError,RecursionError):
                continue
            for item in output.walk():
                # SUM(item amount)/COUNT(raw rows) is an item mean even when
                # COUNT names the order FK. DISTINCT order counts and counts
                # over already aggregated CTE rows are different, legal cases.
                for division in item.expression.find_all(exp.Div):
                    counts = list(division.expression.find_all(exp.Count))
                    if not counts or any(isinstance(count.this, exp.Distinct) for count in counts):
                        continue
                    for total in division.this.find_all(exp.Sum):
                        for column in total.find_all(exp.Column):
                            child = next((part for part in item.downstream
                                          if part.name == column.sql(dialect='postgres')), None)
                            if child and isinstance(child.expression, exp.Table):
                                actual = table_grains.get(child.expression.name)
                                if actual and actual != metric['grain']:
                                    return (f'指标 {metric_id} 应按 {metric["grain"]} 粒度计算均值，'
                                            f'不能用 {actual} 原始行数作分母；请先按目标实体聚合或计数其去重键。')
                for average in item.expression.find_all(exp.Avg):
                    value = average.this.copy()
                    # Conditional predicates select contributors; they are not
                    # the raw value whose entity grain the AVG operates on.
                    for condition in value.find_all(exp.If):
                        condition.set('this', exp.true())
                    for condition in value.find_all(exp.Case):
                        condition.set('this', None)
                    for column in value.find_all(exp.Column):
                        child = next((part for part in item.downstream if part.name == column.sql(dialect='postgres')),None)
                        if child and isinstance(child.expression,exp.Table):
                            actual_grain = table_grains.get(child.expression.name)
                            if actual_grain and actual_grain != metric['grain']:
                                return f'指标 {metric_id} 应按 {metric["grain"]} 粒度计算均值，不能直接平均 {actual_grain} 原始行；请先按目标实体聚合。'
    if len(metrics) == 1 and plan.get('time_grain') and metrics[0].get('time_field'):
        try:
            output = lineage('time',sql,schema=dict(schema),dialect='postgres')
        except (SqlglotError,OptimizeError,RecursionError):
            return None
        sources = {f'{item.expression.name}.{item.name.rsplit(".",1)[-1]}' for item in output.walk()
                   if not item.downstream and isinstance(item.expression,exp.Table)}
        expected = metrics[0]['time_field']
        if len(sources) == 1 and sources != {expected}:
            return f'指标 {metrics[0]["metric_id"]} 的时间分桶来源应为 {expected}，不能改用 {next(iter(sources))}。'
    return None


def _unrequested_null_group_filter(sql, plan, schema):
    """Catch the observed deletion of an unclassified group, including CTEs.

    Only explicit positive IS NOT NULL on a grouped dimension is covered;
    this does not attempt to detect arbitrary extra predicates or join loss.
    """
    if plan.get("query_mode") != "aggregate" or not plan.get("dimensions") or not schema:
        return None
    try:
        root = build_scope(parse_one(sql, read="postgres"))
    except (ParseError, TokenError, RecursionError):
        return None
    if root is None:
        return None
    known = {(table, column) for table, columns in schema.items() for column in columns}
    fixed = {f["dimension"] for f in plan.get("filters", ())}
    grouped_sources = {}
    for projection in root.expression.selects:
        dimension = projection.alias_or_name
        if dimension in plan["dimensions"] and dimension not in fixed:
            grouped_sources[dimension] = _column_lineage(projection.unalias(), root, known)

    def excludes(node, scope, sources):
        if isinstance(node, (exp.Where, exp.Having, exp.Paren)):
            return excludes(node.this, scope, sources)
        if isinstance(node, exp.And):
            return excludes(node.this, scope, sources) or excludes(node.expression, scope, sources)
        if isinstance(node, exp.Or):
            return excludes(node.this, scope, sources) and excludes(node.expression, scope, sources)
        predicate = node if isinstance(node, exp.Is) and node.args.get("negate") else (
            node.this if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and not node.this.args.get("negate") else None
        )
        if predicate is not None and isinstance(predicate.expression, exp.Null):
            actual = _column_lineage(predicate.this, scope, known)
            return bool(actual) and actual <= sources
        return False

    def contributing(scope):
        yield scope
        for _, source in scope.selected_sources.values():
            if isinstance(source, Scope):
                yield from contributing(source)
        for branch in scope.set_operation_scopes:
            yield from contributing(branch)

    for scope in contributing(root):
        group = scope.expression.args.get("group")
        if group is None:
            continue
        group_sources = set().union(*(_column_lineage(expr, scope, known) for expr in group.expressions))
        for dimension, sources in grouped_sources.items():
            if sources and sources <= group_sources and any(
                excludes(scope.expression.args.get(clause), scope, sources) for clause in ("where", "having")
            ):
                return f"SQL 自行排除了分组 {dimension} 的缺失值；计划未要求排除未分类组，请保留NULL分组。"
    return None


def _metric_value_source_mismatch(sql, plan, rules, schema):
    """Check output value lineage, not mentions anywhere in WHERE/unused CTEs.

    This proves only a bounded source constraint. It does not establish SUM
    equivalence, status filtering, weighting or arbitrary metric correctness.
    """
    allocations = {r.get('metric_id') for r in plan.get('attribution_requirements', ())
                   if r.get('policy_mode') == 'payment_share'}
    if not rules:
        return None
    try:
        values_only = parse_one(sql, read='postgres')
    except (ParseError, TokenError, RecursionError):
        return None
    # FILTER/CASE predicates and window partition keys govern which rows
    # contribute, not which monetary column supplies the value. Other checks
    # own those conditions; do not reject valid conditional sums as new money.
    for node in list(values_only.find_all(exp.Filter)):
        node.replace(node.this.copy())
    for node in values_only.find_all(exp.If):
        node.set('this', exp.true())
    for node in values_only.find_all(exp.Case):
        node.set('this', None)
    for node in list(values_only.find_all(exp.Window)):
        node.replace(node.this.copy())
    for metric, rule in rules.items():
        try:
            node = lineage(metric, values_only, schema=dict(schema), dialect='postgres')
        except (SqlglotError, OptimizeError, RecursionError):
            # Malformed/unknown columns still go through policy/DB handling;
            # unsupported lineage is not silently called a semantic proof.
            continue
        leaves = [n for n in node.walk() if not n.downstream]
        if any(not isinstance(n.expression, exp.Table) and
               (isinstance(n.expression, exp.Placeholder) or n.expression.find(exp.Column)) for n in leaves):
            continue
        sources = {f'{n.expression.name}.{n.name.rsplit(".", 1)[-1]}' for n in leaves
                   if isinstance(n.expression, exp.Table)}
        if rule.get('aggregation') == 'sum':
            for entry in node.walk():
                for aggregate in entry.expression.find_all(exp.AggFunc):
                    if not isinstance(aggregate, exp.Sum) or isinstance(aggregate.this, exp.Distinct):
                        return f'指标 {metric} 应累加 Catalog 金额，不能改成均值、极值或按金额去重。'
        allowed = set(rule['allowed'])
        if metric in allocations:
            allowed.update(rule.get('allocation_allowed', ()))
        if not set(rule['required']) <= sources or sources - allowed:
            return f'指标 {metric} 的金额来源与 Catalog 不符；必须使用 {", ".join(rule["required"])}，不能用其他金额列冒名。'
        if rule.get('aggregation') == 'sum' and metric not in allocations:
            formula = _amount_affine_expression(node)
            if formula is not None and (formula[1] != 0 or any(value != 1 for value in formula[0].values())):
                return f'指标 {metric} 应按 Catalog 累加原金额，不能擅自倍增、缩减或加减常数。'
    return None


def _amount_affine_expression(entry):
    """Recognize bounded scalar changes, following contributing CTE outputs.

    A variable identifies an amount column in one contributing row stream.
    Separate CTE streams stay separate: adding disjoint subtotals is not itself
    proof of duplicate inputs. Equal-valued CASE branches with zero/NULL
    exclusions admit scalar checks. Unequal branches, weighted formulas and
    unknown functions remain outside; predicate equivalence is not proved.
    """
    def resolve(column):
        child = next((item for item in entry.downstream if item.name == column.sql(dialect='postgres')), None)
        if child is None:
            return None
        if isinstance(child.expression, exp.Table):
            return {(id(entry.source), child.expression.name, column.name): Fraction(1)}, Fraction(0)
        return _amount_affine_expression(child)

    def linear(node):
        if isinstance(node, (exp.Alias, exp.Paren, exp.Cast, exp.Sum)):
            return linear(node.this)
        if isinstance(node, (exp.Case, exp.If)):
            branches = ([condition.args['true'] for condition in node.args.get('ifs', ())]
                        + [node.args.get('default')] if isinstance(node, exp.Case)
                        else [node.args.get('true'), node.args.get('false')])
            values = [linear(branch) for branch in branches
                      if branch is not None and not isinstance(branch, exp.Null)]
            # Zero/NULL excludes a contributor. Only equal nonzero branch
            # expressions admit this scalar check; arbitrary CASE equivalence
            # remains unproved. Outer rescaling still participates normally.
            if any(value is None for value in values):
                return None
            nonzero = [value for value in values if value != ({}, Fraction(0))]
            return (nonzero[0] if all(value == nonzero[0] for value in nonzero) else None) if nonzero else ({}, Fraction(0))
        if isinstance(node, exp.Column):
            return resolve(node)
        if isinstance(node, exp.Literal) and not node.is_string:
            return {}, Fraction(node.this)
        if isinstance(node, exp.Coalesce):
            if all(isinstance(value, exp.Literal) and not value.is_string and Fraction(value.this) == 0 for value in node.expressions):
                return linear(node.this)
            return None
        if isinstance(node, exp.Neg):
            value = linear(node.this)
            return None if value is None else ({key:-coefficient for key,coefficient in value[0].items()}, -value[1])
        if not isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
            return None
        left, right = linear(node.this), linear(node.expression)
        if left is None or right is None:
            return None
        if isinstance(node, (exp.Add, exp.Sub)):
            sign = -1 if isinstance(node, exp.Sub) else 1
            values = dict(left[0])
            for key, coefficient in right[0].items():
                values[key] = values.get(key, Fraction(0)) + sign * coefficient
            return values, left[1] + sign * right[1]
        if isinstance(node, exp.Mul):
            if left[0] and right[0]:
                return None
            value, multiplier = (left, right[1]) if left[0] else (right, left[1])
        else:
            if right[0] or not right[1]:
                return None
            # Constant integer arithmetic has different PostgreSQL division
            # semantics. Only exact constant divisions enter this small proof.
            if not left[0] and (left[1] / right[1]).denominator != 1:
                return None
            value, multiplier = left, 1 / right[1]
        return {key:coefficient * multiplier for key,coefficient in value[0].items()}, value[1] * multiplier

    return linear(entry.expression)


def _rank_partition_matches(window: exp.Window, dimensions: tuple[str, ...]) -> bool:
    columns = {
        column.name.casefold()
        for column in window.args.get("partition_by") or ()
        if isinstance(column, exp.Column)
    }
    return all(
        dimension.casefold() in columns
        or (dimension == "time" and any(_TIME_PARTITION_HINT.search(column) for column in columns))
        for dimension in dimensions
    )


def _is_planned_rank(column: exp.Expression, scope: Scope, dimensions: tuple[str, ...]) -> bool:
    if not isinstance(column, exp.Column):
        return False
    for alias, (_, source) in scope.selected_sources.items():
        if (column.table and column.table != alias) or not isinstance(source, Scope):
            continue
        for projection in source.expression.selects:
            if projection.alias_or_name.casefold() != column.name.casefold():
                continue
            value = projection.unalias()
            if isinstance(value, exp.Window):
                return (
                    isinstance(value.this, (exp.RowNumber, exp.Rank, exp.DenseRank))
                    and _rank_partition_matches(value, dimensions)
                )
            if _is_planned_rank(value, source, dimensions):
                return True
    return False


def _rank_bound(node: exp.Expression | None, scope: Scope, dimensions: tuple[str, ...], limit: int) -> bool:
    if isinstance(node, (exp.Where, exp.Having, exp.Paren)):
        return _rank_bound(node.this, scope, dimensions, limit)
    if isinstance(node, exp.And):
        return (
            _rank_bound(node.this, scope, dimensions, limit)
            or _rank_bound(node.expression, scope, dimensions, limit)
        )
    if isinstance(node, exp.Or):
        return (
            _rank_bound(node.this, scope, dimensions, limit)
            and _rank_bound(node.expression, scope, dimensions, limit)
        )
    if not isinstance(node, (exp.LTE, exp.LT, exp.GTE, exp.GT)):
        return False
    column, value = node.this, node.expression
    if isinstance(node, (exp.GTE, exp.GT)):
        column, value = value, column
    if not isinstance(value, exp.Literal) or not value.is_int:
        return False
    upper = int(value.this) - int(isinstance(node, (exp.LT, exp.GT)))
    return upper <= limit and _is_planned_rank(column, scope, dimensions)


def _rank_bound_enforced(scope: Scope, dimensions: tuple[str, ...], limit: int) -> bool:
    if isinstance(scope.expression, exp.SetOperation):
        return all(_rank_bound_enforced(branch, dimensions, limit) for branch in scope.set_operation_scopes)
    if any(
        _rank_bound(scope.expression.args.get(clause), scope, dimensions, limit)
        for clause in ("where", "having")
    ):
        return True
    return any(
        isinstance(source, Scope) and _rank_bound_enforced(source, dimensions, limit)
        for _, source in scope.selected_sources.values()
    )


def _column_lineage(node, scope, known_sources, filter_value=None):
    if isinstance(node, (exp.Paren, exp.Cast, exp.Lower, exp.Upper, exp.Trim)):
        return _column_lineage(node.this, scope, known_sources, filter_value)
    if isinstance(node, exp.Coalesce):
        columns = [node.this, *node.expressions]
        if isinstance(columns[0], exp.Literal) or any(
            isinstance(part, exp.Literal) and part.this == filter_value for part in columns
        ):
            return set()
        return set().union(*(_column_lineage(part, scope, known_sources, filter_value)
                             for part in columns if not isinstance(part, exp.Literal)))
    if not isinstance(node, exp.Column):
        return set()
    found = set()
    for alias, (_, source) in scope.selected_sources.items():
        if node.table and node.table != alias:
            continue
        if isinstance(source, exp.Table):
            pair = (source.name, node.name)
            if node.table or len(scope.selected_sources) == 1 or pair in known_sources:
                found.add(pair)
        else:
            for projection in source.expression.selects:
                if projection.alias_or_name == node.name:
                    found.update(_column_lineage(projection.unalias(), source, known_sources, filter_value))
    return found


def _lookup_join_mismatch(root, definitions):
    """Catch a raw lookup joined on a known non-key, including through CTEs.

    This targets the observed fact-row multiplication. Derived/aggregate joins
    and opaque expressions are not treated as proven bad lookup joins.
    """
    allowed = set()
    lookup_tables = {item["table"] for item in definitions}
    for item in definitions:
        condition = parse_one(item["on"], read="postgres")
        allowed.add(frozenset((col.table, col.name) for col in condition.find_all(exp.Column)))
        # A preaggregate can retain the target's own key, e.g. sums grouped
        # by orders.order_id and then joined back to orders.order_id. The
        # Catalog's single-key many-to-one target is still unique; this is
        # not a join on status/city or another non-key attribute.
        if isinstance(condition, exp.EQ) and all(
            isinstance(side, exp.Column) for side in (condition.this, condition.expression)
        ):
            target = [col for col in condition.find_all(exp.Column) if col.table == item["table"]]
            if len(target) == 1:
                allowed.add(frozenset({(target[0].table, target[0].name)}))
    known_sources = set().union(*allowed) if allowed else set()
    for scope in root.traverse():
        for join in scope.expression.args.get("joins") or []:
            bound = scope.selected_sources.get(join.this.alias_or_name)
            if not bound or not isinstance(bound[1], exp.Table) or bound[1].name not in lookup_tables:
                continue
            condition = join.args.get("on")
            if condition is None:
                continue
            comparisons = []
            for equality in condition.find_all(exp.EQ):
                if not any(col.table == join.this.alias_or_name for col in equality.find_all(exp.Column)):
                    continue
                left = _column_lineage(equality.this, scope, known_sources)
                right = _column_lineage(equality.expression, scope, known_sources)
                if left and right:
                    comparisons.extend(frozenset((a, b)) for a in left for b in right)
            if comparisons and not any(pair in allowed for pair in comparisons):
                return f"原始维度表 {bound[1].name} 的关联键不符合 Catalog 的多对一关系，可能重复放大事实行；请使用已提供的关联键，已有维度字段不必重新关联原始表。"
    return None


class _FilterCheck:
    """Check positive predicates on contributing query paths, not SQL text.

    This checks equality filters, not general SQL equivalence or metric math.
    CTE output aliases are followed; unused CTEs and unrelated scalar metric
    branches cannot supply evidence for a missing condition.
    """

    def __init__(self, items):
        item = items[0]
        self.values = frozenset(item['value'] for item in items)
        self.value = item["value"] if len(self.values) == 1 else None
        self.sources = {(source["table"], source["column"]) for item in items for source in item["sources"]}
        self.value_sources = {
            item["value"]: {(source["table"], source["column"]) for source in item.get("value_sources", item["sources"])}
            for item in items
        }

    def lineage(self, node, scope):
        sources = None
        for value in self.values:
            found = _column_lineage(node, scope, self.sources, value)
            sources = found if sources is None else sources & found
        return sources or set()

    def equality(self, left, right, scope):
        if not isinstance(right, exp.Literal) or not right.is_string:
            return False
        expected = {value for value in self.values if right.this == (
            value.lower() if isinstance(left, exp.Lower) else value.upper() if isinstance(left, exp.Upper) else value)}
        sources = self.lineage(left, scope)
        if not expected:
            return False
        value_sources = set().union(*(self.value_sources[value] for value in expected))
        if isinstance(left, exp.Coalesce):
            first = self.lineage(left.this, scope)
            # A known matching display column may legitimately fall back to
            # another authorized encoding. Do not reject that normal SQL form.
            return bool(sources) and sources <= self.sources and bool(first) and first <= value_sources
        return bool(sources) and sources <= value_sources

    def literal_values(self, node, scope):
        """Recognize the complete allowed set, including IN and equivalent OR."""
        if isinstance(node, (exp.Where, exp.Having, exp.Paren)):
            return self.literal_values(node.this, scope)
        if isinstance(node, exp.And):
            left = self.literal_values(node.this, scope)
            right = self.literal_values(node.expression, scope)
            return left & right if left is not None and right is not None else (left if left is not None else right)
        if isinstance(node, exp.Or):
            left = self.literal_values(node.this, scope)
            right = self.literal_values(node.expression, scope)
            return left | right if left is not None and right is not None else None
        pairs = []
        if isinstance(node, exp.EQ):
            pairs = [(node.this, node.expression), (node.expression, node.this)]
        elif isinstance(node, exp.In) and node.expressions:
            pairs = [(node.this, value) for value in node.expressions]
            if not all(self.equality(left, right, scope) for left, right in pairs):
                return None
        values = {value for value in self.values
                  for left, right in pairs
                  if self.equality(left, right, scope)
                  and right.this == (value.lower() if isinstance(left, exp.Lower) else value.upper() if isinstance(left, exp.Upper) else value)}
        return values if values else None

    def predicate(self, node, scope):
        values = self.literal_values(node, scope)
        if values is not None:
            return values == self.values
        if isinstance(node, (exp.Where, exp.Having, exp.Paren)):
            return self.predicate(node.this, scope)
        if isinstance(node, exp.And):
            return self.predicate(node.this, scope) or self.predicate(node.expression, scope)
        if isinstance(node, exp.Or):
            return self.predicate(node.this, scope) and self.predicate(node.expression, scope)
        if isinstance(node, exp.In) and isinstance(node.args.get("query"), exp.Subquery):
            child = next((s for s in scope.subquery_scopes if s.expression is node.args["query"].this), None)
            if child is not None and len(child.expression.selects) == 1:
                left = self.lineage(node.this, scope)
                right = self.lineage(child.expression.selects[0].unalias(), child)
                # Key membership is a semijoin, just like a correlated EXISTS.
                # Unrelated projected values cannot constrain the fact key.
                if left and right and {c for _, c in left} == {c for _, c in right}:
                    return self.enforced(child)
        if isinstance(node, exp.Exists):
            child = next((s for s in scope.subquery_scopes if s.expression is node.this), None)
            if child is not None:
                def correlated(condition):
                    if isinstance(condition, (exp.Where, exp.Paren)):
                        return correlated(condition.this)
                    if isinstance(condition, exp.And):
                        return correlated(condition.this) or correlated(condition.expression)
                    if isinstance(condition, exp.EQ) and isinstance(condition.this, exp.Column) and isinstance(condition.expression, exp.Column):
                        aliases = {condition.this.table, condition.expression.table}
                        return bool(aliases & child.selected_sources.keys()) and bool(aliases & scope.selected_sources.keys())
                    return False
                return correlated(child.expression.args.get("where")) and self.enforced(child)
        return False

    def enforced(self, scope, output=None):
        query = scope.expression
        if isinstance(query, exp.SetOperation):
            return all(self.enforced(branch, output) for branch in scope.set_operation_scopes)
        if any(self.predicate(query.args.get(clause), scope) for clause in ("where", "having")):
            return True
        joins = query.args.get("joins") or []
        for join in joins:
            if not join.side and self.predicate(join.args.get("on"), scope):
                return True
        # A filtered row CTE joined by an identity key constrains this fact
        # stream. A CROSS JOIN of separately aggregated metrics does not.
        for alias, (_, source) in scope.selected_sources.items():
            if not isinstance(source, Scope):
                continue
            if any(isinstance(node, exp.AggFunc) for projection in source.expression.selects for node in projection.walk()):
                continue
            linked = len(scope.selected_sources) == 1 or any(
                not join.side and any(
                    isinstance(eq.this, exp.Column) and isinstance(eq.expression, exp.Column)
                    and alias in {eq.this.table, eq.expression.table}
                    and eq.this.table != eq.expression.table
                    for eq in (join.args.get("on").find_all(exp.EQ) if join.args.get("on") else ())
                ) for join in joins
            )
            if linked and self.enforced(source):
                return True
        projections = [projection for projection in query.selects
                       if output is None or projection.alias_or_name == output]
        if projections and all(isinstance(p.unalias(), exp.Filter) and
                               self.predicate(p.unalias().expression, scope) for p in projections):
            return True
        paths = []
        for projection in projections:
            for column in projection.find_all(exp.Column):
                for alias, (_, source) in scope.selected_sources.items():
                    if isinstance(source, Scope) and (not column.table or column.table == alias):
                        if any(p.alias_or_name == column.name for p in source.expression.selects):
                            paths.append((source, column.name))
        if not paths and any(isinstance(p.unalias(), exp.Count) for p in projections):
            paths = [(source, None) for _, source in scope.selected_sources.values() if isinstance(source, Scope)]
        return bool(paths) and all(self.enforced(source, name) for source, name in paths)


class _MetricFieldPresence(_FilterCheck):
    """Reuse the contributing-path traversal for a necessary row-filter field."""

    def __init__(self, table, column):
        self.sources = {(table,column)}
        self.values = frozenset()
        self.value_sources = {}

    def lineage(self, node, scope):
        return _column_lineage(node,scope,self.sources)

    def predicate(self, node, scope):
        if node is None:
            return False
        if isinstance(node,(exp.Where,exp.Having,exp.Paren)):
            return self.predicate(node.this,scope)
        if isinstance(node,exp.And):
            return self.predicate(node.this,scope) or self.predicate(node.expression,scope)
        if isinstance(node,exp.Or):
            return self.predicate(node.this,scope) and self.predicate(node.expression,scope)
        if isinstance(node,exp.Exists):
            return super().predicate(node,scope)
        return any(self.lineage(column,scope) & self.sources for column in node.find_all(exp.Column))

    def enforced(self, scope, output=None):
        if super().enforced(scope,output):
            return True
        projections = [projection for projection in scope.expression.selects
                       if output is None or projection.alias_or_name == output]
        return any(self.predicate(condition.this,scope)
                   for projection in projections for condition in projection.find_all(exp.If))
