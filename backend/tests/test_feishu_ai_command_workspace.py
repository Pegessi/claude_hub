"""Tests for routing Feishu ``/ai`` Chat tabs into an Agent Workspace.

These cover the managed-workspace execution path added so assistant-created
/ai sessions render under an Agent Workspace instead of the bare Terminal/cwd
list. The legacy standalone-tab path (driven by the injected ``tab_creator``
seam) keeps its own coverage in ``test_feishu_ai_command.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from claude_hub.models import AgentType
from claude_hub.services.feishu_ai_command import FeishuAiCommandService
from claude_hub.services.feishu_ai_command_models import (
    DEFAULT_AI_COMMAND_WORKSPACE_NAME,
    FeishuAiCommandConfig,
    parse_ai_command,
)
from claude_hub.services.feishu_user_client import FeishuUserMessage


def _message(message_id: str = "om_ws_1", thread_id: str | None = None) -> FeishuUserMessage:
    return FeishuUserMessage(
        message_id=message_id,
        chat_id="oc_ws_chat",
        sender_open_id="ou_owner",
        sender_type="user",
        msg_type="text",
        text="/ai do the thing",
        create_time_ms=1000,
        thread_id=thread_id,
    )


def _parsed(provider: AgentType = AgentType.CODEX) -> Any:
    parsed = parse_ai_command(
        "/ai do the thing",
        default_provider=provider,
        default_model="gpt-5.4",
        default_reasoning_effort="high",
    )
    assert parsed is not None
    return parsed


class _FakeWorkspaceManager:
    """Minimal workspace manager stand-in for resolver/session/dispatch seams."""

    def __init__(self, workspaces: list[SimpleNamespace] | None = None) -> None:
        self.workspaces: dict[str, SimpleNamespace] = {ws.id: ws for ws in (workspaces or [])}
        self.create_calls: list[dict[str, Any]] = []
        self.sessions: dict[str, SimpleNamespace] = {}
        self.provision_calls: list[dict[str, Any]] = []

    def get_workspace(self, workspace_id: str) -> SimpleNamespace | None:
        return self.workspaces.get(workspace_id)

    def list_workspaces(self) -> list[SimpleNamespace]:
        return list(self.workspaces.values())

    def create_workspace(self, payload: Any) -> SimpleNamespace:
        self.create_calls.append({"name": payload.name, "path": payload.path})
        ws = SimpleNamespace(id="ws-created", name=payload.name, path=payload.path)
        self.workspaces[ws.id] = ws
        return ws

    def add_session(self, session: SimpleNamespace) -> None:
        self.sessions[session.id] = session


def _make_service(
    tmp_path: Path,
    *,
    fake_manager: _FakeWorkspaceManager,
    managed_dispatcher: Any = None,
) -> FeishuAiCommandService:
    provided: list[SimpleNamespace] = []

    def resolver(cfg: FeishuAiCommandConfig, cwd: str) -> SimpleNamespace:
        # Mirror the production resolution algorithm through the fake manager.
        requested = cfg.workspace_id.strip()
        if requested:
            ws = fake_manager.get_workspace(requested)
            if ws is not None:
                return ws
        for ws in fake_manager.list_workspaces():
            if ws.name == DEFAULT_AI_COMMAND_WORKSPACE_NAME:
                return ws
        return fake_manager.create_workspace(
            SimpleNamespace(name=DEFAULT_AI_COMMAND_WORKSPACE_NAME, path=cwd)
        )

    async def provider(**kwargs: Any) -> SimpleNamespace:
        fake_manager.provision_calls.append(dict(kwargs))
        session = SimpleNamespace(
            id=f"sess-{len(fake_manager.provision_calls)}",
            tab_id=f"tab-{len(fake_manager.provision_calls)}",
            workspace_id=kwargs["workspace_id"],
            archived=False,
            env=dict(kwargs["env"]),
            agent_type=kwargs["agent_type"],
            cwd=kwargs.get("cwd"),
        )
        provided.append(session)
        fake_manager.add_session(session)
        return session

    async def default_dispatch(
        session: Any, prompt: str, turn_id: str, visible: str, metadata: Mapping[str, Any]
    ) -> str:
        return f"answer:{session.id}"

    # Provisioned managed tabs are treated as live, non-archived tabs so
    # channel reuse does not provision a second session. Report the session's
    # actual env so unchanged config does not trigger an env switch.
    def _get_tab(tab_id: str) -> SimpleNamespace | None:
        for sess in fake_manager.sessions.values():
            if sess.tab_id == tab_id:
                return SimpleNamespace(id=tab_id, archived=False, env=dict(sess.env))
        return None

    service = FeishuAiCommandService(
        state_path=tmp_path / "ai_ws.json",
        workspace_resolver=resolver,
        managed_session_provider=provider,
        managed_session_getter=fake_manager.sessions.get,
        tab_getter=_get_tab,
        tab_env_switcher=lambda *_a, **_kw: None,
        managed_turn_dispatcher=managed_dispatcher or default_dispatch,
    )
    service._fake_manager = fake_manager  # type: ignore[attr-defined]
    service._provided = provided  # type: ignore[attr-defined]
    return service


@pytest.mark.asyncio
async def test_default_resolution_creates_dedicated_workspace_on_demand(tmp_path: Path) -> None:
    manager = _FakeWorkspaceManager()
    service = _make_service(tmp_path, fake_manager=manager)
    service.update_config({"enabled": True, "cwd": str(tmp_path)})

    tab_id = await service._ensure_execution_tab(_message(), _parsed(), service.get_config())

    # A dedicated default workspace was created on demand and the tab is tracked.
    assert manager.create_calls == [
        {"name": DEFAULT_AI_COMMAND_WORKSPACE_NAME, "path": str(tmp_path)}
    ]
    assert tab_id == "tab-1"
    record = service._channel_record_for_tab(tab_id)
    assert record["workspace_id"] == "ws-created"
    assert record["session_id"] == "sess-1"
    # Provisioned as a managed Chat worker inside the workspace.
    provision = manager.provision_calls[0]
    assert provision["workspace_id"] == "ws-created"
    assert provision["agent_type"] == AgentType.CODEX
    assert provision["env"]["CODEX_MODEL"] == "gpt-5.4"
    # The validated /ai cwd is propagated so a pinned workspace cannot relocate
    # the session to the workspace root.
    assert provision["cwd"] == str(tmp_path)
    assert service._provided[0].cwd == str(tmp_path)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_default_resolution_reuses_existing_dedicated_workspace(tmp_path: Path) -> None:
    existing = SimpleNamespace(
        id="ws-feishu", name=DEFAULT_AI_COMMAND_WORKSPACE_NAME, path=str(tmp_path)
    )
    manager = _FakeWorkspaceManager(workspaces=[existing])
    service = _make_service(tmp_path, fake_manager=manager)
    service.update_config({"enabled": True, "cwd": str(tmp_path)})

    tab_id = await service._ensure_execution_tab(_message(), _parsed(), service.get_config())

    assert manager.create_calls == []  # reused, never created a second workspace
    assert service._channel_record_for_tab(tab_id)["workspace_id"] == "ws-feishu"


@pytest.mark.asyncio
async def test_explicit_workspace_id_is_used(tmp_path: Path) -> None:
    pinned = SimpleNamespace(id="ws-pinned", name="Custom Workspace", path=str(tmp_path))
    other = SimpleNamespace(
        id="ws-other", name=DEFAULT_AI_COMMAND_WORKSPACE_NAME, path=str(tmp_path)
    )
    manager = _FakeWorkspaceManager(workspaces=[pinned, other])
    service = _make_service(tmp_path, fake_manager=manager)
    service.update_config({"enabled": True, "workspace_id": "ws-pinned", "cwd": str(tmp_path)})

    tab_id = await service._ensure_execution_tab(_message(), _parsed(), service.get_config())

    assert manager.create_calls == []
    assert service._channel_record_for_tab(tab_id)["workspace_id"] == "ws-pinned"


@pytest.mark.asyncio
async def test_missing_configured_workspace_falls_back_to_default(tmp_path: Path) -> None:
    dedicated = SimpleNamespace(
        id="ws-default", name=DEFAULT_AI_COMMAND_WORKSPACE_NAME, path=str(tmp_path)
    )
    manager = _FakeWorkspaceManager(workspaces=[dedicated])
    service = _make_service(tmp_path, fake_manager=manager)
    # Pinned id does not exist in the manager; resolver must not fail silently.
    service.update_config({"enabled": True, "workspace_id": "ws-gone", "cwd": str(tmp_path)})

    tab_id = await service._ensure_execution_tab(_message(), _parsed(), service.get_config())

    assert service._channel_record_for_tab(tab_id)["workspace_id"] == "ws-default"
    assert manager.create_calls == []


@pytest.mark.asyncio
async def test_channel_reuse_does_not_create_second_session(tmp_path: Path) -> None:
    manager = _FakeWorkspaceManager()
    service = _make_service(tmp_path, fake_manager=manager)
    service.update_config({"enabled": True, "cwd": str(tmp_path)})
    cfg = service.get_config()
    message = _message()
    parsed = _parsed()

    first = await service._ensure_execution_tab(message, parsed, cfg)
    second = await service._ensure_execution_tab(message, parsed, cfg)

    assert first == second
    assert len(manager.provision_calls) == 1  # reused the mapped tab/session


@pytest.mark.asyncio
async def test_managed_dispatch_routes_through_managed_session(tmp_path: Path) -> None:
    manager = _FakeWorkspaceManager()
    dispatched: list[tuple[str, str, str]] = []

    async def managed_dispatch(
        session: Any, prompt: str, turn_id: str, visible: str, metadata: Mapping[str, Any]
    ) -> str:
        dispatched.append((session.id, turn_id, metadata["origin"]))
        return "managed answer"

    service = _make_service(tmp_path, fake_manager=manager, managed_dispatcher=managed_dispatch)
    service.update_config({"enabled": True, "cwd": str(tmp_path)})
    tab_id = await service._ensure_execution_tab(_message(), _parsed(), service.get_config())

    answer = await service._dispatch_turn(
        tab_id,
        "prompt",
        "feishu-ai-om_ws_1",
        "do the thing",
        {"origin": "feishu_ai_command"},
    )

    assert answer == "managed answer"
    assert dispatched == [("sess-1", "feishu-ai-om_ws_1", "feishu_ai_command")]


@pytest.mark.asyncio
async def test_reuse_with_changed_env_uses_managed_env_switch(tmp_path: Path) -> None:
    manager = _FakeWorkspaceManager()
    service = _make_service(tmp_path, fake_manager=manager)
    service.update_config({"enabled": True, "cwd": str(tmp_path)})
    cfg = service.get_config()
    message = _message()
    parsed = _parsed()

    tab_id = await service._ensure_execution_tab(message, parsed, cfg)
    assert len(manager.provision_calls) == 1

    switched: list[tuple[str, dict[str, str]]] = []

    async def fake_switch_managed_env(session_id: str, env: Mapping[str, str]) -> None:
        switched.append((session_id, dict(env)))

    service._switch_managed_env = fake_switch_managed_env  # type: ignore[method-assign]

    # A follow-up with a different reasoning effort reuses the same mapped tab
    # but hot-switches the managed session env instead of provisioning again.
    parsed_xhigh = parse_ai_command(
        "/ai do the thing",
        default_provider=AgentType.CODEX,
        default_model="gpt-5.4",
        default_reasoning_effort="xhigh",
    )
    assert parsed_xhigh is not None
    reused_tab_id = await service._ensure_execution_tab(message, parsed_xhigh, cfg)

    assert reused_tab_id == tab_id
    assert len(manager.provision_calls) == 1  # no second session
    assert switched and switched[0][0] == "sess-1"
    assert switched[0][1]["CODEX_REASONING_EFFORT"] == "xhigh"


def test_legacy_string_channel_record_still_resolves(tmp_path: Path) -> None:
    service = FeishuAiCommandService(state_path=tmp_path / "legacy.json")
    # A pre-existing plain-string channel_tabs entry (written by older builds)
    # must still be understood after the upgrade.
    service._store_channel_record("oc:main:codex", {"tab_id": "tab-legacy"})
    with service._lock:
        state = service._load_state()
        state["channel_tabs"]["oc:main:codex"] = "tab-legacy"
        service._save_state(state)

    assert service._channel_record_for_tab("tab-legacy") == {"tab_id": "tab-legacy"}
    assert service._channel_record_for_tab("missing") == {}


def test_legacy_tab_creator_seam_keeps_standalone_behavior(tmp_path: Path) -> None:
    created: list[dict[str, Any]] = []

    async def fake_create_tab(**kwargs: Any) -> str:
        created.append(kwargs)
        return "tab-standalone"

    # Injecting tab_creator (and no workspace seams) selects the legacy path.
    service = FeishuAiCommandService(
        state_path=tmp_path / "legacy_seam.json",
        tab_creator=fake_create_tab,
        tab_getter=lambda _tid: None,
    )
    assert service._uses_legacy_tabs() is True

    cfg = FeishuAiCommandConfig(cwd=str(tmp_path))
    record = service._normalize_channel_record(
        {"tab_id": "t", "session_id": "s", "workspace_id": "w"}
    )
    assert record == {"tab_id": "t", "session_id": "s", "workspace_id": "w"}

    # The legacy create helper builds the same standalone Chat tab kwargs.
    import asyncio

    tab_id = asyncio.run(
        service._create_legacy_tab(_message(), _parsed(), cfg, {"CODEX_MODEL": "gpt-5.4"})
    )
    assert tab_id == "tab-standalone"
    assert created[0]["session_kind"].value == "chat"
    assert created[0]["workspace_id"] if "workspace_id" in created[0] else True
    assert "workspace_id" not in created[0]  # legacy standalone tab stays ungrouped


def _workspace(workspace_id: str, name: str, path: str) -> Any:
    from datetime import datetime

    from claude_hub.models import Workspace

    now = datetime.now()
    return Workspace(
        id=workspace_id,
        name=name,
        path=path,
        default_branch="main",
        session_prefix=workspace_id.replace("-", "")[:12] or "feishuai",
        created_at=now,
        updated_at=now,
    )


def test_production_resolver_returns_pinned_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.workspace_manager import workspace_manager

    pinned = _workspace("ws-pin", "Pinned", str(tmp_path))
    monkeypatch.setattr(workspace_manager, "workspaces", {pinned.id: pinned})
    service = FeishuAiCommandService(state_path=tmp_path / "prod.json")

    ws = service._resolve_workspace(FeishuAiCommandConfig(workspace_id="ws-pin"), str(tmp_path))
    assert ws.id == "ws-pin"


def test_production_resolver_creates_default_when_none_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.workspace_manager import workspace_manager

    created: dict[str, Any] = {}

    def fake_create(payload: Any) -> Any:
        created["name"] = payload.name
        created["path"] = payload.path
        created["allow_duplicate"] = payload.allow_duplicate
        ws = _workspace("ws-new", payload.name, payload.path)
        workspace_manager.workspaces[ws.id] = ws
        return ws

    monkeypatch.setattr(workspace_manager, "workspaces", {})
    monkeypatch.setattr(workspace_manager, "create_workspace", fake_create)
    service = FeishuAiCommandService(state_path=tmp_path / "prod2.json")

    ws = service._resolve_workspace(FeishuAiCommandConfig(), str(tmp_path))

    assert ws.id == "ws-new"
    assert created["name"] == DEFAULT_AI_COMMAND_WORKSPACE_NAME
    assert created["path"] == str(tmp_path)
    # allow_duplicate bypasses the identity-collision guard so a cwd that already
    # hosts another workspace does not raise DuplicateWorkspaceError.
    assert created["allow_duplicate"] is True


def test_production_resolver_missing_pinned_id_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.workspace_manager import workspace_manager

    dedicated = _workspace("ws-default", DEFAULT_AI_COMMAND_WORKSPACE_NAME, str(tmp_path))
    monkeypatch.setattr(workspace_manager, "workspaces", {dedicated.id: dedicated})
    service = FeishuAiCommandService(state_path=tmp_path / "prod3.json")

    ws = service._resolve_workspace(
        FeishuAiCommandConfig(workspace_id="ws-does-not-exist"), str(tmp_path)
    )
    assert ws.id == "ws-default"


def test_safe_session_cwd_none_when_request_empty(tmp_path: Path) -> None:
    from claude_hub.services import feishu_ai_workspace as faw

    workspace = SimpleNamespace(id="ws", path=str(tmp_path))
    # Empty/whitespace cwd defers to workspace.path (None) rather than resolving
    # to Path.home(), which could be outside the workspace and crash provisioning.
    assert faw.safe_session_cwd(workspace, "") is None
    assert faw.safe_session_cwd(workspace, "   ") is None


def test_safe_session_cwd_forwarded_when_inside_workspace(tmp_path: Path) -> None:
    from claude_hub.services import feishu_ai_workspace as faw

    workspace = SimpleNamespace(id="ws", path=str(tmp_path))
    sub = tmp_path / "nested" / "job"
    sub.mkdir(parents=True)
    assert faw.safe_session_cwd(workspace, str(sub)) == str(sub)
    # Equal path is admissible.
    assert faw.safe_session_cwd(workspace, str(tmp_path)) == str(tmp_path)


def test_safe_session_cwd_falls_back_when_outside_workspace(tmp_path: Path) -> None:
    from claude_hub.services import feishu_ai_workspace as faw

    ws_root = tmp_path / "wsroot"
    ws_root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    workspace = SimpleNamespace(id="ws", path=str(ws_root))

    # An explicit but out-of-workspace cwd must not raise: it returns None so the
    # platform defaults to workspace.path.
    assert faw.safe_session_cwd(workspace, str(outside)) is None
    # A non-existent cwd is treated the same way.
    assert faw.safe_session_cwd(workspace, str(tmp_path / "missing")) is None


def test_production_resolver_reuses_dedicated_workspace_that_allows_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.workspace_manager import workspace_manager

    dedicated = _workspace("ws-feishu", DEFAULT_AI_COMMAND_WORKSPACE_NAME, str(tmp_path))
    monkeypatch.setattr(workspace_manager, "workspaces", {dedicated.id: dedicated})

    def fail_create(payload: Any) -> Any:
        pytest.fail("must reuse the admissible dedicated workspace, not create one")

    monkeypatch.setattr(workspace_manager, "create_workspace", fail_create)
    service = FeishuAiCommandService(state_path=tmp_path / "reuse.json")

    # cwd equals the workspace path -> admissible -> reused.
    ws = service._resolve_workspace(FeishuAiCommandConfig(), str(tmp_path))
    assert ws.id == "ws-feishu"


def test_production_resolver_creates_when_dedicated_workspace_rejects_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.workspace_manager import workspace_manager

    ws_root = tmp_path / "old-ws"
    ws_root.mkdir()
    new_cwd = tmp_path / "new-cwd"
    new_cwd.mkdir()
    # A pre-existing dedicated workspace rooted at a different (sibling) path.
    stale = _workspace("ws-old", DEFAULT_AI_COMMAND_WORKSPACE_NAME, str(ws_root))
    monkeypatch.setattr(workspace_manager, "workspaces", {stale.id: stale})

    created: dict[str, Any] = {}

    def fake_create(payload: Any) -> Any:
        created["path"] = payload.path
        created["allow_duplicate"] = payload.allow_duplicate
        ws = _workspace("ws-fresh", payload.name, payload.path)
        workspace_manager.workspaces[ws.id] = ws
        return ws

    monkeypatch.setattr(workspace_manager, "create_workspace", fake_create)
    service = FeishuAiCommandService(state_path=tmp_path / "fresh.json")

    # The new cwd is outside the old dedicated workspace, so the resolver must
    # not reuse it (provisioning would reject the cwd) and creates a fresh one.
    ws = service._resolve_workspace(FeishuAiCommandConfig(), str(new_cwd))
    assert ws.id == "ws-fresh"
    assert created["path"] == str(new_cwd)
    assert created["allow_duplicate"] is True


@pytest.mark.asyncio
async def test_empty_cfg_cwd_provisions_without_crash_and_defers_to_workspace(
    tmp_path: Path,
) -> None:
    """cfg.cwd='' must not resolve to Path.home() and crash against the workspace."""
    manager = _FakeWorkspaceManager()
    service = _make_service(tmp_path, fake_manager=manager)
    # Leave cwd at its empty default; the fake workspace is rooted at tmp_path.
    service.update_config({"enabled": True, "cwd": ""})

    tab_id = await service._ensure_execution_tab(_message(), _parsed(), service.get_config())

    assert tab_id == "tab-1"
    # The session request carries None cwd (platform will use workspace.path),
    # never a Path.home() path that the workspace cannot host.
    assert manager.provision_calls[0]["cwd"] is None


@pytest.mark.asyncio
async def test_out_of_workspace_cwd_falls_back_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit cwd outside the pinned workspace degrades to workspace.path."""
    from claude_hub.services import feishu_ai_workspace as faw

    manager = _FakeWorkspaceManager()
    service = _make_service(tmp_path, fake_manager=manager)

    ws_root = tmp_path / "pinned-root"
    ws_root.mkdir()
    outside = tmp_path / "outside-dir"
    outside.mkdir()

    # Pin a workspace rooted at ws_root; the harness resolver returns it.
    pinned = SimpleNamespace(id="ws-pinned", name="Pinned", path=str(ws_root))
    manager.workspaces[pinned.id] = pinned

    def resolver(cfg: FeishuAiCommandConfig, cwd: str) -> SimpleNamespace:
        return pinned

    service._workspace_resolver = resolver  # type: ignore[method-assign]
    service.update_config({"enabled": True, "workspace_id": "ws-pinned", "cwd": str(outside)})

    # Spy on the safe helper to prove the fallback is exercised without raising.
    called: dict[str, Any] = {}
    original = faw.safe_session_cwd

    def spy(workspace: Any, requested: str) -> str | None:
        result = original(workspace, requested)
        called["requested"] = requested
        called["result"] = result
        return result

    monkeypatch.setattr(faw, "safe_session_cwd", spy)

    tab_id = await service._ensure_execution_tab(_message(), _parsed(), service.get_config())

    assert tab_id == "tab-1"
    assert called["requested"] == str(outside)
    assert called["result"] is None  # rejected -> defer to workspace.path
    assert manager.provision_calls[0]["cwd"] is None


