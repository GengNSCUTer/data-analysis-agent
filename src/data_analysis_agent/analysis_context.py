"""Token-bounded task history and post-response incremental compaction."""

from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import replace
import json
import logging
from time import perf_counter

from vanna.core.storage import Conversation

from .analysis_state import AnalysisState
from .budget import BudgetUsage, CURRENT_BUDGET, RequestBudget
from .context_builder import _group_complete_turns
from .history_summary import HistorySummary, SEMANTIC_SUMMARY_VERSION, SummaryProvider, summary_prompt
from .token_budget import ContextCapacityError, TokenCounter

logger = logging.getLogger(__name__)


class AnalysisContextBuilder:
    """Retain every unsummarised turn; compact old prose after the answer."""

    def __init__(self, summary_provider: SummaryProvider, *, budget=None, token_counter=None):
        self.summary_provider = summary_provider
        self.budget = budget or RequestBudget()
        self.counter = token_counter or TokenCounter()
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(2)
        self.closed = False

    @staticmethod
    def scope(state):
        return {"task_id": state.task_id, "workspace_id": state.workspace_id,
                "dataset_version": state.dataset_version, "metric_version": state.metric_version}

    def saved(self, conversation, state):
        saved = conversation.metadata.get("history_summary")
        return saved if saved and saved.get("scope") == self.scope(state) else None

    @staticmethod
    def _narrative(turns):
        return [m for turn in turns for m in turn
                if m.role in {"user", "assistant"} and not m.tool_calls]

    async def _read(self, conversation, state, store, *, before=None):
        saved = self.saved(conversation, state)
        start = saved["source_message_end"] + 1 if saved else state.history_start_index
        before = conversation.metadata["message_count"] if before is None else before
        turns, tokens = [], 0
        while start < before:
            messages = await store.get_turn_range(conversation.id, conversation.user,
                                                  start=start, before=before, limit_turns=20)
            if not messages:
                break
            for turn in _group_complete_turns(messages):
                turns.append(turn)
                tokens += self.counter.count_messages(self._narrative([turn]))
                start = turn[-1].metadata["message_index"] + 1
                if tokens > self.budget.max_history_tokens:
                    return saved, turns, tokens, start < before
        return saved, turns, tokens, False

    async def build(self, conversation: Conversation, state: AnalysisState, store, usage) -> dict:
        saved, turns, tokens, more = await self._read(conversation, state, store)
        summary_tokens = self.counter.count_text(saved["text"]) if saved else 0
        if more or tokens + summary_tokens > self.budget.max_history_tokens:
            await self.compact_now(conversation.id, conversation.user, store)
            fresh = await store.get_conversation(conversation.id, conversation.user)
            conversation.metadata["history_summary"] = fresh.metadata.get("history_summary")
            saved, turns, tokens, more = await self._read(conversation, state, store)
            summary_tokens = self.counter.count_text(saved["text"]) if saved else 0
            if more or tokens + summary_tokens > self.budget.max_history_tokens:
                raise ContextCapacityError("历史尚未完成压缩，无法完整装入本轮上下文；请稍后重试或新建分析。")
        artifact = state.previous_result_artifact
        reference = None if artifact is None else {
            "artifact_id": artifact.artifact_id,
            "execution_id": artifact.result_metadata.get("execution_id"),
            "columns": list(artifact.result_columns),
            "dimensions": artifact.query_scope.get("dimensions", []),
            "ordering": artifact.result_metadata.get("ordering", []),
            "validation_status": artifact.result_metadata.get("validation_status"),
        }
        controlled = {**state.scope(), **self.scope(state), "pending_question": state.pending_question,
                      "pending_missing": list(state.pending_missing), "result_reference": reference}
        state_tokens = self.counter.count_text(json.dumps(controlled, ensure_ascii=False))
        start = turns[0][0].metadata["message_index"] if turns else conversation.metadata["message_count"]
        packet = {
            "analysis_state": controlled,
            "recent_dialogue": [{"role": m.role, "content": m.content,
                                 "message_index": m.metadata["message_index"]} for m in self._narrative(turns)],
            "narrative_summary": saved["text"] if saved else None,
            "recent_start_index": start,
            "current_turn_start_index": conversation.metadata["message_count"],
        }
        if saved:
            usage.record_history_summary({k: v for k, v in saved.items() if k != "text"}, saved["text"])
        usage.context_packet = packet

        async def relieve():
            await self.compact_now(conversation.id, conversation.user, store)
            fresh = await store.get_conversation(conversation.id, conversation.user)
            if fresh is None:
                raise ContextCapacityError("会话已失效，无法压缩上下文。")
            fresh_state = AnalysisState.from_mapping(fresh.metadata.get("working_memory"))
            if self.scope(fresh_state) != self.scope(state):
                raise ContextCapacityError("当前任务已变化，请重新提问。")
            # Preserve the original request's current-turn boundary. New tool
            # messages must not accidentally become historical narration.
            boundary = packet["current_turn_start_index"]
            refreshed = await self.build(fresh, fresh_state, store, usage)
            refreshed["current_turn_start_index"] = boundary

        usage.context_compactor = relieve
        usage.record_catalog({"context_packet": {
            "summary_status": "reused" if saved else "not_needed",
            "source_message_end": saved["source_message_end"] if saved else state.history_start_index - 1,
            "recent_start_index": start, "recent_turns": len(turns), "recent_tokens": tokens,
            "summary_tokens": summary_tokens, "state_tokens": state_tokens,
            "state_above_target": state_tokens > self.budget.history_state_target_tokens,
            "tokenizer_mode": self.counter.mode, "tokenizer_id": self.counter.tokenizer_id,
            "last_compaction": conversation.metadata.get("summary_attempt"),
        }})
        return packet

    def schedule(self, conversation_id, user, store):
        key = (user.id, conversation_id)
        if self.closed or key in self.tasks:
            return
        self._start(key, self._worker(conversation_id, user, store))

    def _start(self, key, coroutine):
        # Do not inherit the finished request's budget or message context.
        task = asyncio.create_task(coroutine, context=Context())
        self.tasks[key] = task
        task.add_done_callback(lambda done: self.tasks.pop(key, None))
        return task

    async def _worker(self, conversation_id, user, store, *, one_batch=False, before=None):
        try:
            async with self.semaphore:
                force = one_batch
                while await self._compact_batch(conversation_id, user, store, force=force,
                                               before=before, relieve=one_batch):
                    if one_batch:
                        break
                    force = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("history_compaction_failed type=%s", type(exc).__name__)

    async def compact_now(self, conversation_id, user, store):
        key = (user.id, conversation_id)
        task = self.tasks.get(key)
        if task is None:
            usage = CURRENT_BUDGET.get()
            packet = usage.context_packet if usage is not None else None
            before = packet["current_turn_start_index"] if packet else None
            task = self._start(key, self._worker(conversation_id, user, store, one_batch=True, before=before))
        try:
            await asyncio.wait_for(asyncio.shield(task), self.budget.history_summary_timeout_seconds)
        except TimeoutError:
            pass

    async def _compact_batch(self, conversation_id, user, store, *, force=False, before=None, relieve=False):
        conversation = await store.get_conversation(conversation_id, user)
        if conversation is None:
            return False
        state = AnalysisState.from_mapping(conversation.metadata.get("working_memory"))
        saved, turns, tokens, more = await self._read(conversation, state, store, before=before)
        target = self.budget.history_recent_target_tokens
        if relieve:
            target = min(target, tokens // 2)
        threshold = target if force else self.budget.history_compaction_trigger_tokens
        if not turns or (not more and tokens <= threshold):
            return False
        retain, cut = 0, len(turns)
        while cut > 0:
            size = self.counter.count_messages(self._narrative([turns[cut - 1]]))
            if retain + size > target:
                break
            retain += size
            cut -= 1
        old = turns[:cut]
        if not old:
            return False
        selected = []
        for turn in old:
            candidate = [*selected, turn]
            if self.counter.count_text(self._prompt(candidate, saved)) > self.budget.history_summary_input_tokens:
                break
            selected = candidate
        # Commit only complete turns, even when their prose needs several calls.
        selected = selected or old[:1]
        source = HistorySummary.from_turns(selected,
            source_turn_start=saved["source_turn_end"] + 1 if saved else 1, reason="message_budget")
        expected_end = saved["source_message_end"] if saved else state.history_start_index - 1
        source = replace(source, source_message_start=state.history_start_index,
                         source_message_end=selected[-1][-1].metadata["message_index"],
                         previous_text=saved["text"] if saved else None,
                         source_tokens=self.counter.count_messages(self._narrative(selected)))
        if saved:
            source = replace(source, source_turn_start=saved["source_turn_start"],
                             source_message_count=source.source_message_count + saved["source_message_count"],
                             source_chars=source.source_chars + saved["source_chars"],
                             source_tokens=source.source_tokens + saved["source_tokens"])
        isolated_usage = BudgetUsage(self.budget)
        token = CURRENT_BUDGET.set(isolated_usage)
        started = perf_counter()
        text, error = None, None
        try:
            text = await self._summarize(selected, source)
            if not text or self.counter.count_text(text) > self.budget.history_summary_max_tokens:
                error = "empty_or_over_budget"
                text = None
        except Exception as exc:
            error = type(exc).__name__
        finally:
            CURRENT_BUDGET.reset(token)
        attempt = {"status": "generated" if text else "failed", "error_type": error,
                   "elapsed_ms": round((perf_counter() - started) * 1000),
                   "raw_tokens_before": tokens, "source_message_end": source.source_message_end,
                   "raw_tokens_after": tokens - sum(self.counter.count_messages(self._narrative([turn]))
                                                     for turn in selected) if text else tokens,
                   "history_read_partial": more,
                   "summary_tokens": self.counter.count_text(text) if text else 0,
                   "model_calls": len((isolated_usage.catalog_trace or {}).get("prompt_budgets", [])),
                   "unprocessed_messages": conversation.metadata["message_count"] - source.source_message_end - 1}
        summary = None if not text else {
            **replace(source, version=SEMANTIC_SUMMARY_VERSION, summary_status="semantic_generated",
                      summary_tokens=self.counter.count_text(text)).as_evidence(),
            "text": text, "scope": self.scope(state),
            "latest_batch_start": selected[0][0].metadata["message_index"],
        }
        accepted = await store.save_history_summary(conversation_id, user, scope=self.scope(state),
            expected_end=expected_end, sources=[m for turn in selected for m in turn],
            summary=summary, attempt=attempt)
        logger.info("history_compaction %s", json.dumps({**attempt, "accepted": accepted}))
        if accepted and text:
            fresh = await store.get_conversation(conversation_id, user)
            if fresh is not None:
                fresh_state = AnalysisState.from_mapping(fresh.metadata.get("working_memory"))
                if self.scope(fresh_state) != self.scope(state):
                    return False
                _, _, remaining, more = await self._read(fresh, state, store, before=before)
                return more or remaining > self.budget.history_recent_target_tokens
        return False

    def _prompt(self, turns, saved):
        return summary_prompt(turns, previous_text=saved["text"] if saved else None,
                              target_tokens=self.budget.history_summary_target_tokens)

    async def _summarize(self, turns, source):
        if self.counter.count_text(summary_prompt(turns, previous_text=source.previous_text,
                target_tokens=self.budget.history_summary_target_tokens)) <= self.budget.history_summary_input_tokens:
            return await self._call_summary(turns, source)
        previous = source.previous_text
        for message in self._narrative(turns):
            remaining = message.content or ""
            while remaining:
                low, high = 1, len(remaining)
                while low < high:
                    middle = (low + high + 1) // 2
                    part = message.model_copy(update={"content": remaining[:middle]})
                    size = self.counter.count_text(summary_prompt([[part]], previous_text=previous,
                                      target_tokens=self.budget.history_summary_target_tokens))
                    if size <= self.budget.history_summary_input_tokens:
                        low = middle
                    else:
                        high = middle - 1
                boundary = max(remaining.rfind("\n", 0, low), remaining.rfind("。", 0, low))
                length = boundary + 1 if boundary > low // 2 else low
                part = message.model_copy(update={"content": remaining[:length]})
                prompt = summary_prompt([[part]], previous_text=previous,
                                        target_tokens=self.budget.history_summary_target_tokens)
                if self.counter.count_text(prompt) > self.budget.history_summary_input_tokens:
                    raise ContextCapacityError("摘要提示和旧摘要超过单次输入预算")
                previous = await self._call_summary([[part]], replace(source, previous_text=previous))
                if not previous or self.counter.count_text(previous) > self.budget.history_summary_max_tokens:
                    return None
                remaining = remaining[length:]
        return previous

    async def _call_summary(self, turns, source):
        return await asyncio.wait_for(self.summary_provider(turns, source),
                                     self.budget.history_summary_timeout_seconds)

    async def close(self):
        self.closed = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def recall(self, store, conversation, *, before: int) -> list[dict]:
        messages = await store.get_history_page(conversation.id, conversation.user, before=before, limit=6)
        selected, used = [], 0
        for m in reversed(messages):
            if m.role not in {"user", "assistant"} or m.tool_calls:
                continue
            tokens = self.counter.count_message(m)
            if used + tokens > 800:
                break
            selected.append({"role": m.role, "content": m.content, "message_index": m.metadata["message_index"]})
            used += tokens
            if len(selected) == 3:
                break
        return list(reversed(selected))
