"""Tests for the restricted agent-produced image endpoint.

``GET /api/workspaces/tabs/{tab_id}/stream/agent-image?path=...`` lets the
browser render an image an agent produced (a Codex/TraeX ``view_image`` call or
a Claude ``Read`` of an image). It must behave as a *restricted* reader, never
an arbitrary file-read primitive:

* the resolved real path must stay inside the tab's own working directory
  (absolute escapes, ``..`` traversal and symlink escapes are denied);
* only files whose magic bytes are a whitelisted image are served;
* scope is per-tab — another tab's working dir is out of bounds;
* every denial (bad path / missing / non-image / unknown tab / remote) is an
  opaque 404 with ``X-Content-Type-Options: nosniff``.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from claude_hub.models import (
    AgentRuntimeStatus,
    AgentStreamEvent,
    AgentStreamEventType,
    AgentType,
    EnsureWorkspaceAgentRequest,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    SessionKind,
    TerminalTab,
    User,
    WorkspaceCreate,
    WorkspaceSessionRole,
)

# Keep a real image: retained output uses the attachment store's header and
# dimension validation, and must round-trip the original bytes unchanged.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJ1kAAAAASUVORK5CYII="
)
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
TAB_CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def image_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_manager = importlib.import_module("claude_hub.services.workspace_manager")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(workspace_manager, "STATE_ROOT", state)


def _make_tab(
    tab_id: str, cwd: str, target: ExecutionTarget = ExecutionTarget.LOCAL
) -> SimpleNamespace:
    return SimpleNamespace(
        id=tab_id,
        name=f"tab-{tab_id}",
        created_at=TAB_CREATED_AT,
        cwd=cwd,
        remote_cwd=None,
        target=target,
        remote_profile_id=None,
        remote_reconnect=True,
        solo_mode=True,
        env={},
        managed_session_id=None,
        workspace_id=None,
        workspace_role=None,
        agent_session_id="provider-session-id",
        agent_session_id_verified=False,
        cursor_transport="terminal",
        cursor_data_dir=None,
        cursor_cli_version=None,
        cursor_transcript_path=None,
        cursor_transcript_schema=None,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )


@pytest.fixture
def tab_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The tab's own cwd, containing one valid PNG."""
    from claude_hub.api import agent_stream as agent_stream_api

    cwd = tmp_path / "workspace"
    cwd.mkdir()
    image = cwd / "shot.png"
    image.write_bytes(PNG_BYTES)
    tab = _make_tab("tab-a", str(cwd))
    monkeypatch.setattr(
        agent_stream_api.ttyd_manager,
        "get_tab",
        lambda tab_id: tab if tab_id == "tab-a" else None,
    )
    return cwd


async def _get_image(client: AsyncClient, tab_id: str, path: str):
    return await client.get(
        f"/api/workspaces/tabs/{tab_id}/stream/agent-image",
        params={"path": path},
    )


