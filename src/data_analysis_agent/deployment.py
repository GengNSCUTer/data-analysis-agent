"""Schema/role initialization, run separately from the HTTP app with admin credentials."""
from __future__ import annotations

import argparse
import os

import psycopg2
from psycopg2 import sql
import yaml

from .runtime_paths import RESOURCES, state_directory

SYNTHETIC_VERSION = "synthetic-olist-v1"


def initialize(*, synthetic: bool = False) -> dict:
    reader_password = os.environ["DATA_ANALYSIS_READER_PASSWORD"]
    writer_password = os.environ["DATA_ANALYSIS_WRITER_PASSWORD"]
    connection = psycopg2.connect(os.environ["DATA_ANALYSIS_ADMIN_DSN"])
    try:
        with connection:
            with connection.cursor() as cursor:
                for name in ("analytics.sql", "security.sql"):
                    cursor.execute((RESOURCES / "sql" / name).read_text(encoding="utf-8"))
                for role, password in (("daa_analytics_reader", reader_password), ("daa_app_writer", writer_password)):
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
            for role, password in (("daa_analytics_reader", reader_password), ("daa_app_writer", writer_password)):
                handle.write(":".join(escape(value) for value in (host, port, database, role, password)) + "\n")
        if synthetic:
            catalog = yaml.safe_load((RESOURCES / "catalog/olist_catalog_v3.yaml").read_text(encoding="utf-8"))
            catalog["dataset_version"] = SYNTHETIC_VERSION
            catalog["description"] = "Self-generated synthetic commerce demo, using the Olist-shaped schema; not real Olist transactions."
            (root / "synthetic-catalog.yaml").write_text(yaml.safe_dump(catalog, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return {"status": "initialized", "synthetic": synthetic, "orders": orders}
    finally:
        connection.close()


def main():
    from dotenv import load_dotenv
    load_dotenv(os.getenv("DATA_ANALYSIS_ENV_FILE", ".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true", help="Seed only an empty isolated demo database")
    args = parser.parse_args()
    print(initialize(synthetic=args.synthetic))


if __name__ == "__main__":
    main()
