"""Failure accounting for count-only diagnostics, with no database/model access."""

from time import perf_counter
from types import SimpleNamespace

import pandas as pd
import psycopg2
import pytest

from data_analysis_agent.budget import BudgetUsage, RequestBudget
from data_analysis_agent.postgres_runner import SecurePostgresRunner
from data_analysis_agent.result_validator import ResultValidationError


@pytest.mark.parametrize('failure', ['deadline', 'database'])
def test_diagnostic_failure_still_records_partial_cost_and_rejection(failure):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, *args):
            raise psycopg2.OperationalError('private host detail must not be recorded')

    usage = BudgetUsage(RequestBudget())
    context = SimpleNamespace(metadata={
        'join_source_keys': {'fact_order_items':['order_id','order_item_id']},
        'join_source_grains': {'fact_order_items':'order_item'},
        'metric_fact_grains': {'gmv':'order_item'}, 'budget_usage':usage,
    })
    connection = SimpleNamespace(cursor=Cursor)
    runner = SecurePostgresRunner()
    sql = ('SELECT SUM(i.price) AS gmv FROM analytics.fact_order_items i '
           'JOIN analytics.fact_orders o ON o.order_id=i.order_id')
    for attempt in range(2):
        with pytest.raises(ResultValidationError):
            runner._check_join_evidence(connection, sql, context, 'analyst',
                                        perf_counter() + (-1 if failure == 'deadline' else 5),
                                        pd.DataFrame({'gmv':[1]}))
        evidence = context.metadata['join_evidence']
        assert evidence['status'] == 'partial'
        assert evidence['rejection_reason'] == ('timeout' if failure == 'deadline' else 'diagnostic_failed')
        assert evidence['elapsed_ms'] >= 0 and evidence['executed_checks'] == 0
        assert len(usage.catalog_trace['join_evidence_attempts']) == attempt + 1
        assert 'private host' not in str(evidence)
