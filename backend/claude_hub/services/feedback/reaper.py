"""Silent ephemeral-turn Feedback Reaper runner and structured output parser.

Replaces the legacy board-visible ``WorkspaceTask(internal_kind="feedback_reaper")``
with an internal background ephemeral model turn modeled after
``services/dot/scouting.py``. No Kanban board cards are created and user-facing
workspace orchestrator sessions are never touched.

All heavy workspace/provider imports are function-scoped to avoid runtime import
cycles while keeping unit tests 100% offline via injectable ``ReaperModelCaller``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ...models import (
    AgentType,
    FeedbackLessonCreate,
    FeedbackLessonScope,
)
from .catalog import FeedbackLessonCatalogStore
from .common import FeedbackLessonValidationError

logger = logging.getLogger(__name__)

ReaperModelCaller = Callable[[str, str], Awaitable[str]]

FEEDBACK_MODEL_SETTING = "CLAUDE_HUB_FEEDBACK_MODEL"
PATROL_MODEL_SETTING = "CLAUDE_HUB_PATROL_MODEL"
_REAPER_TURN_TIMEOUT_SECONDS = 300
_LIGHTWEIGHT_MODEL_HINTS = ("flash", "mini", "lite", "small", "fast")
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_SLUG_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-.]*$")


@dataclass(frozen=True)
class ParsedReaperOutput:
    """Structured lessons and/or fallback CLI outcome extracted from a reaper turn."""

    lessons: list[dict[str, Any]]
    created_lesson_ids: list[str]
    merged_lesson_ids: list[str]
    skipped_reason: str | None


@dataclass(frozen=True)
class AppliedReaperResult:
    """Persisted lesson ids and summary outcome for a completed silent reaper turn."""

    created_lesson_ids: list[str]
    merged_lesson_ids: list[str]
    skipped_reason: str | None
    rejected_count: int = 0


def select_maso_reaper_model(
    advertised: Iterable[str],
    *,
    feedback_model: str = "",
    patrol_model: str = "",
) -> str | None:
    """Choose a public-safe MASO model from the live advertised catalog.

    Priority:
    1. Explicit ``CLAUDE_HUB_FEEDBACK_MODEL`` if advertised by the live catalog.
    2. Explicit ``CLAUDE_HUB_PATROL_MODEL`` if advertised by the live catalog.
    3. Advertised lightweight/flash ``*_reasoning_high`` model.
    4. Any advertised ``*_reasoning_high`` model.
    5. Any advertised lightweight/flash model.
    6. First advertised model alphabetically.
    """
    catalog = sorted({m.strip() for m in advertised if isinstance(m, str) and m.strip()})
    if not catalog:
        return None
    for preferred in (feedback_model.strip(), patrol_model.strip()):
        if preferred and preferred in catalog:
            return preferred

    def _rank(name: str) -> tuple[int, str]:
        low = name.lower()
        is_high = low.endswith("_reasoning_high")
        is_light = any(hint in low for hint in _LIGHTWEIGHT_MODEL_HINTS)
        if is_high and is_light:
            return (0, low)
        if is_high:
            return (1, low)
        if is_light:
            return (2, low)
        return (3, low)

    return min(catalog, key=_rank)


async def resolve_reaper_provider() -> tuple[AgentType, dict[str, str]]:
    """Resolve the model provider and launch env for a silent Feedback Reaper turn."""
    from ..agent_stream.maso_http import MasoServiceUnavailable
    from ..agent_stream.providers.spec import all_specs
    from ..cross_reviewer import _advertised_model_ids
    from ..local_runtime_settings import local_setting
    from ..maso_discovery import discover_maso_service

    feedback_pref = local_setting(FEEDBACK_MODEL_SETTING, "").strip()
    patrol_pref = local_setting(PATROL_MODEL_SETTING, "").strip()
    env_hint = (
        {"MASO_BASE_URL": os.environ["MASO_BASE_URL"]} if os.environ.get("MASO_BASE_URL") else {}
    )
    try:
        base_url, models, _default_model = await discover_maso_service(env_hint)
        advertised = _advertised_model_ids(models)
        selected = select_maso_reaper_model(
            advertised,
            feedback_model=feedback_pref,
            patrol_model=patrol_pref,
        )
        if selected:
            launch_env: dict[str, str] = {"MASO_BASE_URL": base_url}
            maso_spec = all_specs().get(AgentType.MASO)
            if maso_spec is not None and maso_spec.model_env_key:
                launch_env[maso_spec.model_env_key] = selected
            return AgentType.MASO, launch_env
    except (MasoServiceUnavailable, Exception):  # noqa: BLE001 - fall back to CLI chat
        logger.debug("feedback reaper: live MASO catalog unavailable; using CLI fallback")

    if shutil.which("codex"):
        return AgentType.CODEX, {"CODEX_REASONING_EFFORT": "low"}
    if shutil.which("claude"):
        return AgentType.CLAUDE, {}
    raise RuntimeError("No supported model provider available for silent Feedback Reaper")


def _resolve_ephemeral_host_workspace_id(workspace_id: str) -> str:
    """Pick a local workspace to host the ephemeral Chat turn (same workspace if local)."""
    from ...models import ExecutionTarget
    from ..workspace_manager import workspace_manager

    target_ws = workspace_manager.workspaces.get(workspace_id)
    if target_ws is not None and target_ws.target == ExecutionTarget.LOCAL:
        return workspace_id
    for ws in workspace_manager.list_workspaces():
        if ws.target == ExecutionTarget.LOCAL:
            return ws.id
    return workspace_id


async def _shield_archive_reaper_session(workspace_id: str, session_id: str) -> None:
    from ..workspace_manager import workspace_manager

    try:
        await asyncio.shield(workspace_manager.archive_session(workspace_id, session_id))
    except Exception:  # pragma: no cover - cleanup is best effort
        logger.warning(
            "feedback reaper: failed to archive ephemeral session %s", session_id, exc_info=True
        )


async def _run_ephemeral_reaper_turn(
    session: Any,
    prompt: str,
    *,
    timeout_seconds: int = _REAPER_TURN_TIMEOUT_SECONDS,
) -> str:
    from ...api.agent_stream import get_managed_tailer_manager
    from ...models import AgentStreamEventType
    from .. import managed_chat
    from ..agent_stream.base import is_substantive_answer
    from ..workspace_manager import workspace_manager

    manager = get_managed_tailer_manager()
    call_id = f"feedback-reaper-{session.id}"
    queue = await manager.subscribe_completion(session)
    try:
        await workspace_manager.send_chat_session_message(session.id, prompt, call_id=call_id)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await managed_chat.cancel(session, expected_turn_id=call_id)
                raise TimeoutError("Silent Feedback Reaper turn timed out")
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                await managed_chat.cancel(session, expected_turn_id=call_id)
                raise TimeoutError("Silent Feedback Reaper turn timed out") from None
            if event.turn_id != call_id:
                continue
            if event.type == AgentStreamEventType.APPROVAL_REQUIRED:
                await manager.retire_external_turn(session, expected_turn_id=call_id)
                raise RuntimeError("Silent Feedback Reaper turn unexpectedly required approval")
            if event.type == AgentStreamEventType.ERROR:
                message = event.payload.get("message")
                if isinstance(message, str) and message.strip():
                    raise RuntimeError(f"Silent Feedback Reaper turn error: {message}")
                continue
            if event.type != AgentStreamEventType.TURN_COMPLETED:
                continue
            status_value = event.payload.get("status")
            if status_value not in (None, "completed", "success"):
                error_message = event.payload.get("error_message")
                raise RuntimeError(
                    error_message
                    if isinstance(error_message, str) and error_message
                    else f"Silent Feedback Reaper turn ended with status {status_value}"
                )
            assistant_text = event.payload.get("assistant_text")
            if isinstance(assistant_text, str) and is_substantive_answer(assistant_text):
                final_text = event.payload.get("final_text")
                if isinstance(final_text, str) and is_substantive_answer(final_text):
                    return final_text.strip()
                return assistant_text.strip()
            raise RuntimeError("Silent Feedback Reaper turn completed without substantive text")
    finally:
        manager.unsubscribe_completion(session.id, queue)


async def _ephemeral_reaper_turn(workspace_id: str, prompt: str) -> str:
    from ...api.agent_stream import get_managed_tailer_manager
    from ...models import (
        ChatMode,
        EnsureWorkspaceAgentRequest,
        SessionKind,
        WorkspaceSessionRole,
    )
    from ..workspace_manager import workspace_manager

    agent_type, launch_env = await resolve_reaper_provider()
    host_workspace_id = _resolve_ephemeral_host_workspace_id(workspace_id)
    request = EnsureWorkspaceAgentRequest(
        agent_type=agent_type,
        session_kind=SessionKind.CHAT,
        role=WorkspaceSessionRole.WORKER,
        title=f"Feedback reaper: {workspace_id[:8]}",
        # CLI fallback runs non-solo / read-only so it never executes writes.
        solo_mode=(agent_type == AgentType.MASO),
        reuse_existing=False,
        ephemeral=True,
        caller_owned_ephemeral=True,
        env=launch_env,
    )
    session = await workspace_manager.ensure_workspace_agent(
        host_workspace_id,
        request,
        bootstrap=False,
    )
    try:
        if agent_type != AgentType.MASO:
            try:
                await get_managed_tailer_manager().set_mode(session, ChatMode.PLAN.value)
            except Exception:  # noqa: BLE001 - best-effort read-only plan mode
                logger.debug(
                    "feedback reaper: plan mode unavailable for %s; continuing non-solo",
                    agent_type.value,
                )
        return await _run_ephemeral_reaper_turn(session, prompt)
    finally:
        await _shield_archive_reaper_session(host_workspace_id, session.id)


async def default_reaper_model_caller(workspace_id: str, prompt: str) -> str:
    """Run one silent Feedback Reaper turn via an ephemeral Chat session."""
    last_exc: BaseException | None = None
    for attempt in (1, 2):
        try:
            return await _ephemeral_reaper_turn(workspace_id, prompt)
        except (RuntimeError, TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "silent feedback reaper turn failed workspace_id=%s attempt=%s",
                workspace_id,
                attempt,
                exc_info=True,
            )
    assert last_exc is not None
    raise last_exc


def _decode_json_candidate(text: str) -> Any | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    for match in _FENCED_BLOCK_RE.finditer(stripped):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    for index, ch in enumerate(stripped):
        if ch not in "{[":
            continue
        try:
            parsed, _end = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _extract_named_value(text: str, field: str) -> str | None:
    match = re.search(rf"{re.escape(field)}\s*[:=]\s*([^\n;|]+)", text)
    if not match:
        return None
    value = match.group(1).strip().strip('"')
    if value.lower() in {"", "none", "null", "[]"}:
        return None
    return value


def _extract_named_ids(text: str, field: str) -> list[str]:
    raw = _extract_named_value(text, field)
    if not raw:
        return []
    tokens = [item.strip() for item in re.split(r"[,\s]+", raw.strip("[] ")) if item.strip()]
    return [token for token in tokens if _SLUG_ID_RE.match(token)]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def parse_reaper_response(raw_text: str) -> ParsedReaperOutput:
    """Parse structured JSON (raw or fenced) or CLI-reported fallback output."""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("Silent Feedback Reaper returned empty output")

    decoded = _decode_json_candidate(text)
    if isinstance(decoded, list):
        lessons = [item for item in decoded if isinstance(item, dict)]
        return ParsedReaperOutput(
            lessons=lessons,
            created_lesson_ids=[],
            merged_lesson_ids=[],
            skipped_reason=None if lessons else "no_new_lessons",
        )
    if isinstance(decoded, dict):
        raw_lessons: list[Any] = []
        for candidate_key in ("lessons", "lesson_drafts", "promoted_lessons"):
            candidate_value = decoded.get(candidate_key)
            if isinstance(candidate_value, list):
                raw_lessons = candidate_value
                break
        lessons = [item for item in raw_lessons if isinstance(item, dict)]
        created_ids = [
            item
            for item in _string_list(decoded.get("created_lesson_ids"))
            if _SLUG_ID_RE.match(item)
        ]
        merged_ids = [
            item
            for item in _string_list(decoded.get("merged_lesson_ids"))
            if _SLUG_ID_RE.match(item)
        ]
        raw_skipped = decoded.get("skipped_reason")
        skipped_reason = (
            str(raw_skipped).strip()
            if isinstance(raw_skipped, str)
            and raw_skipped.strip()
            and raw_skipped.strip().lower() not in {"none", "null"}
            else None
        )
        if not lessons and not created_ids and not merged_ids and not skipped_reason:
            if any(
                key in decoded
                for key in (
                    "lessons",
                    "lesson_drafts",
                    "promoted_lessons",
                    "skipped_reason",
                    "created_lesson_ids",
                    "merged_lesson_ids",
                )
            ):
                skipped_reason = "no_new_lessons"
            else:
                raise ValueError("Silent Feedback Reaper JSON missing expected lesson fields")
        return ParsedReaperOutput(
            lessons=lessons,
            created_lesson_ids=created_ids,
            merged_lesson_ids=merged_ids,
            skipped_reason=skipped_reason,
        )

    # Fallback when the model invoked CLI/API directly and echoed key=value fields.
    created_ids = _extract_named_ids(text, "created_lesson_ids")
    merged_ids = _extract_named_ids(text, "merged_lesson_ids")
    skipped_reason = _extract_named_value(text, "skipped_reason")
    if created_ids or merged_ids or skipped_reason:
        return ParsedReaperOutput(
            lessons=[],
            created_lesson_ids=created_ids,
            merged_lesson_ids=merged_ids,
            skipped_reason=skipped_reason,
        )
    raise ValueError("Silent Feedback Reaper returned unparseable output")


def _coerce_lesson_create(raw: dict[str, Any]) -> FeedbackLessonCreate:
    title = str(raw.get("title") or "").strip() or None
    do_text = str(raw.get("do") or "").strip()
    avoid_text = str(raw.get("avoid") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    if not summary and title and do_text:
        summary = f"{title.rstrip('.')}: {do_text}"[:200]
    elif not summary and title:
        summary = title[:200]

    scope_raw = str(raw.get("scope") or "workspace").strip().lower()
    try:
        scope = FeedbackLessonScope(scope_raw)
    except ValueError:
        scope = FeedbackLessonScope.WORKSPACE

    fingerprint_raw = str(raw.get("fingerprint") or "").strip()
    if not fingerprint_raw or (fingerprint_raw.startswith("<") and fingerprint_raw.endswith(">")):
        fingerprint: str | None = None
    else:
        fingerprint = fingerprint_raw

    confidence_raw = raw.get("confidence")
    confidence: float | None = None
    if isinstance(confidence_raw, (int, float)) and not isinstance(confidence_raw, bool):
        confidence = max(0.0, min(1.0, float(confidence_raw)))

    tags = _string_list(raw.get("tags"))
    if len(tags) == 1 and "," in tags[0]:
        tags = [item.strip() for item in tags[0].split(",") if item.strip()]

    return FeedbackLessonCreate(
        id=str(raw.get("id") or "").strip() or None,
        title=title,
        fingerprint=fingerprint,
        summary=summary,
        applies_when=_string_list(raw.get("applies_when")),
        do=do_text,
        avoid=avoid_text,
        tags=tags[:6],
        scope=scope,
        source_draft_ids=_string_list(raw.get("source_draft_ids")),
        source_record_ids=_string_list(raw.get("source_record_ids")),
        evidence_task_ids=_string_list(raw.get("evidence_task_ids")),
        confidence=confidence,
    )


def apply_reaper_output(
    store: FeedbackLessonCatalogStore,
    workspace_id: str,
    parsed: ParsedReaperOutput,
    *,
    now: datetime | None = None,
) -> AppliedReaperResult:
    """Persist structured reaper lessons through the feedback store with echo-merge."""
    existing_ids = {lesson.id for lesson in store.list_lessons(workspace_id)}
    created_ids: list[str] = []
    merged_ids: list[str] = []
    rejected_count = 0

    for candidate in parsed.lessons:
        try:
            payload = _coerce_lesson_create(candidate)
            lesson = store.create_lesson(
                workspace_id,
                payload,
                now=now,
                enforce_iteration_signal=True,
                allow_manual_without_evidence=False,
            )
        except (FeedbackLessonValidationError, ValueError) as exc:
            rejected_count += 1
            logger.warning(
                "silent feedback reaper rejected lesson candidate workspace_id=%s error=%s",
                workspace_id,
                exc,
            )
            continue
        if lesson.id in existing_ids:
            if lesson.id not in merged_ids:
                merged_ids.append(lesson.id)
        else:
            existing_ids.add(lesson.id)
            if lesson.id not in created_ids:
                created_ids.append(lesson.id)

    # Honor verified CLI-reported fallback IDs if the model called the lessons API directly.
    if parsed.created_lesson_ids or parsed.merged_lesson_ids:
        known_ids = {lesson.id for lesson in store.list_lessons(workspace_id)}
        for lid in parsed.created_lesson_ids:
            if lid in known_ids and lid not in created_ids and lid not in merged_ids:
                created_ids.append(lid)
        for lid in parsed.merged_lesson_ids:
            if lid in known_ids and lid not in merged_ids and lid not in created_ids:
                merged_ids.append(lid)

    if created_ids or merged_ids:
        skipped_reason: str | None = None
    elif parsed.skipped_reason:
        skipped_reason = parsed.skipped_reason
    elif rejected_count > 0:
        skipped_reason = "all_candidates_rejected"
    else:
        skipped_reason = "no_new_lessons"

    return AppliedReaperResult(
        created_lesson_ids=created_ids,
        merged_lesson_ids=merged_ids,
        skipped_reason=skipped_reason,
        rejected_count=rejected_count,
    )
