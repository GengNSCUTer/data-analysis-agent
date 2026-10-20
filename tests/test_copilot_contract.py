from __future__ import annotations

import pytest
import os
from pydantic import ValidationError
from vanna.core.user import User

from data_analysis_agent.copilot_contract import (
    BootstrapContractError,
    CopilotBootstrapRequest,
    WorkspaceAccessRegistry,
    WorkspaceGrant,
    build_copilot_bootstrap,
)
from data_analysis_agent.metric_context import OLIST_WORKSPACE


def _registry() -> WorkspaceAccessRegistry:
    return WorkspaceAccessRegistry(
        {
            OLIST_WORKSPACE.workspace_id: WorkspaceGrant(
                workspace=OLIST_WORKSPACE,
                allowed_subjects=frozenset({"demo-analyst", "demo-admin"}),
                allowed_roles=frozenset({"analyst", "admin"}),
            )
        }
    )


def test_valid_bootstrap_is_server_scoped() -> None:
    response = build_copilot_bootstrap(
        user=User(id="demo-analyst", username="demo-analyst", group_memberships=["analyst"]),
        request=CopilotBootstrapRequest(
            surface_id="orders-page",
            requested_workspace_id=OLIST_WORKSPACE.workspace_id,
            selected_entity_refs=("order:opaque-1",),
        ),
        access_registry=_registry(),
    )

    assert response.subject_id == "demo-analyst"
    assert response.role == "analyst"
    assert response.conversation_scope.workspace_id == OLIST_WORKSPACE.workspace_id
    assert response.conversation_scope.dataset_version == OLIST_WORKSPACE.dataset_version
    assert response.surface.selected_entity_refs == ("order:opaque-1",)


def test_client_cannot_submit_identity_role_or_raw_context() -> None:
    with pytest.raises(ValidationError):
        CopilotBootstrapRequest.model_validate(
            {
                "surface_id": "orders-page",
                "user_id": "demo-admin",
                "role": "admin",
                "workspace_id": OLIST_WORKSPACE.workspace_id,
                "sql": "SELECT 1",
                "dom": "<table>secret</table>",
            }
        )


@pytest.mark.parametrize(
    "surface_id",
    ["", "orders page", "javascript:alert(1)", "a" * 65],
)
def test_invalid_surface_is_rejected(surface_id: str) -> None:
    with pytest.raises(ValidationError):
        CopilotBootstrapRequest(surface_id=surface_id)


def test_workspace_and_role_are_not_client_selectable() -> None:
    request = CopilotBootstrapRequest(
        surface_id="orders-page", requested_workspace_id="thelook-analytics"
    )
    with pytest.raises(BootstrapContractError, match="not a member"):
        build_copilot_bootstrap(
            user=User(id="demo-analyst", username="demo-analyst", group_memberships=["analyst"]),
            request=request,
            access_registry=_registry(),
        )


def test_unknown_subject_fails_closed() -> None:
    with pytest.raises(BootstrapContractError, match="not a member"):
        build_copilot_bootstrap(
            user=User(id="attacker", username="attacker", group_memberships=["admin"]),
            request=CopilotBootstrapRequest(surface_id="orders-page"),
            access_registry=_registry(),
        )


def test_local_account_membership_requires_an_explicit_grant() -> None:
    user = User(id="local:analyst", username="analyst", group_memberships=["analyst"])
    request = CopilotBootstrapRequest(surface_id="orders-page")
    with pytest.raises(BootstrapContractError):
        build_copilot_bootstrap(user=user, request=request, access_registry=_registry())
    granted = WorkspaceAccessRegistry(
        {
            OLIST_WORKSPACE.workspace_id: WorkspaceGrant(
                workspace=OLIST_WORKSPACE,
                allowed_subjects=frozenset(),
                allowed_roles=frozenset({"analyst"}),
                allow_local_accounts=True,
            )
        }
    )
    response = build_copilot_bootstrap(user=user, request=request, access_registry=granted)
    assert response.subject_id == "local:analyst"


@pytest.mark.postgres
@pytest.mark.skipif(os.getenv("RUN_PROJECT_DB") != "1", reason="needs project PostgreSQL")
def test_demo_bootstrap_route_preserves_server_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from data_analysis_agent.server import create_app

    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_USERNAME", "bootstrapuser")
    monkeypatch.setenv("DATA_ANALYSIS_BOOTSTRAP_PASSWORD", "bootstrap-password")
    with TestClient(create_app()) as client:
        anonymous = client.post(
            "/api/copilot/bootstrap",
            json={"surface_id": "orders-page"},
        )
        assert anonymous.status_code == 401
        assert client.get("/api/project/conversations/unknown/execution-memory").status_code == 401
        assert client.post("/api/project/demo-session", json={"role": "analyst"}).status_code == 401
        logged_in = client.post(
            "/api/auth/login",
            json={"username": "bootstrapuser", "password": "bootstrap-password"},
        )
        assert logged_in.status_code == 200
        assert client.get("/api/project/conversations/bad.id/execution-memory").status_code == 400
        assert client.get("/api/project/conversations/unknown/execution-memory").status_code == 404
        allowed = client.post(
            "/api/copilot/bootstrap",
            json={
                "surface_id": "orders-page",
                "requested_workspace_id": OLIST_WORKSPACE.workspace_id,
                "selected_entity_refs": ["order:opaque-1"],
            },
        )
        assert allowed.status_code == 200
        assert allowed.json()["subject_id"] == "local:bootstrapuser"
        assert allowed.json()["conversation_scope"]["workspace_id"] == OLIST_WORKSPACE.workspace_id

        injected = client.post(
            "/api/copilot/bootstrap",
            json={
                "surface_id": "orders-page",
                "user_id": "demo-admin",
                "role": "admin",
            },
        )
        assert injected.status_code == 422

        denied = client.post(
            "/api/copilot/bootstrap",
            json={"surface_id": "orders-page", "requested_workspace_id": "thelook-analytics"},
        )
        assert denied.status_code == 403
