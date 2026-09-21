"""Bounded, provenance-backed conversation context for the trusted Agent."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from vanna.core.filter import ConversationFilter
from vanna.core.storage import Message

from .budget import BudgetUsage, CURRENT_BUDGET
from .history_summary import (
    SEMANTIC_SUMMARY_VERSION,
    HistorySummary,
    SummaryProvider,
    message_chars,
)
from .token_budget import DEFAULT_TOKEN_COUNTER, TokenCounter

if TYPE_CHECKING:
    from vanna.core.storage import Message as MessageType


_COMPACT_TOOL_CONTENT = "[当前回合的工具输出已因上下文预算压缩；不可据此推断新事实。]"
_COMPACT_TURN_CONTENT = "[当前回合的工具交互已因上下文预算压缩；请仅依据当前结构化状态。]"


class ContextBudgetFilter(ConversationFilter):
    """Keep recent complete turns and add a safe boundary for omitted history.

    History summaries are request-local, deterministic provenance markers. They
    do not summarize user/assistant prose and are not persisted as memory.
    """

    def __init__(
        self,
        max_chars: int,
        max_messages: int,
        usage: BudgetUsage | None = None,
        *,
        max_tokens: int | None = None,
        max_prompt_tokens: int | None = None,
        token_counter: TokenCounter | None = None,
        summary_provider: SummaryProvider | None = None,
    ):
        if max_chars <= 0 or max_messages <= 0:
            raise ValueError("context limits must be positive")
        self.max_chars = max_chars
        self.max_messages = max_messages
        self.usage = usage
        self.max_tokens = max_tokens
        self.max_prompt_tokens = max_prompt_tokens
        self.token_counter = token_counter or DEFAULT_TOKEN_COUNTER
        self.summary_provider = summary_provider
        self._summary_cache: dict[str, HistorySummary] = {}

    async def filter_messages(
        self, messages: list["MessageType"]
    ) -> list["MessageType"]:
        turns = _group_complete_turns(messages)
        input_chars = sum(message_chars(message) for message in messages)
        input_messages = len(messages)
        input_tokens = self.token_counter.count_messages(messages)
        if not turns:
            return self._record(
                list(messages),
                input_chars=input_chars,
                input_messages=input_messages,
                turn_count=0,
                retained_turn_count=0,
                omitted_turn_count=0,
                summary=None,
                current_turn_compacted=False,
                current_user_exceeds_budget=False,
                input_tokens=input_tokens,
            )

        start = _latest_suffix_start(
            turns,
            max_chars=self.max_chars,
            max_messages=self.max_messages,
            max_tokens=self.max_tokens,
            token_counter=self.token_counter,
        )
        omitted = turns[:start]
        retained = turns[start:]
        summary: HistorySummary | None = None
        # A summary itself consumes one message and some character budget. If
        # needed, move additional *oldest retained* turns into its contiguous
        # source range. This guarantees no holes in the retained suffix.
        while omitted:
            summary = HistorySummary.from_turns(
                omitted,
                source_turn_start=1,
                reason=_omission_reason(
                    turns, max_chars=self.max_chars, max_messages=self.max_messages
                ),
            )
            summary = replace(
                summary,
                source_tokens=self.token_counter.count_messages(
                    message for turn in omitted for message in turn
                ),
            )
            cached = self._summary_cache.get(summary.source_sha256)
            if cached is not None:
                summary = cached
            elif self.summary_provider is not None:
                try:
                    semantic = await self.summary_provider(omitted, summary)
                except Exception:
                    semantic = None
                if semantic:
                    summary = HistorySummary(
                        source_turn_start=summary.source_turn_start,
                        source_turn_end=summary.source_turn_end,
                        source_message_count=summary.source_message_count,
                        source_chars=summary.source_chars,
                        source_sha256=summary.source_sha256,
                        reason=summary.reason,
                        version=SEMANTIC_SUMMARY_VERSION,
                        semantic_text=semantic[:6_000],
                        summary_status="semantic_generated",
                        summary_tokens=self.token_counter.count_text(semantic),
                        source_tokens=summary.source_tokens,
                    )
                    self._summary_cache[summary.source_sha256] = summary
            usage = self.usage or CURRENT_BUDGET.get()
            if usage is not None and summary.semantic_text:
                usage.record_history_summary(summary.as_evidence(), summary.semantic_text)
            candidate = [summary.as_message(), *_flatten(retained)]
            if (
                len(candidate) <= self.max_messages
                and sum(message_chars(message) for message in candidate)
                <= self.max_chars
                and (
                    self.max_tokens is None
                    or self.token_counter.count_messages(candidate) <= self.max_tokens
                )
            ):
                break
            if len(retained) <= 1:
                # The newest turn must survive. The fallback below compacts it
                # instead of slicing arbitrary natural-language content.
                break
            omitted.append(retained.pop(0))

        current_turn_compacted = False
        current_user_exceeds_budget = False
        if omitted and summary is not None:
            result = [summary.as_message(), *_flatten(retained)]
        else:
            result = _flatten(retained)

        if (
            len(result) > self.max_messages
            or sum(message_chars(message) for message in result) > self.max_chars
            or (
                self.max_tokens is not None
                and self.token_counter.count_messages(result) > self.max_tokens
            )
        ):
            current_turn_compacted = True
            prefix = [summary.as_message()] if omitted and summary is not None else []
            compacted, current_user_exceeds_budget = _compact_latest_turn(
                retained[-1],
                max_chars=max(0, self.max_chars - sum(message_chars(item) for item in prefix)),
                max_messages=max(0, self.max_messages - len(prefix)),
            )
            # A full current user question is more important than the summary
            # marker. If the two cannot coexist, omit only the marker and keep
            # the omitted-range provenance in server evidence.
            if prefix and (
                len(prefix) + len(compacted) > self.max_messages
                or sum(message_chars(item) for item in [*prefix, *compacted])
                > self.max_chars
            ):
                prefix = []
                compacted, current_user_exceeds_budget = _compact_latest_turn(
                    retained[-1],
                    max_chars=self.max_chars,
                    max_messages=self.max_messages,
                )
                summary = None
            result = [*prefix, *compacted]

        return self._record(
            result,
            input_chars=input_chars,
            input_messages=input_messages,
            turn_count=len(turns),
            retained_turn_count=len(retained),
            omitted_turn_count=len(omitted),
            summary=summary,
            current_turn_compacted=current_turn_compacted,
            current_user_exceeds_budget=current_user_exceeds_budget,
            input_tokens=input_tokens,
        )

    def _record(
        self,
        result: list["MessageType"],
        *,
        input_chars: int,
        input_messages: int,
        turn_count: int,
        retained_turn_count: int,
        omitted_turn_count: int,
        summary: HistorySummary | None,
        current_turn_compacted: bool,
        current_user_exceeds_budget: bool,
        input_tokens: int = 0,
    ) -> list["MessageType"]:
        output_chars = sum(message_chars(message) for message in result)
        truncated = bool(omitted_turn_count or current_turn_compacted)
        usage = self.usage or CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_context(output_chars, truncated)
            usage.record_context_budget(
                {
                    "version": "context-budget-v1",
                    "max_context_chars": self.max_chars,
                    "max_context_messages": self.max_messages,
                    "input_chars": input_chars,
                    "input_messages": input_messages,
                    "input_turns": turn_count,
                    "output_chars": output_chars,
                    "output_messages": len(result),
                    "retained_turns": retained_turn_count,
                    "omitted_turns": omitted_turn_count,
                    "summary_inserted": summary is not None,
                    "summary": summary.as_evidence() if summary else None,
                    "current_turn_compacted": current_turn_compacted,
                    "current_user_exceeds_budget": current_user_exceeds_budget,
                    "max_context_tokens": self.max_tokens,
                    "max_prompt_tokens": self.max_prompt_tokens,
                    "input_context_tokens": input_tokens,
                    "output_context_tokens": self.token_counter.count_messages(result),
                    "tokenizer_mode": self.token_counter.mode,
                    "tokenizer_id": self.token_counter.tokenizer_id,
                }
            )
        return result


def _group_complete_turns(messages: list["MessageType"]) -> list[list[Message]]:
    """Split history at each user message; preserve all following tool traffic."""
    turns: list[list[Message]] = []
    current: list[Message] = []
    for message in messages:
        if message.role == "user" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return turns


def _latest_suffix_start(
    turns: list[list[Message]], *, max_chars: int, max_messages: int,
    max_tokens: int | None = None, token_counter: TokenCounter | None = None,
) -> int:
    """Return a contiguous newest suffix; never skip a middle turn."""
    used_chars = 0
    used_messages = 0
    used_tokens = 0
    start = len(turns)
    for index in range(len(turns) - 1, -1, -1):
        group = turns[index]
        group_chars = sum(message_chars(message) for message in group)
        token_over = max_tokens is not None and token_counter is not None and (
            used_tokens + token_counter.count_messages(group) > max_tokens
        )
        if start != len(turns) and (
            used_chars + group_chars > max_chars
            or used_messages + len(group) > max_messages
            or token_over
        ):
            break
        start = index
        used_chars += group_chars
        used_messages += len(group)
        if max_tokens is not None and token_counter is not None:
            used_tokens += token_counter.count_messages(group)
    return start


def _compact_latest_turn(
    turn: list[Message], *, max_chars: int, max_messages: int
) -> tuple[list[Message], bool]:
    """Keep current user text and replace tool traffic with fixed markers."""
    if not turn:
        return [], False
    user = next((message for message in turn if message.role == "user"), turn[0])
    user_chars = message_chars(user)
    if max_messages <= 0:
        # This only occurs when an impossible tiny message budget has already
        # been consumed by a summary; caller will drop that summary and retry.
        return [user], user_chars > max_chars
    if max_messages == 1 or user_chars >= max_chars:
        return [user], user_chars > max_chars

    # Preserve role/tool-call shape whenever the message budget permits it.
    compacted = [user]
    for message in turn:
        if message is user:
            continue
        marker = _COMPACT_TOOL_CONTENT if message.role == "tool" else _COMPACT_TURN_CONTENT
        candidate = message.model_copy(update={"content": marker})
        if len(compacted) >= max_messages:
            break
        if sum(message_chars(item) for item in [*compacted, candidate]) > max_chars:
            break
        compacted.append(candidate)
    if len(compacted) == 1 and max_messages > 1:
        assistant = next(
            (message for message in reversed(turn) if message.role == "assistant"), None
        )
        if assistant is not None:
            compacted.append(
                assistant.model_copy(
                    update={"content": _COMPACT_TURN_CONTENT, "tool_calls": None}
                )
            )
    return compacted, user_chars > max_chars


def _omission_reason(
    turns: list[list[Message]], *, max_chars: int, max_messages: int
) -> str:
    total_chars = sum(message_chars(message) for turn in turns for message in turn)
    total_messages = sum(len(turn) for turn in turns)
    chars = total_chars > max_chars
    messages = total_messages > max_messages
    if chars and messages:
        return "both"
    return "char_budget" if chars else "message_budget"


def _flatten(turns: list[list[Message]]) -> list[Message]:
    return [message for turn in turns for message in turn]
