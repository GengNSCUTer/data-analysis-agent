"""Reject candidate SQL that drops a server-owned query-plan rule.

The SQL model still writes the statement. Equality filters follow contributing
AST scopes; ranking and attribution checks are additional bounded checks, not
a proof of SQL equivalence or metric correctness.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, TokenError
from sqlglot.optimizer.scope import Scope, build_scope


_LIMIT = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)
_TOP_N_DIMENSIONS = {"customer_city": 20, "seller_city": 20}
_TIME_PARTITION_HINT = re.compile(
    r"(?:time|date|day|week|month|quarter|year|period|timestamp)",
    re.IGNORECASE,
)


def sql_plan_mismatch(sql: str, plan: Mapping[str, Any] | None) -> str | None:
    """Return a user-safe reason when SQL omits a required plan rule."""

    if not isinstance(sql, str) or not sql.strip() or not isinstance(plan, Mapping):
        return None
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
        partition_sets = [
            {
                column.name.casefold()
                for column in (window.args.get("partition_by") or ())
                if isinstance(column, exp.Column)
            }
            for window in rank_windows
        ]
        partitioned_as_planned = any(
            all(
                (
                    dimension == "time"
                    and any(_TIME_PARTITION_HINT.search(column) for column in columns)
                )
                or dimension.casefold() in columns
                for dimension in partition_dimensions
            )
            for columns in partition_sets
        )
        if not partitioned_as_planned:
            return f"分区 Top-N 缺少计划分区字段：{', '.join(partition_dimensions)}。"
        # PostgreSQL has no QUALIFY. The model must filter the generated rank
        # in an outer query; inspect the generated rank alias so an unrelated
        # date/value predicate cannot satisfy this contract by accident.
        rank_aliases = {
            alias.alias_or_name.casefold()
            for alias in root.find_all(exp.Alias)
            if isinstance(alias.this, exp.Window)
            and isinstance(alias.this.this, (exp.RowNumber, exp.Rank, exp.DenseRank))
        }
        rank_filter_values = []
        for predicate in (*root.find_all(exp.LTE), *root.find_all(exp.LT)):
            column = predicate.this
            value = predicate.expression
            if (
                isinstance(column, exp.Column)
                and column.name.casefold() in rank_aliases
                and isinstance(value, exp.Literal)
                and not value.is_string
            ):
                rank_filter_values.append((type(predicate), int(value.this)))
        if not any(
            (kind is exp.LTE and value <= limit)
            or (kind is exp.LT and value <= limit + 1)
            for kind, value in rank_filter_values
        ):
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
    for requirement in plan.get("attribution_requirements") or []:
        if not isinstance(requirement, Mapping):
            continue
        mode = requirement.get("policy_mode")
        dimension = str(requirement.get("dimension") or "")
        if mode == "payment_share" and (
            "payment_value" not in folded or "payment_type" not in folded
        ):
            return "按支付方式拆金额时，SQL 必须用 payment_value 占比分摊，并按 payment_type 汇总。"
        if mode == "multi_membership" and dimension and dimension.casefold() not in folded:
            return f"SQL 必须包含分组列 {dimension}，并允许同一订单进入多个分组。"
    return None


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
            return all(self.enforced(branch, output) for branch in scope.union_scopes)
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
