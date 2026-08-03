"""Signed, explicitly demo-only role sessions for the trusted Olist host page."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from typing import Final, Literal

from vanna.core.user import RequestContext, User, UserResolver


DemoRole = Literal["analyst", "admin"]
SESSION_COOKIE_NAME: Final = "daa_demo_session"
SESSION_MAX_AGE_SECONDS: Final = 8 * 60 * 60
DEMO_IDENTITIES: Final[dict[DemoRole, str]] = {
    "analyst": "demo-analyst",
    "admin": "demo-admin",
}


@dataclass(frozen=True)
class DemoSession:
    role: DemoRole
    user_id: str


class DemoSessionSigner:
    """Serialize a fixed demo role with an HMAC integrity check and expiry."""

    def __init__(self, secret: str, max_age_seconds: int = SESSION_MAX_AGE_SECONDS):
        if not secret:
            raise ValueError("demo session secret must not be empty")
        self.secret = secret.encode("utf-8")
        self.max_age_seconds = max_age_seconds

    def dumps(self, role: DemoRole, now: int | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = json.dumps(
            {"role": role, "issued_at": issued_at}, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).digest()
        return f"{encoded.decode('ascii')}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"

    def loads(self, token: str | None, now: int | None = None) -> DemoSession | None:
        if not token or token.count(".") != 1:
            return None
        encoded_text, signature_text = token.split(".", 1)
        try:
            encoded = encoded_text.encode("ascii")
            expected = hmac.new(self.secret, encoded, hashlib.sha256).digest()
            actual = base64.urlsafe_b64decode(self._pad(signature_text))
            if not hmac.compare_digest(actual, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(self._pad(encoded_text)))
            role = payload["role"]
            issued_at = int(payload["issued_at"])
        except (binascii.Error, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None
        if role not in DEMO_IDENTITIES:
            return None
        current_time = int(time.time() if now is None else now)
        if issued_at > current_time or current_time - issued_at > self.max_age_seconds:
            return None
        return DemoSession(role=role, user_id=DEMO_IDENTITIES[role])

    @staticmethod
    def _pad(value: str) -> str:
        return value + "=" * (-len(value) % 4)


class DemoRoleResolver(UserResolver):
    """Resolve only signed demo sessions; request headers are not authority."""

    def __init__(self, signer: DemoSessionSigner):
        self.signer = signer

    async def resolve_user(self, request_context: RequestContext) -> User:
        session = self.signer.loads(request_context.get_cookie(SESSION_COOKIE_NAME))
        if session is None:
            session = DemoSession(role="analyst", user_id=DEMO_IDENTITIES["analyst"])
        return User(
            id=session.user_id,
            username=session.user_id,
            group_memberships=[session.role],
        )
