"""Opt-in integration coverage for the project-local PostgreSQL trust boundary."""

from __future__ import annotations

import os
import asyncio
import time
import threading
from dataclasses import replace
import uuid

import psycopg2
import pytest

from data_analysis_agent.postgres_runner import (
    PostgresConnectionSettings,
    SecurePostgresRunner,
    _cancellable_query,
)
from data_analysis_agent.budget import BudgetedToolRegistry, BudgetUsage, RequestBudget
from data_analysis_agent.budget import BudgetSafetyMiddleware
from data_analysis_agent.chat_runtime import BudgetedChatHandler
from data_analysis_agent.conversation_store import PostgresConversationStore
from data_analysis_agent.result_validator import ResultValidationError, ResultValidator
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.run_recorder import PostgresRunRecorder
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever
from data_analysis_agent.sql_policy import PolicyViolation, SqlPolicy
from data_analysis_agent.sql_repair import SafeSqlExecutionError
from data_analysis_agent.query_filters import QueryFilter
from data_analysis_agent.trusted_sql_tool import TrustedRunSqlTool
from vanna import Agent
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.agent.config import AgentConfig
from vanna.core.llm import LlmRequest, LlmResponse, LlmService, LlmStreamChunk
from vanna.core.tool import ToolCall, ToolContext
from vanna.core.user import RequestContext
from vanna.core.user.resolver import UserResolver
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory
from vanna.integrations.local.file_system import LocalFileSystem
from vanna.servers.base.models import ChatRequest


pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def require_project_database() -> None:
    if os.getenv("RUN_PROJECT_DB") != "1":
        pytest.skip("set RUN_PROJECT_DB=1 to run against the project-local PostgreSQL instance")


def make_context(user_id: str, role: str = "analyst") -> ToolContext:
    return ToolContext(
        user=User(id=user_id, group_memberships=[role]),
        conversation_id=f"postgres-integration-{uuid.uuid4().hex}",
        request_id=f"postgres-integration-{uuid.uuid4().hex}",
        agent_memory=DemoAgentMemory(),
        metadata={"question": "integration test"},
    )


@pytest.mark.asyncio
async def test_runtime_month_buckets_use_utc_even_if_database_connection_defaults_to_local_zone(monkeypatch):
    connect = psycopg2.connect

    def local_zone_connection(*args, **kwargs):
        if kwargs.get("user") == "daa_analytics_reader":
            kwargs["options"] = "-c timezone=Asia/Shanghai"
        return connect(*args, **kwargs)

    monkeypatch.setattr(psycopg2, "connect", local_zone_connection)
    context = make_context("utc-calendar-test")
    context.metadata.update({"required_result_columns": ["time", "paid_order_count"],
                             "metric_result_columns": ["paid_order_count"], "result_time_column": "time"})
    statement = ("SELECT DATE_TRUNC('month', TIMESTAMPTZ '2023-01-31 23:30:00+00') AS time, "
                 "COUNT(*) AS paid_order_count FROM analytics.fact_orders")
    frame = await SecurePostgresRunner().run_sql(RunSqlToolArgs(sql=statement), context)
    assert frame.iloc[0]["time"].isoformat() == "2023-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_runtime_join_probe_rejects_repeated_item_inputs():
    """Actual reader evidence rejects a fanout even when SUM returns one row."""
    context = make_context(f"join-proof-{uuid.uuid4().hex}")
    context.metadata.update({
        "required_result_columns": ["gmv"], "metric_result_columns": ["gmv"],
        "metric_fact_grains": {"gmv": "order_item"},
        "join_source_keys": {"fact_order_items": ["order_id", "order_item_id"]},
        "join_source_grains": {"fact_order_items": "order_item", "fact_orders": "order"},
    })
    runner = SecurePostgresRunner()
    good = "SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id"
    frame = await runner.run_sql(RunSqlToolArgs(sql=good), context)
    assert len(frame) == 1
    assert context.metadata["join_evidence"]["status"] == "checked"
    assert all(n == 1 for n in context.metadata["join_multiplicity"].values())
    bad = good + " CROSS JOIN (SELECT 1 AS n UNION ALL SELECT 2 AS n) duplicate_rows"
    with pytest.raises(ResultValidationError, match="重复放大"):
        await runner.run_sql(RunSqlToolArgs(sql=bad), context)
    assert context.metadata["join_evidence"]["status"] == "amplified"
    evidence = context.metadata["join_evidence"]["checks"][0]
    assert evidence["duplicate_group_input_rows"] == 2 * evidence["duplicate_group_distinct_facts"]


