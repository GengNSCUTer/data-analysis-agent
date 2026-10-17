from __future__ import annotations

import pytest
from datetime import date
from dataclasses import dataclass, replace
import json
from vanna.core.user import User

from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.semantic_intent import (
    SemanticIntentError,
    SemanticIntentInterpreter,
    RelativeTime,
    parse_semantic_intent,
    resolve_relative_time,
    route_semantic_intent,
    select_intent_catalog,
)
from data_analysis_agent.question_router import QuestionRouter
from data_analysis_agent.semantic_catalog import CatalogRetriever
from data_analysis_agent.metric_context import OLIST_RUNTIME_WORKSPACE
from data_analysis_agent.query_plan import QueryPlan
from data_analysis_agent.result_budget import ResultBudget
from data_analysis_agent.semantic_catalog import ResultContract
from data_analysis_agent.candidate_sql_generator import build_runtime_candidate_request


def test_parse_semantic_intent_rejects_unknown_catalog_objects() -> None:
    catalog = CatalogLoader().load()
    with pytest.raises(SemanticIntentError, match="unknown_or_invisible_metric"):
        parse_semantic_intent(
            '{"request_kind":"data_query","metric_ids":["gmv","made_up"],'
            '"dimensions":["customer_state","seller_id"],"time_grain":"month",'
            '"time_range":{"start":"2017-01-01","end":"2017-02-01",'
            '"end_semantics":"exclusive"},"confidence":0.9}',
            catalog,
        )


def test_parse_semantic_intent_rejects_invalid_json_or_kind() -> None:
    catalog = CatalogLoader().load()
    with pytest.raises(SemanticIntentError):
        parse_semantic_intent("not json", catalog)
    with pytest.raises(SemanticIntentError):
        parse_semantic_intent('{"request_kind":"sql"}', catalog)


def test_intent_preserves_structured_shape_without_reparsing_text() -> None:
    catalog = CatalogLoader().load()
    intent = parse_semantic_intent(
        '{"request_kind":"data_query","metric_ids":["gmv"],'
        '"dimensions":["customer_state"],"time_grain":"month",'
        '"time_range":{"start":"2017-01-01","end":"2017-02-01",'
        '"end_semantics":"exclusive"},"assumptions":["按月"]}',
        catalog,
    )
    assert intent is not None
    assert intent.metric_ids == ("gmv",)
    assert intent.dimensions == ("customer_state",)
    assert intent.time_grain == "month"
    assert intent.time_range.start == "2017-01-01"


def test_relative_time_is_resolved_by_server_calendar() -> None:
    assert resolve_relative_time(
        RelativeTime(period="current_month"),
        date(2026, 10, 14),
    ).model_dump() == {
        "start": "2026-10-01",
        "end": "2026-11-01",
        "end_semantics": "exclusive",
    }


def test_semantic_route_never_allows_unknown_metric_or_missing_metric() -> None:
    catalog = CatalogLoader().load()
    router = QuestionRouter(CatalogRetriever(catalog))
    intent = parse_semantic_intent(
        '{"request_kind":"data_query","metric_ids":[],"clarification":"你想看销售额还是订单量？"}',
        catalog,
    )
    selection = select_intent_catalog(
        intent, router.retriever, User(id="u"), question="最近经营情况"
    )
    route = route_semantic_intent(intent, router, selection)
    assert route.state == "clarification_required"
    assert route.reason_code == "semantic_ambiguity"


@pytest.mark.asyncio
async def test_interpreter_calls_json_boundary_only() -> None:
    catalog = CatalogLoader().load()
    calls: list[str] = []

    async def complete(prompt: str, user: User) -> str:
        calls.append(prompt)
        return '{"request_kind":"data_query","metric_ids":["gmv"],"confidence":0.8}'

    interpreter = SemanticIntentInterpreter(catalog, complete)
    intent = await interpreter.interpret("最近销售额怎么样", User(id="u"))

    assert intent is not None
    assert intent.metric_ids == ("gmv",)
    assert calls and "不生成 SQL" in calls[0]


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"metric_ids": ["not_a_metric"]}, "unknown_or_invisible_metric"),
        ({"dimensions": ["seller_id"]}, "unknown_or_invisible_dimension"),
        (
            {"filters": [{"dimension": "password", "value": "x"}]},
            "unknown_or_invisible_filter",
        ),
        ({"role": "admin"}, "invalid_intent_output"),
        (
            {"time_range": {"start": "2017-02-30", "end": "2018-01-01"}},
            "invalid_intent_output",
        ),
        (
            {"time_range": {"start": "2018-01-01", "end": "2017-01-01"}},
            "invalid_intent_output",
        ),
        ({"metric_ids": ["gmv", "gmv"]}, "invalid_intent_output"),
    ],
)
def test_invalid_proposals_are_not_silently_trimmed(payload, reason):
    with pytest.raises(SemanticIntentError, match=reason):
        parse_semantic_intent(
            json.dumps({"request_kind": "data_query", **payload}),
            CatalogLoader().load(),
        )