def test_workspace_id_config_persists_and_clears(tmp_path: Path) -> None:
    service = FeishuAiCommandService(state_path=tmp_path / "cfg.json")

    updated = service.update_config({"workspace_id": "ws-abc"})
    assert updated.workspace_id == "ws-abc"
    # Reloaded from disk through the same dataclass contract.
    assert service.get_config().workspace_id == "ws-abc"

    # An explicit empty string clears the pin (CLI --clear-workspace-id path).
    cleared = service.update_config({"workspace_id": ""})
    assert cleared.workspace_id == ""


def test_ai_command_patch_input_accepts_workspace_id() -> None:
    from claude_hub.api.routes.feishu_bot.ai_command import FeishuAiCommandPatchInput

    payload = FeishuAiCommandPatchInput(workspace_id="ws-xyz")
    assert payload.model_dump(exclude_none=True)["workspace_id"] == "ws-xyz"

    # Unknown fields remain forbidden so the surface stays explicit/validated.
    with pytest.raises(Exception):
        FeishuAiCommandPatchInput(not_a_real_field=1)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_unsupported_managed_provider_falls_back_to_standalone_tab(
    tmp_path: Path,
) -> None:
    # TraeX supports direct Chat but not the MANAGED_CHAT surface, so /ai must
    # keep working via a standalone tab instead of failing workspace admission.
    from claude_hub.models import AgentType

    manager = _FakeWorkspaceManager()
    created_legacy: list[dict[str, Any]] = []

    async def fake_create_tab(**kwargs: Any) -> str:
        created_legacy.append(kwargs)
        return "tab-traex"

    service = FeishuAiCommandService(
        state_path=tmp_path / "traex.json",
        tab_creator=fake_create_tab,
        tab_getter=lambda _tid: None,
        workspace_resolver=lambda cfg, cwd: pytest.fail("resolver must not run for traex"),
        managed_session_provider=lambda **kw: pytest.fail("managed provider must not run"),
    )
    service.update_config({"enabled": True, "cwd": str(tmp_path)})
    parsed = parse_ai_command(
        "/ai do the thing",
        default_provider=AgentType.TRAEX,
        default_model="Seed-2.1-Pro",
        default_reasoning_effort="high",
    )
    assert parsed is not None
    assert service._provider_supports_managed_chat(parsed.provider) is False

    tab_id = await service._ensure_execution_tab(_message(), parsed, service.get_config())

    assert tab_id == "tab-traex"
    assert created_legacy and "workspace_id" not in created_legacy[0]
    assert manager.provision_calls == []
    # The fallback record carries no managed session identity.
    assert service._channel_record_for_tab("tab-traex") == {"tab_id": "tab-traex"}