@pytest.mark.asyncio
@pytest.mark.parametrize('shape,accepted,status', [
    ('weighted', False, 'partial'), ('missing_identity', False, 'partial'), ('missing_all_identity', False, 'partial'),
    ('preaggregate', True, 'partial'), ('max', True, 'partial'), ('distinct', True, 'not_applicable'),
])
async def test_partial_join_admission_and_telemetry_use_real_reader(shape, accepted, status):
    context = make_context('partial-' + uuid.uuid4().hex)
    keys = {'fact_order_items':['order_id','order_item_id'], 'fact_orders':['order_id'],
            'fact_payments':['order_id','payment_sequential']}
    context.metadata.update({
        'required_result_columns':['gmv'], 'metric_result_columns':['gmv'],
        'metric_fact_grains':{'gmv':'order_item'}, 'join_source_keys':keys,
        'join_source_grains':{'fact_order_items':'order_item','fact_orders':'order','fact_payments':'payment'},
        'budget_usage': BudgetUsage(RequestBudget()),
    })
    if shape == 'preaggregate':
        sql = ('WITH amounts AS (SELECT order_id,SUM(price) AS amount FROM analytics.fact_order_items GROUP BY order_id) '
               'SELECT SUM(a.amount) AS gmv FROM amounts a JOIN analytics.fact_orders o ON o.order_id=a.order_id')
    else:
        aggregate = {'weighted':'SUM(i.price*p.payment_value)', 'missing_identity':'SUM(i.price)', 'missing_all_identity':'SUM(i.price)',
                     'max':'MAX(i.price)', 'distinct':'SUM(DISTINCT i.price)'}[shape]
        sql = f'SELECT {aggregate} AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id'
        if shape == 'missing_identity':
            del keys['fact_order_items']
        elif shape == 'missing_all_identity':
            keys.clear()
    runner = SecurePostgresRunner()
    if accepted:
        assert len(await runner.run_sql(RunSqlToolArgs(sql=sql), context)) == 1
    else:
        with pytest.raises(ResultValidationError, match='尚未核实'):
            await runner.run_sql(RunSqlToolArgs(sql=sql), context)
    evidence = context.metadata['join_evidence']
    assert evidence['status'] == status
    assert evidence['rejected'] is not accepted
    assert evidence['elapsed_ms'] >= 0
    assert evidence['executed_checks'] == len(evidence['checks'])
    assert context.metadata['budget_usage'].catalog_trace['join_evidence_attempts'] == [evidence]


@pytest.mark.asyncio
async def test_payment_value_aliased_as_gmv_is_rejected_before_database(tmp_path):
    context = make_context('gmv-source-' + uuid.uuid4().hex)
    context.metadata.update({
        'query_plan':{'metric_ids':['gmv']}, 'required_result_columns':['gmv'], 'metric_result_columns':['gmv'],
        'metric_value_sources':{'gmv':{'required':['fact_order_items.price'], 'allowed':['fact_order_items.price','fact_orders.order_status']}},
    })
    runner = SecurePostgresRunner()
    tool = TrustedRunSqlTool(runner, file_system=LocalFileSystem(str(tmp_path)))
    result = await tool.execute(context, RunSqlToolArgs(sql='SELECT SUM(p.payment_value) AS gmv FROM analytics.fact_payments p'))
    assert not result.success and 'Catalog' in result.error
    assert context.metadata['safe_sql_error'].retryable
    assert 'result_artifact' not in context.metadata and 'result_validation' not in context.metadata
    # A correctly sourced candidate still runs the complete real execution chain.
    result = await tool.execute(context, RunSqlToolArgs(sql='SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i'))
    assert result.success and context.metadata['result_validation']['state'] == 'valid'


@pytest.mark.asyncio
@pytest.mark.parametrize('expression', ['SUM(i.price)*2', 'SUM(i.price/2)', 'SUM(i.price)+1'])
async def test_amount_formula_scalar_changes_are_refused_by_the_complete_tool(expression, tmp_path):
    context = make_context('amount-formula-' + uuid.uuid4().hex)
    context.metadata.update({
        'query_plan':{'metric_ids':['gmv']}, 'required_result_columns':['gmv'], 'metric_result_columns':['gmv'],
        'metric_value_sources':{'gmv':{'aggregation':'sum','required':['fact_order_items.price'],
                                    'allowed':['fact_order_items.price']}},
    })
    tool = TrustedRunSqlTool(SecurePostgresRunner(), file_system=LocalFileSystem(str(tmp_path)))
    result = await tool.execute(context, RunSqlToolArgs(sql=f'SELECT {expression} AS gmv FROM analytics.fact_order_items i'))
    assert not result.success
    assert 'result_artifact' not in context.metadata
    assert context.metadata['safe_sql_error'].retryable


