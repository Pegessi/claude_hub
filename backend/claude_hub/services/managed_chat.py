"""Managed Workspace Chat runtime bridge.

This module adapts Workspace-managed sessions to the native Chat transport
without routing through HTTP or tmux. It also owns a small delivery-evidence
sidecar because the stream's durable ``turn_started`` event is written before
the provider accepts a turn and therefore cannot serve as an acceptance receipt.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import AbstractSet, Any, Literal, Optional, Sequence

from ..models import (
    AgentRuntimeStatus,
    ManagedSession,
    SessionKind,
    WorkspaceAttachment,
    WorkspaceAttachmentCreate,
)
from .agent_stream.attachments import preview_within_cache_bounds
from .agent_stream.native import _detect_image_mime
from .agent_stream.store import AgentStreamStore
from .agent_stream.tailer import NativeRuntimeSnapshot, TailerManager
from .ttyd_manager import ttyd_manager

DeliveryState = Literal["not_sent", "sending", "accepted", "uncertain"]

_PROCESS_TOKEN = uuid.uuid4().hex
_ATTEMPT_VERSION = 1
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_session_locks: dict[str, asyncio.Lock] = {}
_turn_state_cache: dict[
    str,
    tuple[Path, int, int, int, Literal["empty", "terminal", "unfinished", "unknown"]],
] = {}


class ManagedChatDeliveryUncertain(RuntimeError):
    """A managed Chat turn may have reached its provider and must not replay."""


def _manager() -> TailerManager:
    # Import lazily: the API module owns the singleton and imports Workspace
    # services during application startup.
    from ..api.agent_stream import get_managed_tailer_manager

    return get_managed_tailer_manager()


def _workspace_manager() -> Any:
    from .workspace_manager import workspace_manager

    return workspace_manager


def _delivery_paths(workspace_id: str, session_id: str) -> tuple[Path, Path]:
    stream_path = AgentStreamStore(workspace_id, session_id).path
    return (
        stream_path.with_suffix(".managed-delivery.json"),
        stream_path.with_suffix(".maso-delivery.json"),
    )


def _attempt_path(session: ManagedSession) -> Path:
    return _delivery_paths(session.workspace_id, session.id)[0]


def _maso_delivery_path(session: ManagedSession) -> Path:
    return _delivery_paths(session.workspace_id, session.id)[1]


def validate_delivery_state_discard(
    workspace_id: str,
    session_id: str,
    *,
    acknowledged_call_ids: AbstractSet[str],
    provider_fenced: bool,
) -> None:
    """Fail unless explicit deletion can safely retire delivery evidence."""

    managed_path, maso_path = _delivery_paths(workspace_id, session_id)
    if maso_path.exists():
        try:
            maso_evidence = json.loads(maso_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedChatDeliveryUncertain(
                "cannot discard unreadable MASO delivery recovery evidence"
            ) from exc
        if not isinstance(maso_evidence, dict) or maso_evidence.get("uncertain") is not False:
            raise ManagedChatDeliveryUncertain(
                "cannot discard a managed Chat while MASO delivery is uncertain"
            )
        if not provider_fenced:
            raise ManagedChatDeliveryUncertain(
                "cannot discard MASO delivery evidence without a verified runtime fence"
            )
    if not managed_path.exists():
        return
    try:
        value = json.loads(managed_path.read_text(encoding="utf-8"))
        attempts = (
            value.get("attempts")
            if isinstance(value, dict) and value.get("version") == _ATTEMPT_VERSION
            else None
        )
    except (OSError, json.JSONDecodeError) as exc:
        if not provider_fenced:
            raise ManagedChatDeliveryUncertain(
                "cannot discard unreadable managed Chat delivery evidence without a runtime fence"
            ) from exc
        return
    if not isinstance(attempts, dict):
        if not provider_fenced:
            raise ManagedChatDeliveryUncertain(
                "cannot discard invalid managed Chat delivery evidence without a runtime fence"
            )
        return
    unresolved = [
        call_id
        for call_id, record in attempts.items()
        if call_id not in acknowledged_call_ids
        and (not isinstance(record, dict) or record.get("state") not in {"accepted"})
    ]
    if unresolved and not provider_fenced:
        raise ManagedChatDeliveryUncertain(
            "cannot discard managed Chat delivery evidence without a verified runtime fence"
        )


def discard_delivery_state(workspace_id: str, session_id: str) -> None:
    """Remove delivery sidecars after explicit deletion has passed its fence."""

    for path in _delivery_paths(workspace_id, session_id):
        path.unlink(missing_ok=True)
    _turn_state_cache.pop(session_id, None)


def _read_attempts(session: ManagedSession) -> dict[str, Any]:
    path = _attempt_path(session)
    if not path.exists():
        return {"version": _ATTEMPT_VERSION, "attempts": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedChatDeliveryUncertain(
            f"managed Chat delivery evidence is unreadable for session {session.id}"
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("version") != _ATTEMPT_VERSION
        or not isinstance(value.get("attempts"), dict)
    ):
        raise ManagedChatDeliveryUncertain(
            f"managed Chat delivery evidence is invalid for session {session.id}"
        )
    return value


def _write_attempts(session: ManagedSession, value: dict[str, Any]) -> None:
    path = _attempt_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file, sort_keys=True, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _decode_data_url(value: str) -> tuple[str, bytes]:
    if not value.startswith("data:") or "," not in value:
        raise ValueError("Chat attachment must be a base64 data URL")
    header, encoded = value[5:].split(",", 1)
    parts = header.split(";")
    if "base64" not in parts[1:]:
        raise ValueError("Chat attachment data URL must use base64 encoding")
    try:
        return parts[0] or "application/octet-stream", base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Chat attachment has invalid base64 data") from exc


def _validated_image(
    filename: str, declared_mime: str, data: bytes
) -> tuple[bytes, dict[str, Any]]:
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError(f"Chat attachment {filename!r} exceeds 20 MiB")
    detected = _detect_image_mime(data)
    if declared_mime not in _IMAGE_MIME_TYPES or detected != declared_mime:
        raise ValueError(f"Chat attachment {filename!r} is not a supported image")
    return data, {
        "filename": filename,
        "mime_type": declared_mime,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _prepare_attachments(
    attachments: Sequence[Any],
) -> tuple[list[bytes], list[Optional[bytes]], list[dict[str, Any]]]:
    images: list[bytes] = []
    previews: list[Optional[bytes]] = []
    identities: list[dict[str, Any]] = []
    for raw in attachments:
        if isinstance(raw, WorkspaceAttachment):
            path = Path(raw.path)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"Chat attachment {raw.filename!r} is unavailable") from exc
            image, identity = _validated_image(raw.filename, raw.mime_type, data)
        else:
            if isinstance(raw, WorkspaceAttachmentCreate):
                filename, declared_mime, data_url = (
                    raw.filename,
                    raw.mime_type,
                    raw.data_url,
                )
            else:
                filename_value = getattr(raw, "filename", None)
                declared_mime_value = getattr(raw, "mime_type", None)
                data_url_value = getattr(raw, "data_url", None)
                if not isinstance(filename_value, str) or not filename_value:
                    raise ValueError("unsupported managed Chat attachment filename")
                if not isinstance(declared_mime_value, str) or not declared_mime_value:
                    raise ValueError("unsupported managed Chat attachment MIME type")
                if not isinstance(data_url_value, str) or not data_url_value:
                    raise ValueError("unsupported managed Chat attachment data")
                filename = filename_value
                declared_mime = declared_mime_value
                data_url = data_url_value
            encoded_mime, data = _decode_data_url(data_url)
            if encoded_mime != declared_mime:
                raise ValueError(
                    f"Chat attachment {filename!r} MIME type does not match its data URL"
                )
            image, identity = _validated_image(filename, declared_mime, data)
        images.append(image)
        previews.append(
            image if preview_within_cache_bounds(identity["mime_type"], image) else None
        )
        identities.append(identity)
    return images, previews, identities


def _payload_digest(text: str, identities: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        {"text": text, "attachments": identities},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_chat(session: ManagedSession) -> None:
    if session.session_kind != SessionKind.CHAT:
        raise ValueError("managed Chat bridge is unavailable for Terminal sessions")


def _record_delivery_state(session: ManagedSession, call_id: str, record: Any) -> DeliveryState:
    if call_id in session.delivered_call_ids:
        return "accepted"
    if not isinstance(record, dict):
        return "not_sent"
    state = record.get("state")
    if state == "accepted":
        return "accepted"
    if state == "uncertain":
        return "uncertain"
    if state == "sending":
        return "sending" if record.get("process_token") == _PROCESS_TOKEN else "uncertain"
    return "uncertain"


def delivery_state(session: ManagedSession, call_id: str) -> DeliveryState:
    """Return durable provider-handoff evidence for ``call_id``."""

    if call_id in session.delivered_call_ids:
        return "accepted"
    return _record_delivery_state(
        session, call_id, _read_attempts(session)["attempts"].get(call_id)
    )


def _durable_turn_state(
    session: ManagedSession,
) -> Literal["empty", "terminal", "unfinished", "unknown"]:
    path = AgentStreamStore(session.workspace_id, session.id).path
    try:
        stat = path.stat()
    except FileNotFoundError:
        _turn_state_cache.pop(session.id, None)
        return "empty"
    except OSError:
        return "unknown"
    cached = _turn_state_cache.get(session.id)
    identity = (path, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    if cached is not None and cached[:4] == identity:
        return cached[4]

    unfinished: set[str] = set()
    saw_lifecycle = False
    state: Literal["empty", "terminal", "unfinished", "unknown"]
    try:
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("stream event must be an object")
                event_type = event.get("type")
                turn_id = event.get("turn_id")
                run_epoch = event.get("run_epoch")
                key = turn_id if isinstance(turn_id, str) else f"epoch:{run_epoch}"
                if event_type == "turn_started":
                    saw_lifecycle = True
                    unfinished.add(key)
                elif event_type == "turn_completed":
                    saw_lifecycle = True
                    unfinished.discard(key)
        state = "unfinished" if unfinished else ("terminal" if saw_lifecycle else "empty")
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        state = "unknown"
    _turn_state_cache[session.id] = (*identity, state)
    return state


def _runtime_snapshot(session: ManagedSession) -> Optional[NativeRuntimeSnapshot]:
    tailer = _manager().get_tailer(session.id)
    if tailer is None:
        return None
    transport = tailer.native_transport
    if tailer.hard_failed or tailer.native_error is not None:
        return NativeRuntimeSnapshot(
            AgentRuntimeStatus.OFFLINE,
            tailer.native_error or tailer.last_error or "native provider unavailable",
        )
    if transport is None:
        return None
    if transport.last_error is not None:
        return NativeRuntimeSnapshot(AgentRuntimeStatus.OFFLINE, transport.last_error)
    if tailer.native_terminal_status is not None:
        return tailer.native_terminal_status
    if transport.turn_in_flight:
        return NativeRuntimeSnapshot(
            AgentRuntimeStatus.WORKING, "native provider turn is in flight"
        )
    return NativeRuntimeSnapshot(AgentRuntimeStatus.IDLE, "native provider is ready")


def _has_running_turn(session: ManagedSession, attempts: dict[str, Any]) -> bool:
    if any(
        _record_delivery_state(session, call_id, record) == "sending"
        for call_id, record in attempts.items()
    ):
        return True
    snapshot = _runtime_snapshot(session)
    if snapshot is not None and snapshot.status == AgentRuntimeStatus.WORKING:
        return True
    durable_state = _durable_turn_state(session)
    if durable_state in {"unfinished", "unknown"}:
        return True
    return durable_state == "empty" and any(
        _record_delivery_state(session, call_id, record) == "accepted"
        and call_id not in session.delivered_call_ids
        for call_id, record in attempts.items()
    )


def is_turn_running(session: ManagedSession) -> bool:
    """Return whether this session has an active local/provider handoff."""

    try:
        attempts = _read_attempts(session)["attempts"]
    except ManagedChatDeliveryUncertain:
        return True
    return _has_running_turn(session, attempts)


def runtime_status(session: ManagedSession) -> AgentRuntimeStatus:
    """Return the monitor-facing managed Chat runtime status."""

    try:
        attempts = _read_attempts(session)["attempts"]
        states = {
            _record_delivery_state(session, call_id, record) for call_id, record in attempts.items()
        }
    except ManagedChatDeliveryUncertain:
        return AgentRuntimeStatus.ATTENTION
    if "uncertain" in states:
        return AgentRuntimeStatus.ATTENTION
    if "sending" in states:
        return AgentRuntimeStatus.WORKING
    snapshot = _runtime_snapshot(session)
    if snapshot is not None:
        return snapshot.status
    durable_state = _durable_turn_state(session)
    if durable_state == "terminal":
        return AgentRuntimeStatus.IDLE
    has_unreconciled_accepted = any(
        state == "accepted" and call_id not in session.delivered_call_ids
        for call_id, state in (
            (call_id, _record_delivery_state(session, call_id, record))
            for call_id, record in attempts.items()
        )
    )
    if durable_state in {"unfinished", "unknown"} or (
        has_unreconciled_accepted and durable_state == "empty"
    ):
        return AgentRuntimeStatus.ATTENTION
    return session.runtime_status


async def send_message(
    session: ManagedSession,
    text: str,
    attachments: Sequence[Any],
    call_id: str,
) -> None:
    """Hand one immutable turn to the provider and persist acceptance evidence."""

    _require_chat(session)
    if not call_id.strip():
        raise ValueError("managed Chat call_id must not be blank")
    if not text.strip() and not attachments:
        raise ValueError("managed Chat message requires text or an image attachment")
    images, previews, identities = await asyncio.to_thread(_prepare_attachments, attachments)
    digest = _payload_digest(text, identities)
    lock = _session_locks.setdefault(session.id, asyncio.Lock())
    async with lock:
        evidence = _read_attempts(session)
        attempts = evidence["attempts"]
        existing = attempts.get(call_id)
        if isinstance(existing, dict):
            if existing.get("payload_sha256") != digest or existing.get("turn_id") != call_id:
                raise ValueError(f"call_id {call_id} is already bound to a different Chat payload")
            state = _record_delivery_state(session, call_id, existing)
            if state == "accepted":
                return
            if state == "sending":
                raise RuntimeError(f"managed Chat call_id {call_id} is already sending")
            raise ManagedChatDeliveryUncertain(
                f"delivery of managed Chat call_id {call_id} is uncertain; do not replay automatically"
            )

        attempts[call_id] = {
            "payload_sha256": digest,
            "process_token": _PROCESS_TOKEN,
            "state": "sending",
            "turn_id": call_id,
        }
        _write_attempts(session, evidence)
        try:
            await _manager().send_message(
                session,
                text,
                images,
                call_id,
                previews=previews,
                turn_metadata={"origin": "workspace"},
            )
        except BaseException:
            attempts[call_id]["state"] = "uncertain"
            _write_attempts(session, evidence)
            raise
        attempts[call_id]["state"] = "accepted"
        _write_attempts(session, evidence)


async def cancel(session: ManagedSession, *, expected_turn_id: Optional[str] = None) -> bool:
    """Cancel only this managed Chat's matching active turn."""

    _require_chat(session)
    return await _manager().cancel_turn(session, expected_turn_id=expected_turn_id)


