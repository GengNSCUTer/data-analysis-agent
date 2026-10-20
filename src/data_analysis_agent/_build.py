"""Assemble product assets once for editable development and wheel builds."""
from pathlib import Path
import shutil

from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parents[2]


def copy_assets(destination):
    sources = {
        "embeddable_copilot_shell.html": ROOT / "examples/embeddable_copilot_shell.html",
        "catalog/olist_catalog.yaml": ROOT / "data/catalog/olist_catalog.yaml",
        "catalog/olist_catalog_v3.yaml": ROOT / "data/catalog/olist_catalog_v3.yaml",
        "catalog/thelook_catalog_v2.yaml": ROOT / "data/catalog/thelook_catalog_v2.yaml",
        "catalog/thelook_runtime_shape_bounds_v1.json": ROOT / "data/catalog/thelook_runtime_shape_bounds_v1.json",
        "workspaces/olist.yaml": ROOT / "data/workspaces/olist.yaml",
        "workspaces/thelook.yaml": ROOT / "data/workspaces/thelook.yaml",
        "sql/app.sql": ROOT / "infra/postgres/app.sql",
        "sql/analytics.sql": ROOT / "infra/postgres/analytics.sql",
        "sql/security.sql": ROOT / "infra/postgres/security.sql",
        "sql/synthetic.sql": ROOT / "infra/postgres/synthetic.sql",
        "static/vanna-components.js": ROOT / "frontends/webcomponent/dist/vanna-components.js",
    }
    for relative, source in sources.items():
        if not source.is_file():
            raise RuntimeError(f"Missing product asset {source.name}; run npm install --package-lock=false && npm run build in frontends/webcomponent first")
        target = Path(destination) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


class ProductBuild(build_py):
    def run(self):
        super().run()
        copy_assets(Path(self.build_lib) / "data_analysis_agent/resources")


if __name__ == "__main__":
    copy_assets(ROOT / "src/data_analysis_agent/resources")