@pytest.mark.asyncio
async def test_monthly_state_top_five_executes_all_twelve_partitions(tmp_path):
    from collections import Counter
    from data_analysis_agent.query_plan import QueryPlan
    from data_analysis_agent.question_router import QuestionRouter
    from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, ResultContract
    from data_analysis_agent.semantic_intent import SemanticIntent, select_intent_catalog, route_semantic_intent
    from data_analysis_agent.result_budget import ResultBudget
    from data_analysis_agent.runtime_workspace import load_runtime_workspace
    context = make_context('partition-budget-' + uuid.uuid4().hex)
    runner = SecurePostgresRunner(workspace=load_runtime_workspace().profile)
    catalog = CatalogLoader(runner.workspace).load()
    router = QuestionRouter(CatalogRetriever(catalog),full_results=True)
    intent = SemanticIntent.model_validate({'request_kind':'data_query','metric_ids':['gmv'],
        'dimensions':['customer_state'],'top_n':5,'time_grain':'month',
        'time_range':{'start':'2017-01-01','end':'2018-01-01'}})
    selection = select_intent_catalog(intent,router.retriever,context.user)
    route = route_semantic_intent(intent,router,selection,max_result_rows=5000)
    plan = QueryPlan.from_selection(selection,'',route,
        {'semantic_intent':intent.as_dict(),'time_range':intent.time_range.model_dump()},
        result_budget=ResultBudget.aggregate())
    assert plan.result_budget.aggregate_result_rows == 60
    contract = ResultContract.from_selection(selection,'',plan.time_range,catalog=catalog,
        required_result_columns=plan.required_result_columns,requested_dimensions=plan.dimensions,
        result_budget=plan.result_budget)
    context.metadata.update({**contract.as_tool_metadata(),'query_plan':plan.as_dict()})
    sql = ("WITH monthly AS (SELECT DATE_TRUNC('month',o.order_purchase_timestamp) AS time,"
        "c.customer_state,SUM(i.price) AS gmv FROM analytics.fact_orders o "
        'JOIN analytics.fact_order_items i ON i.order_id=o.order_id '
        'JOIN analytics.dim_customers c ON c.customer_id=o.customer_id '
        "WHERE o.order_status NOT IN ('canceled','unavailable') AND o.order_purchase_timestamp >= DATE '2017-01-01' "
        "AND o.order_purchase_timestamp < DATE '2018-01-01' GROUP BY 1,2), "
        'ranked AS (SELECT time,customer_state,gmv,ROW_NUMBER() OVER (PARTITION BY time ORDER BY gmv DESC) AS rn '
        'FROM monthly) SELECT time,customer_state,gmv FROM ranked WHERE rn<=5 ORDER BY time,gmv DESC')
    tool = TrustedRunSqlTool(runner,file_system=LocalFileSystem(str(tmp_path)))
    result = await tool.execute(context,RunSqlToolArgs(sql=sql))
    assert result.success, (result.error,context.metadata.get('join_evidence'))
    rows = result.metadata['results']
    # The disposable fixture has three states/month, still 36 > the old 27
    # cap. There must be twelve complete partitions without manufacturing rows.
    counts = Counter(str(row['time'])[:7] for row in rows)
    assert len(rows)==36 and len(counts)==12
    assert set(counts.values()) == {3}


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate', [False, True])
async def test_shared_order_view_join_is_safe_only_when_its_identity_is_preserved(tmp_path, duplicate):
    context = make_context('shared-view-' + uuid.uuid4().hex)
    context.metadata.update({'required_result_columns':['gmv'],'metric_result_columns':['gmv'],
        'metric_fact_grains':{'gmv':'order_item'},
        'join_source_keys':{'fact_order_items':['order_id','order_item_id'],'fact_orders':['order_id']},
        'join_source_grains':{'fact_order_items':'order_item','fact_orders':'order'}})
    projection = "SELECT order_id FROM analytics.fact_orders WHERE order_status NOT IN ('canceled','unavailable')"
    if duplicate:
        projection += ' UNION ALL '+projection
    sql = (f'WITH valid_orders AS ({projection}), amounts AS ('
        'SELECT i.order_id,SUM(i.price) AS amount FROM analytics.fact_order_items i '
        'JOIN valid_orders v ON v.order_id=i.order_id GROUP BY i.order_id) '
        'SELECT SUM(a.amount) AS gmv FROM amounts a JOIN valid_orders v ON v.order_id=a.order_id')
    tool = TrustedRunSqlTool(SecurePostgresRunner(),file_system=LocalFileSystem(str(tmp_path)))
    result = await tool.execute(context,RunSqlToolArgs(sql=sql))
    assert result.success is not duplicate, result.error
    evidence = context.metadata['join_evidence']
    assert evidence['status'] == ('amplified' if duplicate else 'partial')
    if not duplicate:
        settings = PostgresConnectionSettings.from_environment()
        with psycopg2.connect(host=settings.host,port=settings.port,database=settings.database,user=settings.reader_user) as reader:
            reader.set_session(readonly=True)
            with reader.cursor() as cursor:
                cursor.execute("SELECT SUM(i.price) FROM analytics.fact_order_items i JOIN analytics.fact_orders o "
                               "ON o.order_id=i.order_id WHERE o.order_status NOT IN ('canceled','unavailable')")
                expected = cursor.fetchone()[0]
        assert float(result.metadata['results'][0]['gmv']) == pytest.approx(float(expected))


@pytest.mark.asyncio
@pytest.mark.parametrize('risk', ['status', 'mean_grain', 'time_source'])
async def test_catalog_formula_fields_have_positive_and_negative_complete_tool_paths(risk, tmp_path):
    context = make_context('formula-field-' + uuid.uuid4().hex)
    metric = 'average_order_value' if risk == 'mean_grain' else 'gmv'
    plan = {'metric_ids':[metric],'metric_plans':[{'metric_id':metric,
        'grain':'order' if risk == 'mean_grain' else 'order_item',
        'time_field':'fact_orders.order_purchase_timestamp',
        'default_filters':["fact_orders.order_status NOT IN ('canceled','unavailable')"]}]}
    context.metadata.update({'query_plan':plan, 'metric_result_columns':[metric],
        'required_result_columns':[metric], 'metric_fact_grains':{metric:plan['metric_plans'][0]['grain']},
        'join_source_keys':{'fact_orders':['order_id'],'fact_order_items':['order_id','order_item_id']},
        'join_source_grains':{'fact_orders':'order','fact_order_items':'order_item'}})
    source = 'analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id'
    status = "o.order_status NOT IN ('canceled','unavailable')"
    if risk == 'status':
        wrong = f'SELECT SUM(i.price) AS gmv FROM {source}'
        right = wrong + f' WHERE {status}'
    elif risk == 'mean_grain':
        wrong = f'SELECT AVG(i.price) AS average_order_value FROM {source} WHERE {status}'
        right = (f'WITH totals AS (SELECT i.order_id,SUM(i.price) AS amount FROM {source} '
                 f'WHERE {status} GROUP BY i.order_id) SELECT AVG(amount) AS average_order_value FROM totals')
    else:
        plan['time_grain'] = 'month'
        context.metadata['required_result_columns'] = ['time','gmv']
        context.metadata['result_time_column'] = 'time'
        wrong = (f"SELECT DATE_TRUNC('month',o.order_approved_at) AS time,SUM(i.price) AS gmv "
                 f'FROM {source} WHERE {status} GROUP BY 1')
        right = wrong.replace('o.order_approved_at','o.order_purchase_timestamp')
    tool = TrustedRunSqlTool(SecurePostgresRunner(),file_system=LocalFileSystem(str(tmp_path)))
    rejected = await tool.execute(context,RunSqlToolArgs(sql=wrong))
    assert not rejected.success and 'result_artifact' not in context.metadata
    accepted = await tool.execute(context,RunSqlToolArgs(sql=right))
    assert accepted.success, accepted.error
    assert context.metadata['result_validation']['state'] == 'valid'


