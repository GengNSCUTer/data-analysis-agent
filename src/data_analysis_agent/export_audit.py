"""Audit a full CSV download without copying the question or the SQL."""

from __future__ import annotations

import psycopg2

from .postgres_runner import PostgresConnectionSettings


class ExportAudit:
    def __init__(self, settings: PostgresConnectionSettings | None = None):
        self.settings = settings or PostgresConnectionSettings.from_environment()

    def record(
        self,
        *,
        artifact_id: str,
        user_id: str,
        user_role: str,
        workspace_id: str,
        row_count: int,
        csv_sha256: str,
    ) -> None:
        connection = psycopg2.connect(
            host=self.settings.host,
            port=self.settings.port,
            database=self.settings.database,
            user=self.settings.writer_user,
        )
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO app.export_audits (
                            artifact_id, user_id, user_role, workspace_id, row_count, csv_sha256
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (artifact_id, user_id, user_role, workspace_id, row_count, csv_sha256),
                    )
        finally:
            connection.close()
