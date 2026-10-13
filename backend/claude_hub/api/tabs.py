import asyncio
import logging
from contextlib import suppress
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..models import (
    SwitchEnvRequest,
    TerminalAgentStatus,
    TerminalTab,
    TerminalTabCreate,
    TerminalTabUpdate,
    User,
)
from ..services import chat_sidebar_preferences, ttyd_manager
from ..services.ttyd_manager import TabLimitExceededError, TabStartupTimeoutError
from .agent_stream import _get_tab_tailer_manager, _terminal_tab_stream_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tabs", tags=["tabs"])


class TabOrderUpdate(BaseModel):
    tab_ids: List[str]


class ChatSidebarPreferencesResponse(BaseModel):
    pinned_tab_ids: List[str]


class ChatPinUpdate(BaseModel):
    pinned: bool


class LegacyBrowserPinsMigration(BaseModel):
    pinned_tab_ids: List[str]


class ForkTabRequest(BaseModel):
    """Body for forking a tab from a specific turn."""

    ordinal: int


@router.get("", response_model=List[TerminalTab])
async def list_tabs(current_user: User = Depends(get_current_user)) -> List[TerminalTab]:
    """List all terminal tabs."""
    tabs = ttyd_manager.list_tabs()
    # Attach the unread flag (computed from the event stream) so the sidebar
    # can highlight tabs with unread completed turns.
    for tab in tabs:
        process = ttyd_manager.processes.get(tab.id)
        if process is not None:
            tab.is_unread = await ttyd_manager.compute_tab_unread(process)
    return tabs


@router.get("/status", response_model=List[TerminalAgentStatus])
async def list_tab_statuses(
    current_user: User = Depends(get_current_user),
) -> List[TerminalAgentStatus]:
    """List best-effort runtime statuses for terminal agents."""
    return await ttyd_manager.list_tab_agent_statuses()


async def _wait_for_disconnect(request: Request) -> None:
    """Wait for the ASGI disconnect emitted after the request body is consumed."""
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _rollback_create_task(create_task: asyncio.Task[TerminalTab]) -> None:
    """Cancel an in-flight create or delete its unacknowledged result."""
    if not create_task.done():
        create_task.cancel()
    try:
        created = await create_task
    except asyncio.CancelledError:
        return
    except Exception:
        # TTYDManager owns rollback for failed startup before registration.
        return
    if not await ttyd_manager.delete_tab(created.id):
        raise RuntimeError(f"newly created tab {created.id} was not registered for rollback")


async def _finish_create_rollback(create_task: asyncio.Task[TerminalTab]) -> None:
    """Finish request-owned rollback despite repeated handler cancellation."""
    rollback_task = asyncio.create_task(_rollback_create_task(create_task))
    while not rollback_task.done():
        try:
            await asyncio.shield(rollback_task)
        except asyncio.CancelledError:
            continue
    await rollback_task


