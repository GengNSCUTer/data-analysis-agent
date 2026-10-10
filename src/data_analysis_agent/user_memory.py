"""Explicit user preferences and saved analyses.

These records are visible and deletable. They are not injected into SQL prompts
and cannot supply metrics, filters, or result facts.
"""

from __future__ import annotations

import uuid

import psycopg2
import psycopg2.extras

from .postgres_runner import PostgresConnectionSettings


ORGANIZATION_ID = "demo"
ALLOWED_PREFERENCES: dict[str, frozenset[str]] = {
    "language": frozenset({"zh", "en"}),
    "timezone": frozenset({"Asia/Shanghai", "UTC"}),
    "default_chart": frozenset({"bar", "line", "none"}),
    "display_format": frozenset({"plain", "currency"}),
}


class UserMemoryStore:
    def __init__(self, settings: PostgresConnectionSettings | None = None):
        self.settings = settings or PostgresConnectionSettings.from_environment()

    def _connect(self):
        return psycopg2.connect(
            host=self.settings.host,
            port=self.settings.port,
            database=self.settings.database,
            user=self.settings.writer_user,
        )

    def list_preferences(self, subject_id: str, workspace_id: str) -> list[dict[str, str]]:
        connection = self._connect()
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT memory_kind, value_text, updated_at
                    FROM app.user_preferences
                    WHERE organization_id = %s AND subject_id = %s AND workspace_id = %s
                    ORDER BY memory_kind
                    """,
                    (ORGANIZATION_ID, subject_id, workspace_id),
                )
                return [
                    {
                        "memory_kind": row["memory_kind"],
                        "value": row["value_text"],
                        "updated_at": row["updated_at"].isoformat(),
                    }
                    for row in cursor.fetchall()
                ]
        finally:
            connection.close()

    def put_preference(
        self, subject_id: str, workspace_id: str, memory_kind: str, value: str
    ) -> None:
        allowed = ALLOWED_PREFERENCES.get(memory_kind)
        if allowed is None or value not in allowed:
            raise ValueError("unsupported preference")
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO app.user_preferences (
                            organization_id, subject_id, workspace_id, memory_kind, value_text
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (organization_id, subject_id, workspace_id, memory_kind)
                        DO UPDATE SET value_text = EXCLUDED.value_text, updated_at = CURRENT_TIMESTAMP
                        """,
                        (ORGANIZATION_ID, subject_id, workspace_id, memory_kind, value),
                    )
        finally:
            connection.close()

    def delete_preference(self, subject_id: str, workspace_id: str, memory_kind: str) -> bool:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM app.user_preferences
                        WHERE organization_id = %s AND subject_id = %s
                          AND workspace_id = %s AND memory_kind = %s
                        """,
                        (ORGANIZATION_ID, subject_id, workspace_id, memory_kind),
                    )
                    return cursor.rowcount == 1
        finally:
            connection.close()

    def save_analysis(
        self,
        *,
        subject_id: str,
        workspace_id: str,
        dataset_version: str,
        metric_version: str,
        artifact_id: str | None,
        summary: str,
    ) -> str:
        saved_id = f"saved_{uuid.uuid4().hex[:12]}"
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO app.saved_analyses (
                            saved_id, organization_id, subject_id, workspace_id,
                            dataset_version, metric_version, artifact_id, summary
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            saved_id,
                            ORGANIZATION_ID,
                            subject_id,
                            workspace_id,
                            dataset_version,
                            metric_version,
                            artifact_id,
                            summary[:1_200],
                        ),
                    )
            return saved_id
        finally:
            connection.close()

    def list_analyses(self, subject_id: str, workspace_id: str) -> list[dict[str, str | None]]:
        connection = self._connect()
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT saved_id, dataset_version, metric_version, artifact_id, summary, created_at
                    FROM app.saved_analyses
                    WHERE organization_id = %s AND subject_id = %s AND workspace_id = %s
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    (ORGANIZATION_ID, subject_id, workspace_id),
                )
                return [
                    {
                        "saved_id": row["saved_id"],
                        "dataset_version": row["dataset_version"],
                        "metric_version": row["metric_version"],
                        "artifact_id": row["artifact_id"],
                        "summary": row["summary"],
                        "created_at": row["created_at"].isoformat(),
                    }
                    for row in cursor.fetchall()
                ]
        finally:
            connection.close()

    def delete_analysis(self, subject_id: str, workspace_id: str, saved_id: str) -> bool:
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM app.saved_analyses
                        WHERE saved_id = %s AND organization_id = %s
                          AND subject_id = %s AND workspace_id = %s
                        """,
                        (saved_id, ORGANIZATION_ID, subject_id, workspace_id),
                    )
                    return cursor.rowcount == 1
        finally:
            connection.close()
