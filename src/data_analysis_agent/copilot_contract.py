"""Fail-closed host integration contracts for the embedded Copilot (M1).

The browser may describe where the Copilot is mounted and which opaque entity
references the trusted host selected.  It is never authoritative for subject,
role, workspace, dataset versions, or permissions; those values are derived by
the server from the resolved user and workspace registry.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator
from vanna.core.user import User

from .workspace import WorkspaceProfile


_SURFACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_ENTITY_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_CONTRACT_VERSION = "copilot-bootstrap-v1"


class BootstrapContractError(ValueError):
    """Raised when a host bootstrap request cannot be trusted."""


class CopilotBootstrapRequest(BaseModel):
    """The only host-provided fields accepted by the bootstrap endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface_id: str = Field(min_length=1, max_length=64)
    requested_workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
    selected_entity_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=16)

    @field_validator("surface_id")
    @classmethod
    def validate_surface_id(cls, value: str) -> str:
        if not _SURFACE_ID.fullmatch(value):
            raise ValueError("surface_id contains unsupported characters")
        return value

    @field_validator("requested_workspace_id")
    @classmethod
    def validate_requested_workspace(cls, value: str | None) -> str | None:
        if value is not None and not _ENTITY_REF.fullmatch(value):
            raise ValueError("requested_workspace_id contains unsupported characters")
        return value

    @field_validator("selected_entity_refs")
    @classmethod
    def validate_entity_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("selected_entity_refs must not contain duplicates")
        if any(not _ENTITY_REF.fullmatch(value) for value in values):
            raise ValueError("selected_entity_refs must be opaque safe references")
        return values


class AnalysisSurfaceContext(BaseModel):
    """Bounded page context supplied by a trusted host integration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface_id: str
    selected_entity_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=16)


class ConversationScope(BaseModel):
    """Server-owned subject, role and version scope for a conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str
    role: str
    workspace_id: str
    dataset_id: str
    dataset_version: str
    metric_version: str
    catalog_version: str
    policy_version: str


class CopilotBootstrap(BaseModel):
    """Public bootstrap response; no raw page content or arbitrary authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = _CONTRACT_VERSION
    subject_id: str
    role: str
    surface: AnalysisSurfaceContext
    conversation_scope: ConversationScope
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceGrant:
    workspace: WorkspaceProfile
    allowed_subjects: frozenset[str]
    allowed_roles: frozenset[str]
    allow_local_accounts: bool = False


class WorkspaceAccessRegistry:
    """Server-side workspace membership registry for the current host.

    This is deliberately small and explicit for the demo.  Production SSO or
    organization membership can replace the registry without changing the
    bootstrap contract.
    """

    def __init__(self, grants: Mapping[str, WorkspaceGrant]):
        self._grants = dict(grants)

    def resolve(self, user: User, requested_workspace_id: str | None) -> WorkspaceGrant:
        subject_id = str(user.id)
        roles = frozenset(str(role) for role in user.group_memberships)
        candidates = list(self._grants.values())
        if requested_workspace_id is not None:
            candidates = [
                grant
                for grant in candidates
                if grant.workspace.workspace_id == requested_workspace_id
            ]
        for grant in candidates:
            local_account = subject_id.startswith("local:")
            subject_allowed = subject_id in grant.allowed_subjects or (
                grant.allow_local_accounts and local_account
            )
            if subject_allowed and roles & grant.allowed_roles:
                return grant
        raise BootstrapContractError("subject is not a member of the requested workspace")


def build_copilot_bootstrap(
    *,
    user: User,
    request: CopilotBootstrapRequest,
    access_registry: WorkspaceAccessRegistry,
) -> CopilotBootstrap:
    """Build a bootstrap response from server-owned identity and workspace data."""

    grant = access_registry.resolve(user, request.requested_workspace_id)
    role = "admin" if "admin" in user.group_memberships else "analyst"
    workspace = grant.workspace
    surface = AnalysisSurfaceContext(
        surface_id=request.surface_id,
        selected_entity_refs=request.selected_entity_refs,
    )
    scope = ConversationScope(
        subject_id=str(user.id),
        role=role,
        workspace_id=workspace.workspace_id,
        dataset_id=workspace.dataset_id,
        dataset_version=workspace.dataset_version,
        metric_version=workspace.metric_version,
        catalog_version=workspace.catalog_version,
        policy_version=workspace.policy_version,
    )
    return CopilotBootstrap(
        subject_id=str(user.id),
        role=role,
        surface=surface,
        conversation_scope=scope,
        capabilities=("conversation", "query", "trusted_result", "chart"),
    )