@router.post("", response_model=TerminalTab, status_code=201)
async def create_tab(
    tab: TerminalTabCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Create a new terminal tab."""
    logger.info(
        f"Received create_tab request: name={tab.name}, solo_mode={tab.solo_mode}, shell={tab.shell}, cwd={tab.cwd}, agent_type={tab.agent_type}, session_kind={tab.session_kind}, target={tab.target}, remote_profile_id={tab.remote_profile_id}, agent_session_id={tab.agent_session_id}, user={current_user.email}"
    )
    create_task = asyncio.create_task(
        ttyd_manager.create_tab(
            name=tab.name,
            shell=tab.shell,
            cwd=tab.cwd,
            solo_mode=tab.solo_mode,
            agent_type=tab.agent_type,
            session_kind=tab.session_kind,
            chat_mode=tab.chat_mode,
            target=tab.target,
            remote_profile_id=tab.remote_profile_id,
            remote_cwd=tab.remote_cwd,
            remote_reconnect=tab.remote_reconnect,
            env=tab.env,
            agent_session_id=tab.agent_session_id,
        )
    )
    disconnect_task = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            {create_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if disconnect_task in done:
            await _finish_create_rollback(create_task)
            raise HTTPException(
                status_code=499, detail="Client disconnected; tab creation rolled back"
            )
        return await create_task
    except asyncio.CancelledError:
        # Server shutdown/reload can cancel the handler without producing an
        # ASGI disconnect event. The child task must not outlive its request.
        await _finish_create_rollback(create_task)
        raise
    except TabLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except TabStartupTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        disconnect_task.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect_task


@router.put("/order")
async def update_tab_order(
    order: TabOrderUpdate,
    current_user: User = Depends(get_current_user),
) -> dict[str, bool]:
    """Update the order of tabs."""
    logger.info(f"Updating tab order: {order.tab_ids}, user={current_user.email}")
    ttyd_manager.set_tab_order(order.tab_ids)
    return {"success": True}


def _valid_pinnable_tab_ids() -> set[str]:
    return {
        tab.id
        for tab in [*ttyd_manager.list_tabs(), *ttyd_manager.list_archived_tabs()]
        if tab.session_kind.value == "chat" and not tab.workspace_id
    }


@router.get("/sidebar-preferences", response_model=ChatSidebarPreferencesResponse)
async def get_chat_sidebar_preferences(
    current_user: User = Depends(get_current_user),
) -> ChatSidebarPreferencesResponse:
    """Return this server user's Chat sidebar preferences."""

    valid_tab_ids = _valid_pinnable_tab_ids()
    pinned_tab_ids = chat_sidebar_preferences.get_store().get_pinned(current_user.open_id)
    return ChatSidebarPreferencesResponse(
        pinned_tab_ids=[tab_id for tab_id in pinned_tab_ids if tab_id in valid_tab_ids]
    )


@router.post(
    "/sidebar-preferences/migrate-browser-pins",
    response_model=ChatSidebarPreferencesResponse,
)
async def migrate_browser_chat_pins(
    migration: LegacyBrowserPinsMigration,
    current_user: User = Depends(get_current_user),
) -> ChatSidebarPreferencesResponse:
    """Merge valid legacy localStorage pins once, then use only server state."""

    valid_tab_ids = _valid_pinnable_tab_ids()
    legacy_ids = [tab_id for tab_id in migration.pinned_tab_ids if tab_id in valid_tab_ids]
    pinned_tab_ids = chat_sidebar_preferences.get_store().migrate_browser_pins(
        current_user.open_id, legacy_ids
    )
    return ChatSidebarPreferencesResponse(
        pinned_tab_ids=[tab_id for tab_id in pinned_tab_ids if tab_id in valid_tab_ids]
    )


@router.put(
    "/sidebar-preferences/pins/{tab_id}",
    response_model=ChatSidebarPreferencesResponse,
)
async def update_chat_pin(
    tab_id: str,
    update: ChatPinUpdate,
    current_user: User = Depends(get_current_user),
) -> ChatSidebarPreferencesResponse:
    """Idempotently pin or unpin one Chat without whole-list lost updates."""

    valid_tab_ids = _valid_pinnable_tab_ids()
    if update.pinned and tab_id not in valid_tab_ids:
        raise HTTPException(status_code=404, detail="Chat tab not found")
    pinned_tab_ids = chat_sidebar_preferences.get_store().set_pinned(
        current_user.open_id, tab_id, update.pinned
    )
    return ChatSidebarPreferencesResponse(
        pinned_tab_ids=[value for value in pinned_tab_ids if value in valid_tab_ids]
    )


@router.post("/{tab_id}/duplicate", response_model=TerminalTab, status_code=201)
async def duplicate_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Duplicate a terminal tab, preserving launch configuration like solo mode.

    Returns 404 if the source tab is missing and 429 at the live tab limit.
    """
    logger.info(f"Duplicating tab {tab_id}, user={current_user.email}")
    try:
        tab = await ttyd_manager.duplicate_tab(tab_id)
    except TabLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except TabStartupTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.post("/{tab_id}/fork", response_model=TerminalTab, status_code=201)
async def fork_tab(
    tab_id: str,
    req: ForkTabRequest,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Fork a new tab from a specific turn (0-based ordinal, inclusive).

    The forked tab copies the source's structured history up to and including
    the given turn and starts a fresh provider conversation with the same
    launch configuration. Returns 404 if the source tab is missing, 400 if
    the ordinal is out of range, and 429 if the source tab's fork cap is
    reached.
    """
    logger.info(f"Forking tab {tab_id} at ordinal {req.ordinal}, user={current_user.email}")
    try:
        tab = await ttyd_manager.fork_tab(tab_id, req.ordinal)
    except TabLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.post("/{tab_id}/switch-env", response_model=TerminalTab)
async def switch_tab_env(
    tab_id: str,
    req: SwitchEnvRequest,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Hot-swap environment variables and/or solo mode for a live local Claude/Codex tab.

    Rewrites the launch wrapper (and settings.json for Claude) and uses
    ``tmux respawn-pane -k`` to relaunch the agent with resume flags so
    conversation history is preserved (``--resume`` for Claude,
    ``codex resume --last`` for Codex).
    Returns 400 for non-Claude/Codex, remote, or stopped tabs.
    """
    logger.info(
        "switch_env for tab %s: %d env vars, solo_mode=%s, user=%s",
        tab_id,
        len(req.env),
        req.solo_mode,
        current_user.email,
    )
    try:
        tab = await ttyd_manager.switch_env(tab_id, req.env, solo_mode=req.solo_mode)
    except KeyError:
        raise HTTPException(status_code=404, detail="Tab not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # For native Chat sessions the provider transport owns the subprocess and
    # reads session.env per turn. Push the updated env into the live transport
    # so the next turn picks it up without a restart.
    if tab.session_kind.value == "chat":
        session = _terminal_tab_stream_session(tab_id)
        if session is not None:
            _get_tab_tailer_manager().set_env(session, dict(tab.env))

    return tab


@router.get("/archived", response_model=List[TerminalTab])
async def list_archived_tabs(
    current_user: User = Depends(get_current_user),
) -> List[TerminalTab]:
    """List archived (soft-deleted) tabs, most recently archived first.

    Declared before ``GET /{tab_id}`` so "archived" is not captured as a tab id.
    """
    return ttyd_manager.list_archived_tabs()


@router.post("/{tab_id}/archive", response_model=TerminalTab)
async def archive_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Archive a tab: release its runtime resources but keep its history.

    The tab is hidden from the default list and its agent is stopped, but the
    JSONL conversation log is retained so it can be restored later.
    """
    logger.info(f"Archiving tab {tab_id}, user={current_user.email}")
    tab = await ttyd_manager.archive_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.post("/{tab_id}/unarchive", response_model=TerminalTab)
async def unarchive_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Restore an archived tab and best-effort restart its runtime.

    Returns 404 if the tab is missing and 429 at the live tab limit.
    """
    logger.info(f"Unarchiving tab {tab_id}, user={current_user.email}")
    try:
        tab = await ttyd_manager.unarchive_tab(tab_id)
    except TabLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.post("/{tab_id}/view", response_model=TerminalTab)
async def mark_tab_viewed(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Mark a tab as viewed (clears its unread flag)."""
    if not ttyd_manager.mark_tab_viewed(tab_id):
        raise HTTPException(status_code=404, detail="Tab not found")
    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    tab.is_unread = False
    return tab


@router.get("/{tab_id}", response_model=TerminalTab)
async def get_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Get a terminal tab by ID."""
    tab = ttyd_manager.get_tab(tab_id)
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    process = ttyd_manager.processes.get(tab_id)
    if process is not None:
        tab.is_unread = await ttyd_manager.compute_tab_unread(process)
    return tab


@router.put("/{tab_id}", response_model=TerminalTab)
async def update_tab(
    tab_id: str,
    tab_update: TerminalTabUpdate,
    current_user: User = Depends(get_current_user),
) -> TerminalTab:
    """Update a terminal tab."""
    try:
        tab = await ttyd_manager.update_tab(
            tab_id,
            tab_update.name,
            tab_update.shell,
            tab_update.cwd,
            tab_update.solo_mode,
            tab_update.agent_type,
            tab_update.target,
            tab_update.remote_profile_id,
            tab_update.remote_cwd,
            tab_update.remote_reconnect,
            tab_update.env,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not tab:
        raise HTTPException(status_code=404, detail="Tab not found")
    return tab


@router.delete("/{tab_id}", status_code=204)
async def delete_tab(
    tab_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a terminal tab."""
    success = await ttyd_manager.delete_tab(tab_id)
    if not success:
        raise HTTPException(status_code=404, detail="Tab not found")
    try:
        chat_sidebar_preferences.get_store().remove_tab(tab_id)
    except OSError:
        # The destructive operation already succeeded. A stale preference is
        # filtered from every API response, so do not misreport the deletion
        # as failed solely because best-effort preference cleanup could not be
        # persisted.
        logger.exception("Failed to remove deleted tab %s from Chat pins", tab_id)
