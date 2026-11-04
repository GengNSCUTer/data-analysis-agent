"""Replay reviewed SQL to measure diagnostic coverage/cost, without an LLM.

This measures candidate admission, not Chinese understanding or model accuracy.
Business reads use each workspace reader; existing query audits use its writer.
"""

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
from time import perf_counter
import uuid

from sqlglot import exp, parse_one
import yaml

from data_analysis_agent.join_evidence import summarize_join_evidence
from data_analysis_agent.postgres_runner import PostgresConnectionSettings, SecurePostgresRunner
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.result_validator import ResultValidationError
from data_analysis_agent.runtime_paths import RESOURCES
from data_analysis_agent.runtime_workspace import load_runtime_workspace
from data_analysis_agent.semantic_catalog import CatalogLoader, CatalogRetriever, ResultContract
from data_analysis_agent.trusted_sql_tool import TrustedRunSqlTool
from data_analysis_agent.sql_policy import PolicyViolation
from vanna.capabilities.sql_runner import RunSqlToolArgs
from vanna.core.tool import ToolContext
from vanna.core.user import User
from vanna.integrations.local.agent_memory import DemoAgentMemory


OLIST = [
    ('gmv', 'SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id'),
    ('freight', 'SELECT SUM(i.freight_value) AS freight_amount FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id'),
    ('preaggregate', 'WITH a AS (SELECT order_id,SUM(price) AS amount FROM analytics.fact_order_items GROUP BY order_id) SELECT SUM(a.amount) AS gmv FROM a JOIN analytics.fact_orders o ON o.order_id=a.order_id'),
    ('conditional', "SELECT SUM(CASE WHEN o.order_status='delivered' THEN i.price ELSE 0 END) AS gmv FROM analytics.fact_order_items i JOIN analytics.fact_orders o ON o.order_id=i.order_id"),
    ('distinct_count', 'SELECT COUNT(DISTINCT o.order_id) AS paid_order_count FROM analytics.fact_orders o JOIN analytics.fact_order_items i ON i.order_id=o.order_id'),
]


def named_preaggregation(sql):
    """Keep oracle arithmetic but name inline aggregate relations per Policy."""
    tree = parse_one(sql, read='postgres')
    for index, subquery in enumerate(list(tree.find_all(exp.Subquery))):
        if subquery.find(exp.Group) and subquery.alias:
            name = f'review_amount_{index}'
            value = subquery.this.copy()
            alias = subquery.args['alias'].copy()
            subquery.replace(exp.Table(this=exp.to_identifier(name), alias=alias))
            tree = tree.with_(name, as_=value)
    return tree.sql(dialect='postgres')


async def run(args):
    suite = yaml.safe_load(args.cases.read_text())
    records = []
    for name, database in [('olist', args.olist_database), ('thelook', args.thelook_database)]:
        workspace = load_runtime_workspace(RESOURCES / f'workspaces/{name}.yaml').profile
        catalog = CatalogLoader(workspace=workspace).load()
        retriever = CatalogRetriever(catalog)
        runner = SecurePostgresRunner(settings=replace(PostgresConnectionSettings.from_environment(), database=database), workspace=workspace)
        tool = TrustedRunSqlTool(runner)
        cases = OLIST if name == 'olist' else [(c['id'], c['reference_sql']) for c in suite['cases'] if c['category'] == 'metric']
        for case_id, sql in cases:
            sql = named_preaggregation(sql)
            columns = parse_one(sql, read='postgres').named_selects
            metrics = [m for m in columns if m in catalog.metrics_by_id]
            user = User(id='join-diagnostics-review', group_memberships=['analyst'])
            selection = retriever.retrieve('查看指标', user, metric_ids=metrics)
            budget = ResultBudget.aggregate(execution_max_rows=runner.settings.max_aggregate_rows,
                statement_timeout_ms=runner.settings.statement_timeout_ms, result_max_bytes=runner.settings.max_result_bytes)
            contract = ResultContract.from_selection(selection, '查看指标', None, catalog=catalog,
                required_result_columns=columns, result_budget=budget)
            context = ToolContext(user=user, conversation_id='join-review-' + uuid.uuid4().hex,
                request_id=uuid.uuid4().hex, agent_memory=DemoAgentMemory(),
                metadata={**contract.as_tool_metadata(), 'query_plan': {**budget.as_metadata(), 'metric_ids':metrics}})
            started = perf_counter()
            record = {'workspace':name, 'case_id':case_id, 'expected_safe':True}
            mismatch = tool._candidate_mismatch(sql, context)
            if mismatch:
                record.update(accepted=False, reason=mismatch)
            else:
                try:
                    await runner.run_sql(RunSqlToolArgs(sql=sql), context)
                    record['accepted'] = True
                except (ResultValidationError, PolicyViolation) as exc:
                    record.update(accepted=False, reason=str(exc))
            record['total_ms'] = round((perf_counter() - started) * 1000, 3)
            record['evidence'] = context.metadata.get('join_evidence')
            records.append(record)
    diagnosed = [r for r in records if r['evidence'] is not None]
    return {
        'scope':'reviewed_sql_admission_without_llm',
        'count':len(records), 'accepted':sum(r['accepted'] for r in records),
        'diagnostics':summarize_join_evidence(diagnosed),
        'records':records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=Path('evals/cases/thelook_product_queries.yaml'))
    parser.add_argument('--olist-database', default=PostgresConnectionSettings.from_environment().database)
    parser.add_argument('--thelook-database', default='thelook_analytics')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k:v for k,v in report.items() if k != 'records'}, ensure_ascii=False))
    return int(report['accepted'] != report['count'])


if __name__ == '__main__':
    raise SystemExit(main())