@pytest.mark.asyncio
async def test_one_order_two_items_two_payments_keeps_300_instead_of_600(tmp_path, request):
    """Owned disposable database only: the same order can have two payments."""
    admin_dsn = os.getenv('RESULT_EVIDENCE_TEST_ADMIN_DSN')
    if not admin_dsn or os.getenv('DATA_ANALYSIS_DATASET_VERSION') != 'synthetic-olist-v1':
        pytest.skip('requires the disposable synthetic database administrator')
    from psycopg2 import sql as pg_sql
    from psycopg2.extras import RealDictCursor
    settings = PostgresConnectionSettings.from_environment()
    order_id = uuid.uuid4().hex
    with psycopg2.connect(admin_dsn) as admin:
        # A mistaken DSN must never insert fixture rows in the real business DB.
        assert admin.info.port == settings.port and admin.info.port != 35434
        assert admin.info.dbname == settings.database
        with admin.cursor(cursor_factory=RealDictCursor) as cursor:
            def copy_row(table, changes):
                cursor.execute(pg_sql.SQL('SELECT * FROM analytics.{} LIMIT 1').format(pg_sql.Identifier(table)))
                row = dict(cursor.fetchone())
                row.update(changes)
                cursor.execute(pg_sql.SQL('INSERT INTO analytics.{} ({}) VALUES ({})').format(
                    pg_sql.Identifier(table),pg_sql.SQL(',').join(map(pg_sql.Identifier,row)),
                    pg_sql.SQL(',').join(pg_sql.Placeholder() for _ in row)),list(row.values()))
            copy_row('fact_orders',{'order_id':order_id,'order_status':'delivered'})
            for number,price in [(1,100),(2,200)]:
                copy_row('fact_order_items',{'order_id':order_id,'order_item_id':number,'price':price})
            for number in (1,2):
                copy_row('fact_payments',{'order_id':order_id,'payment_sequential':number,'payment_value':150})
    def remove_owned_rows():
        with psycopg2.connect(admin_dsn) as connection:
            with connection.cursor() as cursor:
                for table in ('fact_payments','fact_order_items','fact_orders'):
                    cursor.execute(pg_sql.SQL('DELETE FROM analytics.{} WHERE order_id=%s').format(
                        pg_sql.Identifier(table)),(order_id,))
    request.addfinalizer(remove_owned_rows)
    source = 'analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id'
    condition = f"o.order_id='{order_id}' AND o.order_status NOT IN ('canceled','unavailable')"
    correct = f'SELECT SUM(i.price) AS gmv FROM {source} WHERE {condition}'
    fanout = f'SELECT SUM(i.price) AS gmv FROM {source} JOIN analytics.fact_payments p ON p.order_id=o.order_id WHERE {condition}'
    with psycopg2.connect(host=settings.host,port=settings.port,database=settings.database,user=settings.reader_user) as reader:
        reader.set_session(readonly=True)
        with reader.cursor() as cursor:
            cursor.execute(correct)
            assert cursor.fetchone()[0] == 300
            cursor.execute(fanout)
            assert cursor.fetchone()[0] == 600
    def context():
        result = make_context('minimal-join-' + uuid.uuid4().hex)
        result.metadata.update({'required_result_columns':['gmv'],'metric_result_columns':['gmv'],
            'metric_fact_grains':{'gmv':'order_item'},
            'join_source_keys':{'fact_order_items':['order_id','order_item_id'],'fact_orders':['order_id'],
                                'fact_payments':['order_id','payment_sequential']},
            'join_source_grains':{'fact_order_items':'order_item','fact_orders':'order','fact_payments':'payment'}})
        return result
    tool = TrustedRunSqlTool(SecurePostgresRunner(),file_system=LocalFileSystem(str(tmp_path)))
    good = await tool.execute(context(),RunSqlToolArgs(sql=correct))
    assert good.success
    rejected_context = context()
    bad = await tool.execute(rejected_context,RunSqlToolArgs(sql=fanout))
    assert not bad.success and rejected_context.metadata['join_evidence']['status'] == 'amplified'


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate', [False, True])
async def test_valid_payment_share_allocation_and_duplicated_weights(duplicate):
    context = make_context('allocation-' + uuid.uuid4().hex)
    context.metadata.update({
        'required_result_columns':['payment_type','gmv'], 'metric_result_columns':['gmv'],
        'metric_fact_grains':{'gmv':'order_item'},
        'join_source_keys':{'fact_order_items':['order_id','order_item_id'], 'fact_payments':['order_id','payment_sequential']},
        'join_source_grains':{'fact_order_items':'order_item', 'fact_payments':'payment'},
        'metric_value_sources':{'gmv':{'required':['fact_order_items.price']}},
        'query_plan':{'attribution_requirements':[{'metric_id':'gmv', 'policy_mode':'payment_share'}]},
    })
    extra = ' CROSS JOIN (SELECT 1 AS n UNION ALL SELECT 2 AS n) copies' if duplicate else ''
    sql = ('WITH totals AS (SELECT order_id,SUM(payment_value) AS total FROM analytics.fact_payments GROUP BY order_id) '
           'SELECT p.payment_type,SUM(i.price*p.payment_value/NULLIF(t.total,0)) AS gmv '
           'FROM analytics.fact_order_items i JOIN analytics.fact_payments p ON p.order_id=i.order_id '
           'JOIN totals t ON t.order_id=i.order_id' + extra + ' GROUP BY p.payment_type')
    if duplicate:
        with pytest.raises(ResultValidationError, match='重复放大'):
            await SecurePostgresRunner().run_sql(RunSqlToolArgs(sql=sql), context)
    else:
        assert len(await SecurePostgresRunner().run_sql(RunSqlToolArgs(sql=sql), context)) > 0
    evidence = context.metadata['join_evidence']
    assert evidence['status'] == ('amplified' if duplicate else 'checked')
    assert evidence['checks'][0]['mode'] == 'allocation'
    assert bool(evidence['checks'][0]['allocation_violating_facts']) is duplicate


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", [False, True])
async def test_private_key_preaggregation_probe_runs_as_analyst_and_still_detects_fanout(duplicate):
    context = make_context(f"private-group-{uuid.uuid4().hex}")
    context.metadata.update({
        "required_result_columns": ["customer_state", "gmv"], "metric_result_columns": ["gmv"],
        "metric_fact_grains": {"gmv": "order_item"},
        "join_source_keys": {"fact_order_items": ["order_id", "order_item_id"], "fact_orders": ["order_id"], "dim_customers": ["customer_id"]},
        "join_source_grains": {"fact_order_items": "order_item", "fact_orders": "order", "dim_customers": "customer"},
    })
    extra = " CROSS JOIN (SELECT 1 AS n UNION ALL SELECT 2 AS n) copies" if duplicate else ""
    sql = (
        "WITH amount AS (SELECT i.order_id, SUM(i.price) AS amount "
        "FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id"
        + extra + " GROUP BY i.order_id) SELECT c.customer_state, SUM(a.amount) AS gmv FROM amount a "
        "JOIN analytics.fact_orders o ON o.order_id=a.order_id "
        "JOIN analytics.dim_customers c ON c.customer_id=o.customer_id GROUP BY c.customer_state"
    )
    runner = SecurePostgresRunner()
    if duplicate:
        with pytest.raises(ResultValidationError, match="重复放大"):
            await runner.run_sql(RunSqlToolArgs(sql=sql), context)
        assert context.metadata["join_evidence"]["status"] == "amplified"
    else:
        frame = await runner.run_sql(RunSqlToolArgs(sql=sql), context)
        assert list(frame.columns) == ["customer_state", "gmv"]
        assert context.metadata["join_evidence"]["checks"]
        assert all(value == 1 for value in context.metadata["join_multiplicity"].values())
    with pytest.raises(PolicyViolation, match="sensitive"):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT order_id FROM analytics.fact_orders"), context)


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_filter", [True, False])
async def test_grouping_error_repair_rechecks_plan_before_real_reader(keep_filter, tmp_path):
    """Use a controlled candidate, real PostgreSQL error, policy and validation."""
    context = make_context(f"repair-chain-{uuid.uuid4().hex}")
    context.metadata.update({
        "required_result_columns": ["customer_state", "paid_order_count"],
        "metric_result_columns": ["paid_order_count"],
        "query_plan": {"metric_ids": ["paid_order_count"], "filters": [
            {"dimension": "customer_state", "value": "SP",
             "sources": [{"table": "dim_customers", "column": "customer_state"}]},
        ]},
    })
    original = (
        "SELECT c.customer_state, COUNT(DISTINCT o.order_id) AS paid_order_count "
        "FROM analytics.fact_orders o JOIN analytics.dim_customers c ON c.customer_id=o.customer_id "
        "WHERE o.order_status NOT IN ('canceled', 'unavailable') AND c.customer_state='SP'"
    )
    prompts = []

    async def candidate(prompt, _):
        prompts.append(prompt)
        sql = original if keep_filter else original.replace(" AND c.customer_state='SP'", "")
        return sql + " GROUP BY c.customer_state"

    runner = SecurePostgresRunner(result_validator=ResultValidator())
    tool = TrustedRunSqlTool(
        runner, repair_provider=candidate, repair_policy=SqlPolicy(),
        file_system=LocalFileSystem(str(tmp_path)),
    )
    result = await tool.execute(context, RunSqlToolArgs(sql=original))
    assert len(prompts) == 1 and "syntax" in prompts[0] and "current_query_plan" in prompts[0]
    assert result.success is keep_filter
    audits = runner.audit.list_recent(context.user.id, "analyst")
    assert len(audits) == 2 and audits[1]["policy_status"] == "execution_error"
    if not keep_filter:
        assert audits[0]["policy_status"] == "rejected"
        assert result.metadata["terminal_reason"] == "plan_sql_mismatch"
        return
    assert audits[0]["policy_status"] == "allowed"
    assert context.metadata["result_validation"]["state"] == "valid"
    with psycopg2.connect(
        host=runner.settings.host, port=runner.settings.port,
        database=runner.settings.database, user=runner.settings.reader_user,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM analytics.fact_orders WHERE order_status NOT IN ('canceled','unavailable') "
                "AND customer_id IN (SELECT customer_id FROM analytics.dim_customers WHERE customer_state='SP')"
            )
            expected = cursor.fetchone()[0]
    assert result.metadata["results"] == [{"customer_state": "SP", "paid_order_count": expected}]