def _write_image_reference(
    text: str,
    *,
    tab_id: str = "tab-a",
    kind: str = "text_delta",
    role: str = "assistant",
    split: bool = False,
) -> None:
    from claude_hub.services.agent_stream.store import AgentStreamStore

    path = AgentStreamStore("terminal-tabs", f"terminal-tab-{tab_id}").path
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = [text[: len(text) // 2], text[len(text) // 2 :]] if split else [text]
    events = [
        {
            "session_id": f"terminal-tab-{tab_id}",
            "tab_id": tab_id,
            "type": kind,
            "turn_id": "turn-a",
            "message_id": f"turn-a:{role}",
            "run_epoch": 1,
            "redacted": True,
            "payload": {
                "role": role,
                "assistant_text" if kind == "turn_completed" else "text": chunk,
            },
        }
        for chunk in chunks
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


@pytest.mark.parametrize(
    "kind,split",
    [("text_delta", False), ("text_delta", True), ("turn_completed", False)],
)
async def test_assistant_referenced_temp_image_is_allowed(
    client: AsyncClient, tab_dir: Path, tmp_path: Path, kind: str, split: bool
) -> None:
    outside = tmp_path / "preview & detail.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"![Preview](<{outside}>)", kind=kind, split=split)
    resp = await _get_image(client, "tab-a", str(outside))
    assert resp.status_code == 200
    assert resp.content == PNG_BYTES


@pytest.mark.parametrize(
    "kind,role,tab_id",
    [
        ("text_delta", "user", "tab-a"),
        ("tool_call_completed", "assistant", "tab-a"),
        ("text_delta", "assistant", "tab-b"),
    ],
)
async def test_other_sources_do_not_grant_temp_image_access(
    client: AsyncClient,
    tab_dir: Path,
    tmp_path: Path,
    kind: str,
    role: str,
    tab_id: str,
) -> None:
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"![Preview]({outside})", kind=kind, role=role, tab_id=tab_id)
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_temp_reference_grants_exact_file_only(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "preview.png"
    outside.write_bytes(PNG_BYTES)
    other = tmp_path / "other.png"
    other.write_bytes(PNG_BYTES)
    _write_image_reference(f"![Preview]({outside})")
    assert (await _get_image(client, "tab-a", str(other))).status_code == 404
    outside.write_text("not an image")
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_referenced_temp_symlink_is_denied(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "linked.png"
    real = tmp_path / "target.png"
    real.write_bytes(PNG_BYTES)
    outside.symlink_to(real)
    _write_image_reference(f"![Preview]({outside})")
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_reference_cannot_grant_non_temp_file(
    client: AsyncClient, tab_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.agent_stream import local_images

    # Model a non-temp file without inspecting or modifying the real home.
    monkeypatch.setattr(local_images, "_TEMP_IMAGE_ROOTS", (tmp_path / "temp",))
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"![Preview]({outside})")
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_bare_path_mention_is_not_a_grant(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"File at {outside}.")
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_local_image_link_grants_the_inline_preview(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "output.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_reference(f"[View result]({outside})")
    assert (await _get_image(client, "tab-a", str(outside))).content == PNG_BYTES


def _write_image_tool(path: Path, name: str = "Read", *, role: str = "assistant"):
    from claude_hub.models import AgentStreamEvent
    from claude_hub.services.agent_stream.store import AgentStreamStore

    args = {"file_path" if name == "Read" else "path": str(path)}
    event = AgentStreamEvent(
        stream_sequence=0,
        tab_id="tab-a",
        session_id="terminal-tab-tab-a",
        agent_type=AgentType.CLAUDE,
        type=AgentStreamEventType.TOOL_CALL_STARTED,
        payload={"name": name, "args": args, "role": role},
        created_at=datetime.now(timezone.utc),
    )
    store = AgentStreamStore("terminal-tabs", event.session_id)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(event.model_dump_json() + "\n")
    return event


@pytest.mark.parametrize("name", ["Read", "view_image"])
async def test_temporary_image_view_tool_grants_only_its_exact_file(
    client: AsyncClient, tab_dir: Path, tmp_path: Path, name: str
) -> None:
    outside = tmp_path / "dot-mobile-work.png"
    outside.write_bytes(PNG_BYTES)
    sibling = tmp_path / "dot-mobile-home.png"
    sibling.write_bytes(PNG_BYTES)
    _write_image_tool(outside, name)
    assert (await _get_image(client, "tab-a", str(outside))).content == PNG_BYTES
    assert (await _get_image(client, "tab-a", str(sibling))).status_code == 404


@pytest.mark.parametrize("name,role", [("Bash", "assistant"), ("Read", "user")])
async def test_other_tool_or_user_cannot_grant_a_temporary_image(
    client: AsyncClient, tab_dir: Path, tmp_path: Path, name: str, role: str
) -> None:
    outside = tmp_path / "private.png"
    outside.write_bytes(PNG_BYTES)
    _write_image_tool(outside, name, role=role)
    assert (await _get_image(client, "tab-a", str(outside))).status_code == 404


async def test_live_image_is_retained_before_any_browser_load(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    from claude_hub.api.agent_stream import _terminal_tab_session_or_404
    from claude_hub.services.agent_stream.local_images import cache_event_images

    outside = tmp_path / "output.png"
    outside.write_bytes(PNG_BYTES)
    event = _write_image_tool(outside)
    await cache_event_images(_terminal_tab_session_or_404("tab-a"), event)
    outside.unlink()
    assert (await _get_image(client, "tab-a", str(outside))).content == PNG_BYTES


async def test_long_answer_split_image_link_is_retained_before_truncated_completion(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    from claude_hub.api.agent_stream import _terminal_tab_session_or_404
    from claude_hub.models import AgentStreamEvent
    from claude_hub.services.agent_stream.redaction import redact_event
    from claude_hub.services.agent_stream.registry import get_adapter_for_session
    from claude_hub.services.agent_stream.tailer import SessionTailer

    outside = tmp_path / "long-answer.png"
    outside.write_bytes(PNG_BYTES)
    session = _terminal_tab_session_or_404("tab-a")
    adapter = get_adapter_for_session(session)
    assert adapter is not None
    tailer = SessionTailer(session.workspace_id, session.id, adapter, lambda: session)
    tailer._is_live = True
    chunks = ["x" * 3000, "y" * 1500 + "\n[Open result](", str(outside) + ")"]
    base: dict[str, Any] = dict(
        stream_sequence=-1,
        tab_id="tab-a",
        session_id=session.id,
        agent_type=AgentType.CLAUDE,
        message_id="image:assistant",
        turn_id="image",
        run_epoch=1,
        created_at=datetime.now(timezone.utc),
    )
    for text in chunks:
        event = AgentStreamEvent(
            **base, type=AgentStreamEventType.TEXT_DELTA, payload={"text": text}
        )
        await tailer._persist_and_fanout(redact_event(event))
    completion = redact_event(
        AgentStreamEvent(
            **base,
            type=AgentStreamEventType.TURN_COMPLETED,
            payload={"assistant_text": "".join(chunks), "status": "completed"},
        )
    )
    assert str(outside) not in completion.payload["assistant_text"]
    await tailer._persist_and_fanout(completion)
    outside.unlink()
    assert (await _get_image(client, "tab-a", str(outside))).content == PNG_BYTES


async def test_original_bytes_survive_deletion_and_concurrent_reads_deduplicate(
    client: AsyncClient, tab_dir: Path
) -> None:
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    responses = await asyncio.gather(*[_get_image(client, "tab-a", "shot.png") for _ in range(8)])
    assert all(response.content == PNG_BYTES for response in responses)
    entries = AgentStreamAttachmentStore._read_index()
    assert len(entries) == 1
    assert entries[0]["size"] == len(PNG_BYTES)
    assert "shot.png" not in json.dumps(entries)
    (tab_dir / "shot.png").unlink()
    # Each request constructs a new store; no in-memory mapping is required.
    assert (await _get_image(client, "tab-a", str(tab_dir / "shot.png"))).content == PNG_BYTES


async def test_overwritten_image_is_not_frozen_at_its_first_cached_version(
    client: AsyncClient, tab_dir: Path
) -> None:
    assert (await _get_image(client, "tab-a", "shot.png")).content == PNG_BYTES
    changed = PNG_BYTES + b"changed original bytes"
    (tab_dir / "shot.png").write_bytes(changed)
    assert (await _get_image(client, "tab-a", "shot.png")).content == changed
    (tab_dir / "shot.png").unlink()
    assert (await _get_image(client, "tab-a", "shot.png")).content == changed


async def test_image_over_cache_cap_stays_original_without_growing_storage(
    client: AsyncClient, tab_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.config import settings
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    monkeypatch.setattr(settings, "attachment_max_preview_bytes", len(PNG_BYTES) - 1)
    assert (await _get_image(client, "tab-a", "shot.png")).content == PNG_BYTES
    assert AgentStreamAttachmentStore._read_index() == []


async def test_uncacheable_replacement_cannot_resurrect_the_previous_image(
    client: AsyncClient, tab_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.config import settings

    assert (await _get_image(client, "tab-a", "shot.png")).content == PNG_BYTES
    monkeypatch.setattr(settings, "attachment_max_preview_bytes", len(PNG_BYTES))
    replacement = PNG_BYTES + b"oversized replacement"
    (tab_dir / "shot.png").write_bytes(replacement)
    assert (await _get_image(client, "tab-a", "shot.png")).content == replacement
    (tab_dir / "shot.png").unlink()
    assert (await _get_image(client, "tab-a", "shot.png")).status_code == 404


async def test_concurrent_replacement_retains_the_newer_source(
    client: AsyncClient, tab_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.services.agent_stream import local_images

    original_reader = local_images._read_referenced_image
    first_read = threading.Event()
    release = threading.Event()

    def delayed_first(session, raw_path):
        result = original_reader(session, raw_path)
        if not first_read.is_set():
            first_read.set()
            assert release.wait(5), "first read was not released"
        return result

    monkeypatch.setattr(local_images, "_read_referenced_image", delayed_first)
    first = asyncio.create_task(_get_image(client, "tab-a", "shot.png"))
    second = None
    try:
        assert await asyncio.to_thread(first_read.wait, 5)
        newer = PNG_BYTES + b"newer source contents"
        (tab_dir / "shot.png").write_bytes(newer)
        second = asyncio.create_task(_get_image(client, "tab-a", "shot.png"))
        # An unlocked implementation publishes B while A is suspended, then
        # incorrectly replaces B with A as soon as the first reader resumes.
        await asyncio.wait({second}, timeout=0.1)
    finally:
        release.set()
    assert second is not None
    await asyncio.gather(first, second)
    (tab_dir / "shot.png").unlink()
    assert (await _get_image(client, "tab-a", "shot.png")).content == newer


@pytest.mark.parametrize("valid_source", [True, False])
async def test_cache_and_cleanup_failure_do_not_mask_the_source_response(
    client: AsyncClient, tab_dir: Path, monkeypatch: pytest.MonkeyPatch, valid_source: bool
) -> None:
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    async def storage_failure(*args, **kwargs):
        raise OSError("cache storage unavailable")

    monkeypatch.setattr(AgentStreamAttachmentStore, "save", storage_failure)
    monkeypatch.setattr(AgentStreamAttachmentStore, "discard_source", storage_failure)
    if not valid_source:
        (tab_dir / "shot.png").write_bytes(b"not an image")
    response = await _get_image(client, "tab-a", "shot.png")
    assert response.status_code == (200 if valid_source else 404)
    if valid_source:
        assert response.content == PNG_BYTES


async def test_valid_absolute_image_returns_bytes_and_content_type(
    client: AsyncClient, tab_dir: Path
) -> None:
    resp = await _get_image(client, "tab-a", str(tab_dir / "shot.png"))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.content == PNG_BYTES


async def test_valid_cwd_relative_image_is_anchored_at_cwd(
    client: AsyncClient, tab_dir: Path
) -> None:
    resp = await _get_image(client, "tab-a", "shot.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


async def test_relative_subdir_path_is_allowed(client: AsyncClient, tab_dir: Path) -> None:
    nested = tab_dir / "tasks" / "run"
    nested.mkdir(parents=True)
    (nested / "g.png").write_bytes(PNG_BYTES)
    resp = await _get_image(client, "tab-a", os.path.join("tasks", "run", "g.png"))
    assert resp.status_code == 200


async def test_jpeg_sniff_content_type(client: AsyncClient, tab_dir: Path) -> None:
    (tab_dir / "pic.jpg").write_bytes(JPEG_BYTES)
    resp = await _get_image(client, "tab-a", "pic.jpg")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"


async def test_parent_traversal_is_denied(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG_BYTES)
    # From cwd, climb out then reference a sibling image.
    resp = await _get_image(client, "tab-a", f"../{secret.name}")
    assert resp.status_code == 404
    assert resp.headers["x-content-type-options"] == "nosniff"


async def test_absolute_path_outside_cwd_is_denied(client: AsyncClient, tab_dir: Path) -> None:
    resp = await _get_image(client, "tab-a", "/etc/passwd")
    assert resp.status_code == 404


async def test_absolute_image_outside_cwd_is_denied(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    resp = await _get_image(client, "tab-a", str(outside))
    assert resp.status_code == 404


async def test_symlink_to_file_outside_cwd_is_denied(
    client: AsyncClient, tab_dir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "target.png"
    outside.write_bytes(PNG_BYTES)
    link = tab_dir / "link.png"
    os.symlink(outside, link)
    resp = await _get_image(client, "tab-a", str(link))
    assert resp.status_code == 404


async def test_symlink_to_etc_passwd_is_denied(client: AsyncClient, tab_dir: Path) -> None:
    link = tab_dir / "escape"
    os.symlink("/etc/passwd", link)
    resp = await _get_image(client, "tab-a", str(link))
    assert resp.status_code == 404


async def test_non_image_file_under_cwd_is_denied(client: AsyncClient, tab_dir: Path) -> None:
    (tab_dir / "notes.txt").write_bytes(b"this is plain text, not an image")
    resp = await _get_image(client, "tab-a", "notes.txt")
    assert resp.status_code == 404


async def test_image_extension_with_non_image_bytes_is_denied(
    client: AsyncClient, tab_dir: Path
) -> None:
    (tab_dir / "lying.png").write_bytes(b"not really a png")
    resp = await _get_image(client, "tab-a", "lying.png")
    assert resp.status_code == 404


async def test_missing_file_is_404(client: AsyncClient, tab_dir: Path) -> None:
    resp = await _get_image(client, "tab-a", "does-not-exist.png")
    assert resp.status_code == 404


async def test_empty_path_is_404(client: AsyncClient, tab_dir: Path) -> None:
    resp = await _get_image(client, "tab-a", "   ")
    assert resp.status_code == 404


async def test_unknown_tab_is_404(client: AsyncClient, tab_dir: Path) -> None:
    resp = await _get_image(client, "tab-other", "shot.png")
    assert resp.status_code == 404


async def test_cross_tab_cwd_is_out_of_scope(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An image that lives in a different tab's cwd is not readable here."""
    from claude_hub.api import agent_stream as agent_stream_api

    cwd_a = tmp_path / "tabA"
    cwd_b = tmp_path / "tabB"
    cwd_a.mkdir()
    cwd_b.mkdir()
    (cwd_b / "b.png").write_bytes(PNG_BYTES)

    def get_tab(tab_id: str) -> Optional[SimpleNamespace]:
        if tab_id == "tab-a":
            return _make_tab("tab-a", str(cwd_a))
        if tab_id == "tab-b":
            return _make_tab("tab-b", str(cwd_b))
        return None

    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", get_tab)

    # tab-a must not read tab-b's file even with its absolute path.
    resp = await _get_image(client, "tab-a", str(cwd_b / "b.png"))
    assert resp.status_code == 404
    # tab-b can read its own file — proving the 404 was scope, not a bad file.
    resp_own = await _get_image(client, "tab-b", "b.png")
    assert resp_own.status_code == 200


async def test_remote_session_cannot_read(
    client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api

    cwd = tmp_path / "remote-ws"
    cwd.mkdir()
    (cwd / "r.png").write_bytes(PNG_BYTES)
    tab = _make_tab("tab-r", str(cwd), target=ExecutionTarget.REMOTE)
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", lambda tab_id: tab)
    resp = await _get_image(client, "tab-r", "r.png")
    assert resp.status_code == 404


def _managed_image_session(workspace_id: str, cwd: Path, *, created_at: datetime) -> ManagedSession:
    return ManagedSession(
        id="img-agent-1",
        workspace_id=workspace_id,
        tab_id="managed-image-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
        status=ManagedSessionStatus.IDLE,
        runtime_status=AgentRuntimeStatus.IDLE,
        title="Managed image",
        workspace_path=str(cwd),
        tmux_session="",
        target=ExecutionTarget.LOCAL,
        created_at=created_at,
        updated_at=created_at,
    )


@pytest.mark.asyncio
async def test_managed_image_response_is_no_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services.workspace_manager import WorkspaceManager

    cwd = tmp_path / "managed"
    cwd.mkdir()
    (cwd / "managed.png").write_bytes(PNG_BYTES)
    manager = WorkspaceManager()
    session = _managed_image_session("managed-images", cwd, created_at=datetime.now(timezone.utc))
    manager.sessions[session.id] = session
    monkeypatch.setattr(agent_stream_api, "workspace_manager", manager)

    response = await agent_stream_api.get_session_agent_image(
        session.id,
        "managed.png",
        User(open_id="image-user", name="Image User", email="image@example.test"),
    )

    assert response.body == PNG_BYTES
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_managed_image_read_cannot_commit_after_delete_and_allocator_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services import agent_stream as agent_stream_service
    from claude_hub.services.agent_stream import local_images
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore
    from claude_hub.services.ttyd_manager import ttyd_manager
    from claude_hub.services.workspace_manager import WorkspaceManager

    cwd = tmp_path / "managed-race"
    cwd.mkdir()
    image = cwd / "managed.png"
    image.write_bytes(PNG_BYTES)
    manager = WorkspaceManager()
    monkeypatch.setattr(manager, "_save_state", lambda: None)
    monkeypatch.setattr(manager, "_build_session_bootstrap_prompt", lambda *_args: "")
    workspace = manager.create_workspace(
        WorkspaceCreate(name="Images", path=str(cwd), session_prefix="img")
    )
    old = _managed_image_session(workspace.id, cwd, created_at=datetime.now(timezone.utc))
    manager.sessions[old.id] = old
    monkeypatch.setattr(agent_stream_api, "workspace_manager", manager)

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_read = local_images._read_referenced_image

    def delayed_read(session: ManagedSession, raw_path: str):
        try:
            result = original_read(session, raw_path)
            entered.set()
            assert release.wait(5), "blocked image read was not released"
            return result
        finally:
            finished.set()

    async def delete_tab(_tab_id: str) -> bool:
        return True

    tab_counter = 0

    async def create_tab(**kwargs: Any) -> TerminalTab:
        nonlocal tab_counter
        tab_counter += 1
        now = datetime.now(timezone.utc)
        return TerminalTab(
            id=f"fresh-image-tab-{tab_counter}",
            name=str(kwargs["name"]),
            cwd=str(cwd),
            solo_mode=True,
            agent_type=kwargs["agent_type"],
            session_kind=kwargs["session_kind"],
            target=ExecutionTarget.LOCAL,
            port=19010 + tab_counter,
            created_at=now,
            is_active=True,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
            workspace_role=kwargs["workspace_role"],
            managed_session_id=kwargs["managed_session_id"],
        )

    async def fence_session(_session_id: str) -> bool:
        return True

    monkeypatch.setattr(local_images, "_read_referenced_image", delayed_read)
    monkeypatch.setattr(ttyd_manager, "delete_tab", delete_tab)
    monkeypatch.setattr(ttyd_manager, "create_tab", create_tab)
    monkeypatch.setattr(ttyd_manager, "list_tabs", lambda: [])
    monkeypatch.setattr(ttyd_manager, "list_archived_tabs", lambda: [])
    monkeypatch.setattr(agent_stream_service, "fence_session_stream", fence_session)
    user = User(open_id="image-user", name="Image User", email="image@example.test")
    request = asyncio.create_task(
        agent_stream_api.get_session_agent_image(old.id, "managed.png", user)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await manager.delete_session(old.id)
        assert not AgentStreamAttachmentStore(old.workspace_id, old.id).has_residual_state()
        fresh = await manager.ensure_workspace_agent(
            workspace.id,
            EnsureWorkspaceAgentRequest(
                role=WorkspaceSessionRole.WORKER,
                agent_type=AgentType.CLAUDE,
                session_kind=SessionKind.CHAT,
                reuse_existing=False,
            ),
        )
        assert fresh.id == old.id
        image.unlink()
        release.set()
        with pytest.raises(HTTPException) as stale_error:
            await request
        assert stale_error.value.status_code == 404
        with pytest.raises(HTTPException) as fresh_error:
            await agent_stream_api.get_session_agent_image(fresh.id, "managed.png", user)
        assert fresh_error.value.status_code == 404
        assert not AgentStreamAttachmentStore(fresh.workspace_id, fresh.id).has_residual_state()
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 5)
        if not request.done():
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request


@pytest.mark.asyncio
async def test_direct_image_read_cannot_leave_cache_after_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services.agent_stream import local_images
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore
    from claude_hub.services.ttyd_manager import TTYDManager, TTYDProcess

    cwd = tmp_path / "direct-race"
    cwd.mkdir()
    image = cwd / "direct.png"
    image.write_bytes(PNG_BYTES)
    created_at = datetime.now(timezone.utc)
    process = TTYDProcess(
        tab_id="direct-delete-race",
        port=19020,
        name="Direct image",
        cwd=str(cwd),
        created_at=created_at,
        agent_type=AgentType.CLAUDE,
        session_kind=SessionKind.CHAT,
    )
    process.stop = AsyncMock()  # type: ignore[method-assign]
    tabs = TTYDManager()
    tabs.processes[process.tab_id] = process
    tabs._tab_order = [process.tab_id]
    monkeypatch.setattr(tabs, "_save_state", lambda: None)
    monkeypatch.setattr(tabs, "_save_order", lambda: None)
    monkeypatch.setattr(agent_stream_api, "ttyd_manager", tabs)

    entered = threading.Event()
    release = threading.Event()
    original_read = local_images._read_referenced_image

    def delayed_read(session: ManagedSession, raw_path: str):
        result = original_read(session, raw_path)
        entered.set()
        assert release.wait(5), "blocked image read was not released"
        return result

    monkeypatch.setattr(local_images, "_read_referenced_image", delayed_read)
    user = User(open_id="image-user", name="Image User", email="image@example.test")
    request = asyncio.create_task(
        agent_stream_api.get_tab_agent_image(process.tab_id, "direct.png", user)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert await tabs.delete_tab(process.tab_id)
        image.unlink()
        release.set()
        with pytest.raises(HTTPException) as stale_error:
            await request
        assert stale_error.value.status_code == 404
        store = AgentStreamAttachmentStore("terminal-tabs", f"terminal-tab-{process.tab_id}")
        assert not store.has_residual_state()
    finally:
        release.set()
        if not request.done():
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request


@pytest.mark.asyncio
async def test_cancelled_image_read_cannot_publish_after_background_read_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_hub.api import agent_stream as agent_stream_api
    from claude_hub.services.agent_stream import local_images
    from claude_hub.services.agent_stream.attachments import AgentStreamAttachmentStore

    cwd = tmp_path / "cancelled-read"
    cwd.mkdir()
    image = cwd / "cancelled.png"
    image.write_bytes(PNG_BYTES)
    tab = _make_tab("cancelled-image-tab", str(cwd))
    current = {tab.id: tab}
    monkeypatch.setattr(agent_stream_api.ttyd_manager, "get_tab", current.get)

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_read = local_images._read_referenced_image

    def delayed_read(session: ManagedSession, raw_path: str):
        try:
            result = original_read(session, raw_path)
            entered.set()
            assert release.wait(5), "blocked image read was not released"
            return result
        finally:
            finished.set()

    monkeypatch.setattr(local_images, "_read_referenced_image", delayed_read)
    request = asyncio.create_task(
        agent_stream_api.get_tab_agent_image(
            tab.id,
            "cancelled.png",
            User(open_id="image-user", name="Image User", email="image@example.test"),
        )
    )
    assert await asyncio.to_thread(entered.wait, 5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    current.pop(tab.id)
    store = AgentStreamAttachmentStore("terminal-tabs", f"terminal-tab-{tab.id}")
    await store.clear()
    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    await asyncio.sleep(0)
    assert not store.has_residual_state()


@pytest.mark.parametrize("blocked_phase", ["append", "capture"])
@pytest.mark.asyncio
async def test_tailer_image_capture_revalidates_session_incarnation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_phase: str,
) -> None:
    from claude_hub.services.agent_stream import tailer as tailer_module
    from claude_hub.services.agent_stream.base import AgentStreamAdapter
    from claude_hub.services.agent_stream.store import AgentStreamStore
    from claude_hub.services.agent_stream.tailer import SessionTailer, StructuredSourceUnavailable

    now = datetime.now(timezone.utc)
    session = _managed_image_session("tailer-image-workspace", tmp_path, created_at=now)
    session.id = f"tailer-image-{blocked_phase}"
    sessions = {session.id: session}
    phase_entered = asyncio.Event()
    release_phase = asyncio.Event()
    cache_calls: list[tuple[str, list[str]]] = []
    fanned_out: list[AgentStreamEvent] = []

    class ControlledStore:
        cursor_path = tmp_path / "cursor.json"

        async def append(self, event: AgentStreamEvent) -> AgentStreamEvent:
            if blocked_phase == "append":
                phase_entered.set()
                await release_phase.wait()
            return event.model_copy(update={"stream_sequence": 1})

    async def controlled_cache(
        live: ManagedSession,
        paths: list[str],
        *,
        owner_is_current=None,
    ) -> None:
        cache_calls.append((live.id, paths))
        if blocked_phase == "capture":
            phase_entered.set()
            await release_phase.wait()

    monkeypatch.setattr(tailer_module, "cache_image_paths", controlled_cache)
    tailer = SessionTailer(
        session.workspace_id,
        session.id,
        cast(AgentStreamAdapter, SimpleNamespace()),
        lambda: sessions.get(session.id),
        store=cast(AgentStreamStore, ControlledStore()),
    )
    tailer._is_live = True
    monkeypatch.setattr(tailer, "_fanout", fanned_out.append)
    event = AgentStreamEvent(
        stream_sequence=-1,
        session_id=session.id,
        tab_id=session.tab_id,
        agent_type=session.agent_type,
        type=AgentStreamEventType.TEXT_DELTA,
        turn_id="image-turn",
        message_id="image-turn:assistant",
        run_epoch=1,
        payload={"role": "assistant", "text": "[result](image.png)"},
        created_at=now,
    )
    publishing = asyncio.create_task(tailer._persist_and_fanout(event))
    await asyncio.wait_for(phase_entered.wait(), timeout=1)
    sessions[session.id] = session.model_copy(
        update={
            "tab_id": "replacement-tab",
            "created_at": now + timedelta(seconds=1),
        }
    )
    release_phase.set()

    with pytest.raises(StructuredSourceUnavailable, match="deleted or replaced"):
        await asyncio.wait_for(publishing, timeout=1)
    assert fanned_out == []
    assert len(cache_calls) == (1 if blocked_phase == "capture" else 0)