def test_role_scope_applies_to_model_prompt_and_parser():
    catalog = CatalogLoader().load()
    hidden = replace(catalog.metrics[0], role_visibility=frozenset({"admin"}))
    catalog = replace(catalog, metrics=(hidden,))
    user = User(id="analyst", group_memberships=["analyst"])
    with pytest.raises(SemanticIntentError, match="invisible"):
        parse_semantic_intent(
            json.dumps(
                {
                    "request_kind": "data_query",
                    "metric_ids": [hidden.metric_id],
                }
            ),
            catalog,
            user=user,
        )
    interpreter = SemanticIntentInterpreter(catalog, None)
    catalog_line = next(
        line
        for line in interpreter.prompt("看看销售额", user).splitlines()
        if line.startswith("Catalog: ")
    )
    assert catalog_line == "Catalog: []"


@pytest.mark.parametrize(
    "period,count,today,start,end",
    [
        ("previous_month", 1, date(2026, 1, 15), "2025-12-01", "2026-01-01"),
        ("current_month", 1, date(2026, 12, 15), "2026-12-01", "2027-01-01"),
        ("last_days", 7, date(2024, 3, 1), "2024-02-24", "2024-03-02"),
        ("last_months", 3, date(2026, 1, 15), "2025-10-01", "2026-01-01"),
    ],
)
def test_calendar_boundaries(period, count, today, start, end):
    resolved = resolve_relative_time(RelativeTime(period=period, count=count), today)
    assert (resolved.start, resolved.end) == (start, end)


def _build_semantic(payload, question="自然问法"):
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    user = User(id="analyst", group_memberships=["analyst"])
    router = QuestionRouter(
        CatalogRetriever(catalog), max_result_rows=100_000, full_results=True
    )
    intent = parse_semantic_intent(
        json.dumps(payload), catalog, user=user, today=date(2026, 10, 14)
    )
    selection = select_intent_catalog(intent, router.retriever, user, question=question)
    route = route_semantic_intent(intent, router, selection)
    state = {
        "semantic_intent": intent.as_dict(),
        "time_range": intent.time_range.model_dump() if intent.time_range else None,
        "comparison_baseline": intent.comparison_baseline,
    }
    if not route.should_generate_sql:
        return intent, route, None
    plan = QueryPlan.from_selection(
        selection,
        question,
        route,
        state,
        result_budget=ResultBudget.aggregate(execution_max_rows=100_000),
    )
    contract = ResultContract.from_selection(
        selection,
        question,
        plan.time_range,
        catalog=catalog,
        required_result_columns=plan.required_result_columns,
        requested_dimensions=plan.dimensions,
        result_budget=plan.result_budget,
    )
    build_runtime_candidate_request(
        question=question,
        selection=selection,
        query_plan=plan,
        result_contract=contract,
        workspace=OLIST_RUNTIME_WORKSPACE,
        route=route,
    )
    return intent, route, plan


def test_plan_reads_structured_dates_dimensions_and_grain_not_old_regex():
    _, route, plan = _build_semantic(
        {
            "request_kind": "data_query",
            "metric_ids": ["gmv"],
            "dimensions": [],
            "time_grain": "month",
            "relative_time": {"period": "current_month"},
            "filters": [{"dimension": "customer_state", "value": "SP"}],
        },
        question="本月圣保罗州销售走势，customer_state只是筛选，不是分组",
    )
    assert route.should_generate_sql
    assert plan.dimensions == ()
    assert plan.required_result_columns == ("time", "gmv")
    assert plan.time_range == {
        "start": "2026-10-01",
        "end": "2026-11-01",
        "end_semantics": "exclusive",
    }
    assert "filter_customer_state:SP" in plan.warnings