@pytest.mark.asyncio
async def test_cancel_stops_actual_postgres_worker_and_closes_connection():
    started = threading.Event()
    connections = []
    settings = PostgresConnectionSettings.from_environment()

    def slow_query(cancellation):
        connection = psycopg2.connect(host=settings.host, port=settings.port, database=settings.database,
                                      user=settings.reader_user, application_name='stage5-cancel-test')
        connections.append(connection)
        try:
            cancellation.attach(connection)
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '10s'")
                started.set()
                # Test the cancellation transport, not an online allowed function.
                cursor.execute('SELECT pg_sleep(8)')
        finally:
            cancellation.close(connection)

    worker = asyncio.create_task(_cancellable_query(slow_query))
    assert await asyncio.to_thread(started.wait, 2)
    await asyncio.sleep(.05)
    begin = time.monotonic()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert time.monotonic() - begin < 2
    assert connections[0].closed
    connection = psycopg2.connect(host=settings.host, port=settings.port, database=settings.database, user=settings.reader_user)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name='stage5-cancel-test'")
            assert cursor.fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_actual_reader_statement_timeout_is_audited_without_partial_result():
    settings = replace(PostgresConnectionSettings.from_environment(), statement_timeout_ms=1)
    runner = SecurePostgresRunner(settings=settings)
    context = make_context('stage5-timeout-' + uuid.uuid4().hex[:8])
    with pytest.raises(SafeSqlExecutionError):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT SUM(i.price) AS gmv FROM fact_order_items i JOIN fact_orders o ON o.order_id=i.order_id WHERE o.order_status NOT IN ('canceled','unavailable')"), context)
    assert context.metadata['sql_error']['category'] == 'timeout'
    assert not context.metadata.get('result_contract_satisfied')
    assert runner.audit.list_recent(context.user.id, 'analyst')[0]['policy_status'] == 'execution_error'


