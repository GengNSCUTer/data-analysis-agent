from data_analysis_agent.runtime_paths import session_secret
from data_analysis_agent.local_auth import SignedToken


def test_persisted_secret_accepts_cookie_after_process_reconstruction(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ANALYSIS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("DATA_ANALYSIS_DEMO_SESSION_SECRET", raising=False)
    old = SignedToken(session_secret(), max_age_seconds=60)
    cookie = old.dumps({"subject_id": "local:analyst"})
    new = SignedToken(session_secret(), max_age_seconds=60)
    assert new.loads(cookie)["subject_id"] == "local:analyst"
    assert (tmp_path / "session-secret").stat().st_mode & 0o777 == 0o600


def test_operator_secret_does_not_write_runtime_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ANALYSIS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ANALYSIS_DEMO_SESSION_SECRET", "operator-provided-secret")
    assert session_secret() == "operator-provided-secret"
    assert not (tmp_path / "session-secret").exists()