def test_top_n_is_structured_and_not_inferred_from_metric_alias():
    _, _, plan = _build_semantic(
        {
            "request_kind": "data_query",
            "metric_ids": ["gmv"],
            "dimensions": ["customer_state"],
            "top_n": 5,
        },
        question="销售额最高的五个地方",
    )
    assert plan.top_n == (("customer_state", 5),)


def test_daily_all_time_is_bounded_by_resources_not_a_date_word_gate():
    _, route, plan = _build_semantic(
        {
            "request_kind": "data_query",
            "metric_ids": ["gmv"],
            "time_grain": "day",
        },
        question="销售每天的走势",
    )
    assert route.should_generate_sql
    assert plan.time_range is None
    assert plan.result_budget.aggregate_result_rows <= 100_000


@pytest.mark.parametrize(
    "requested,status,chart_type",
    [
        ("line chart", "valid", "line"),
        ("area", "valid", "area"),
        (None, "not_requested", None),
        ("scatter", "unsupported", None),
    ],
)
def test_chart_reads_typed_request_without_chinese_keyword_gate(
    requested, status, chart_type
):
    from data_analysis_agent.chart_contract import ChartContract

    intent, route, plan = _build_semantic(
        {
            "request_kind": "data_query",
            "metric_ids": ["gmv"],
            "time_grain": "month",
            "time_range": {"start": "2017-01-01", "end": "2018-01-01"},
            "chart_request": requested,
        },
        question="画条曲线",
    )
    assert route.should_generate_sql
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    selection = select_intent_catalog(intent, CatalogRetriever(catalog), User(id="u"))
    result = ResultContract.from_selection(
        selection,
        "画条曲线",
        catalog=catalog,
        required_result_columns=plan.required_result_columns,
        requested_dimensions=plan.dimensions,
    )
    chart = ChartContract.from_query_plan(
        "画条曲线", plan, result, semantic_intent=intent.as_dict()
    )
    assert (chart.status, chart.chart_type) == (status, chart_type)


@pytest.mark.asyncio
async def test_interpreter_failure_is_not_an_executable_intent():
    calls = 0

    async def unavailable(prompt, user):
        nonlocal calls
        calls += 1
        raise OSError("provider unavailable")

    interpreter = SemanticIntentInterpreter(
        CatalogLoader().load(), unavailable, retry_delay_seconds=0
    )
    with pytest.raises(SemanticIntentError, match="semantic_service_unavailable"):
        await interpreter.interpret("销售额多少", User(id="u"))
    assert calls == 2


@pytest.mark.asyncio
async def test_interpreter_retries_once_then_accepts_valid_json():
    calls = 0

    async def flaky(prompt, user):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("transient")
        return '{"request_kind":"data_query","metric_ids":["gmv"],"confidence":0.8}'

    interpreter = SemanticIntentInterpreter(
        CatalogLoader().load(), flaky, retry_delay_seconds=0
    )
    intent = await interpreter.interpret("销售额多少", User(id="u"))
    assert intent.metric_ids == ("gmv",)
    assert calls == 2


def test_interpreter_does_not_copy_untrusted_prose_into_query_scope():
    async def complete(prompt, user):
        return "{}"

    interpreter = SemanticIntentInterpreter(CatalogLoader().load(), complete)
    prompt = interpreter.prompt(
        "看看订单量",
        User(id="u"),
        {
            "dimensions": ["customer_state"],
            "previous_result_summary": "SECRET_SUMMARY_WITH_INVENTED_FILTER",
            "history_summary": "ARBITRARY_HISTORY_WITH_SQL",
            "role": "INJECTED_ROLE",
        },
    )
    assert "customer_state" in prompt
    assert all(
        value not in prompt
        for value in (
            "SECRET_SUMMARY_WITH_INVENTED_FILTER",
            "ARBITRARY_HISTORY_WITH_SQL",
            "INJECTED_ROLE",
        )
    )


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"clarification": "按什么期间比较？"}, "semantic_ambiguity"),
    ],
)
def test_unimplemented_or_ambiguous_scope_does_not_become_sql(payload, reason):
    _, route, plan = _build_semantic(
        {
            "request_kind": "data_query",
            "metric_ids": ["gmv"],
            **payload,
        }
    )
    assert not route.should_generate_sql
    assert route.reason_code == reason
    assert plan is None