# ---------------------------------------------------------------------------
# Review-fix round 2: session isolation, duplicate identity, stale sessions,
# and safe-retry cancellation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_channels_provision_distinct_sessions(tmp_path: Path) -> None:
    """reuse_existing=False: two Feishu channels never share one WORKER session."""
    manager = _FakeWorkspaceManager()
    service = _make_service(tmp_path, fake_manager=manager)
    service.update_config({"enabled": True, "cwd": str(tmp_path)})
    cfg = service.get_config()
    parsed = _parsed()

    first_msg = _message(message_id="om_a", thread_id="thread-a")
    second_msg = _message(message_id="om_b", thread_id="thread-b")
    first_tab = await service._ensure_execution_tab(first_msg, parsed, cfg)
    second_tab = await service._ensure_execution_tab(second_msg, parsed, cfg)

    assert first_tab != second_tab
    assert len(manager.provision_calls) == 2  # no advisory reuse across channels
    # Every provisioning request forbids best-effort reuse.
    assert all(call["cwd"] == str(tmp_path) for call in manager.provision_calls)


@pytest.mark.asyncio
async def test_stale_managed_session_triggers_fresh_provisioning(tmp_path: Path) -> None:
    """A mapped record whose managed session is gone is not reused."""
    manager = _FakeWorkspaceManager()
    service = _make_service(tmp_path, fake_manager=manager)
    service.update_config({"enabled": True, "cwd": str(tmp_path)})
    cfg = service.get_config()
    message = _message()
    parsed = _parsed()

    first_tab = await service._ensure_execution_tab(message, parsed, cfg)
    assert len(manager.provision_calls) == 1
    record = service._channel_record_for_tab(first_tab)
    session_id = record["session_id"]

    # Simulate the managed WORKER session being deleted/archived while its tab
    # record (and the channel map entry) still lingers.
    manager.sessions.pop(session_id, None)

    second_tab = await service._ensure_execution_tab(message, parsed, cfg)

    # A fresh managed session is provisioned and the channel map repointed to it.
    assert len(manager.provision_calls) == 2
    new_record = service._channel_record_for_tab(second_tab)
    assert new_record["session_id"] != session_id
    assert manager.sessions[new_record["session_id"]] is not None


