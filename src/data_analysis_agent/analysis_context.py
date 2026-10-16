"""Assemble task-scoped narrative context without turning prose into SQL state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from time import perf_counter

from vanna.core.storage import Conversation, Message

from .analysis_state import AnalysisState
from .context_builder import _group_complete_turns
from .history_summary import (
    HistorySummary,
    SEMANTIC_SUMMARY_VERSION,
    SummaryProvider,
    summary_prompt,
)
from .token_budget import ContextCapacityError, TokenCounter


class AnalysisContextBuilder:
    """Six recent turns plus a persisted incremental narrative, scoped to one task."""

    def __init__(self, summary_provider: SummaryProvider, *, token_counter=None):
        self.summary_provider = summary_provider
        self.counter = token_counter or TokenCounter()
        self.recent_tokens = 6_000
        self.summary_tokens = 1_000
        self.state_tokens = 2_000
        self.summary_input_tokens = 6_000

    async def build(
        self, conversation: Conversation, state: AnalysisState, store, usage
    ) -> dict:
        scope = {
            "task_id": state.task_id,
            "workspace_id": state.workspace_id,
            "dataset_version": state.dataset_version,
            "metric_version": state.metric_version,
        }
        saved = conversation.metadata.get("history_summary")
        saved = saved if saved and saved.get("scope") == scope else None
        start = state.history_start_index
        turns = _group_complete_turns(
            [m for m in conversation.messages if m.metadata["message_index"] >= start]
        )[-6:]
        while (
            turns
            and self.counter.count_messages(self._narrative(turns)) > self.recent_tokens
        ):
            turns.pop(0)
        recent_start = (
            turns[0][0].metadata["message_index"]
            if turns
            else conversation.metadata["message_count"]
        )
        watermark = saved["source_message_end"] + 1 if saved else start
        status = "reused" if saved else "not_needed"
        summary_error_type = None
        summary_elapsed_ms = 0
        # Read only a bounded set of complete turns. Unprocessed backlog remains
        # explicit; no clipped prefix is labelled as a fully compressed history.
        old = await store.get_turn_range(
            conversation.id, conversation.user, start=watermark, before=recent_start
        )
        groups = _group_complete_turns(old)
        selected = []
        for turn in groups:
            candidate = [*selected, turn]
            prompt = summary_prompt(
                candidate, previous_text=saved["text"] if saved else None
            )
            if self.counter.count_text(prompt) > self.summary_input_tokens:
                break
            selected = candidate
        if selected:
            source = HistorySummary.from_turns(
                selected,
                source_turn_start=(saved["source_turn_end"] + 1 if saved else 1),
                reason="message_budget",
            )
            source = replace(
                source,
                source_message_start=start,
                source_message_end=selected[-1][-1].metadata["message_index"],
                previous_text=saved["text"] if saved else None,
                source_tokens=self.counter.count_messages(self._narrative(selected)),
            )
            if saved:
                source = replace(
                    source,
                    source_turn_start=saved["source_turn_start"],
                    source_message_count=source.source_message_count
                    + saved["source_message_count"],
                    source_chars=source.source_chars + saved["source_chars"],
                    source_tokens=source.source_tokens + saved["source_tokens"],
                )
            started = perf_counter()
            try:
                text = await asyncio.wait_for(
                    self.summary_provider(selected, source), timeout=30
                )
            except Exception as exc:
                text = None
                summary_error_type = type(exc).__name__
            summary_elapsed_ms = round((perf_counter() - started) * 1000)
            if text and self.counter.count_text(text) <= self.summary_tokens:
                source = replace(
                    source,
                    semantic_text=text,
                    version=SEMANTIC_SUMMARY_VERSION,
                    summary_status="semantic_generated",
                    summary_tokens=self.counter.count_text(text),
                )
                saved = {
                    **source.as_evidence(),
                    "text": text,
                    "scope": scope,
                    # The existing digest identifies this increment; watermark
                    # and counters above describe the cumulative narrative.
                    "latest_batch_start": selected[0][0].metadata["message_index"],
                }
                conversation.metadata["history_summary"] = saved
                await store.update_conversation(conversation)
                status = "generated"
            else:
                status = "summary_failed"
                summary_error_type = summary_error_type or (
                    "empty_or_incomplete" if not text else "summary_over_budget"
                )
        elif old:
            status = "source_turn_too_large"

        artifact = state.previous_result_artifact
        # The interpreter needs an opaque result reference and ordering metadata,
        # not row values or the full plan/CSV. The resolver owns all row access.
        references = (
            None
            if artifact is None
            else {
                "artifact_id": artifact.artifact_id,
                "execution_id": artifact.result_metadata.get("execution_id"),
                "columns": list(artifact.result_columns),
                "dimensions": artifact.query_scope.get("dimensions", []),
                "ordering": artifact.result_metadata.get("ordering", []),
                "validation_status": artifact.result_metadata.get("validation_status"),
            }
        )
        controlled = {
            **state.scope(),
            **scope,
            "pending_question": state.pending_question,
            "pending_missing": list(state.pending_missing),
            "result_reference": references,
        }
        state_tokens = self.counter.count_text(
            json.dumps(controlled, ensure_ascii=False)
        )
        if state_tokens > self.state_tokens:
            raise ContextCapacityError(
                "当前分析状态超过上下文预算，请拆分分析或清空后重新提问。"
            )
        packet = {
            "analysis_state": controlled,
            "recent_dialogue": [
                {
                    "role": m.role,
                    "content": m.content,
                    "message_index": m.metadata["message_index"],
                }
                for m in self._narrative(turns)
            ],
            "narrative_summary": saved["text"] if saved else None,
            "recent_start_index": recent_start,
            "current_turn_start_index": conversation.metadata["message_count"],
        }
        if saved:
            usage.record_history_summary(
                {k: v for k, v in saved.items() if k != "text"}, saved["text"]
            )
        evidence = {
            "summary_status": status,
            "summary_error_type": summary_error_type,
            "summary_elapsed_ms": summary_elapsed_ms,
            "source_message_end": saved["source_message_end"] if saved else start - 1,
            "pending_before": recent_start,
            "recent_start_index": recent_start,
            "recent_turns": len(turns),
            "recent_tokens": self.counter.count_messages(self._narrative(turns)),
            "summary_tokens": self.counter.count_text(saved["text"]) if saved else 0,
            "state_tokens": state_tokens,
            "tokenizer_mode": self.counter.mode,
            "tokenizer_id": self.counter.tokenizer_id,
        }
        usage.context_packet = packet
        usage.record_catalog({"context_packet": evidence})
        return packet

    @staticmethod
    def _narrative(turns) -> list[Message]:
        return [
            m
            for turn in turns
            for m in turn
            if m.role in {"user", "assistant"} and not m.tool_calls
        ]

    async def recall(self, store, conversation, *, before: int) -> list[dict]:
        """Explicit thread-local recall interface; not an automatic fact restore.

        Call only after a user refers to an earlier discussion. Return at most
        three complete prose messages/800 tokens, each with its source index.
        Result entities still require an independently verified artifact.
        """
        messages = await store.get_history_page(
            conversation.id, conversation.user, before=before, limit=6
        )
        selected = []
        used = 0
        for m in reversed(messages):
            if m.role not in {"user", "assistant"} or m.tool_calls:
                continue
            tokens = self.counter.count_message(m)
            if used + tokens > 800:
                break
            selected.append(
                {
                    "role": m.role,
                    "content": m.content,
                    "message_index": m.metadata["message_index"],
                }
            )
            used += tokens
            if len(selected) == 3:
                break
        return list(reversed(selected))
