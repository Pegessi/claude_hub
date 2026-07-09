from datetime import datetime

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentType,
    ExecutionTarget,
    TerminalAgentStatus,
    TerminalTab,
)


@pytest.mark.asyncio
async def test_list_tabs_empty(client: AsyncClient) -> None:
    """Test that listing tabs returns an empty list when no tabs exist."""
    response = await client.get("/api/tabs")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


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


def _fake_statuses(sampled_at: datetime, kind: str = "idle") -> list[TerminalAgentStatus]:
    """Build a deterministic TerminalAgentStatus list for ETag tests.

    ``sampled_at`` is intentionally a fresh value per call (mirroring the real
    endpoint's ``datetime.now()`` sampling); the ETag must ignore it via the
    _VOLATILE_STATUS_FIELDS strip so idle ticks still 304.
    """
    status_map = {
        "idle": (AgentRuntimeStatus.IDLE, "Claude idle", None),
        "working": (AgentRuntimeStatus.WORKING, "Claude coding", "writing src/foo.py"),
    }
    status, status_text, detail = status_map[kind]
    return [
        TerminalAgentStatus(
            tab_id="tab-a",
            tab_name="Claude",
            agent_type=AgentType.CLAUDE,
            status=status,
            status_text=status_text,
            detail=detail,
            tmux_session="claude-tab-a",
            last_changed_at=datetime(2026, 7, 10, 12, 0, 0),
            sampled_at=sampled_at,
        ),
        TerminalAgentStatus(
            tab_id="tab-b",
            tab_name="Codex",
            agent_type=AgentType.CODEX,
            status=AgentRuntimeStatus.IDLE,
            status_text="Codex idle",
            detail=None,
            tmux_session="codex-tab-b",
            last_changed_at=datetime(2026, 7, 10, 12, 0, 0),
            sampled_at=sampled_at,
        ),
    ]


@pytest.mark.asyncio
async def test_tabs_status_etag_returns_304_when_unchanged(
    client: AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    """Idle /status polls emit an ETag and short-circuit to 304 on match.

    Mirrors ``test_board_etag_returns_304_when_unchanged`` for the workspace board.
    Even though ``sampled_at`` ticks on every call (the helper passes a fresh
    ``datetime.now()`` each invocation), the ETag must stay stable because
    ``sampled_at`` is classified volatile and stripped before hashing.
    """
    calls: list[str] = []

    async def fake_list_statuses() -> list[TerminalAgentStatus]:
        import time

        calls.append("x")
        # Intentionally churn sampled_at on every call to exercise the strip.
        return _fake_statuses(datetime.now(), kind="idle")

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.list_tab_agent_statuses",
        fake_list_statuses,
    )

    first = await client.get("/api/tabs/status")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag and etag.startswith('"') and etag.endswith('"')
    assert len(etag) == 34  # quoted 32-hex digest
    first_data = first.json()
    assert len(first_data) == 2

    # A matching If-None-Match short-circuits to a bodyless 304; the ETag is
    # still returned so the client can keep using its cached value.
    cached = await client.get("/api/tabs/status", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.headers.get("etag") == etag
    assert cached.content == b""
    assert cached.headers.get("cache-control") == "no-cache"

    # A stale/unknown tag returns the full list with the same ETag (content is
    # still byte-stable, sampled_at churn is stripped).
    stale = await client.get("/api/tabs/status", headers={"If-None-Match": '"deadbeef"'})
    assert stale.status_code == 200
    assert stale.headers.get("etag") == etag

    # When content changes (status transitions IDLE → WORKING) the ETag rotates
    # and the follow-up If-None-Match with the old etag yields 200.
    async def fake_list_statuses_changed() -> list[TerminalAgentStatus]:
        calls.append("c")
        return _fake_statuses(datetime.now(), kind="working")

    monkeypatch.setattr(
        "claude_hub.api.tabs.ttyd_manager.list_tab_agent_statuses",
        fake_list_statuses_changed,
    )
    changed = await client.get("/api/tabs/status", headers={"If-None-Match": etag})
    assert changed.status_code == 200
    new_etag = changed.headers.get("etag")
    assert new_etag != etag
    changed_data = changed.json()
    assert changed_data[0]["status"] == "working"
    assert changed_data[0]["detail"] == "writing src/foo.py"

    # The new ETag must itself be stable across repeated idle polls of the
    # new content.
    cached2 = await client.get("/api/tabs/status", headers={"If-None-Match": new_etag})
    assert cached2.status_code == 304
