"""Aggregate request outcomes without exposing questions, SQL, or other users."""

from __future__ import annotations

import psycopg2
import psycopg2.extras

from .postgres_runner import PostgresConnectionSettings


class RuntimeDiagnostics:
    def __init__(self, settings: PostgresConnectionSettings | None = None):
        self.settings = settings or PostgresConnectionSettings.from_environment()

    def _connect(self):
        return psycopg2.connect(
            host=self.settings.host,
            port=self.settings.port,
            database=self.settings.database,
            user=self.settings.writer_user,
        )

    def summary(self, *, subject_id: str, include_workspace: bool) -> dict[str, object]:
        connection = self._connect()
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                return {
                    "scope": "workspace" if include_workspace else "self",
                    "termination_counts": self._termination_counts(
                        cursor,
                        subject_id=None if include_workspace else subject_id,
                    ),
                    "repair_succeeded": self._repair_count(
                        cursor, subject_id=None if include_workspace else subject_id
                    ),
                }
        finally:
            connection.close()

    @staticmethod
    def _termination_counts(cursor, *, subject_id: str | None) -> dict[str, int]:
        if subject_id is None:
            cursor.execute(
                """
                SELECT termination_reason AS label, COUNT(*) AS count
                FROM app.agent_runs
                GROUP BY termination_reason
                ORDER BY termination_reason
                """
            )
        else:
            cursor.execute(
                """
                SELECT termination_reason AS label, COUNT(*) AS count
                FROM app.agent_runs
                WHERE user_id = %s
                GROUP BY termination_reason
                ORDER BY termination_reason
                """,
                (subject_id,),
            )
        return {str(row["label"]): int(row["count"]) for row in cursor.fetchall()}

    @staticmethod
    def _repair_count(cursor, *, subject_id: str | None) -> int:
        if subject_id is None:
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM app.agent_runs
                WHERE repair_evidence->>'repair_execution_status' = 'succeeded'
                """
            )
        else:
            cursor.execute(
                """
                SELECT COUNT(*) AS count FROM app.agent_runs
                WHERE user_id = %s
                  AND repair_evidence->>'repair_execution_status' = 'succeeded'
                """,
                (subject_id,),
            )
        return int(cursor.fetchone()["count"])
