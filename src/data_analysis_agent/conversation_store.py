"""PostgreSQL-backed Vanna conversation storage with strict user ownership."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from typing import Any

import psycopg2
import psycopg2.extras

from vanna.core.storage import Conversation, ConversationStore, Message
from vanna.core.tool import ToolCall
from vanna.core.user import User

from .postgres_runner import PostgresConnectionSettings


class ConversationOwnershipError(PermissionError):
    """Raised when an existing conversation belongs to another user."""


class InvalidConversationId(ValueError):
    """Raised when a client supplies an invalid conversation identifier."""


class PostgresConversationStore(ConversationStore):
    """Persist Vanna conversations in the application PostgreSQL schema."""

    _conversation_id_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

    def __init__(self, settings: PostgresConnectionSettings | None = None):
        self.settings = settings or PostgresConnectionSettings.from_environment()

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
        self, conversation_id: str, user: User
    ) -> Conversation | None:
        self._validate_conversation_id(conversation_id)
        return await asyncio.to_thread(self._get_sync, conversation_id, user)

    async def update_conversation(self, conversation: Conversation) -> None:
        self._validate_conversation_id(conversation.id)
        await asyncio.to_thread(self._update_sync, conversation)

    async def delete_conversation(self, conversation_id: str, user: User) -> bool:
        self._validate_conversation_id(conversation_id)
        return await asyncio.to_thread(self._delete_sync, conversation_id, user)

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

    def _get_sync(self, conversation_id: str, user: User) -> Conversation | None:
        connection = self._connect()
        try:
            with connection.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cursor:
                cursor.execute(
                    "SELECT * FROM app.conversations WHERE conversation_id = %s AND status = 'active'",
                    (conversation_id,),
                )
                conversation_row = cursor.fetchone()
                if not conversation_row or conversation_row["user_id"] != user.id:
                    return None
                cursor.execute(
                    """
                    SELECT role, content, tool_calls_json, tool_call_id, metadata_json, created_at
                    FROM app.messages
                    WHERE conversation_id = %s
                    ORDER BY message_index ASC
                    """,
                    (conversation_id,),
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
                    "working_memory": conversation_row.get("working_memory") or {},
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
        title = self._title_for(conversation)
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO app.conversations (
                            conversation_id, user_id, user_role, title,
                            dataset_version_id, metric_version, working_memory,
                            message_count, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (conversation_id) DO NOTHING
                        """,
                        (
                            conversation.id,
                            conversation.user.id,
                            role,
                            title,
                            dataset_version,
                            metric_version,
                            psycopg2.extras.Json(
                                conversation.metadata.get("working_memory", {})
                            ),
                            0,
                            self._utc(conversation.created_at),
                            self._utc(conversation.updated_at),
                        ),
                    )
                    cursor.execute(
                        "SELECT user_id FROM app.conversations WHERE conversation_id = %s FOR UPDATE",
                        (conversation.id,),
                    )
                    owner = cursor.fetchone()
                    if not owner or owner[0] != conversation.user.id:
                        raise ConversationOwnershipError(
                            "conversation belongs to another user"
                        )
                    for index, message in enumerate(conversation.messages):
                        cursor.execute(
                            """
                            INSERT INTO app.messages (
                                conversation_id, message_index, user_id, user_role, role,
                                content, tool_calls_json, tool_call_id, metadata_json, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (conversation_id, message_index) DO NOTHING
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
                    cursor.execute(
                        """
                        UPDATE app.conversations
                        SET title = %s, message_count = %s, updated_at = %s,
                            dataset_version_id = %s, metric_version = %s,
                            working_memory = %s
                        WHERE conversation_id = %s
                        """,
                        (
                            title,
                            len(conversation.messages),
                            self._utc(conversation.updated_at),
                            dataset_version,
                            metric_version,
                            psycopg2.extras.Json(
                                conversation.metadata.get("working_memory", {})
                            ),
                            conversation.id,
                        ),
                    )
        finally:
            connection.close()

    def _delete_sync(self, conversation_id: str, user: User) -> bool:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM app.conversations WHERE conversation_id = %s AND user_id = %s RETURNING conversation_id",
                        (conversation_id, user.id),
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
                    WHERE user_id = %s AND status = 'active' AND message_count > 0
                    ORDER BY updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user.id, limit, offset),
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
            metadata=row["metadata_json"] or {},
            timestamp=row["created_at"],
        )

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
        for message in conversation.messages:
            if message.role == "user" and message.content.strip():
                return message.content.strip()[:120]
        return None

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
