"""REST API for Dot, the persistent personal agent (see ``docs/DOT.md``)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..auth.dependencies import get_current_user
from ..config import settings
from ..models.schemas import User
from ..services.dot import Card, DotService, get_dot_service
from ..services.dot.service import DelegateNotStarted, LessonNotFound
from ..services.dot.store import LESSON_RULE_LIMIT
from ..services.ttyd_manager import TabLimitExceededError
from ..services.workspace_manager._task_execution import TaskExecutionConflict

router = APIRouter(prefix="/api/dot", tags=["dot"])

CardKind = Literal["user", "work", "knowledge", "concept"]


class CardWrite(BaseModel):
    model_config = {"extra": "forbid"}

    id: Optional[str] = Field(default=None, max_length=128)
    kind: CardKind = "knowledge"
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(default="", max_length=1000)
    body: str = Field(default="", max_length=200_000)
    tags: List[str] = Field(default_factory=list, max_length=32)
    importance: int = Field(default=2, ge=1, le=5)
    links: List[str] = Field(default_factory=list, max_length=64)
    files: List[str] = Field(default_factory=list, max_length=32)
    source: str = Field(default="", max_length=500)


class SetupRequest(BaseModel):
    """Provider launch env for Dot's Chat tab, like a normal new Chat."""

    model_config = {"extra": "forbid"}

    env: Optional[Dict[str, str]] = None
    env_preset: Optional[str] = Field(default=None, max_length=200)


class FileUpload(BaseModel):
    model_config = {"extra": "forbid"}

    filename: str = Field(min_length=1, max_length=255)
    data_base64: str


class FocusCreate(BaseModel):
    model_config = {"extra": "forbid"}

    title: str = Field(min_length=1, max_length=300)
    why: str = Field(default="", max_length=1000)
    priority: int = Field(default=3, ge=1, le=5)


class FocusUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    why: Optional[str] = Field(default=None, max_length=1000)
    priority: Optional[int] = Field(default=None, ge=1, le=5)
    status: Optional[Literal["active", "done", "dropped"]] = None


class DelegateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    workspace_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=8000)
    # One verifiable check per item; the worker answers each by number.
    done_when: List[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        min_length=1, max_length=20
    )
    output: str = Field(default="", max_length=4000)
    context: str = Field(default="", max_length=20_000)
    out_of_scope: str = Field(default="", max_length=4000)
    constraints: str = Field(default="", max_length=4000)
    agent_type: Literal["codex", "claude", "cursor", "traex"] = "codex"
    task_mode: Optional[Literal["direct", "reviewed", "autonomous"]] = None
    start: bool = True
    request_key: Optional[str] = Field(default=None, min_length=1, max_length=128)
    stall_hours: Optional[float] = Field(default=None, gt=0, le=720)
    # Send even when the brief check has warnings.
    confirm: bool = False


class ReviewRequest(BaseModel):
    model_config = {"extra": "forbid"}

    verdict: Literal["ok", "redo", "skip"]
    cause: Optional[Literal["brief", "env", "worker", "change"]] = None
    note: str = Field(default="", max_length=2000)
    lesson: str = Field(default="", max_length=LESSON_RULE_LIMIT)
    lesson_id: Optional[str] = Field(default=None, min_length=1, max_length=64)


class LessonWrite(BaseModel):
    model_config = {"extra": "forbid"}

    rule: str = Field(min_length=1, max_length=LESSON_RULE_LIMIT)


class LessonMerge(BaseModel):
    model_config = {"extra": "forbid"}

    keep: str = Field(min_length=1, max_length=64)
    others: List[str] = Field(min_length=1, max_length=20)


class WatchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    stall_hours: Optional[float] = Field(default=None, gt=0, le=720)


class NotifyRequest(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=2000)


class IdentityUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    name: Optional[str] = Field(default=None, max_length=200)
    avatar: Optional[str] = Field(default=None, max_length=64)


class AvatarUpload(BaseModel):
    model_config = {"extra": "forbid"}

    data_base64: str = Field(max_length=3_000_000)


class InboxAck(BaseModel):
    model_config = {"extra": "forbid"}

    ids: Optional[List[str]] = None


def _service() -> DotService:
    return get_dot_service()


async def _commit(service: DotService, message: str) -> None:
    await asyncio.to_thread(service.store.commit, message)