def test_few_shot_examples_do_not_reference_role_hidden_metrics():
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    catalog = replace(
        catalog,
        metrics=tuple(
            replace(metric, role_visibility=frozenset({"admin"}))
            for metric in catalog.metrics
        ),
    )
    interpreter = SemanticIntentInterpreter(catalog, None)
    prompt = interpreter.prompt("各州销售", User(id="u", group_memberships=["analyst"]))
    examples_line = prompt.split("参考理解（展示语义，不要求用户模仿句式）：\n")[
        1
    ].splitlines()[0]
    assert json.loads(examples_line) == []


def test_eval_semantic_checks_distinguish_valid_json_from_wrong_scope():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "scripts" / "evals" / "run_semantic_router_baseline.py"
    spec = importlib.util.spec_from_file_location("semantic_baseline", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    check_expectations = module.check_expectations

    case = {
        "expected": {
            "intent": {"dimensions": ["customer_state"]},
            "route": {"should_generate_sql": True},
        }
    }
    row = {
        "status": "parsed",
        "intent": {"dimensions": ["customer_state", "customer_city"]},
        "route": {"should_generate_sql": True},
    }
    result = check_expectations(case, row)
    assert not result["passed"]
    assert "intent.dimensions" in result["mismatches"]
    row["intent"]["dimensions"] = ["customer_state"]
    assert check_expectations(case, row)["passed"]
    row["status"] = "interpretation_failed"
    assert not check_expectations(case, row)["passed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion,expected_sql",
    [
        (
            '{"request_kind":"data_query","metric_ids":["gmv"],"relative_time":{"period":"current_month"},"time_grain":"month","confidence":0.9}',
            True,
        ),
        (
            '{"request_kind":"clarification","clarification":"销售额还是订单量？"}',
            False,
        ),
        ("invalid", False),
    ],
)
async def test_handler_semantic_path_does_not_call_legacy_router(
    completion, expected_sql, monkeypatch
):
    from data_analysis_agent.budget import RequestBudget
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.run_recorder import AgentRun
    from vanna.core.user import RequestContext
    from vanna.servers.base.models import ChatRequest

    user = User(id="semantic-handler", group_memberships=["analyst"])
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    router = QuestionRouter(
        CatalogRetriever(catalog), max_result_rows=100_000, full_results=True
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("online semantic path must not use lexical routing")

    monkeypatch.setattr(router, "classify", forbidden)
    monkeypatch.setattr(router, "admit_sql_request", forbidden)
    from data_analysis_agent.working_memory import WorkingMemory

    monkeypatch.setattr(WorkingMemory, "apply", forbidden)

    class Resolver:
        async def resolve_user(self, context):
            return user

    class Store:
        conversation = None

        async def get_conversation(self, conversation_id, user):
            return self.conversation

        async def update_conversation(self, conversation):
            self.conversation = conversation

    @dataclass
    class Recorder:
        usage = None

        async def start(self, **kwargs):
            return AgentRun(
                "run-semantic", kwargs["request_id"], kwargs["conversation_id"]
            )

        async def finish(self, run, usage):
            self.usage = usage

    class Agent:
        def __init__(self):
            self.user_resolver, self.conversation_store = Resolver(), Store()
            self.called = False

        async def send_message(self, request_context, message, conversation_id=None):
            self.called = True
            if False:
                yield

    async def complete(prompt, user):
        return completion

    agent, recorder = Agent(), Recorder()
    handler = BudgetedChatHandler(
        agent,
        RequestBudget(),
        recorder,
        question_router=router,
        workspace=OLIST_RUNTIME_WORKSPACE,
        semantic_interpreter=SemanticIntentInterpreter(
            catalog, complete, today=lambda: date(2026, 10, 14)
        ),
        result_budget=ResultBudget.aggregate(execution_max_rows=100_000),
    )
    request = ChatRequest(
        message="本月经营情况",
        conversation_id="semantic-conversation",
        request_context=RequestContext(metadata={"semantic_intent": {"role": "admin"}}),
    )
    _chunks = [chunk async for chunk in handler.handle_stream(request)]
    assert agent.called is expected_sql
    if expected_sql:
        plan = request.request_context.metadata["query_plan"]
        assert plan["time_grain"] == "month"
        assert plan["time_range"]["end"] == "2026-11-01"
        memory = agent.conversation_store.conversation.metadata["working_memory"]
        assert memory["time_range"] == plan["time_range"]
    else:
        assert recorder.usage.sql_calls_used == 0


@pytest.mark.asyncio
async def test_ranked_reference_filter_reaches_effective_intent_and_sql_plan(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from data_analysis_agent.analysis_state import AnalysisState
    from data_analysis_agent.budget import RequestBudget
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.result_artifact import ResultArtifact
    from data_analysis_agent.result_artifact_store import ResultArtifactStore
    from data_analysis_agent.run_recorder import AgentRun
    from vanna.core.storage import Conversation
    from vanna.core.user import RequestContext
    from vanna.servers.base.models import ChatRequest

    workspace = OLIST_RUNTIME_WORKSPACE
    user = User(id="reference-handoff", group_memberships=["analyst"])
    catalog = CatalogLoader(workspace).load()
    dates = {"start": "2017-01-01", "end": "2018-01-01", "end_semantics": "exclusive"}
    state = replace(AnalysisState.new(workspace), metric_ids=("gmv",),
                    dimensions=("customer_state",), time_range=dates)
    artifact_store = ResultArtifactStore(tmp_path)
    manifest = artifact_store.save_result_csv(
        artifact_id="ranking", user_id=user.id, workspace_id=workspace.workspace_id,
        columns=["customer_state", "gmv"], rows=[{"customer_state": "SP", "gmv": 100}, {"customer_state": "RJ", "gmv": 80}],
        row_count=2, dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version,
        result_metadata={"validation_status": "valid"},
    )
    artifact = ResultArtifact(
        "ranking", "可信排名", ("gmv",), ("customer_state", "gmv"),
        workspace.dataset_version_id, workspace.metric_version, "not_rendered",
        workspace_id=workspace.workspace_id, query_scope=state.scope(), csv_sha256=manifest["csv_sha256"],
        result_metadata={"validation_status": "valid",
                         "ordering": [{"column": "gmv", "direction": "desc"}],
                         "ranked_entities": [
                             {"rank": 1, "value": "SP", "order_value": 100},
                             {"rank": 2, "value": "RJ", "order_value": 80},
                         ]},
    )
    state = state.with_result_artifact(artifact)
    conversation = Conversation(id="reference-handoff", user=user, metadata={
        "workspace_id": workspace.workspace_id,
        "dataset_version_id": workspace.dataset_version_id,
        "metric_version": workspace.metric_version, "working_memory": state.as_dict(),
    })
    async def complete(prompt, user):
        return json.dumps({"request_kind": "data_query", "action": "update_task",
            "state_patch": {"operations": [{"op": "set", "field": "dimensions", "value": ["product_category_name"]}]},
            "reference_requests": [{"kind": "previous_ranked_entity", "dimension": "customer_state",
                                    "rank": 2, "match_mode": "first_row"}]})

    async def send_message(*args, **kwargs):
        if False:
            yield

    store = SimpleNamespace(get_conversation=AsyncMock(return_value=conversation),
                            update_conversation=AsyncMock())
    agent = SimpleNamespace(user_resolver=SimpleNamespace(resolve_user=AsyncMock(return_value=user)),
                            conversation_store=store, send_message=send_message)
    recorder = SimpleNamespace(start=AsyncMock(return_value=AgentRun("ref-run", "ref-request", conversation.id)),
                               finish=AsyncMock())
    handler = BudgetedChatHandler(
        agent, RequestBudget(), recorder, workspace=workspace,
        question_router=QuestionRouter(CatalogRetriever(catalog), full_results=True),
        semantic_interpreter=SemanticIntentInterpreter(catalog, complete),
        result_artifact_store=artifact_store,
    )
    request = ChatRequest(message="刚才排名第二的州哪些品类成交额高？",
                          conversation_id=conversation.id, request_context=RequestContext())
    _chunks = [chunk async for chunk in handler.handle_stream(request)]
    plan = request.request_context.metadata["query_plan"]
    intent = request.request_context.metadata["semantic_intent"]
    assert "filter_customer_state:RJ" in plan["warnings"]
    assert intent["filters"] == [{"dimension": "customer_state", "value": "RJ"}]
    assert plan["time_range"] == dates
    memory = conversation.metadata["working_memory"]
    assert memory["task_id"] == state.task_id
    assert memory["filters"] == ["customer_state=RJ"]


def test_category_catalog_keeps_filter_source_and_all_join_bridges():
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    intent = parse_semantic_intent(json.dumps({
        "request_kind": "data_query", "metric_ids": ["gmv"],
        "dimensions": ["product_category_name"],
        "filters": [{"dimension": "customer_state", "value": "SP"}], "top_n": 10,
    }), catalog)
    selection = select_intent_catalog(intent, CatalogRetriever(catalog, max_tables=6), User(id="u"))
    assert {"fact_orders", "fact_order_items", "dim_customers", "dim_products"} <= set(selection.trace.selected_tables)
    assert "customer_state" in dict(selection.trace.selected_columns)["dim_customers"]
    assert any(j.to_table == "dim_customers" or j.from_table == "dim_customers" for j in selection.joins)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["data_query", "clarification"])
async def test_clarification_keeps_known_scope_until_metric_is_supplied(kind):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from data_analysis_agent.analysis_state import AnalysisState
    from data_analysis_agent.budget import RequestBudget
    from data_analysis_agent.chat_runtime import BudgetedChatHandler
    from data_analysis_agent.run_recorder import AgentRun
    from vanna.core.storage import Conversation
    from vanna.core.user import RequestContext
    from vanna.servers.base.models import ChatRequest

    user = User(id="clarify-draft", group_memberships=["analyst"])
    catalog = CatalogLoader(OLIST_RUNTIME_WORKSPACE).load()
    conversation = Conversation(id="clarify-draft", user=user, metadata={
        "workspace_id": OLIST_RUNTIME_WORKSPACE.workspace_id,
        "dataset_version_id": OLIST_RUNTIME_WORKSPACE.dataset_version_id,
        "metric_version": OLIST_RUNTIME_WORKSPACE.metric_version,
        "working_memory": AnalysisState.new(OLIST_RUNTIME_WORKSPACE).as_dict(),
    })
    completions = iter([
        {"request_kind": kind, "action": "update_task", "state_patch": {"operations": [
            {"op": "set", "field": "dimensions", "value": ["product_category_name"]},
            {"op": "set", "field": "filters", "value": ["customer_state=SP"]},
            {"op": "set", "field": "top_n", "value": 10},
        ]}},
        {"request_kind": "data_query", "action": "update_task", "state_patch": {"operations": [
            {"op": "set", "field": "metric_ids", "value": ["gmv"]},
        ]}},
    ])
    async def complete(prompt, user):
        return json.dumps(next(completions))

    calls = []
    async def send_message(*args, **kwargs):
        calls.append(kwargs)
        if False:
            yield

    store = SimpleNamespace(get_conversation=AsyncMock(return_value=conversation),
                            update_conversation=AsyncMock())
    agent = SimpleNamespace(user_resolver=SimpleNamespace(resolve_user=AsyncMock(return_value=user)),
                            conversation_store=store, send_message=send_message)
    recorder = SimpleNamespace(start=AsyncMock(return_value=AgentRun("draft-run", "draft-request", conversation.id)),
                               finish=AsyncMock())
    handler = BudgetedChatHandler(agent, RequestBudget(), recorder, workspace=OLIST_RUNTIME_WORKSPACE,
        question_router=QuestionRouter(CatalogRetriever(catalog, max_tables=6), full_results=True),
        semantic_interpreter=SemanticIntentInterpreter(catalog, complete))
    first = ChatRequest(message="SP州排名前十的商品种类", conversation_id=conversation.id, request_context=RequestContext())
    _ = [chunk async for chunk in handler.handle_stream(first)]
    draft = conversation.metadata["working_memory"]
    assert draft["dimensions"] == ["product_category_name"]
    assert draft["top_n"] == 10 and draft["filters"] == ["customer_state=SP"]
    assert draft["status"] == "clarifying" and calls == []
    second = ChatRequest(message="我想看销售额", conversation_id=conversation.id, request_context=RequestContext())
    _ = [chunk async for chunk in handler.handle_stream(second)]
    plan = second.request_context.metadata["query_plan"]
    assert plan["metric_ids"] == ["gmv"] and plan["dimensions"] == ["product_category_name"]
    assert plan["top_n"] == {"product_category_name": 10}
    assert "filter_customer_state:SP" in plan["warnings"] and len(calls) == 1