def _maso_delivery_uncertain(session: ManagedSession) -> bool:
    path = _maso_delivery_path(session)
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("uncertain") is True
    except (OSError, json.JSONDecodeError, AttributeError):
        return True


def _live_session_for_reset(expected: ManagedSession) -> ManagedSession:
    live = _workspace_manager().sessions.get(expected.id)
    if not isinstance(live, ManagedSession):
        raise RuntimeError("managed Chat session disappeared during context reset")
    if (
        live.workspace_id != expected.workspace_id
        or live.tab_id != expected.tab_id
        or live.session_kind != SessionKind.CHAT
    ):
        raise RuntimeError("managed Chat identity changed during context reset")
    return live


def _persist_fresh_identity(expected: ManagedSession) -> None:
    session = _live_session_for_reset(expected)
    process = ttyd_manager.processes.get(session.tab_id)
    if process is None:
        raise RuntimeError("managed Chat backing tab is unavailable; context was not reset")
    old_session_identity = (session.agent_session_id, session.agent_session_id_verified)
    old_tab_identity = (process.agent_session_id, process.agent_session_id_verified)
    try:
        session.agent_session_id = None
        session.agent_session_id_verified = False
        process.agent_session_id = None
        process.agent_session_id_verified = False
        _workspace_manager()._save_state()
        ttyd_manager._save_state()
    except BaseException:
        session.agent_session_id, session.agent_session_id_verified = old_session_identity
        process.agent_session_id, process.agent_session_id_verified = old_tab_identity
        try:
            _workspace_manager()._save_state()
            ttyd_manager._save_state()
        except Exception:
            pass
        raise


async def reset_context(session: ManagedSession) -> None:
    """Start a fresh provider conversation while retaining display history."""

    _require_chat(session)
    lock = _session_locks.setdefault(session.id, asyncio.Lock())
    async with lock:
        evidence = _read_attempts(session)
        attempts = evidence["attempts"]
        states = {
            _record_delivery_state(session, call_id, record) for call_id, record in attempts.items()
        }
        if _has_running_turn(session, attempts) or "sending" in states:
            raise RuntimeError("cannot reset managed Chat context while a turn is active")
        if "uncertain" in states or _maso_delivery_uncertain(session):
            raise ManagedChatDeliveryUncertain(
                "cannot reset managed Chat context while provider delivery is uncertain"
            )
        await _manager().forget_session(session.id, require_fenced=True)
        _persist_fresh_identity(session)


async def close(session: ManagedSession) -> None:
    """Release this session's runtime without deleting history or receipts."""

    await _manager().forget_session(session.id)
