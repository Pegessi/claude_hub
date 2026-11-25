"""Behavior that must not depend on another test file's process state."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import httpx
import pytest

from claude_hub.services import cross_reviewer, maso_session_archive
from claude_hub.services.agent_stream import tailer
from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore
from claude_hub.services.feishu_user_token_store import FeishuUserTokenStore
from claude_hub.services.feishu_web_session import client as web_client


def test_model_resolution_searches_manager_tailers(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = SimpleNamespace(
        session=SimpleNamespace(tab_id="wanted"), current_model=lambda: " model "
    )
    other = SimpleNamespace(session=SimpleNamespace(tab_id="other"))
    manager = SimpleNamespace(
        get_tailer=lambda _tab_id: None,
        tailers=lambda: [
            SimpleNamespace(native_transport=other),
            SimpleNamespace(native_transport=transport),
        ],
    )
    monkeypatch.setattr(tailer, "_TAILER_MANAGERS", [manager])
    assert cross_reviewer.resolve_active_maso_tab_model("wanted") == "model"
    assert cross_reviewer.resolve_active_maso_tab_model("missing") is None


def test_lock_account_uses_saved_token_for_missing_identity(tmp_path) -> None:
    store = FeishuUserTokenStore(path=tmp_path / "tokens.json")
    store.save_oauth_token(open_id="user", app_id="app", access_token="token")
    assert store._resolve_lock_account(open_id="user") == ("app", "user")
    assert store._resolve_lock_account(app_id="app") == ("app", "user")


async def test_archive_rejects_scheduling_after_lifetime() -> None:
    coordinator = maso_session_archive.MasoSessionArchives()
    async with coordinator.lifetime():
        pass
    coordinator.schedule(
        maso_session_archive.MasoSessionIdentity("http://localhost", "session"), archived=True
    )
    assert not coordinator._pending
    assert not coordinator._jobs


async def test_clear_missing_preview_workspace_preserves_other_workspace(
    monkeypatch, tmp_path
) -> None:
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", tmp_path)
    store = AgentStreamAttachmentStore("missing", "session")
    sibling = store._session_dir.parent.parent / "sibling" / "sentinel"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"kept")
    await store.clear()
    assert sibling.read_bytes() == b"kept"


async def test_web_client_owns_and_closes_internal_http_client(monkeypatch, tmp_path) -> None:
    requests = []
    clients = []
    real_client = httpx.AsyncClient

    def make_client(**kwargs):
        client = real_client(
            transport=httpx.MockTransport(
                lambda request: (requests.append(request), httpx.Response(200, text="ok"))[1]
            ),
            **kwargs,
        )
        clients.append(client)
        return client

    monkeypatch.setattr(web_client.httpx, "AsyncClient", make_client)
    client = web_client.FeishuWebClient(
        store=web_client.FeishuWebSessionStore(path=tmp_path / "web.json")
    )
    result = await client._do_request(
        "POST", "https://example.test/", headers={"X-Test": "value"}, content=b"body"
    )
    assert result.text == "ok"
    assert requests[0].headers["X-Test"] == "value"
    assert requests[0].content == b"body"
    assert clients[0].is_closed


async def test_frontier_cancellation_propagates_without_retry(monkeypatch, tmp_path) -> None:
    entered = asyncio.Event()
    states: list[Exception | None] = []
    listener = web_client.FeishuWebFrontierListener(
        web_client.FeishuWebClient(
            store=web_client.FeishuWebSessionStore(path=tmp_path / "web.json")
        ),
        lambda _message: None,
        on_state=states.append,
    )

    async def once(_stop):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(listener, "_once", once)
    task = asyncio.create_task(listener.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert states == []
