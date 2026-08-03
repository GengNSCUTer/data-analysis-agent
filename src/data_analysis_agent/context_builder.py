"""Bounded conversation context for the trusted Text-to-SQL Agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vanna.core.filter import ConversationFilter
from vanna.core.storage import Message

from .budget import BudgetUsage, CURRENT_BUDGET

if TYPE_CHECKING:
    from vanna.core.storage import Message as MessageType


def _message_chars(message: Message) -> int:
    return len(message.content or "") + 16


class ContextBudgetFilter(ConversationFilter):
    """Keep recent complete turns while bounding model input size.

    A turn is a user message followed by its assistant/tool messages. Keeping
    whole turns prevents an orphaned tool response from being sent to the
    OpenAI-compatible API. The newest turn is always retained; older turns are
    discarded from oldest to newest when the budget is exhausted.
    """

    def __init__(
        self, max_chars: int, max_messages: int, usage: BudgetUsage | None = None
    ):
        if max_chars <= 0 or max_messages <= 0:
            raise ValueError("context limits must be positive")
        self.max_chars = max_chars
        self.max_messages = max_messages
        self.usage = usage

    async def filter_messages(
        self, messages: list["MessageType"]
    ) -> list["MessageType"]:
        groups: list[list[Message]] = []
        current: list[Message] = []
        for message in messages:
            if message.role == "user" and current:
                groups.append(current)
                current = []
            current.append(message)
        if current:
            groups.append(current)

        selected: list[list[Message]] = []
        total_chars = 0
        total_messages = 0
        truncated = False
        for group in reversed(groups):
            group_chars = sum(_message_chars(message) for message in group)
            if selected and (
                total_chars + group_chars > self.max_chars
                or total_messages + len(group) > self.max_messages
            ):
                truncated = True
                continue
            selected.append(group)
            total_chars += group_chars
            total_messages += len(group)

        selected.reverse()
        result = [message for group in selected for message in group]
        if not result and messages:
            result = [messages[-1]]
            truncated = True
        if len(result) != len(messages):
            truncated = True
        if len(result) > self.max_messages:
            # Preserve the latest user question and the latest assistant
            # answer when a pathological turn contains too many tool steps.
            latest_user = next(
                (message for message in reversed(result) if message.role == "user"),
                result[0],
            )
            latest_answer = next(
                (
                    message
                    for message in reversed(result)
                    if message.role == "assistant"
                ),
                result[-1],
            )
            if self.max_messages == 1:
                result = [latest_user]
            else:
                result = (
                    [latest_user]
                    if latest_user is latest_answer
                    else [latest_user, latest_answer]
                )
            truncated = True
        total = sum(_message_chars(message) for message in result)
        if total > self.max_chars:
            compacted: list[Message] = []
            remaining = self.max_chars
            for message in result:
                overhead = 16
                available = max(0, remaining - overhead)
                content = (message.content or "")[:available]
                compacted.append(message.model_copy(update={"content": content}))
                remaining -= overhead + len(content)
            result = compacted
            truncated = True
        usage = self.usage or CURRENT_BUDGET.get()
        if usage is not None:
            usage.record_context(
                sum(_message_chars(message) for message in result), truncated
            )
        return result