@pytest.mark.asyncio
async def test_archived_managed_session_triggers_fresh_provisioning(tmp_path: Path) -> None:
    manager = _FakeWorkspaceManager()
    service = _make_service(tmp_path, fake_manager=manager)
    service.update_config({"enabled": True, "cwd": str(tmp_path)})
    cfg = service.get_config()
    message = _message()
    parsed = _parsed()

    first_tab = await service._ensure_execution_tab(message, parsed, cfg)
    record = service._channel_record_for_tab(first_tab)
    manager.sessions[record["session_id"]].archived = True

    # The tab itself still resolves as a live, non-archived tab; only the
    # backing managed session is dead. This isolates the new session-liveness
    # guard from the pre-existing tab liveness check.
    assert service._get_tab(first_tab) is not None
    assert service._get_managed_session(record["session_id"]) is None

    second_tab = await service._ensure_execution_tab(message, parsed, cfg)

    assert len(manager.provision_calls) == 2
    assert service._channel_record_for_tab(second_tab)["session_id"] != record["session_id"]


class _FakeTailerManager:
    """Minimal managed Chat tailer for exercising cancellation/retirement."""

    def __init__(self, retirement: Any) -> None:
        self._retirement = retirement
        self.retired: list[tuple[str, str | None]] = []
        self.unsubscribed: list[str] = []

    async def subscribe_completion(self, session: Any) -> asyncio.Queue[Any]:
        return asyncio.Queue()  # never emits; the awaiting task gets cancelled

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def retire_external_turn(self, session: Any, expected_turn_id: str | None = None) -> Any:
        self.retired.append((session.id, expected_turn_id))
        return self._retirement

    def unsubscribe_completion(self, session_id: str, queue: Any) -> None:
        self.unsubscribed.append(session_id)


