"""Owner-scoped result directory; commit runs inside the conversation transaction."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

from .postgres_runner import complete_database_io
from .result_artifact import ResultArtifact


@dataclass(frozen=True)
class HistoryArtifact:
    artifact: ResultArtifact
    ordinal: int
    source_message_index: int
    context_epoch: int
    created_at: datetime
    expires_at: datetime
    parent_artifact_id: str | None = None

    def card(self) -> dict:
        """Bounded metadata only: no CSV rows, raw SQL or narrative summary."""
        return {"artifact_id": self.artifact.artifact_id, "ordinal": self.ordinal,
                "query_scope": self.artifact.query_scope,
                "row_count": self.artifact.result_metadata.get("row_count"),
                "ordering": self.artifact.result_metadata.get("ordering", []),
                "population_coverage": self.artifact.result_metadata.get("population_coverage"),
                "created_at": self.created_at.isoformat(), "expires_at": self.expires_at.isoformat(),
                "snapshot_created_at": self.artifact.result_metadata.get("snapshot_created_at", self.created_at.isoformat())}


class ArtifactHistory:
    def __init__(self, settings, workspace, catalog):
        self.settings, self.workspace, self.catalog = settings, workspace, catalog

    def _connect(self):
        return psycopg2.connect(host=self.settings.host, port=self.settings.port,
                               database=self.settings.database, user=self.settings.writer_user)

    async def candidates(self, conversation_id, user, *, metric_ids=(), limit=64,
                         artifact_id=None, include_previous_context=False) -> list[HistoryArtifact]:
        rows = await complete_database_io(self._candidates, conversation_id, user,
            tuple(metric_ids), min(limit, 64), artifact_id, include_previous_context)
        role = "admin" if "admin" in user.group_memberships else "analyst"
        visible = {m.metric_id for m in self.catalog.metrics if role in m.role_visibility}
        items = []
        for row in rows:
            artifact = ResultArtifact.from_mapping(row["descriptor"])
            if artifact is None or not set(artifact.metric_ids) <= visible:
                continue
            # Re-evaluate current Catalog/table permissions rather than trusting
            # the model or an opaque artifact id as an access credential.
            tables = artifact.result_metadata.get("source_tables", ())
            if any(t not in self.catalog.tables_by_id or role not in self.catalog.table(t).role_visibility
                   or (role != "admin" and t not in self.workspace.analyst_tables) for t in tables):
                continue
            dimensions = set(artifact.query_scope.get("dimensions") or ())
            dimensions.update(f["dimension"] for f in artifact.query_scope.get("filters") or ())
            if any(not dimensions <= set(self.catalog.metrics_by_id[m].allowed_dimensions) for m in artifact.metric_ids):
                continue
            items.append(HistoryArtifact(artifact, row["result_ordinal"], row["source_message_index"],
                         row["context_epoch"], row["created_at"], row["expires_at"], row["parent_artifact_id"]))
        return items

    def _candidates(self, conversation_id, user, metric_ids, limit, artifact_id, include_previous):
        ws = self.workspace
        connection = self._connect()
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    """SELECT a.* FROM app.result_artifacts a
                    JOIN app.conversations c ON c.conversation_id=a.conversation_id
                    WHERE a.conversation_id=%s AND a.user_id=%s AND a.workspace_id=%s
                      AND c.user_id=a.user_id AND c.workspace_id=a.workspace_id AND c.status='active'
                      AND c.dataset_version_id=a.dataset_version AND c.metric_version=a.metric_version
                      AND (%s OR a.context_epoch=c.context_epoch)
                      AND a.status='READY' AND a.expires_at > CURRENT_TIMESTAMP
                      AND a.dataset_version=%s AND a.metric_version=%s
                      AND a.catalog_version=%s AND a.policy_version=%s AND a.user_role=%s
                      AND (%s IS NULL OR a.artifact_id=%s)
                      AND a.descriptor->'metric_ids' @> %s::jsonb
                    ORDER BY a.created_at DESC LIMIT %s""",
                    (conversation_id, user.id, ws.workspace_id, include_previous,
                     ws.dataset_version_id, ws.metric_version, self.catalog.catalog_version,
                     self.catalog.policy_version, "admin" if "admin" in user.group_memberships else "analyst",
                     artifact_id, artifact_id, psycopg2.extras.Json(list(metric_ids)), limit),
                )
                return cursor.fetchall()
        finally:
            connection.close()

    @staticmethod
    def commit(cursor, conversation, pending):
        """Called with the conversation row locked and its state_version checked."""
        artifact = ResultArtifact.from_mapping(pending["artifact"])
        manifest = pending["manifest"]
        if (artifact is None or artifact.result_metadata.get("validation_status") != "valid"
                or artifact.csv_sha256 != manifest["csv_sha256"]):
            raise ValueError("only a validated, manifest-bound result can enter history")
        cursor.execute("SELECT message_index FROM app.messages WHERE conversation_id=%s AND role='user' "
                       "AND metadata_json->>'run_id'=%s ORDER BY message_index DESC LIMIT 1",
                       (conversation.id, pending["run_id"]))
        source = cursor.fetchone()
        if source is None:
            raise ValueError("result source message was not committed")
        cursor.execute("SELECT COALESCE(MAX(result_ordinal),0)+1 FROM app.result_artifacts WHERE conversation_id=%s",
                       (conversation.id,))
        ordinal = cursor.fetchone()[0]
        descriptor = artifact.as_dict()
        # Persist the descriptor, not a potentially large copy of result evidence.
        descriptor["summary"] = "历史可信结果：读取时重新计算证据。"
        cursor.execute(
            """INSERT INTO app.result_artifacts (
                artifact_id, conversation_id, user_id, workspace_id, context_epoch, result_ordinal,
                source_message_index, run_id, parent_artifact_id, user_role, dataset_version,
                metric_version, catalog_version, policy_version, descriptor, created_at, expires_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (artifact.artifact_id, conversation.id, conversation.user.id, artifact.workspace_id,
             conversation.metadata.get("context_epoch", 0), ordinal, source[0], pending["run_id"],
             pending.get("parent_artifact_id"),
             "admin" if "admin" in conversation.user.group_memberships else "analyst",
             artifact.dataset_version, artifact.metric_version, pending["catalog_version"], pending["policy_version"],
             psycopg2.extras.Json(descriptor), datetime.fromtimestamp(manifest["created_at"], timezone.utc),
             datetime.fromtimestamp(manifest["expires_at"], timezone.utc)),
        )

    async def expire(self):
        return await complete_database_io(self._expire)

    async def registered_scopes(self, store):
        rows = await complete_database_io(self._registered)
        return {store._scope(user, workspace, artifact) for user, workspace, artifact in rows}

    def _registered(self):
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT user_id,workspace_id,artifact_id FROM app.result_artifacts WHERE status='READY'")
                return cursor.fetchall()
        finally:
            connection.close()

    def _expire(self):
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute("UPDATE app.result_artifacts SET status='EXPIRED' "
                               "WHERE status='READY' AND expires_at <= CURRENT_TIMESTAMP")
                return cursor.rowcount
        finally:
            connection.close()
