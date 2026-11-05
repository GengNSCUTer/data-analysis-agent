"""Real metadata transactions, no writes to business tables."""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from uuid import uuid4

import pytest
from vanna.core.storage import Conversation, Message
from vanna.core.user import User

from data_analysis_agent.analysis_lifecycle import clear_active_analysis_state
from data_analysis_agent.analysis_state import AnalysisState
from data_analysis_agent.artifact_history import ArtifactHistory
from data_analysis_agent.artifact_reuse_planner import ArtifactReusePlanner
from data_analysis_agent.budget import RequestBudget
from data_analysis_agent.conversation_store import ConversationStateConflict, PostgresConversationStore
from data_analysis_agent.result_artifact import ResultArtifact
from data_analysis_agent.result_artifact_store import ResultArtifactIntegrityError, ResultArtifactStore
from data_analysis_agent.run_recorder import PostgresRunRecorder
from data_analysis_agent.runtime_workspace import load_runtime_workspace
from data_analysis_agent.semantic_catalog import CatalogLoader
from data_analysis_agent.semantic_intent import RequestedAnswerSpec, SemanticIntent

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="needs project PostgreSQL")]


@pytest.fixture
async def history_setup(tmp_path):
    workspace = load_runtime_workspace().profile
    catalog = CatalogLoader(workspace).load()
    store = PostgresConversationStore(default_workspace_id=workspace.workspace_id,
        default_dataset_version=workspace.dataset_version_id, default_metric_version=workspace.metric_version)
    files = ResultArtifactStore(tmp_path, retention_seconds=3600)
    history = ArtifactHistory(store.settings, workspace, catalog)
    recorder = PostgresRunRecorder(store.settings)
    user = User(id="history-"+uuid4().hex, group_memberships=["analyst"])
    conversation = Conversation(id="history-"+uuid4().hex, user=user, metadata={
        "workspace_id":workspace.workspace_id, "dataset_version_id":workspace.dataset_version_id,
        "metric_version":workspace.metric_version, "working_memory":AnalysisState.new(workspace).as_dict()})
    await store.update_conversation(conversation)

    async def add(conversation, *, metric="gmv", rows=None):
        run = await recorder.start(request_id=uuid4().hex, conversation_id=conversation.id, user=user,
            question="受控历史索引测试", budget=RequestBudget(), dataset_version_id=workspace.dataset_version_id,
            metric_version=workspace.metric_version)
        aid = "history_" + uuid4().hex
        rows = rows or [{"customer_state":"SP",metric:300}, {"customer_state":"RJ",metric:200}]
        manifest = files.save_result_csv(artifact_id=aid, user_id=user.id, workspace_id=workspace.workspace_id,
            columns=["customer_state",metric], rows=rows, row_count=len(rows),
            dataset_version=workspace.dataset_version_id, metric_version=workspace.metric_version,
            result_metadata={"validation_status":"valid", "result_complete":True,
                "population_coverage":"complete", "row_count":len(rows), "source_tables":["fact_orders"],
                "history_index_required":True, "ordering":[{"column":metric,"direction":"desc"}]})
        artifact = ResultArtifact(artifact_id=aid, summary="可信结果", metric_ids=(metric,),
            result_columns=("customer_state",metric), dataset_version=workspace.dataset_version_id,
            metric_version=workspace.metric_version, workspace_id=workspace.workspace_id, chart_state="not_rendered",
            csv_sha256=manifest["csv_sha256"], csv_ref="result.csv", csv_bytes=manifest["csv_bytes"],
            result_metadata=manifest["result_metadata"], query_scope={"metric_ids":[metric], "dimensions":["customer_state"],
                "filters":[], "time_range":None, "time_grain":None, "comparison_baseline":None, "top_n":None})
        conversation.add_message(Message(role="user", content="受控问题", metadata={"run_id":run.run_id}))
        conversation.add_message(Message(role="assistant", content="受控回答", metadata={"run_id":run.run_id}))
        conversation.metadata["working_memory"] = AnalysisState.from_mapping(conversation.metadata["working_memory"]).with_result_artifact(artifact).as_dict()
        conversation.metadata["pending_history_artifact"] = {"artifact":artifact.as_dict(), "manifest":manifest,
            "run_id":run.run_id, "catalog_version":catalog.catalog_version, "policy_version":catalog.policy_version}
        await store.update_conversation(conversation)
        return artifact
    yield workspace, catalog, store, files, history, user, conversation, add
    await store.delete_conversation(conversation.id, user)


async def test_index_is_scoped_by_user_workspace_conversation_role_and_epoch(history_setup):
    ws, catalog, store, files, history, user, conversation, add = history_setup
    source = await add(conversation)
    assert (await history.candidates(conversation.id, user))[0].artifact.artifact_id == source.artifact_id
    assert await history.candidates(conversation.id, User(id="intruder",group_memberships=["analyst"])) == []
    assert await history.candidates("other-conversation", user) == []
    assert await history.candidates(conversation.id, User(id=user.id,group_memberships=["admin"])) == []
    other_ws = replace(ws, workspace_id="other-workspace")
    assert await ArtifactHistory(store.settings, other_ws, catalog).candidates(conversation.id, user) == []
    for name in ("dataset_version", "metric_version"):
        changed = replace(ws, **{name: "changed-version"})
        assert await ArtifactHistory(store.settings, changed, catalog).candidates(conversation.id, user) == []
    for name in ("catalog_version", "policy_version"):
        changed = replace(catalog, **{name: "changed-version"})
        assert await ArtifactHistory(store.settings, ws, changed).candidates(conversation.id, user) == []
    modified = replace(catalog, metrics=tuple(replace(m, role_visibility=frozenset({"admin"})) if m.metric_id=="gmv" else m for m in catalog.metrics))
    assert await ArtifactHistory(store.settings, ws, modified).candidates(conversation.id,user) == []
    clear_active_analysis_state(conversation, ws)
    await store.update_conversation(conversation)
    assert await history.candidates(conversation.id,user) == []
    assert len(await history.candidates(conversation.id,user,include_previous_context=True)) == 1
    planner = ArtifactReusePlanner(history, files, catalog)
    with pytest.raises(FileNotFoundError):
        await planner.verified(conversation.id,user,source.artifact_id)
    assert (await planner.verified(conversation.id,user,source.artifact_id,include_previous_context=True))[0].artifact.artifact_id == source.artifact_id