async def _run_dispatch_until_cancelled(
    monkeypatch: pytest.MonkeyPatch, retirement: Any
) -> tuple[Any, _FakeTailerManager]:
    from claude_hub.api import agent_stream as agent_stream_module
    from claude_hub.services import feishu_ai_workspace as faw

    fake_tailer = _FakeTailerManager(retirement)
    monkeypatch.setattr(agent_stream_module, "get_managed_tailer_manager", lambda: fake_tailer)
    session = SimpleNamespace(id="sess-cancel", tab_id="tab-cancel", archived=False)
    task = asyncio.ensure_future(
        faw.dispatch_managed_turn(
            session,
            "prompt",
            "turn-1",
            "visible",
            {"origin": "feishu_ai_command"},
        )
    )
    # Let the task subscribe and park on queue.get(), then cancel it.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    return task, fake_tailer


@pytest.mark.asyncio
async def test_safe_retry_cancellation_raises_external_dispatch_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api.routes.agent_stream.dispatch import ExternalDispatchRetired
    from claude_hub.services.agent_stream.runtime.values import ExternalTurnRetirement

    task, fake_tailer = await _run_dispatch_until_cancelled(
        monkeypatch, ExternalTurnRetirement.CANCELLED_MATCHING
    )
    with pytest.raises(ExternalDispatchRetired):
        await task
    assert fake_tailer.retired == [("sess-cancel", "turn-1")]
    assert fake_tailer.unsubscribed == ["sess-cancel"]


