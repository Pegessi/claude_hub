from datetime import datetime

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from claude_hub.models import AgentType, ExecutionTarget, TerminalTab


@pytest.mark.asyncio
async def test_list_tabs_empty(client: AsyncClient) -> None:
    """Test that listing tabs returns an empty list when no tabs exist."""
    response = await client.get("/api/tabs")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_create_tab_rejects_retired_agent_session_kind(client: AsyncClient) -> None:
    response = await client.post(
        "/api/tabs",
        json={
            "name": "Retired Agent Surface",
            "agent_type": "claude",
            "session_kind": "agent",
        },
    )

    assert response.status_code == 422
    assert "session_kind" in response.text


@pytest.mark.asyncio
async def test_update_tab_order_route_not_shadowed(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    """Test that the static order route is not handled as a tab ID update."""
    captured_order: list[str] = []

    def fake_set_tab_order(tab_ids: list[str]) -> None:
        captured_order.extend(tab_ids)

    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.set_tab_order", fake_set_tab_order)

    response = await client.put("/api/tabs/order", json={"tab_ids": []})

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert captured_order == []


@pytest.mark.asyncio
async def test_duplicate_tab_route_preserves_solo_mode(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    captured_tab_ids: list[str] = []

    async def fake_duplicate_tab(tab_id: str) -> TerminalTab:
        captured_tab_ids.append(tab_id)
        return TerminalTab(
            id="copy-id",
            name="Codex (copy)",
            shell="codex",
            cwd="/tmp",
            solo_mode=True,
            agent_type=AgentType.CODEX,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12345,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.duplicate_tab",
        fake_duplicate_tab,
    )

    response = await client.post("/api/tabs/source-id/duplicate")

    assert response.status_code == 201
    assert captured_tab_ids == ["source-id"]
    data = response.json()
    assert data["name"] == "Codex (copy)"
    assert data["solo_mode"] is True
    assert data["agent_type"] == "codex"


@pytest.mark.asyncio
async def test_duplicate_tab_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_duplicate_tab(tab_id: str) -> None:
        return None

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.duplicate_tab",
        fake_duplicate_tab,
    )

    response = await client.post("/api/tabs/missing-id/duplicate")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_switch_env_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_switch_env(tab_id: str, env, solo_mode=None):
        raise KeyError(tab_id)

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.switch_env",
        fake_switch_env,
    )

    response = await client.post(
        "/api/tabs/missing-id/switch-env",
        json={"env": {"ANTHROPIC_MODEL": "x"}},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_switch_env_route_returns_400_for_invalid_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_switch_env(tab_id: str, env, solo_mode=None):
        raise ValueError("switch_env is only supported for Claude tabs")

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.switch_env",
        fake_switch_env,
    )

    response = await client.post(
        "/api/tabs/codex-tab/switch-env",
        json={"env": {"ANTHROPIC_MODEL": "x"}},
    )
    assert response.status_code == 400
    assert "Claude tabs" in response.json()["detail"]


@pytest.mark.asyncio
async def test_switch_env_route_returns_200_on_success(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_switch_env(tab_id: str, env, solo_mode=None):
        return TerminalTab(
            id=tab_id,
            name="Switched",
            shell=None,
            cwd=None,
            solo_mode=bool(solo_mode),
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12345,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
            env=env,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.switch_env",
        fake_switch_env,
    )

    response = await client.post(
        "/api/tabs/live-tab/switch-env",
        json={"env": {"ANTHROPIC_MODEL": "claude-opus"}, "solo_mode": True},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "live-tab"
    assert data["solo_mode"] is True
    assert data["env"]["ANTHROPIC_MODEL"] == "claude-opus"


@pytest.mark.asyncio
async def test_archive_tab_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_archive_tab(tab_id: str) -> None:
        return None

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.archive_tab",
        fake_archive_tab,
    )

    response = await client.post("/api/tabs/missing-id/archive")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unarchive_tab_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    async def fake_unarchive_tab(tab_id: str) -> None:
        return None

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.unarchive_tab",
        fake_unarchive_tab,
    )

    response = await client.post("/api/tabs/missing-id/unarchive")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_archived_route_not_shadowed(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    """GET /api/tabs/archived must list archived tabs, not treat "archived" as a tab id."""
    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.list_archived_tabs",
        lambda: [],
    )

    response = await client.get("/api/tabs/archived")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_archive_tab_route_returns_archived_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    archived_at = datetime.now()

    async def fake_archive_tab(tab_id: str) -> TerminalTab:
        return TerminalTab(
            id=tab_id,
            name="Archived",
            shell=None,
            cwd=None,
            solo_mode=False,
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12345,
            created_at=datetime.now(),
            is_active=False,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
            archived=True,
            archived_at=archived_at,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.archive_tab",
        fake_archive_tab,
    )

    response = await client.post("/api/tabs/live-tab/archive")

    assert response.status_code == 200
    data = response.json()
    assert data["archived"] is True
    assert data["archived_at"] is not None


async def test_mark_tab_viewed_route_clears_unread(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    def fake_mark_tab_viewed(tab_id: str) -> bool:
        return True

    def fake_get_tab(tab_id: str) -> TerminalTab:
        return TerminalTab(
            id=tab_id,
            name="Viewed",
            shell=None,
            cwd=None,
            solo_mode=False,
            agent_type=AgentType.CLAUDE,
            target=ExecutionTarget.LOCAL,
            remote_profile_id=None,
            remote_cwd=None,
            remote_reconnect=True,
            port=12345,
            created_at=datetime.now(),
            is_active=False,
            workspace_id=None,
            workspace_name=None,
            workspace_role=None,
            archived=False,
            archived_at=None,
            last_viewed_at=datetime.now(),
            is_unread=False,
        )

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.mark_tab_viewed",
        fake_mark_tab_viewed,
    )
    monkeypatch.setattr("claude_hub.api.tabs.ttyd_manager.get_tab", fake_get_tab)

    response = await client.post("/api/tabs/live-tab/view")

    assert response.status_code == 200
    data = response.json()
    assert data["is_unread"] is False
    assert data["last_viewed_at"] is not None


async def test_mark_tab_viewed_route_returns_404_for_missing_tab(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    def fake_mark_tab_viewed(tab_id: str) -> bool:
        return False

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.mark_tab_viewed",
        fake_mark_tab_viewed,
    )

    response = await client.post("/api/tabs/missing-id/view")

    assert response.status_code == 404