@pytest.mark.asyncio
async def test_display_filter_resolves_to_its_actual_reader_column():
    runner = SecurePostgresRunner()
    sources = (("dim_products", "product_category_name"),
               ("dim_category_translation", "product_category_name"),
               ("dim_category_translation", "product_category_name_english"))
    filters = (QueryFilter("customer_state", "SP", (("dim_customers", "customer_state"),)),
               QueryFilter("product_category_name", "bed_bath_table", sources))
    resolved = await runner.resolve_filter_sources(filters)
    assert resolved[0] == filters[0]
    assert resolved[1].sources == sources
    assert resolved[1].value_sources == (("dim_category_translation", "product_category_name_english"),)
    raw = await runner.resolve_filter_sources((QueryFilter("product_category_name", "cama_mesa_banho", sources),))
    assert raw[0].value_sources == sources[:2]
    # Values are parameters. A quote/SQL-looking value is neither executed
    # nor turned into a mandatory clarification when it has no match.
    unknown = QueryFilter("product_category_name", "missing' OR TRUE --", sources)
    assert await runner.resolve_filter_sources((unknown,)) == (unknown,)


@pytest.mark.asyncio
async def test_runner_executes_allowed_query_and_audits_policy_rejection() -> None:
    user_id = f"postgres-test-{uuid.uuid4().hex[:12]}"
    runner = SecurePostgresRunner()
    context = make_context(user_id)
    envelope = ResultBudget.aggregate().as_metadata()
    context.metadata.update({
        **envelope,
        "required_result_columns": ["customer_state", "paid_order_count"],
        "metric_result_columns": ["paid_order_count"],
        "additive_metric_ids": ["paid_order_count"],
        "query_plan": {**envelope, "top_n": {"customer_state": 5}},
    })

    dataframe = await runner.run_sql(
        RunSqlToolArgs(
            sql=(
                "SELECT customer_state, COUNT(DISTINCT order_id) AS paid_order_count "
                "FROM fact_orders JOIN dim_customers USING (customer_id) "
                "WHERE order_status NOT IN ('canceled', 'unavailable') "
                "GROUP BY customer_state ORDER BY paid_order_count DESC LIMIT 5"
            )
        ),
        context,
    )

    # Check against this fixture's facts, not the real Olist snapshot's top state.
    # The bundled synthetic database has three states; LIMIT 5 need not yield five.
    with psycopg2.connect(
        host=runner.settings.host, port=runner.settings.port,
        database=runner.settings.database, user=runner.settings.reader_user,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT c.customer_state, COUNT(DISTINCT o.order_id) "
                "FROM analytics.fact_orders o JOIN analytics.dim_customers c "
                "ON c.customer_id=o.customer_id "
                "WHERE o.order_status NOT IN ('canceled', 'unavailable') "
                "GROUP BY c.customer_state"
            )
            expected = dict(cursor.fetchall())
    assert len(dataframe) == min(5, len(expected))
    assert dataframe['paid_order_count'].tolist() == sorted(expected.values(), reverse=True)[:5]
    assert all(expected[row.customer_state] == row.paid_order_count for row in dataframe.itertuples())
    assert context.metadata["result_budget_check"]["state"] == "validated"
    from data_analysis_agent.result_evidence import RESULT_EVIDENCE_PREFIX
    import json
    evidence = json.loads(context.metadata["validated_result_summary"].removeprefix(RESULT_EVIDENCE_PREFIX))
    assert evidence["coverage"] == "complete"
    assert evidence["statistics"]["paid_order_count"]["sum_of_returned_rows"] == sum(sorted(expected.values(), reverse=True)[:5])

    with pytest.raises(PolicyViolation, match="only SELECT"):
        await runner.run_sql(RunSqlToolArgs(sql="DELETE FROM fact_orders"), context)

    audits = runner.audit.list_recent(user_id, "analyst")
    assert [audit["policy_status"] for audit in audits[:2]] == ["rejected", "allowed"]
    assert audits[1]["question"] == "integration test"
    assert "analytics.fact_orders" in audits[1]["final_sql"]


