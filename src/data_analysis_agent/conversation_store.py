"""PostgreSQL-backed Vanna conversation storage with strict user ownership."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import re
import uuid
from typing import Any

import psycopg2
import psycopg2.extras

from vanna.core.storage import Conversation, ConversationStore, Message
from vanna.core.tool import ToolCall
from vanna.core.user import User

from .postgres_runner import PostgresConnectionSettings
from .budget import CURRENT_BUDGET


CURRENT_MESSAGE_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("message_context", default=None)


class ConversationOwnershipError(PermissionError):
    """Raised when an existing conversation belongs to another user."""


class ConversationStateConflict(RuntimeError):
    """Raised when a stale conversation attempts to overwrite newer state."""


class InvalidConversationId(ValueError):
    """Raised when a client supplies an invalid conversation identifier."""


class PostgresConversationStore(ConversationStore):
    """Persist Vanna conversations in the application PostgreSQL schema."""

    _conversation_id_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

    def __init__(
        self,
        settings: PostgresConnectionSettings | None = None,
        *,
        default_workspace_id: str = "olist-demo",
    ):
        self.settings = settings or PostgresConnectionSettings.from_environment()
        if not isinstance(default_workspace_id, str) or not default_workspace_id.strip():
            raise ValueError("default_workspace_id must be a non-empty string")
        self.default_workspace_id = default_workspace_id.strip()

    async def create_conversation(
        self, conversation_id: str, user: User, initial_message: str
    ) -> Conversation:
        self._validate_conversation_id(conversation_id)
        conversation = Conversation(
            id=conversation_id,
            user=user,
            messages=[Message(role="user", content=initial_message)],
        )
        await self.update_conversation(conversation)
        return conversation

    async def get_conversation(
        self, conversation_id: str, user: User, workspace_id: str | None = None,
        *, recent_turns: int | None = 20,
    ) -> Conversation | None:
        self._validate_conversation_id(conversation_id)
        usage = CURRENT_BUDGET.get()
        packet = usage.context_packet if usage is not None else None
        start = packet["recent_start_index"] if packet is not None and recent_turns is not None else None
        return await asyncio.to_thread(
            self._get_sync, conversation_id, user, workspace_id or self.default_workspace_id, recent_turns, start
        )

    async def get_recent_turns(self, conversation_id: str, user: User, *, limit: int = 6) -> Conversation | None:
        return await self.get_conversation(conversation_id, user, recent_turns=limit)

    async def get_history_page(
        self, conversation_id: str, user: User, *, before: int | None = None, limit: int = 50,
    ) -> list[Message]:
        self._validate_conversation_id(conversation_id)
        if limit <= 0 or (before is not None and before < 0):
            raise ValueError("invalid history cursor or page size")
        return await asyncio.to_thread(self._history_page_sync, conversation_id, user, before, min(limit, 200))

    async def get_turn_range(
        self, conversation_id: str, user: User, *, start: int, before: int,
        limit_turns: int = 20,
    ) -> list[Message]:
        """Read complete old turns for incremental compaction, scoped to owner/workspace."""
        self._validate_conversation_id(conversation_id)
        if start < 0 or before < start or not 1 <= limit_turns <= 100:
            raise ValueError("invalid compaction range")
        return await asyncio.to_thread(
            self._turn_range_sync, conversation_id, user, start, before, limit_turns
        )

    async def append_turn_messages(self, conversation: Conversation, messages: list[Message]) -> None:
        for message in messages:
            conversation.add_message(message)
        await self.update_conversation(conversation)

    async def update_conversation(self, conversation: Conversation) -> None:
        self._validate_conversation_id(conversation.id)
        await asyncio.to_thread(self._update_sync, conversation)

    async def save_history_summary(
        self, conversation_id: str, user: User, *, scope: dict,
        expected_end: int, sources: list[Message], summary: dict | None, attempt: dict,
    ) -> bool:
        """Compare-and-update derived memory without touching messages or state_version."""
        self._validate_conversation_id(conversation_id)
        return await asyncio.to_thread(
            self._save_history_summary_sync, conversation_id, user, scope,
            expected_end, sources, summary, attempt,
        )

    def _save_history_summary_sync(self, conversation_id, user, scope, expected_end, sources, summary, attempt):
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT working_memory, dataset_version_id, metric_version FROM app.conversations "
                        "WHERE conversation_id=%s AND user_id=%s AND workspace_id=%s AND status='active' FOR UPDATE",
                        (conversation_id, user.id, scope["workspace_id"]),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return False
                    memory = row[0] or {}
                    current_scope = {k: memory.get(k) for k in scope}
                    if current_scope != scope or row[1] != scope["dataset_version"] or row[2] != scope["metric_version"]:
                        return False
                    saved = memory.get("__history_summary")
                    end = saved["source_message_end"] if saved and saved.get("scope") == scope else memory.get("history_start_index", 0) - 1
                    if end != expected_end:
                        return False
                    cursor.execute(
                        "SELECT message_id, message_index FROM app.messages WHERE conversation_id=%s "
                        "AND message_index >= %s AND message_index <= %s ORDER BY message_index",
                        (conversation_id, sources[0].metadata["message_index"], sources[-1].metadata["message_index"]),
                    )
                    if cursor.fetchall() != [(m.metadata["message_id"], m.metadata["message_index"]) for m in sources]:
                        return False
                    if summary is not None:
                        memory["__history_summary"] = summary
                    memory["__summary_attempt"] = attempt
                    cursor.execute(
                        "UPDATE app.conversations SET working_memory=%s WHERE conversation_id=%s",
                        (psycopg2.extras.Json(memory), conversation_id),
                    )
                    return True
        finally:
            connection.close()

    async def truncate_from_last_user_message(
        self, conversation_id: str, user: User
    ) -> int | None:
        """Delete the latest user message and every message after it.

        Returns the removed user-message index, or ``None`` when that
        conversation has no user message. Working memory is cleared so the
        replacement question does not reuse the deleted turn.
        """

        self._validate_conversation_id(conversation_id)
        return await asyncio.to_thread(
            self._truncate_from_last_user_sync,
            conversation_id,
            user,
            self.default_workspace_id,
        )

    async def delete_conversation(self, conversation_id: str, user: User) -> bool:
        self._validate_conversation_id(conversation_id)
        return await asyncio.to_thread(
            self._delete_sync, conversation_id, user, self.default_workspace_id
        )

    async def list_conversations(
        self, user: User, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        if limit <= 0 or offset < 0:
            raise ValueError("limit must be positive and offset must not be negative")
        return await asyncio.to_thread(self._list_sync, user, min(limit, 50), offset)

    def _connect(self):
        return psycopg2.connect(
            host=self.settings.host,
            port=self.settings.port,
            database=self.settings.database,
            user=self.settings.writer_user,
        )

    @classmethod
    def _validate_conversation_id(cls, conversation_id: str) -> None:
        if not isinstance(
            conversation_id, str
        ) or not cls._conversation_id_pattern.fullmatch(conversation_id):
            raise InvalidConversationId(
                "conversation_id must be 1-128 characters of letters, digits, '_' or '-'; "
                "it must start with a letter or digit"
            )

    def _get_sync(
        self, conversation_id: str, user: User, workspace_id: str, recent_turns: int | None = 20,
        context_start: int | None = None,
    ) -> Conversation | None:
        connection = self._connect()
        try:
            with connection.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cursor:
                cursor.execute(
                    """
                    SELECT * FROM app.conversations
                    WHERE conversation_id = %s AND user_id = %s
                      AND workspace_id = %s AND status = 'active'
                    """,
                    (conversation_id, user.id, workspace_id),
                )
                conversation_row = cursor.fetchone()
                if not conversation_row:
                    return None
                start_index = 0
                if context_start is not None:
                    start_index = context_start
                elif recent_turns is not None:
                    if recent_turns <= 0:
                        raise ValueError("recent_turns must be positive")
                    cursor.execute(
                        "SELECT message_index FROM app.messages WHERE conversation_id=%s AND role='user' "
                        "ORDER BY message_index DESC LIMIT %s",
                        (conversation_id, recent_turns),
                    )
                    starts = cursor.fetchall()
                    start_index = starts[-1]["message_index"] if starts else 0
                    start_index = max(start_index, (conversation_row.get("working_memory") or {}).get("history_start_index", 0))
                cursor.execute(
                    """
                    SELECT message_id, message_index, role, content, tool_calls_json, tool_call_id, metadata_json, created_at
                    FROM app.messages
                    WHERE conversation_id = %s AND message_index >= %s
                    ORDER BY message_index ASC
                    """,
                    (conversation_id, start_index),
                )
                messages = [self._message_from_row(row) for row in cursor.fetchall()]
            return Conversation(
                id=conversation_row["conversation_id"],
                user=self._user_from_row(conversation_row),
                messages=messages,
                created_at=conversation_row["created_at"],
                updated_at=conversation_row["updated_at"],
                metadata={
                    "title": conversation_row["title"],
                    "message_count": conversation_row["message_count"],
                    "dataset_version_id": conversation_row["dataset_version_id"],
                    "metric_version": conversation_row["metric_version"],
                    "workspace_id": conversation_row["workspace_id"],
                    "state_version": conversation_row["state_version"],
                    "working_memory": conversation_row.get("working_memory") or {},
                    "history_summary": (conversation_row.get("working_memory") or {}).get("__history_summary"),
                    "summary_attempt": (conversation_row.get("working_memory") or {}).get("__summary_attempt"),
                    "history_start_index": start_index,
                    "loaded_message_count": len(messages),
                },
            )
        finally:
            connection.close()

    def _update_sync(self, conversation: Conversation) -> None:
        role = self._role_for_user(conversation.user)
        dataset_version = conversation.metadata.get(
            "dataset_version_id", "olist-kaggle-v2-2026-08-03"
        )
        metric_version = conversation.metadata.get("metric_version", "0.1-draft")
        workspace_id = conversation.metadata.get("workspace_id", self.default_workspace_id)
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise ValueError("conversation workspace_id must be a non-empty string")
        workspace_id = workspace_id.strip()
        expected_state_version = conversation.metadata.get("state_version", 0)
        if not isinstance(expected_state_version, int) or expected_state_version < 0:
            raise ValueError("conversation state_version must be a non-negative integer")
        title = self._title_for(conversation)
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    stored_memory = dict(conversation.metadata.get("working_memory", {}))
                    if conversation.metadata.get("history_summary"):
                        stored_memory["__history_summary"] = conversation.metadata["history_summary"]
                    cursor.execute(
                        """
                        INSERT INTO app.conversations (
                            conversation_id, user_id, user_role, workspace_id, title,
                            dataset_version_id, metric_version, working_memory,
                            message_count, state_version, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (conversation_id) DO NOTHING
                        """,
                        (
                            conversation.id,
                            conversation.user.id,
                            role,
                            workspace_id,
                            title,
                            dataset_version,
                            metric_version,
                            psycopg2.extras.Json(
                                stored_memory
                            ),
                            0,
                            0,
                            self._utc(conversation.created_at),
                            self._utc(conversation.updated_at),
                        ),
                    )
                    cursor.execute(
                        """
                        SELECT user_id, workspace_id, state_version, working_memory
                        FROM app.conversations WHERE conversation_id = %s FOR UPDATE
                        """,
                        (conversation.id,),
                    )
                    owner = cursor.fetchone()
                    if not owner or owner[0] != conversation.user.id:
                        raise ConversationOwnershipError(
                            "conversation belongs to another user"
                        )
                    if owner[1] != workspace_id:
                        raise ConversationStateConflict("conversation workspace does not match")
                    if owner[2] != expected_state_version:
                        raise ConversationStateConflict(
                            "conversation state_version is stale"
                        )
                    # A background summary may have advanced after this object
                    # was loaded. Never replace it with a stale foreground copy.
                    db_memory = owner[3] or {}
                    stored_memory.pop("__history_summary", None)
                    stored_memory.pop("__summary_attempt", None)
                    same_task = all(db_memory.get(k) == stored_memory.get(k) for k in (
                        "task_id", "workspace_id", "dataset_version", "metric_version", "history_start_index"
                    ))
                    if same_task and db_memory.get("__history_summary"):
                        stored_memory["__history_summary"] = db_memory["__history_summary"]
                    if same_task and db_memory.get("__summary_attempt"):
                        stored_memory["__summary_attempt"] = db_memory["__summary_attempt"]
                    conversation.metadata["history_summary"] = stored_memory.get("__history_summary")
                    loaded = conversation.metadata.get("loaded_message_count", 0)
                    offset = conversation.metadata.get("history_start_index", 0)
                    turn_context = dict(CURRENT_MESSAGE_CONTEXT.get() or {})
                    for index, message in enumerate(conversation.messages[loaded:], start=offset + loaded):
                        if message.role == "user" and CURRENT_MESSAGE_CONTEXT.get() is None:
                            turn_context = {"turn_id": str(uuid.uuid4())}
                        message.metadata.update(turn_context)
                        cursor.execute(
                            """
                            INSERT INTO app.messages (
                                conversation_id, message_index, user_id, user_role, role,
                                content, tool_calls_json, tool_call_id, metadata_json, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (conversation_id, message_index) DO NOTHING
                            RETURNING message_id
                            """,
                            (
                                conversation.id,
                                index,
                                conversation.user.id,
                                role,
                                message.role,
                                message.content,
                                psycopg2.extras.Json(
                                    [
                                        call.model_dump(mode="json")
                                        for call in message.tool_calls
                                    ]
                                )
                                if message.tool_calls
                                else None,
                                message.tool_call_id,
                                psycopg2.extras.Json(message.metadata),
                                self._utc(message.timestamp),
                            ),
                        )
                        inserted = cursor.fetchone()
                        if inserted:
                            message.metadata.update(message_id=inserted[0], message_index=index)
                    cursor.execute(
                        """
                        UPDATE app.conversations
                        SET title = %s, message_count = %s, updated_at = %s,
                            dataset_version_id = %s, metric_version = %s,
                            working_memory = %s, state_version = state_version + 1
                        WHERE conversation_id = %s AND workspace_id = %s
                          AND state_version = %s
                        """,
                        (
                            title,
                            offset + len(conversation.messages),
                            self._utc(conversation.updated_at),
                            dataset_version,
                            metric_version,
                            psycopg2.extras.Json(
                                stored_memory
                            ),
                            conversation.id,
                            workspace_id,
                            expected_state_version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConversationStateConflict("conversation state_version is stale")
                    conversation.metadata["workspace_id"] = workspace_id
                    conversation.metadata["state_version"] = expected_state_version + 1
                    conversation.metadata["message_count"] = offset + len(conversation.messages)
                    conversation.metadata["loaded_message_count"] = len(conversation.messages)
        finally:
            connection.close()

    def _truncate_from_last_user_sync(
        self, conversation_id: str, user: User, workspace_id: str
    ) -> int | None:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT workspace_id, state_version, working_memory
                        FROM app.conversations
                        WHERE conversation_id = %s AND user_id = %s AND status = 'active'
                        FOR UPDATE
                        """,
                        (conversation_id, user.id),
                    )
                    row = cursor.fetchone()
                    if row is None or row[0] != workspace_id:
                        return None
                    cursor.execute(
                        """
                        SELECT MAX(message_index) FROM app.messages
                        WHERE conversation_id = %s AND role = 'user'
                        """,
                        (conversation_id,),
                    )
                    user_index = cursor.fetchone()[0]
                    if user_index is None:
                        return None
                    cursor.execute(
                        """
                        DELETE FROM app.messages
                        WHERE conversation_id = %s AND message_index >= %s
                        """,
                        (conversation_id, user_index),
                    )
                    cursor.execute(
                        """
                        SELECT COUNT(*) FROM app.messages WHERE conversation_id = %s
                        """,
                        (conversation_id,),
                    )
                    remaining = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        UPDATE app.conversations
                        SET message_count = %s, working_memory = %s,
                            state_version = state_version + 1, updated_at = CURRENT_TIMESTAMP
                        WHERE conversation_id = %s AND state_version = %s
                        """,
                        (
                            remaining,
                            psycopg2.extras.Json({}),
                            conversation_id,
                            row[1],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ConversationStateConflict("conversation state_version is stale")
                    return int(user_index)
        finally:
            connection.close()

    def _delete_sync(self, conversation_id: str, user: User, workspace_id: str) -> bool:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM app.conversations
                        WHERE conversation_id = %s AND user_id = %s AND workspace_id = %s
                        RETURNING conversation_id
                        """,
                        (conversation_id, user.id, workspace_id),
                    )
                    return cursor.fetchone() is not None
        finally:
            connection.close()

    def _list_sync(self, user: User, limit: int, offset: int) -> list[Conversation]:
        connection = self._connect()
        try:
            with connection.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cursor:
                cursor.execute(
                    """
                    SELECT * FROM app.conversations
                    WHERE user_id = %s AND workspace_id = %s
                      AND status = 'active' AND message_count > 0
                    ORDER BY updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user.id, self.default_workspace_id, limit, offset),
                )
                return [
                    Conversation(
                        id=row["conversation_id"],
                        user=self._user_from_row(row),
                        messages=[],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                        metadata={
                            "title": row["title"],
                            "dataset_version_id": row["dataset_version_id"],
                            "metric_version": row["metric_version"],
                            "workspace_id": row["workspace_id"],
                            "state_version": row["state_version"],
                            "working_memory": row.get("working_memory") or {},
                            "message_count": row["message_count"],
                        },
                    )
                    for row in cursor.fetchall()
                ]
        finally:
            connection.close()

    @staticmethod
    def _message_from_row(row: Any) -> Message:
        raw_calls = row["tool_calls_json"] or []
        return Message(
            role=row["role"],
            content=row["content"],
            tool_calls=[ToolCall.model_validate(call) for call in raw_calls] or None,
            tool_call_id=row["tool_call_id"],
            metadata={**(row["metadata_json"] or {}), "message_id": row["message_id"], "message_index": row["message_index"]},
            timestamp=row["created_at"],
        )

    def _history_page_sync(self, conversation_id: str, user: User, before: int | None, limit: int) -> list[Message]:
        connection = self._connect()
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT m.* FROM app.messages m JOIN app.conversations c USING(conversation_id) "
                    "WHERE c.conversation_id=%s AND c.user_id=%s AND c.workspace_id=%s AND c.status='active' "
                    "AND (%s IS NULL OR m.message_index < %s) ORDER BY m.message_index DESC LIMIT %s",
                    (conversation_id, user.id, self.default_workspace_id, before, before, limit),
                )
                return [self._message_from_row(row) for row in reversed(cursor.fetchall())]
        finally:
            connection.close()

    def _turn_range_sync(self, conversation_id, user, start, before, limit_turns):
        connection = self._connect()
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT m.message_index FROM app.messages m "
                    "JOIN app.conversations c USING(conversation_id) "
                    "WHERE c.conversation_id=%s AND c.user_id=%s AND c.workspace_id=%s "
                    "AND c.status='active' AND m.role='user' "
                    "AND m.message_index >= %s AND m.message_index < %s "
                    "ORDER BY m.message_index LIMIT %s",
                    (conversation_id, user.id, self.default_workspace_id, start, before, limit_turns + 1),
                )
                starts = cursor.fetchall()
                if not starts:
                    return []
                end = starts[limit_turns]["message_index"] if len(starts) > limit_turns else before
                cursor.execute(
                    "SELECT m.* FROM app.messages m JOIN app.conversations c USING(conversation_id) "
                    "WHERE c.conversation_id=%s AND c.user_id=%s AND c.workspace_id=%s "
                    "AND c.status='active' AND m.message_index >= %s AND m.message_index < %s "
                    "ORDER BY m.message_index",
                    (conversation_id, user.id, self.default_workspace_id, starts[0]["message_index"], end),
                )
                return [self._message_from_row(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    @staticmethod
    def _user_from_row(row: Any) -> User:
        return User(
            id=row["user_id"],
            username=row["user_id"],
            group_memberships=[row["user_role"]],
        )

    @staticmethod
    def _role_for_user(user: User) -> str:
        return "admin" if "admin" in user.group_memberships else "analyst"

    @staticmethod
    def _title_for(conversation: Conversation) -> str | None:
        if conversation.metadata.get("title"):
            return conversation.metadata["title"]
        for message in conversation.messages:
            if message.role == "user" and message.content.strip():
                return message.content.strip()[:120]
        return None

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