async def test_edit_revokes_only_removed_turn_and_delete_revokes_all(history_setup):
    _, _, store, _, history, user, conversation, add = history_setup
    first = await add(conversation)
    await add(conversation,metric="paid_order_count")
    assert len(await history.candidates(conversation.id,user)) == 2
    assert await store.truncate_from_last_user_message(conversation.id,user) == 2
    remaining = await history.candidates(conversation.id,user)
    assert [i.artifact.artifact_id for i in remaining] == [first.artifact_id]
    await store.delete_conversation(conversation.id,user)
    assert await history.candidates(conversation.id,user) == []


async def test_failed_index_commit_rolls_back_state_and_orphan_is_not_visible(history_setup, monkeypatch):
    _, _, store, files, history, user, conversation, add = history_setup
    version = conversation.metadata["state_version"]
    with monkeypatch.context() as patch:
        def fail(*_):
            raise RuntimeError("injected index commit failure")
        patch.setattr(ArtifactHistory,"commit",fail)
        with pytest.raises(RuntimeError,match="injected"):
            await add(conversation)
    fresh = await store.get_conversation(conversation.id,user)
    assert fresh.metadata["state_version"] == version
    assert fresh.messages == [] and not await history.candidates(conversation.id,user)
    registered = await history.registered_scopes(files)
    for path in files.root.glob("*/*/*/manifest.json"):
        value = json.loads(path.read_text())
        value["created_at"] = time.time()-7200
        path.write_text(json.dumps(value))
    assert files.cleanup_expired(registered_scopes=registered)["artifacts"] == 1


async def test_stale_writer_cannot_publish_ready_or_duplicate_ordinals(history_setup):
    _, _, store, _, history, user, conversation, add = history_setup
    stale = await store.get_conversation(conversation.id,user)
    await add(conversation)
    with pytest.raises(ConversationStateConflict):
        await add(stale)
    items = await history.candidates(conversation.id,user)
    assert len(items)==1 and items[0].ordinal == 1


async def test_automatic_retrieval_uses_older_metric_without_state_mutation_and_rejects_tamper(history_setup):
    _, catalog, _, files, history, user, conversation, add = history_setup
    first = await add(conversation)
    await add(conversation,metric="paid_order_count")
    state = AnalysisState.from_mapping(conversation.metadata["working_memory"])
    before = state.as_dict()
    planner = ArtifactReusePlanner(history,files,catalog)
    intent = SemanticIntent(request_kind="data_query", action="new_task",metric_ids=("gmv",),
        requested_answer=RequestedAnswerSpec(metric_ids=("gmv",),filters=[{"dimension":"customer_state","value":"SP"}]))
    plan = await planner.plan(conversation.id,user,intent,state,state)
    assert plan.decision.mode == "LOCAL_COMPUTE" and plan.decision.artifact_id == first.artifact_id
    assert plan.frame.iloc[0]["gmv"] == 300
    assert state.as_dict() == before
    (files._scope(user.id,first.workspace_id,first.artifact_id)/"result.csv").write_text("gmv\n9999999\n")
    with pytest.raises(ResultArtifactIntegrityError):
        await planner.verified(conversation.id,user,first.artifact_id)
    assert (await planner.plan(conversation.id,user,intent,state,state)).decision.mode == "SQL_REQUIRED"


async def test_selected_history_scope_is_not_erased_by_empty_task_fields(history_setup):
    _, catalog, _, files, history, user, conversation, add = history_setup
    first = await add(conversation)
    await add(conversation, metric="paid_order_count")
    state = AnalysisState.from_mapping(conversation.metadata["working_memory"])
    before = state.as_dict()
    planner = ArtifactReusePlanner(history, files, catalog)
    for operation in ("compare", "chart"):
        intent = SemanticIntent.model_validate({
            "request_kind": "result_followup", "action": "keep", "metric_ids": [],
            "dimensions": [], "filters": [], "time_range": None, "time_grain": None,
            "history_reference": {"artifact_id": first.artifact_id, "mutation_intent": "inspect"},
            **({"result_inspections": [{"operation": "compare", "metric_id": "gmv", "row_numbers": [1, 2]}]}
               if operation == "compare" else {"chart_request": "bar"}),
        })
        plan = await planner.plan(conversation.id, user, intent, state, state)
        assert plan.decision.mode == ("LOCAL_COMPUTE" if operation == "compare" else "DIRECT_REUSE")
        assert plan.item.artifact.artifact_id == first.artifact_id
        assert plan.spec.metric_ids == ("gmv",)
        assert list(plan.frame["gmv"]) == [300, 200]
        assert state.as_dict() == before