@pytest.mark.asyncio
async def test_runner_audits_unimplemented_modes_without_a_reader_query() -> None:
    runner = SecurePostgresRunner()
    context = make_context(f"budget-mode-{uuid.uuid4().hex[:12]}")
    envelope = ResultBudget(query_mode="export", aggregate_result_rows=None).as_metadata()
    context.metadata.update({**envelope, "query_plan": envelope})
    with pytest.raises(PolicyViolation, match="export is not enabled"):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT 1 AS gmv"), context)
    audit = runner.audit.list_recent(context.user.id, "analyst")[0]
    assert audit["policy_status"] == "rejected"
    assert audit["final_sql"] is None
    assert audit["policy_reason"] == "result_budget:query_mode export is not enabled"


@pytest.mark.asyncio
async def test_runner_rejects_columns_outside_the_server_result_contract() -> None:
    """A Policy-allowed projection must still satisfy the server contract."""
    runner = SecurePostgresRunner()
    context = make_context(f"exact-columns-{uuid.uuid4().hex[:12]}")
    context.metadata.update(
        {
            "required_result_columns": ["paid_order_count"],
            "metric_result_columns": ["paid_order_count"],
            "exact_result_columns": True,
            "metric_value_constraints": {
                "paid_order_count": {"minimum": 0, "integer_like": True}
            },
        }
    )

    frame = await runner.run_sql(
        RunSqlToolArgs(
            sql=(
                "SELECT COUNT(DISTINCT order_id) AS paid_order_count, "
                "'extra' AS extra FROM fact_orders"
            )
        ),
        context,
    )

    validation = context.metadata["result_validation"]
    assert validation["state"] == "degraded"
    assert list(frame.columns) == ["paid_order_count"]
    assert context.metadata.get("result_contract_satisfied") is None
    assert "extra" in context.metadata["result_display_notice"]


@pytest.mark.asyncio
async def test_empty_query_preserves_database_column_metadata():
    runner = SecurePostgresRunner()
    context = make_context(f"empty-columns-{uuid.uuid4().hex[:12]}")
    context.metadata.update({"required_result_columns": ["customer_state", "gmv"],
                             "metric_result_columns": ["gmv"], "exact_result_columns": True})
    frame = await runner.run_sql(RunSqlToolArgs(sql=(
        "SELECT customer_state, SUM(price) AS gmv FROM fact_order_items "
        "JOIN fact_orders USING(order_id) JOIN dim_customers USING(customer_id) "
        "WHERE order_purchase_timestamp >= TIMESTAMP '2020-01-01' GROUP BY customer_state"
    )), context)
    assert frame.empty and list(frame.columns) == ["customer_state", "gmv"]
    validation = context.metadata["result_validation"]
    assert validation["state"] == "degraded" and not validation["missing_columns"]
    assert "没有返回数据" in validation["reason"]
    assert not context.metadata.get("result_contract_satisfied")


@pytest.mark.asyncio
async def test_runner_treats_resource_cap_as_possible_truncation() -> None:
    """An unplanned query's resource cap cannot prove result completeness."""
    runner = SecurePostgresRunner(replace(PostgresConnectionSettings.from_environment(), max_rows=2))
    context = make_context(f"policy-limit-{uuid.uuid4().hex[:12]}")

    frame = await runner.run_sql(
        RunSqlToolArgs(sql="SELECT customer_state FROM dim_customers"), context
    )

    validation = context.metadata["result_validation"]
    assert validation["state"] == "degraded"
    assert validation["truncated"] is True
    assert validation["row_count"] == 2
    assert len(frame) == 2
    assert context.metadata.get("result_contract_satisfied") is None


@pytest.mark.asyncio
async def test_result_contract_state_flows_through_real_runner_and_budget_registry(tmp_path) -> None:
    """Exercise the production runner/tool/registry boundary against PostgreSQL."""
    suffix = uuid.uuid4().hex[:12]
    user_id = f"contract-chain-{suffix}"
    settings = PostgresConnectionSettings.from_environment()
    usage = BudgetUsage(RequestBudget(max_tool_calls=3, max_sql_calls=2))
    runner = SecurePostgresRunner(
        settings=settings,
        result_validator=ResultValidator(settings.max_rows),
        model_name="integration-test-model",
    )
    registry = BudgetedToolRegistry()
    registry.register_local_tool(
        TrustedRunSqlTool(
            sql_runner=runner,
            file_system=LocalFileSystem(str(tmp_path)),
        ),
        access_groups=["analyst"],
    )
    context = ToolContext(
        user=User(id=user_id, group_memberships=["analyst"]),
        conversation_id=f"contract-conversation-{suffix}",
        request_id=f"contract-request-{suffix}",
        agent_memory=DemoAgentMemory(),
        metadata={
            "question": "统计有效订单数",
            "budget_usage": usage,
            "required_result_columns": ["paid_order_count"],
            "metric_result_columns": ["paid_order_count"],
        },
    )

    first = await registry.execute(
        ToolCall(
            id="first-sql",
            name="run_sql",
            arguments={
                "sql": (
                    "SELECT COUNT(DISTINCT order_id) AS paid_order_count "
                    "FROM fact_orders "
                    "WHERE order_status NOT IN ('canceled', 'unavailable')"
                )
            },
        ),
        context,
    )

    assert first.success is True
    assert context.metadata["result_validation"]["state"] == "valid"
    assert context.metadata["result_contract_satisfied"] is True
    assert usage.result_contract_satisfied is True
    assert usage.result_summary
    assert usage.sql_calls_used == 1
    assert usage.tool_calls_used == 1

    second = await registry.execute(
        ToolCall(
            id="redundant-sql",
            name="run_sql",
            arguments={"sql": "SELECT 1 AS paid_order_count"},
        ),
        context,
    )

    assert second.success is True
    assert second.metadata["suppressed_after_result_contract"] is True
    assert "不要再次执行 SQL" in second.result_for_llm
    assert usage.sql_calls_used == 1
    assert usage.tool_calls_used == 1
    assert usage.extra_sql_suppressed == 1
    assert usage.termination_reason == "completed"

    audits = runner.audit.list_recent(user_id, "analyst")
    assert len(audits) == 1
    assert audits[0]["policy_status"] == "allowed"


