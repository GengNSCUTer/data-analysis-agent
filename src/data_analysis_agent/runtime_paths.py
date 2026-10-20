"""Installation resources and operator-owned persistent runtime files."""
from pathlib import Path
import os
import secrets

RESOURCES = Path(__file__).resolve().parent / "resources"


def state_directory() -> Path:
    return Path(os.getenv("DATA_ANALYSIS_STATE_DIR", str(Path.home() / ".local/share/data-analysis-agent")))


def session_secret() -> str:
    configured = os.getenv("DATA_ANALYSIS_DEMO_SESSION_SECRET")
    if configured:
        return configured
    path = state_directory() / "session-secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            path.chmod(0o600)
            secret = secrets.token_urlsafe(32)
            handle.write(secret)
            return secret
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