def _actor_key(user: User) -> str:
    return hashlib.sha256(
        json.dumps(
            {"user": user.open_id, "oauth_app": settings.feishu_app_id or ""}, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _decode_base64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="data_base64 is not valid base64") from None


@router.get("/status")
async def dot_status(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    return _service().status()


@router.post("/setup")
async def dot_setup(
    payload: Optional[SetupRequest] = None,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Create the memory home, the Dot Chat tab and its schedules (idempotent)."""
    from ..models.schemas import SwitchEnvRequest
    from ..services.env_preset_resolver import merge_env_with_preset
    from .tabs import switch_tab_env

    service = _service()
    try:
        env = merge_env_with_preset(
            preset=payload.env_preset if payload else None,
            explicit_env=dict(payload.env or {}) if payload else {},
        )
        tab_id = await service.ensure_tab(env=env)
        if service.env_seed_needed(env):
            # Same path as the tab env switcher, so a live Chat picks it up too.
            await switch_tab_env(tab_id, SwitchEnvRequest(env=env), current_user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except TabLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    return service.status()


@router.get("/identity")
async def dot_identity(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    return _service().store.read_identity()


@router.patch("/identity")
async def dot_update_identity(
    payload: IdentityUpdate, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    try:
        return await _service().update_identity(name=payload.name, avatar=payload.avatar)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/identity/avatar")
async def dot_upload_avatar(
    payload: AvatarUpload, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    data = _decode_base64(payload.data_base64)
    service = _service()
    try:
        path = service.store.store_avatar(data)
        return await service.update_identity(avatar_image=path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.delete("/identity/avatar")
async def dot_clear_avatar(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    return await _service().update_identity(avatar_image="")


@router.post("/rotate")
async def dot_rotate(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    _service().request_rotation()
    return {"rotate_requested": True}


@router.post("/notify")
async def dot_notify(
    payload: NotifyRequest, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    if not payload.text.strip():
        raise HTTPException(status_code=422, detail="text is empty")
    if await _service().notify(payload.text):
        return {"sent": True}
    return {
        "sent": False,
        "reason": "No Feishu bot is bound to the Dot Chat, or Feishu refused the message.",
    }


@router.get("/health")
async def dot_health(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    return await asyncio.to_thread(_service().health)


# ── memory ──────────────────────────────────────────────────────────────


@router.get("/recall")
async def dot_recall(
    q: str = Query(..., min_length=1, max_length=2000),
    kind: Optional[CardKind] = None,
    limit: int = Query(default=8, ge=1, le=50),
    include_archived: bool = False,
    current_user: User = Depends(get_current_user),
) -> List[dict[str, Any]]:
    hits = await asyncio.to_thread(
        _service().index.search, q, kind=kind, limit=limit, include_archived=include_archived
    )
    return [hit.__dict__ for hit in hits]


@router.get("/cards")
async def dot_list_cards(
    kind: Optional[CardKind] = None,
    include_archived: bool = False,
    current_user: User = Depends(get_current_user),
) -> List[dict[str, Any]]:
    cards = _service().store.list_cards(kind, include_archived=include_archived)
    cards.sort(key=lambda card: card.updated, reverse=True)
    return [card.to_dict(with_body=False) for card in cards]


@router.get("/cards/{card_id}")
async def dot_get_card(
    card_id: str, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    card = _service().store.get_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return card.to_dict()


@router.post("/cards")
async def dot_save_card(
    payload: CardWrite, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    service = _service()
    card_id = (payload.id or "").strip()
    existing = service.store.get_card(card_id) if card_id else None
    if card_id and existing is None:
        raise HTTPException(status_code=404, detail="Card not found")
    # Only new links and files are checked: one whose target was deleted since
    # must not lock the card against every later edit.
    for link in payload.links:
        if existing and link in existing.links:
            continue
        if service.store.get_card(link) is None:
            raise HTTPException(status_code=400, detail=f"Linked card not found: {link}")
    for path in payload.files:
        if existing and path in existing.files:
            continue
        if service.store.resolve_home_path(path) is None:
            raise HTTPException(status_code=400, detail=f"File not found in Dot home: {path}")
    card = Card(
        id=card_id,
        kind=payload.kind,
        title=payload.title,
        summary=payload.summary.strip(),
        body=payload.body,
        tags=[tag.strip() for tag in payload.tags if tag.strip()],
        importance=payload.importance,
        links=payload.links,
        files=payload.files,
        source=payload.source,
        created=existing.created if existing else "",
        # An update keeps the card archived; it is not a way to bring it back.
        archived=existing.archived if existing else False,
    )
    try:
        saved = service.store.save_card(card)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await _commit(service, f"dot: {'update' if existing else 'remember'} {saved.id}")
    return saved.to_dict()


@router.delete("/cards/{card_id}")
async def dot_forget_card(
    card_id: str,
    hard: bool = False,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    service = _service()
    if hard:
        if not service.store.delete_card(card_id):
            raise HTTPException(status_code=404, detail="Card not found")
    elif service.store.archive_card(card_id) is None:
        raise HTTPException(status_code=404, detail="Card not found")
    await _commit(service, f"dot: {'delete' if hard else 'archive'} {card_id}")
    return {"id": card_id, "deleted": hard, "archived": not hard}


@router.post("/files")
async def dot_upload_file(
    payload: FileUpload, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    data = _decode_base64(payload.data_base64)
    service = _service()
    try:
        path = service.store.store_file(data, payload.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"path": path, "bytes": len(data)}


_INLINE_FILE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


@router.get("/files/{relative_path:path}")
async def dot_get_file(
    relative_path: str, current_user: User = Depends(get_current_user)
) -> FileResponse:
    service = _service()
    path = service.store.resolve_file(f"files/{relative_path}")
    if path is None:
        raise HTTPException(status_code=404, detail="File not found")
    media_type = service.store.guess_mime(path)
    # Only raster images render inline; anything else (HTML, SVG, ...) is a
    # download, so a stored file can never run script on the Hub origin.
    inline = media_type in _INLINE_FILE_TYPES
    return FileResponse(
        path,
        media_type=media_type if inline else "application/octet-stream",
        filename=path.name,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/journal")
async def dot_journal(
    day: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    store = _service().store
    days = store.journal_days()
    chosen = day or (days[-1] if days else None)
    text = ""
    if chosen:
        try:
            # A write cut off mid-character must not hide the whole day.
            text = store.journal_path(chosen).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    return {"days": days, "day": chosen, "text": text}


# ── focus / work / inbox ────────────────────────────────────────────────


@router.get("/focus")
async def dot_list_focus(
    include_all: bool = Query(default=False, alias="all"),
    current_user: User = Depends(get_current_user),
) -> List[dict[str, Any]]:
    return _service().store.list_focus(include_closed=include_all)


@router.post("/focus")
async def dot_add_focus(
    payload: FocusCreate, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    if not payload.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    service = _service()
    try:
        item = service.store.add_focus(payload.title, payload.why, payload.priority)
    except ValueError as exc:
        # The only other refusal is a full focus list.
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await _commit(service, f"dot: focus + {item['id']}")
    return item


@router.patch("/focus/{focus_id}")
async def dot_update_focus(
    focus_id: str, payload: FocusUpdate, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    service = _service()
    try:
        item = service.store.update_focus(focus_id, **payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if item is None:
        raise HTTPException(status_code=404, detail="Focus item not found")
    await _commit(service, f"dot: focus ~ {focus_id}")
    return item


@router.get("/work")
async def dot_list_work(
    include_all: bool = Query(default=False, alias="all"),
    current_user: User = Depends(get_current_user),
) -> List[dict[str, Any]]:
    return _service().store.list_work(include_closed=include_all)


@router.post("/delegate")
async def dot_delegate(
    payload: DelegateRequest, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    try:
        return await _service().delegate(**payload.model_dump(), actor_key=_actor_key(current_user))
    except DelegateNotStarted as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except TaskExecutionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except KeyError:
        raise HTTPException(status_code=404, detail="Workspace not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "invalid_delegate") from None


@router.post("/work/{task_id}/review")
async def dot_review(
    task_id: str, payload: ReviewRequest, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    try:
        return await _service().review(task_id, **payload.model_dump())
    except LessonNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Lesson not found: {exc.args[0]}") from None
    except KeyError:
        raise HTTPException(status_code=404, detail="Dot is not tracking this task") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


# ── lessons ─────────────────────────────────────────────────────────────


@router.get("/lessons")
async def dot_lessons(
    include_all: bool = Query(default=False, alias="all"),
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    service = _service()
    return {
        "lessons": service.lessons(include_dropped=include_all),
        "stats": service.review_stats(),
    }


@router.post("/lessons")
async def dot_add_lesson(
    payload: LessonWrite, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    service = _service()
    try:
        lesson = service.store.add_lesson(payload.rule)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await _commit(service, f"dot: lesson + {lesson['id']}")
    return lesson


@router.patch("/lessons/{lesson_id}")
async def dot_edit_lesson(
    lesson_id: str, payload: LessonWrite, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    service = _service()
    try:
        lesson = service.store.update_lesson(lesson_id, rule=payload.rule)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    await _commit(service, f"dot: lesson ~ {lesson_id}")
    return lesson


@router.delete("/lessons/{lesson_id}")
async def dot_drop_lesson(
    lesson_id: str, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    service = _service()
    lesson = service.store.update_lesson(lesson_id, dropped=True)
    if lesson is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    await _commit(service, f"dot: lesson - {lesson_id}")
    return lesson


@router.post("/lessons/merge")
async def dot_merge_lessons(
    payload: LessonMerge, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    service = _service()
    try:
        lesson = service.store.merge_lessons(payload.keep, payload.others)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Lesson not found: {exc.args[0]}") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    await _commit(service, f"dot: merge lessons into {payload.keep}")
    return lesson


@router.post("/work/{task_id}/watch")
async def dot_watch(
    task_id: str,
    payload: Optional[WatchRequest] = None,
    current_user: User = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await _service().watch(task_id, stall_hours=payload.stall_hours if payload else None)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/work/{task_id}/unwatch")
async def dot_unwatch(
    task_id: str, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    item = await _service().unwatch(task_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Dot is not tracking this task")
    return item


@router.get("/inbox")
async def dot_inbox(
    include_all: bool = Query(default=False, alias="all"),
    current_user: User = Depends(get_current_user),
) -> List[dict[str, Any]]:
    return _service().store.list_inbox(unread_only=not include_all)


@router.post("/inbox/ack")
async def dot_inbox_ack(
    payload: InboxAck, current_user: User = Depends(get_current_user)
) -> dict[str, Any]:
    return {"acked": _service().store.mark_inbox_read(payload.ids)}


__all__ = ["router"]
