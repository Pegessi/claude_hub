"""Pure and filesystem helpers for task execution commands."""

import hashlib
import os
import re
import secrets
import shlex
import stat
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import click

from claude_hub.cli.client import HubError

_SECRET_TASK_FIELDS = {"reporter_key", "new_reporter_key"}


def _reject_secret_task_fields(body: Dict[str, Any]) -> None:
    if _SECRET_TASK_FIELDS & set(body):
        raise click.ClickException(
            "Reporter keys must be supplied through the dedicated key-file option."
        )


def _create_reporter_key(path: Path) -> str:
    key = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise click.ClickException(
            f"Reporter key file already exists: {path}. Inspect the existing Task before retrying; "
            "use the explicit reuse flag only for the same logical request."
        ) from exc
    except OSError as exc:
        raise click.ClickException(f"Cannot create reporter key file: {path}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise click.ClickException(
            f"Cannot persist reporter key file directory: {path.parent}"
        ) from exc
    return key


def _prepare_reporter_key(path: Path, reuse: bool) -> str:
    return _read_reporter_key(path) if reuse else _create_reporter_key(path)


REPORTER_KEY_DIR_ENV = "CLAUDE_HUB_REPORTER_KEY_DIR"
_KEY_FILE_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def _reporter_key_dir() -> Path:
    # Client-side and checkout-independent, so every later thread finds the key.
    configured = os.environ.get(REPORTER_KEY_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude_hub" / "reporter-keys"


def _default_reporter_key_file(task_id: str) -> Path:
    if not _KEY_FILE_TASK_ID.fullmatch(task_id):
        raise click.ClickException(
            "This Task ID cannot name a default key file; pass the key-file option."
        )
    return _reporter_key_dir() / f"{task_id}.key"


def _staged_reporter_key_file() -> Path:
    directory = _reporter_key_dir()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise click.ClickException(f"Cannot create reporter key directory: {directory}") from exc
    return directory / f"pending-{uuid.uuid4().hex}.key"


def _staged_key_or_none(path: Path) -> Path | None:
    """Recognize a staged key passed back explicitly for a retry."""
    if not (path.name.startswith("pending-") and path.suffix == ".key"):
        return None
    try:
        same_dir = path.parent.resolve() == _reporter_key_dir().resolve()
    except OSError:
        return None
    return path if same_dir else None


def _install_reporter_key(staged: Path, task_id: Any) -> Path:
    """Move a staged key to the Task's default file once the Task is known."""
    if not (isinstance(task_id, str) and _KEY_FILE_TASK_ID.fullmatch(task_id)):
        raise click.ClickException(
            f"The Task changed, but its reporter key is still at {staged}; "
            "pass that file with --reporter-key-file."
        )
    final = _default_reporter_key_file(task_id)
    try:
        os.replace(staged, final)
    except OSError as exc:
        raise click.ClickException(
            f"The Task changed, but its reporter key is still at {staged}; move it to {final}."
        ) from exc
    return final


def _drop_superseded_default_key(task_id: str, current: Path) -> None:
    """Remove the default key file once a handoff bound the Task to another file."""
    if not _KEY_FILE_TASK_ID.fullmatch(task_id):
        return
    default = _default_reporter_key_file(task_id)
    try:
        if not default.exists() or default.resolve() == current.resolve():
            return
        default.unlink()
    except OSError:
        click.echo(
            f"Warning: {default} holds the previous execution's key; remove it.",
            err=True,
        )


def _settle_staged_key(
    error: click.ClickException,
    exc: HubError,
    staged: Path | None,
    *,
    fresh: bool,
    retry_flags: Callable[[Path], str],
) -> click.ClickException:
    """Drop a fresh staged key the Hub rejected; keep any key an earlier attempt sent."""
    if staged is None or not staged.exists():
        return error
    if exc.status is not None and 400 <= exc.status < 500:
        if fresh:
            staged.unlink(missing_ok=True)
            return error
        return click.ClickException(
            f"{error.message}. An earlier attempt with this key may have succeeded, so "
            f"{shlex.quote(str(staged))} was kept; repeat that earlier request unchanged, "
            "as its outcome-unknown message printed it."
        )
    return click.ClickException(
        f"{error.message}. The outcome is unknown; to retry the same request, pass "
        f"{retry_flags(staged)}."
    )


def _registration_request_key(reporter_key: str) -> str:
    return hashlib.sha256(
        b"claude-hub-task-register-v1\0" + reporter_key.encode("utf-8")
    ).hexdigest()


def _source_reference(
    kind: Optional[str], tab_id: Optional[str], agent_id: Optional[str]
) -> Optional[Dict[str, Any]]:
    if kind is None:
        if tab_id is not None or agent_id is not None:
            raise click.UsageError("--source-tab-id/--source-agent-id require --source-kind")
        return None
    source: Dict[str, Any] = {"kind": kind}
    if tab_id is not None:
        source["tab_id"] = tab_id
    if agent_id is not None:
        source["agent_id"] = agent_id
    return source


def _execution_reference(
    provider: Optional[str],
    session_id: Optional[str],
    thread_id: Optional[str],
    turn_id: Optional[str],
    run_epoch: Optional[int],
) -> Optional[Dict[str, Any]]:
    values = {
        "provider": provider,
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "run_epoch": run_epoch,
    }
    result = {key: value for key, value in values.items() if value is not None}
    return result or None


def _action_summary(data: Any, *, reporter_key_file: Path | None = None) -> Dict[str, Any]:
    envelope = data if isinstance(data, dict) else {}
    task_obj = envelope.get("task", envelope)
    task = task_obj if isinstance(task_obj, dict) else {}
    event_obj = envelope.get("event")
    event = event_obj if isinstance(event_obj, dict) else {}
    event_payload_obj = event.get("payload")
    event_payload = event_payload_obj if isinstance(event_payload_obj, dict) else {}
    result: Dict[str, Any] = {
        "workspace_id": task.get("workspace_id"),
        "task_id": task.get("id"),
        "status": task.get("status"),
        "execution_control": task.get("execution_control"),
        "execution_epoch": task.get("execution_epoch"),
        "progress_revision": task.get("progress_revision"),
    }
    if event_payload.get("state") is not None:
        result["progress_state"] = event_payload["state"]
    if "replayed" in envelope:
        result["replayed"] = bool(envelope["replayed"])
    if reporter_key_file is not None:
        result["reporter_key_file"] = str(reporter_key_file)
    return result


_REPORTER_KEY_MIN_BYTES = 32
_REPORTER_KEY_MAX_BYTES = 256


def _read_reporter_key(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise click.ClickException(
                    "Reporter key file must be a regular file with mode 0600."
                )
            raw = os.read(descriptor, _REPORTER_KEY_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    except click.ClickException:
        raise
    except OSError as exc:
        raise click.ClickException(f"Cannot read reporter key file: {path}") from exc
    if not (_REPORTER_KEY_MIN_BYTES <= len(raw) <= _REPORTER_KEY_MAX_BYTES):
        raise click.ClickException("Reporter key file is invalid.")
    if any(byte < 33 or byte > 126 for byte in raw):
        raise click.ClickException("Reporter key file is invalid.")
    return raw.decode("ascii")


_SAFE_TASK_ERROR_CODES = {"task_create_request_key_conflict"}


def _safe_task_operation_error(operation: str, exc: HubError) -> click.ClickException:
    if exc.message in _SAFE_TASK_ERROR_CODES:
        return click.ClickException(exc.message)
    by_status = {
        400: f"invalid_task_{operation}",
        403: f"task_{operation}_forbidden",
        404: "task_not_found",
        409: f"task_{operation}_conflict",
        422: f"invalid_task_{operation}",
    }
    code = by_status.get(exc.status) if exc.status is not None else None
    return click.ClickException(code or f"task_{operation}_failed")


_TASK_EXECUTION_FIELDS = {
    "execution_control",
    "execution_epoch",
    "progress_revision",
    "execution_released",
    "latest_progress",
    "reporter_key",
    "new_reporter_key",
}


def _reject_task_execution_fields(body: Dict[str, Any]) -> None:
    if _TASK_EXECUTION_FIELDS & set(body):
        raise click.ClickException(
            "Execution control and progress use task handoff/progress, not task update."
        )


def _task_in_board(board: Any, task_id: str) -> Optional[dict]:
    tasks = board.get("tasks", []) if isinstance(board, dict) else []
    return next(
        (item for item in tasks if isinstance(item, dict) and item.get("id") == task_id),
        None,
    )


def _context_summary(
    workspace_id: str, task_value: Optional[dict], capabilities: Any
) -> Dict[str, Any]:
    task = task_value or {}
    caps = capabilities if isinstance(capabilities, dict) else {}
    task_id = task.get("id")
    key_file = None
    if (
        task.get("execution_control") == "initiator"
        and isinstance(task_id, str)
        and _KEY_FILE_TASK_ID.fullmatch(task_id)
    ):
        candidate = _default_reporter_key_file(task_id)
        key_file = str(candidate) if candidate.is_file() else None
    return {
        "workspace_id": workspace_id,
        "task_id": task_id,
        "reporter_key_file": key_file,
        "status": task.get("status"),
        "task_mode": task.get("task_mode"),
        "execution_control": task.get("execution_control"),
        "execution_epoch": task.get("execution_epoch"),
        "progress_revision": task.get("progress_revision"),
        "execution_released": task.get("execution_released"),
        "supported_execution_controls": caps.get("supported_execution_controls", []),
        "progress_states": caps.get("progress_states", []),
        "record_only_requires_reporter_key": caps.get("record_only_requires_reporter_key"),
        "handoff_requires_release": caps.get("handoff_requires_release"),
    }


def _execution_ref_options(function: Callable[..., Any]) -> Callable[..., Any]:
    options = (
        click.option("--execution-run-epoch", type=int, default=None),
        click.option("--execution-turn-id", default=None),
        click.option("--execution-thread-id", default=None),
        click.option("--execution-session-id", default=None),
        click.option("--execution-provider", default=None),
    )
    for option in reversed(options):
        function = option(function)
    return function