@pytest.mark.asyncio
async def test_sse_agent_middleware_removes_redundant_sql_after_real_contract(tmp_path) -> None:
    """Verify the LLM-response guard, not only the registry fallback, against PostgreSQL."""
    suffix = uuid.uuid4().hex[:12]
    user = User(id=f"contract-sse-{suffix}", group_memberships=["analyst"])
    conversation_id = f"contract-sse-conversation-{suffix}"
    settings = PostgresConnectionSettings.from_environment()

    class Resolver(UserResolver):
        async def resolve_user(self, request_context: RequestContext) -> User:
            return user

    class SequencedLlm(LlmService):
        def __init__(self) -> None:
            self.requests: list[LlmRequest] = []

        async def send_request(self, request: LlmRequest) -> LlmResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                return LlmResponse(
                    tool_calls=[
                        ToolCall(
                            id="validated-query",
                            name="run_sql",
                            arguments={
                                "sql": (
                                    "SELECT COUNT(DISTINCT order_id) AS paid_order_count "
                                    "FROM fact_orders "
                                    "WHERE order_status NOT IN ('canceled', 'unavailable')"
                                )
                            },
                        )
                    ]
                )
            return LlmResponse(
                content="这条调用会由合同守卫抑制。",
                tool_calls=[
                    ToolCall(
                        id="redundant-query",
                        name="run_sql",
                        arguments={"sql": "SELECT 1 AS paid_order_count"},
                    )
                ],
            )

        async def stream_request(self, request: LlmRequest):
            yield LlmStreamChunk(content="")

        async def validate_tools(self, tools) -> list[str]:
            return []

    store = PostgresConversationStore(settings)
    recorder = PostgresRunRecorder(settings, model_name="fixed-sequence-test")
    runner = SecurePostgresRunner(
        settings=settings,
        result_validator=ResultValidator(settings.max_rows),
        model_name="fixed-sequence-test",
    )
    registry = BudgetedToolRegistry()
    registry.register_local_tool(
        TrustedRunSqlTool(
            sql_runner=runner,
            file_system=LocalFileSystem(str(tmp_path)),
        ),
        access_groups=["analyst"],
    )
    llm = SequencedLlm()
    agent = Agent(
        llm_service=llm,
        tool_registry=registry,
        user_resolver=Resolver(),
        agent_memory=DemoAgentMemory(),
        conversation_store=store,
        llm_middlewares=[BudgetSafetyMiddleware()],
        config=AgentConfig(max_tool_iterations=4, stream_responses=False, temperature=0.0),
    )
    handler = BudgetedChatHandler(
        agent,
        RequestBudget(max_tool_iterations=4, max_tool_calls=4, max_sql_calls=2),
        recorder,
        question_router=QuestionRouter(CatalogRetriever(CatalogLoader().load())),
    )
    request = ChatRequest(
        message="统计有效订单数",
        conversation_id=conversation_id,
        request_context=RequestContext(),
    )

    try:
        chunks = [chunk async for chunk in handler.handle_stream(request)]
        assert chunks
        assert len(llm.requests) == 2

        connection = psycopg2.connect(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.writer_user,
        )
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT sql_calls_used, tool_calls_used, termination_reason, catalog_trace "
                    "FROM app.agent_runs WHERE request_id = %s",
                    (request.request_id,),
                )
                run = cursor.fetchone()
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM app.query_audits WHERE request_id = %s",
                    (request.request_id,),
                )
                audit_count = cursor.fetchone()["count"]
        finally:
            connection.close()

        assert run["sql_calls_used"] == 1
        assert run["tool_calls_used"] == 1
        assert run["termination_reason"] == "completed"
        assert run["catalog_trace"]["result_contract_satisfied"] is True
        assert run["catalog_trace"]["extra_sql_suppressed"] == 1
        assert audit_count == 1
    finally:
        connection = psycopg2.connect(
            host=settings.host,
            port=settings.port,
            database=settings.database,
            user=settings.writer_user,
        )
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM app.conversations WHERE conversation_id = %s",
                        (conversation_id,),
                    )
        finally:
            connection.close()


@pytest.mark.parametrize(
    ("role", "query"),
    [
        ("daa_analytics_reader", "SELECT * FROM app.query_audits LIMIT 1"),
        ("daa_app_writer", "SELECT * FROM analytics.fact_orders LIMIT 1"),
    ],
)
def test_database_roles_cannot_cross_schema_boundaries(role: str, query: str) -> None:
    settings = PostgresConnectionSettings.from_environment()
    connection = psycopg2.connect(
        host=settings.host,
        port=settings.port,
        database=settings.database,
        user=role,
    )
    try:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cursor.execute(query)
    finally:
        connection.close()
