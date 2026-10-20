from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from data_analysis_agent.local_auth import (
    EMBED_AUDIENCE,
    AuthenticationError,
    SignedToken,
    LocalAccount,
    ProductIdentityResolver,
    issue_session,
    hash_password,
    issue_embed_token,
    normalize_origin,
    read_embed_token,
    verify_password,
)
from vanna.core.user import RequestContext


def test_password_hash_does_not_store_the_password() -> None:
    encoded = hash_password("correct-password")
    assert "correct-password" not in encoded
    assert verify_password("correct-password", encoded)
    assert not verify_password("wrong-password", encoded)


def test_embed_token_is_bound_to_origin_and_ignores_a_client_subject() -> None:
    signer = SignedToken("test-secret", max_age_seconds=300)
    token = issue_embed_token(
        signer,
        account_subject="local:analyst",
        username="analyst",
        role="analyst",
        origin="http://127.0.0.1:32014",
        workspace_id="olist-demo",
    )
    payload = read_embed_token(
        signer,
        token,
        parent_origin="http://127.0.0.1:32014",
        allowed_origins=frozenset({"http://127.0.0.1:32014"}),
    )
    assert payload["subject_id"] == "local:analyst"
    assert payload["aud"] == EMBED_AUDIENCE
    with pytest.raises(AuthenticationError):
        read_embed_token(
            signer,
            token,
            parent_origin="http://evil.example",
            allowed_origins=frozenset({"http://127.0.0.1:32014", "http://evil.example"}),
        )
    with pytest.raises(ValueError):
        normalize_origin("http://127.0.0.1:32014/path")


def test_expired_embed_token_is_rejected() -> None:
    signer = SignedToken("test-secret", max_age_seconds=300)
    token = signer.dumps(
        {
            "aud": EMBED_AUDIENCE,
            "kind": "embed",
            "subject_id": "local:analyst",
            "username": "analyst",
            "role": "analyst",
            "origin": "http://127.0.0.1:32014",
            "workspace_id": "olist-demo",
            "nonce": "n",
        },
        now=int(time.time()) - 301,
    )
    with pytest.raises(AuthenticationError):
        read_embed_token(
            signer,
            token,
            parent_origin="http://127.0.0.1:32014",
            allowed_origins=frozenset({"http://127.0.0.1:32014"}),
        )


