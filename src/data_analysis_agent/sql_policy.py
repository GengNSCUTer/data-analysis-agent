"""AST-based SQL policy for the Olist analytics schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlglot import exp, parse
from sqlglot.errors import ParseError

from .workspace import WorkspaceProfile


ANALYTICS_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "dataset_versions": frozenset(
        {
            "dataset_version_id", "dataset_id", "source_url", "source_license",
            "source_version", "archive_sha256", "transform_version", "loaded_at",
        }
    ),
    "dim_customers": frozenset(
        {
            "customer_id", "customer_unique_id", "customer_zip_code_prefix",
            "customer_city", "customer_state", "dataset_version_id",
        }
    ),
    "dim_sellers": frozenset(
        {
            "seller_id", "seller_zip_code_prefix", "seller_city", "seller_state",
            "dataset_version_id",
        }
    ),
    "dim_category_translation": frozenset(
        {"product_category_name", "product_category_name_english", "dataset_version_id"}
    ),
    "dim_products": frozenset(
        {
            "product_id", "product_category_name", "product_name_length",
            "product_description_length", "product_photos_qty", "product_weight_g",
            "product_length_cm", "product_height_cm", "product_width_cm",
            "dataset_version_id",
        }
    ),
    "fact_orders": frozenset(
        {
            "order_id", "customer_id", "order_status", "order_purchase_timestamp",
            "order_approved_at", "order_delivered_carrier_date",
            "order_delivered_customer_date", "order_estimated_delivery_date",
            "dataset_version_id",
        }
    ),
    "fact_order_items": frozenset(
        {
            "order_id", "order_item_id", "product_id", "seller_id",
            "shipping_limit_date", "price", "freight_value", "dataset_version_id",
        }
    ),
    "fact_payments": frozenset(
        {
            "order_id", "payment_sequential", "payment_type", "payment_installments",
            "payment_value", "dataset_version_id",
        }
    ),
    "fact_reviews": frozenset(
        {"review_id", "order_id", "review_score", "review_creation_date", "dataset_version_id"}
    ),
}

ANALYST_TABLES: Final[frozenset[str]] = frozenset(ANALYTICS_COLUMNS) - {"dataset_versions"}
SENSITIVE_PROJECTION_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "customer_id", "customer_unique_id", "customer_zip_code_prefix", "seller_id",
        "seller_zip_code_prefix", "order_id", "review_id", "product_id",
        "dataset_version_id", "archive_sha256", "source_url",
    }
)
FORBIDDEN_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {"PG_SLEEP", "PG_READ_FILE", "PG_READ_BINARY_FILE", "LO_IMPORT", "DBLINK", "SET_CONFIG"}
)
FORBIDDEN_NODES: Final[tuple[type[exp.Expression], ...]] = (
    exp.Alter, exp.Command, exp.Copy, exp.Create, exp.Delete, exp.Drop, exp.Insert,
    exp.Into, exp.Lock, exp.Merge, exp.Transaction, exp.TruncateTable, exp.Update,
)


class PolicyViolation(ValueError):
    """A query was rejected before it reached PostgreSQL."""


@dataclass(frozen=True)
class PolicyDecision:
    original_sql: str
    final_sql: str
    role: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    row_limit: int
    status: str = "allowed"
    reason: str = "SQL passed the AST policy"


class SqlPolicy:
    """Validate and normalize a single read-only query for a workspace."""

    def __init__(
        self,
        analyst_limit: int = 200,
        admin_limit: int = 1000,
        workspace: WorkspaceProfile | None = None,
    ):
        self.limits = {"analyst": analyst_limit, "admin": admin_limit}
        self.workspace = workspace
        self.analytics_columns = (
            dict(workspace.allowed_columns) if workspace else ANALYTICS_COLUMNS
        )
        self.analyst_tables = workspace.analyst_tables if workspace else ANALYST_TABLES
        self.sensitive_projection_columns = (
            workspace.sensitive_projection_columns
            if workspace
            else SENSITIVE_PROJECTION_COLUMNS
        )
        self.analytics_schema = workspace.analytics_schema if workspace else "analytics"
        self.sql_dialect = workspace.sql_dialect if workspace else "postgres"
        self.all_columns = frozenset().union(*self.analytics_columns.values())

    def evaluate(self, sql: str, role: str = "analyst") -> PolicyDecision:
        if role not in self.limits:
            raise PolicyViolation(f"unsupported role: {role}")
        if not sql.strip():
            raise PolicyViolation("SQL must not be empty")
        try:
            statements = [statement for statement in parse(sql, read="postgres") if statement]
        except ParseError as exc:
            raise PolicyViolation(f"SQL parse failed: {exc}") from exc
        if len(statements) != 1:
            raise PolicyViolation("exactly one SQL statement is allowed")
        statement = statements[0]
        if not isinstance(statement, exp.Query):
            raise PolicyViolation("only SELECT or read-only WITH queries are allowed")
        forbidden = next((node for node in statement.walk() if isinstance(node, FORBIDDEN_NODES)), None)
        if forbidden is not None:
            raise PolicyViolation(f"forbidden SQL operation: {forbidden.key}")

        cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
        cte_outputs = {
            cte.alias_or_name.lower(): self._cte_output_columns(cte)
            for cte in statement.find_all(exp.CTE)
        }
        cte_column_refs = dict(cte_outputs)
        for table in statement.find_all(exp.Table):
            cte_name = table.name.lower()
            if not table.db and cte_name in cte_outputs:
                cte_column_refs[cte_name] = cte_outputs[cte_name]
                cte_column_refs[table.alias_or_name.lower()] = cte_outputs[cte_name]
        allowed_tables = (
            frozenset(self.analytics_columns)
            if role == "admin"
            else self.analyst_tables
        )
        tables: set[str] = set()
        for table in statement.find_all(exp.Table):
            table_name = table.name.lower()
            if table_name in cte_names and not table.db:
                continue
            schema = table.db.lower() if table.db else self.analytics_schema
            if schema != self.analytics_schema or table.catalog:
                raise PolicyViolation(f"schema is not allowed: {schema}")
            if table_name not in allowed_tables:
                raise PolicyViolation(f"table is not allowed for {role}: {table_name}")
            if not table.db:
                table.set("db", exp.to_identifier(self.analytics_schema))
            tables.add(table_name)
        if not tables:
            raise PolicyViolation("query must read at least one allowed analytics table")

        derived_aliases = {
            alias.alias.lower() for alias in statement.find_all(exp.Alias) if alias.alias
        }
        columns = {column.name.lower() for column in statement.find_all(exp.Column)}
        unknown_columns = {
            column.name.lower()
            for column in statement.find_all(exp.Column)
            if column.name.lower() not in self.all_columns
            and not (not column.table and column.name.lower() in derived_aliases)
            and not self._is_known_cte_column(column, cte_column_refs)
        }
        if unknown_columns:
            raise PolicyViolation(f"columns are not allowed: {', '.join(sorted(unknown_columns))}")
        if role == "analyst":
            for select in statement.find_all(exp.Select):
                # A CTE is an internal relational stage.  Its grouping/join
                # keys never reach the caller unless an outer SELECT projects
                # them, and that outer projection is checked below.  Rejecting
                # the internal key here forced otherwise safe grain-preserving
                # queries into an unnecessary model retry.
                if select.find_ancestor(exp.CTE) is not None:
                    continue
                for projection in select.expressions:
                    if isinstance(projection, exp.Star):
                        raise PolicyViolation("analyst queries cannot project all columns")
                    blocked = {
                        column.name.lower()
                        for column in self._projection_columns(projection)
                        if column.name.lower() in self.sensitive_projection_columns
                        and column.find_ancestor(exp.Count) is None
                    }
                    if blocked:
                        raise PolicyViolation(
                            f"analyst cannot project sensitive columns: {', '.join(sorted(blocked))}"
                        )
                    alias = projection.alias if isinstance(projection, exp.Alias) else ""
                    if alias and alias.lower() in self.sensitive_projection_columns:
                        raise PolicyViolation(
                            f"analyst cannot use sensitive columns as result aliases: {alias.lower()}"
                        )

                for clause_name in ("group", "order"):
                    clause = select.args.get(clause_name)
                    if clause is None:
                        continue
                    blocked = {
                        column.name.lower()
                        for column in clause.find_all(exp.Column)
                        if column.name.lower() in self.sensitive_projection_columns
                    }
                    if blocked:
                        raise PolicyViolation(
                            f"analyst cannot use sensitive columns in {clause_name.upper()} BY: "
                            f"{', '.join(sorted(blocked))}"
                        )

        for function in statement.find_all(exp.Func):
            function_name = (function.name or function.sql_name()).upper()
            if function_name in FORBIDDEN_FUNCTIONS:
                raise PolicyViolation(f"function is not allowed: {function_name}")

        maximum = self.limits[role]
        limit_expression = statement.args.get("limit")
        if limit_expression is None:
            statement = statement.limit(maximum)
            row_limit = maximum
        else:
            value = limit_expression.expression
            if not isinstance(value, exp.Literal) or not value.is_int:
                raise PolicyViolation("LIMIT must be a literal integer")
            requested = int(value.this)
            if requested <= 0:
                raise PolicyViolation("LIMIT must be greater than zero")
            row_limit = min(requested, maximum)
            if requested > maximum:
                statement.set("limit", exp.Limit(expression=exp.Literal.number(maximum)))

        return PolicyDecision(
            original_sql=sql,
            final_sql=statement.sql(dialect=self.sql_dialect),
            role=role,
            tables=tuple(sorted(tables)),
            columns=tuple(sorted(columns)),
            row_limit=row_limit,
        )

    @staticmethod
    def _cte_output_columns(cte: exp.CTE) -> frozenset[str] | None:
        """Return explicitly known columns exported by one CTE.

        ``None`` is deliberately used for ``SELECT *`` or an otherwise
        unaliased/opaque projection.  The policy must not turn an opaque CTE
        into a wildcard that permits arbitrary qualified column names.
        """
        alias = cte.args.get("alias")
        explicit = alias.args.get("columns") if alias is not None else None
        if explicit:
            return frozenset(identifier.name.lower() for identifier in explicit)

        query = cte.this
        if not isinstance(query, exp.Select):
            return frozenset()
        output_names: set[str] = set()
        for projection in query.expressions:
            if isinstance(projection, exp.Star):
                return None
            if isinstance(projection, exp.Alias) and projection.alias:
                output_names.add(projection.alias.lower())
                continue
            if isinstance(projection, exp.Column):
                output_names.add(projection.name.lower())
                continue
            # sqlglot exposes a stable output_name for a few simple
            # expressions.  If it cannot prove one, fail closed.
            output_name = getattr(projection, "output_name", "")
            if output_name:
                output_names.add(str(output_name).lower())
            else:
                return None
        return frozenset(output_names)

    @staticmethod
    def _is_known_cte_column(
        column: exp.Column, cte_outputs: dict[str, frozenset[str] | None]
    ) -> bool:
        if not column.table:
            return False
        outputs = cte_outputs.get(column.table.lower())
        return outputs is not None and column.name.lower() in outputs

    @classmethod
    def _projection_columns(cls, projection: exp.Expression):
        """Yield columns belonging to the current SELECT projection only.

        A scalar subquery is validated independently when its own ``Select`` is
        visited.  Traversing its columns again as if they were outer projected
        fields incorrectly rejects safe joins such as ``SUM(items.price)``
        because their internal ``order_id`` join keys are sensitive.
        """
        if isinstance(projection, exp.Column):
            yield projection
            return
        for child in projection.iter_expressions():
            if isinstance(child, (exp.Select, exp.Subquery, exp.CTE)):
                continue
            if isinstance(child, exp.Column):
                yield child
            else:
                yield from cls._projection_columns(child)
