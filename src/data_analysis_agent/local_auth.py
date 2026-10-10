"""Local account sessions and short-lived cross-origin embed tokens.

The browser never supplies subject or role. A password session or a signed
embed token is the only way to become a local account. Demo role cookies are
not accepted by the product resolver.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from typing import Mapping
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras

from vanna.core.user import RequestContext, User, UserResolver

from .postgres_runner import PostgresConnectionSettings


LOCAL_SESSION_COOKIE = "daa_session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
EMBED_TOKEN_TTL_SECONDS = 5 * 60
EMBED_AUDIENCE = "copilot-embed"
_USERNAME = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
_PBKDF2_ITERATIONS = 200_000
BOOTSTRAP_PASSWORD_PATH = Path(
    "/disk2/gengnan/data-analysis-agent-data/local-auth/bootstrap-password.txt"
)


class AuthenticationError(ValueError):
    """Credentials or a token cannot be accepted."""


@dataclass(frozen=True)
class LocalAccount:
    subject_id: str
    username: str
    role: str
    password_hash: str


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


_UNKNOWN_USER_HASH = hash_password("timing-pad-not-a-user")


def password_matches(account: LocalAccount | None, password: str) -> bool:
    """Check a password without skipping the hash when the account is missing."""

    encoded = account.password_hash if account is not None else _UNKNOWN_USER_HASH
    matched = verify_password(password, encoded)
    return account is not None and matched


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def normalize_origin(origin: str) -> str:
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("origin must be an http(s) origin")
    if parts.path not in {"", "/"} or parts.query or parts.fragment or parts.username:
        raise ValueError("origin must not include a path, query, or userinfo")
    return f"{parts.scheme}://{parts.netloc}"


class SignedToken:
    def __init__(self, secret: str, *, max_age_seconds: int):
        if not secret:
            raise ValueError("token secret must not be empty")
        self.secret = secret.encode("utf-8")
        self.max_age_seconds = max_age_seconds

    def dumps(self, payload: Mapping[str, object], now: int | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        body = dict(payload)
        body["issued_at"] = issued_at
        encoded = base64.urlsafe_b64encode(
            json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).rstrip(b"=")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).digest()
        return (
            encoded.decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )

    def loads(self, token: str | None, now: int | None = None) -> dict | None:
        if not token or token.count(".") != 1:
            return None
        encoded_text, signature_text = token.split(".", 1)
        try:
            encoded = encoded_text.encode("ascii")
            expected = hmac.new(self.secret, encoded, hashlib.sha256).digest()
            actual = base64.urlsafe_b64decode(_pad(signature_text))
            if not hmac.compare_digest(actual, expected):
                return None
            payload = json.loads(base64.urlsafe_b64decode(_pad(encoded_text)))
            issued_at = int(payload["issued_at"])
        except (binascii.Error, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return None
        current_time = int(time.time() if now is None else now)
        if issued_at > current_time + 30 or current_time - issued_at > self.max_age_seconds:
            return None
        return payload


def _pad(value: str) -> str:
    return value + "=" * (-len(value) % 4)


class AccountStore:
    def __init__(self, settings: PostgresConnectionSettings | None = None):
        self.settings = settings or PostgresConnectionSettings.from_environment()

    def _connect(self):
        return psycopg2.connect(
            host=self.settings.host,
            port=self.settings.port,
            database=self.settings.database,
            user=self.settings.writer_user,
        )

    def find_by_username(self, username: str) -> LocalAccount | None:
        connection = self._connect()
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT subject_id, username, role, password_hash
                    FROM app.accounts WHERE username = %s
                    """,
                    (username,),
                )
                row = cursor.fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return LocalAccount(
            subject_id=row["subject_id"],
            username=row["username"],
            role=row["role"],
            password_hash=row["password_hash"],
        )

    def create_account(self, username: str, password: str, role: str = "analyst") -> LocalAccount:
        if not _USERNAME.fullmatch(username):
            raise ValueError("username is not allowed")
        if role not in {"analyst", "admin"}:
            raise ValueError("role is not allowed")
        account = LocalAccount(
            subject_id=f"local:{username}",
            username=username,
            role=role,
            password_hash=hash_password(password),
        )
        connection = self._connect()
        try:
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO app.accounts (subject_id, username, password_hash, role)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (username) DO NOTHING
                        """,
                        (account.subject_id, account.username, account.password_hash, account.role),
                    )
        finally:
            connection.close()
        stored = self.find_by_username(username)
        if stored is None:
            raise AuthenticationError("account was not stored")
        return stored


def ensure_bootstrap_account(store: AccountStore | None = None) -> str:
    """Create the first local analyst from the environment or a repo-external file."""

    store = store or AccountStore()
    username = os.getenv("DATA_ANALYSIS_BOOTSTRAP_USERNAME", "analyst")
    password = os.getenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD")
    if not password:
        if BOOTSTRAP_PASSWORD_PATH.is_file():
            password = BOOTSTRAP_PASSWORD_PATH.read_text(encoding="utf-8").strip()
        else:
            password = secrets.token_urlsafe(12)
            BOOTSTRAP_PASSWORD_PATH.parent.mkdir(parents=True, exist_ok=True)
            BOOTSTRAP_PASSWORD_PATH.write_text(password + "\n", encoding="utf-8")
            BOOTSTRAP_PASSWORD_PATH.chmod(0o600)
    if store.find_by_username(username) is None:
        store.create_account(username, password, role=os.getenv("DATA_ANALYSIS_BOOTSTRAP_ROLE", "analyst"))
    return username


def issue_session(signer: SignedToken, account: LocalAccount) -> str:
    return signer.dumps(
        {"kind": "local-session", "subject_id": account.subject_id, "username": account.username, "role": account.role}
    )


def issue_embed_token(
    signer: SignedToken,
    *,
    account_subject: str,
    username: str,
    role: str,
    origin: str,
    workspace_id: str,
) -> str:
    normalized = normalize_origin(origin)
    return signer.dumps(
        {
            "aud": EMBED_AUDIENCE,
            "kind": "embed",
            "subject_id": account_subject,
            "username": username,
            "role": role,
            "origin": normalized,
            "workspace_id": workspace_id,
            "nonce": secrets.token_urlsafe(16),
        }
    )


def read_embed_token(signer: SignedToken, token: str, *, parent_origin: str, allowed_origins: frozenset[str]) -> dict:
    payload = signer.loads(token)
    if not payload or payload.get("aud") != EMBED_AUDIENCE or payload.get("kind") != "embed":
        raise AuthenticationError("embed token is invalid")
    origin = str(payload.get("origin") or "")
    if origin not in allowed_origins or normalize_origin(parent_origin) != origin:
        raise AuthenticationError("embed token origin is not allowed")
    subject_id = str(payload.get("subject_id") or "")
    role = str(payload.get("role") or "")
    if not subject_id.startswith("local:") or role not in {"analyst", "admin"}:
        raise AuthenticationError("embed token subject is invalid")
    return payload


class ProductIdentityResolver(UserResolver):
    """Resolve an embed token, then a password session. Demo cookies are ignored."""

    def __init__(
        self,
        *,
        session_signer: SignedToken,
        embed_signer: SignedToken,
        allowed_origins: frozenset[str],
    ):
        self.session_signer = session_signer
        self.embed_signer = embed_signer
        self.allowed_origins = allowed_origins

    async def resolve_user(self, request_context: RequestContext) -> User:
        embed_user = self._embed_user(request_context)
        if embed_user is not None:
            return embed_user
        session = self.session_signer.loads(request_context.get_cookie(LOCAL_SESSION_COOKIE))
        if session and session.get("kind") == "local-session":
            subject_id = str(session.get("subject_id") or "")
            role = str(session.get("role") or "")
            if subject_id.startswith("local:") and role in {"analyst", "admin"}:
                return User(
                    id=subject_id,
                    username=str(session.get("username") or subject_id),
                    group_memberships=[role],
                    metadata={"auth_mode": "local_session"},
                )
        return User(
            id="anonymous",
            username="anonymous",
            group_memberships=["anonymous"],
            metadata={"auth_mode": "anonymous"},
        )

    def _embed_user(self, request_context: RequestContext) -> User | None:
        header = _header(request_context.headers, "authorization")
        parent_origin = _header(request_context.headers, "x-embed-parent-origin")
        if not header or not header.lower().startswith("bearer ") or not parent_origin:
            return None
        try:
            payload = read_embed_token(
                self.embed_signer,
                header.split(" ", 1)[1].strip(),
                parent_origin=parent_origin,
                allowed_origins=self.allowed_origins,
            )
        except (AuthenticationError, ValueError):
            return None
        return User(
            id=str(payload["subject_id"]),
            username=str(payload.get("username") or payload["subject_id"]),
            group_memberships=[str(payload["role"])],
            metadata={"auth_mode": "embed_token", "embed_origin": payload["origin"]},
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def allowed_embed_origins() -> frozenset[str]:
    raw = os.getenv("DATA_ANALYSIS_EMBED_ORIGINS", "")
    origins = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        origins.append(normalize_origin(item))
    return frozenset(origins)
