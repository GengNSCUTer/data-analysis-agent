"""Schema/role initialization, run separately from the HTTP app with admin credentials."""
from __future__ import annotations

import argparse
import os

import psycopg2
from psycopg2 import sql
import yaml

from .runtime_paths import RESOURCES, state_directory
from .runtime_workspace import load_runtime_workspace

SYNTHETIC_VERSION = "synthetic-olist-v1"


def initialize(*, synthetic: bool = False, metadata_only: bool = False) -> dict:
    if synthetic and metadata_only:
        raise ValueError("Use synthetic initialization or existing-business metadata initialization, not both")
    workspace = load_runtime_workspace().profile
    if not metadata_only and workspace.dataset_id != "olist-brazilian-ecommerce":
        raise ValueError("Bundled business initialization is Olist-shaped; use --metadata-only for an existing business database")
    reader_password = os.environ["DATA_ANALYSIS_READER_PASSWORD"]
    writer_password = os.environ["DATA_ANALYSIS_WRITER_PASSWORD"]
    connection = psycopg2.connect(os.environ["DATA_ANALYSIS_ADMIN_DSN"])
    try:
        with connection:
            with connection.cursor() as cursor:
                for name in (("app.sql",) if metadata_only else ("analytics.sql", "app.sql", "security.sql")):
                    cursor.execute((RESOURCES / "sql" / name).read_text(encoding="utf-8"))
                if metadata_only:
                    provision_metadata_writer(cursor, workspace.writer_role, workspace.reader_role)
                for role, password in ((workspace.reader_role, reader_password), (workspace.writer_role, writer_password)):
                    if metadata_only and role == workspace.reader_role:
                        continue  # Existing business reader credentials/permissions are not rotated.
                    cursor.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
                if synthetic:
                    cursor.execute("SELECT dataset_version_id FROM analytics.dataset_versions")
                    versions = {row[0] for row in cursor.fetchall()}
                    if versions and versions != {SYNTHETIC_VERSION}:
                        raise ValueError("Synthetic initialization requires an empty database or an existing synthetic-olist-v1 dataset")
                    cursor.execute("SELECT count(*) FROM analytics.fact_orders")
                    count = cursor.fetchone()[0]
                    if not versions and count:
                        raise ValueError("Refusing to seed an existing business database")
                    if not versions:
                        cursor.execute((RESOURCES / "sql/synthetic.sql").read_text(encoding="utf-8"))
                orders = None
                if not metadata_only:
                    cursor.execute("SELECT count(*) FROM analytics.fact_orders")
                    orders = cursor.fetchone()[0]
        root = state_directory()
        root.mkdir(parents=True, exist_ok=True)
        params = connection.get_dsn_parameters()
        host = os.getenv("DATA_ANALYSIS_POSTGRES_HOST", params["host"])
        port = os.getenv("DATA_ANALYSIS_POSTGRES_PORT", params["port"])
        database = params["dbname"]

        def escape(value):
            return str(value).replace("\\", "\\\\").replace(":", "\\:")

        path = root / "pgpass"
        with open(path, "w", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
            path.chmod(0o600)
            for role, password in ((workspace.reader_role, reader_password), (workspace.writer_role, writer_password)):
                handle.write(":".join(escape(value) for value in (host, port, database, role, password)) + "\n")
        if synthetic:
            catalog = yaml.safe_load((RESOURCES / "catalog/olist_catalog_v3.yaml").read_text(encoding="utf-8"))
            catalog["dataset_version"] = SYNTHETIC_VERSION
            catalog["description"] = "Self-generated synthetic commerce demo, using the Olist-shaped schema; not real Olist transactions."
            (root / "synthetic-catalog.yaml").write_text(yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return {"status": "initialized", "synthetic": synthetic, "metadata_only": metadata_only,
                "workspace_id": workspace.workspace_id, "orders": orders}
    finally:
        connection.close()


def provision_metadata_writer(cursor, writer_role: str, reader_role: str) -> None:
    """Grant only app metadata writes; never grant business-table writes."""
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (writer_role,))
    writer = sql.Identifier(writer_role)
    reader = sql.Identifier(reader_role)
    if cursor.fetchone() is None:
        cursor.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT").format(writer))
    cursor.execute(sql.SQL("ALTER ROLE {} SET search_path = app, pg_catalog").format(writer))
    cursor.execute("REVOKE ALL ON SCHEMA app FROM PUBLIC")
    cursor.execute("REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC")
    cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA app FROM {}").format(reader))
    cursor.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA app FROM {}").format(reader))
    cursor.execute("SELECT current_database()")
    database = sql.Identifier(cursor.fetchone()[0])
    cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, writer))
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA app TO {}").format(writer))
    cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON app.conversations, app.messages, app.agent_runs, app.user_preferences, app.saved_analyses, app.accounts TO {}").format(writer))
    cursor.execute(sql.SQL("GRANT SELECT, INSERT ON app.query_audits, app.export_audits TO {}").format(writer))
    cursor.execute(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app TO {}").format(writer))


def main():
    from dotenv import load_dotenv
    load_dotenv(os.getenv("DATA_ANALYSIS_ENV_FILE", ".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true", help="Seed only an empty isolated demo database")
    parser.add_argument("--metadata-only", action="store_true", help="Initialize app storage for an existing business workspace; preserve business tables and reader grants")
    args = parser.parse_args()
    print(initialize(synthetic=args.synthetic, metadata_only=args.metadata_only))


if __name__ == "__main__":
    main()