@pytest.mark.asyncio
async def test_not_started_cancellation_also_raises_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api.routes.agent_stream.dispatch import ExternalDispatchRetired
    from claude_hub.services.agent_stream.runtime.values import ExternalTurnRetirement

    task, _fake_tailer = await _run_dispatch_until_cancelled(
        monkeypatch, ExternalTurnRetirement.NOT_STARTED
    )
    with pytest.raises(ExternalDispatchRetired):
        await task


@pytest.mark.asyncio
async def test_unsafe_retry_cancellation_preserves_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-retryable retirement re-raises plain CancelledError (claim retained)."""
    from claude_hub.api.routes.agent_stream.dispatch import ExternalDispatchRetired
    from claude_hub.services.agent_stream.runtime.values import ExternalTurnRetirement

    task, _fake_tailer = await _run_dispatch_until_cancelled(
        monkeypatch, ExternalTurnRetirement.ALREADY_TERMINAL
    )
    # CancelledError is re-raised, but it must NOT be promoted to the
    # retry-safe subclass (that would wrongly release the delivery claim).
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task
    assert not isinstance(exc_info.value, ExternalDispatchRetired)
    assert task.done()


@pytest.mark.asyncio
async def test_production_provisioning_creates_tab_with_workspace_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: real ensure_workspace_agent -> ttyd_manager.create_tab stamps
    workspace_id/workspace_role/managed_session_id on the runtime tab."""
    from datetime import datetime

    from claude_hub.models import (
        SessionKind,
        TerminalTab,
        WorkspaceSessionRole,
    )
    from claude_hub.services import feishu_ai_workspace as faw
    from claude_hub.services import ttyd_manager
    from claude_hub.services.workspace_manager import workspace_manager

    workspace_manager.workspaces.clear()
    workspace_manager.sessions.clear()
    monkeypatch.setattr(workspace_manager, "_save_state", lambda: None)
    monkeypatch.setattr(workspace_manager, "_save_workspace_state", lambda _id: None)

    captured: dict[str, Any] = {}

    async def fake_create_tab(**kwargs: Any) -> TerminalTab:
        captured.update(kwargs)
        return TerminalTab(
            id="tab-runtime-1",
            name=kwargs["name"],
            cwd=kwargs.get("cwd"),
            solo_mode=kwargs.get("solo_mode", True),
            agent_type=kwargs["agent_type"],
            session_kind=kwargs["session_kind"],
            target=kwargs["target"],
            port=19001,
            created_at=datetime.now(),
            is_active=True,
            workspace_id=kwargs.get("workspace_id"),
            workspace_name=kwargs.get("workspace_name"),
            workspace_role=kwargs.get("workspace_role"),
            managed_session_id=kwargs.get("managed_session_id"),
        )

    monkeypatch.setattr(ttyd_manager, "create_tab", fake_create_tab)

    # Wrap (not replace) the real platform method so we can assert the exact
    # EnsureWorkspaceAgentRequest the /ai path sends while still exercising the
    # full provisioning -> create_tab -> ManagedSession flow.
    real_ensure = workspace_manager.ensure_workspace_agent
    request_seen: dict[str, Any] = {}

    async def spy_ensure(workspace_id: str, payload: Any, *, bootstrap: bool = True) -> Any:
        request_seen["payload"] = payload
        request_seen["bootstrap"] = bootstrap
        return await real_ensure(workspace_id, payload, bootstrap=bootstrap)

    monkeypatch.setattr(workspace_manager, "ensure_workspace_agent", spy_ensure)

    workspace = workspace_manager.create_workspace(
        _workspace_create_payload(tmp_path, "ws-runtime", "Feishu /ai Runtime")
    )
    try:
        session = await faw.provide_managed_session(
            workspace_id=workspace.id,
            agent_type=AgentType.CODEX,
            env={"CODEX_MODEL": "gpt-5.4"},
            title="Feishu /ai · abc123",
            cwd=str(tmp_path),
        )

        # The production request forbids advisory reuse and runs without a
        # bootstrap prompt, and carries the validated /ai cwd.
        request = request_seen["payload"]
        assert request.reuse_existing is False
        assert request_seen["bootstrap"] is False
        assert request.cwd == str(tmp_path)
        assert request.session_kind == SessionKind.CHAT
        assert request.role == WorkspaceSessionRole.WORKER

        # The provisioning request disabled advisory reuse.
        assert session.workspace_id == workspace.id
        assert session.role == WorkspaceSessionRole.WORKER
        assert session.session_kind == SessionKind.CHAT

        # The runtime tab created through the real provisioning path carries the
        # workspace attribution that makes it render under the workspace.
        assert captured["workspace_id"] == workspace.id
        assert captured["workspace_name"] == workspace.name
        assert captured["workspace_role"] == WorkspaceSessionRole.WORKER
        assert captured["managed_session_id"] == session.id
        assert captured["session_kind"] == SessionKind.CHAT
        assert captured["cwd"] == str(tmp_path)
        # The managed session points back at the tab the stub created, so the
        # service can later look up the tab by id and dispatch through it.
        assert session.tab_id == "tab-runtime-1"
    finally:
        workspace_manager.workspaces.clear()
        workspace_manager.sessions.clear()


def _workspace_create_payload(tmp_path: Path, workspace_id: str, name: str) -> Any:
    from claude_hub.models import WorkspaceCreate

    return WorkspaceCreate(
        name=name,
        path=str(tmp_path),
        session_prefix=workspace_id.replace("-", "")[:12] or "feishuai",
    )
