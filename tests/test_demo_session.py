"""Regression tests for the intentionally limited signed demo session."""

from __future__ import annotations

import pytest

from data_analysis_agent.demo_session import (
    SESSION_COOKIE_NAME,
    DemoRoleResolver,
    DemoSessionSigner,
)
from vanna.core.user import RequestContext


def test_signed_session_round_trip_uses_the_fixed_identity_mapping() -> None:
    signer = DemoSessionSigner("test-secret", max_age_seconds=60)

    session = signer.loads(signer.dumps("admin", now=1_000), now=1_030)

    assert session is not None
    assert session.role == "admin"
    assert session.user_id == "demo-admin"


@pytest.mark.parametrize(
    "token",
    ["not-a-session", "payload.signature", "eyJyb2xlIjoiYWRtaW4ifQ.invalid", "%%%%.%%%%"],
)
def test_invalid_or_tampered_session_is_rejected(token: str) -> None:
    assert DemoSessionSigner("test-secret").loads(token, now=1_000) is None


def test_expired_session_is_rejected() -> None:
    signer = DemoSessionSigner("test-secret", max_age_seconds=60)

    assert signer.loads(signer.dumps("analyst", now=1_000), now=1_061) is None


@pytest.mark.asyncio
async def test_headers_cannot_elevate_an_unsigned_request_to_admin() -> None:
    resolver = DemoRoleResolver(DemoSessionSigner("test-secret"))
    user = await resolver.resolve_user(
        RequestContext(headers={"X-Demo-Role": "admin", "X-Demo-User": "demo-admin"})
    )

    assert user.id == "demo-analyst"
    assert user.group_memberships == ["analyst"]


@pytest.mark.asyncio
async def test_signed_cookie_controls_the_resolved_role() -> None:
    signer = DemoSessionSigner("test-secret")
    resolver = DemoRoleResolver(signer)
    user = await resolver.resolve_user(
        RequestContext(cookies={SESSION_COOKIE_NAME: signer.dumps("admin")})
    )

    assert user.id == "demo-admin"
    assert user.group_memberships == ["admin"]