def test_login_and_embed_route_keep_subject_on_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_USERNAME", "routeanalyst")
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD", "route-password")
    monkeypatch.setenv("DATA_ANALYSIS_EMBED_ORIGINS", "http://127.0.0.1:32014")
    from data_analysis_agent.server import create_app

    with TestClient(create_app()) as client:
        denied = client.post(
            "/api/auth/login",
            json={"username": "routeanalyst", "password": "wrong-password", "user_id": "demo-admin"},
        )
        assert denied.status_code == 422
        failed = client.post(
            "/api/auth/login",
            json={"username": "routeanalyst", "password": "wrong-password"},
        )
        assert failed.status_code == 401
        logged_in = client.post(
            "/api/auth/login",
            json={"username": "routeanalyst", "password": "route-password"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["auth_mode"] == "local_session"
        issued = client.post(
            "/api/copilot/embed-tokens",
            json={"origin": "http://127.0.0.1:32014", "user_id": "demo-admin"},
        )
        assert issued.status_code == 422
        issued = client.post(
            "/api/copilot/embed-tokens",
            json={"origin": "http://evil.example"},
        )
        assert issued.status_code == 403
        issued = client.post(
            "/api/copilot/embed-tokens",
            json={"origin": "http://127.0.0.1:32014"},
        )
        assert issued.status_code == 200
        token = issued.json()["token"]
        client.cookies.clear()
        stolen = client.get(
            "/api/auth/session",
            headers={"Authorization": f"Bearer {token}", "X-Embed-Parent-Origin": "http://evil.example"},
        )
        assert stolen.json()["authenticated"] is False
        accepted = client.get(
            "/api/auth/session",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Embed-Parent-Origin": "http://127.0.0.1:32014",
            },
        )
        assert accepted.json()["authenticated"] is True
        assert accepted.json()["username"] == "routeanalyst"
        client.cookies.update(logged_in.cookies)
        denied_switch = client.post("/api/project/demo-session", json={"role": "admin"})
        assert denied_switch.status_code == 404
        current = client.get("/api/auth/session")
        assert current.json()["auth_mode"] == "local_session"
        assert current.json()["role"] == "analyst"
        assert client.post("/api/auth/logout").status_code == 200
        # Logout revokes captured cookies and tokens, not just this browser's cookie.
        client.cookies.update(logged_in.cookies)
        assert client.get('/api/auth/session').json()['authenticated'] is False
        client.cookies.clear()
        assert client.get('/api/auth/session', headers={
            'Authorization': f'Bearer {token}', 'X-Embed-Parent-Origin': 'http://127.0.0.1:32014',
        }).json()['authenticated'] is False
        client.cookies.set("daa_demo_session", "forged-demo-cookie")
        assert client.get("/api/auth/session").json()["authenticated"] is False
        assert client.get("/api/project/conversations").status_code == 401


def test_login_rate_and_product_exposes_only_authenticated_sse(monkeypatch):
    monkeypatch.setenv('DATA_ANALYSIS_LOGIN_PER_MINUTE', '2')
    from data_analysis_agent.server import create_app
    with TestClient(create_app()) as client:
        assert client.post('/api/auth/login', json={'username': 'missing', 'password': 'wrong'}).status_code == 401
        assert client.post('/api/auth/login', json={'username': 'missing', 'password': 'wrong'}).status_code == 401
        limited = client.post('/api/auth/login', json={'username': 'missing', 'password': 'wrong'})
        assert limited.status_code == 429 and int(limited.headers['Retry-After']) > 0
        assert client.post('/api/vanna/v2/chat_sse', json={'message': 'test'}).status_code == 401
        assert client.post('/api/vanna/v2/cancel', json={'conversation_id': 'a', 'request_id': 'b'}).status_code == 401
        assert not any(route.path.endswith(('chat_poll', 'chat_websocket')) for route in client.app.routes)


@pytest.mark.asyncio
async def test_session_expiry_account_revocation_and_embed_workspace():
    account = LocalAccount('local:alice', 'alice', 'analyst', 'unused', session_version=2)
    session = SignedToken('test', max_age_seconds=8 * 3600)
    embed = SignedToken('test', max_age_seconds=300)
    resolver = ProductIdentityResolver(session_signer=session, embed_signer=embed, allowed_origins=frozenset({'https://shop.example'}),
                                       account_store=SimpleNamespace(find_by_username=lambda _: account), workspace_id='olist-demo')
    token = issue_session(session, account)
    assert (await resolver.resolve_user(RequestContext(cookies={'daa_session': token}))).id == account.subject_id
    expired = session.dumps({'kind': 'local-session', 'subject_id': account.subject_id, 'username': account.username,
                             'role': account.role, 'session_version': 2}, now=int(time.time()) - 8 * 3600 - 1)
    assert (await resolver.resolve_user(RequestContext(cookies={'daa_session': expired}))).id == 'anonymous'
    resolver.account_store.find_by_username = lambda _: LocalAccount('local:alice', 'alice', 'analyst', 'unused', session_version=3)
    assert (await resolver.resolve_user(RequestContext(cookies={'daa_session': token}))).id == 'anonymous'
    resolver.account_store.find_by_username = lambda _: account
    token = issue_embed_token(embed, account_subject=account.subject_id, username=account.username, role=account.role,
                              origin='https://shop.example', workspace_id='thelook-demo', session_version=2)
    headers = {'Authorization': 'Bearer ' + token, 'X-Embed-Parent-Origin': 'https://shop.example'}
    assert (await resolver.resolve_user(RequestContext(headers=headers))).id == 'anonymous'


def test_mutating_conversation_requires_current_turn_to_stop(monkeypatch):
    monkeypatch.setenv('DATA_ANALYSIS_BOOTSTRAP_USERNAME', 'mutationtester')
    monkeypatch.setenv('DATA_ANALYSIS_BOOTSTRAP_PASSWORD', 'mutation-test-password')
    from data_analysis_agent.server import create_app
    with TestClient(create_app()) as client:
        assert client.post('/api/auth/login', json={'username': 'mutationtester', 'password': 'mutation-test-password'}).status_code == 200
        control = client.app.state.runtime_control
        key = ('local:mutationtester', 'olist-demo', 'mutation-test')
        control.inflight[key] = object()
        try:
            for path in ('analysis-context', 'replace-last-question'):
                assert client.post('/api/project/conversations/mutation-test/' + path).status_code == 409
            assert client.delete('/api/project/conversations/mutation-test').status_code == 409
        finally:
            control.inflight.clear()
