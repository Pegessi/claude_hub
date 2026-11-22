"""Run the personal Lark event bridge or submit a normalized producer callback."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Literal

import httpx
from pydantic import HttpUrl

from .events_gateway import MAX_EVENT_BYTES, ExternalEvent

logger = logging.getLogger(__name__)
LARK_EVENTS = {
    "minutes.minute.generated_v1": "Meeting minutes generated",
    "vc.note.generated_v1": "Meeting note generated",
    "task.task.update_user_access_v2": "Task updated",
    "approval.instance.status_changed_v4": "Approval instance status changed",
    "approval.task.status_changed_v4": "Approval task status changed",
}
LARK_REFERENCE_FIELDS = {
    "minutes.minute.generated_v1": "minute_token",
    "vc.note.generated_v1": "note_id",
    "task.task.update_user_access_v2": "task_guid",
    "approval.instance.status_changed_v4": "instance_code",
    "approval.task.status_changed_v4": "task_id",
}
CALLBACK_TYPES = {
    "codebase": ("mr.status_changed", "ci.status_changed"),
    "merlin": ("training.finished", "evaluation.finished"),
}
EventSender = Callable[[ExternalEvent], Awaitable[None]]


def parse_lark_event(
    key: str, line: bytes, *, severity: Literal["urgent", "normal"] = "normal"
) -> ExternalEvent:
    if key not in LARK_EVENTS:
        raise ValueError("Unsupported Lark user event key")
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("Expected a Lark event object")
    if key == "task.task.update_user_access_v2":
        header, details = record.get("header"), record.get("event")
        if not isinstance(header, dict) or not isinstance(details, dict):
            raise ValueError("Expected a Lark task envelope")
        event_type, event_id = header.get("event_type"), header.get("event_id")
        types = details.get("event_types")
        summary = (
            ", ".join(name for name in types if isinstance(name, str))
            if isinstance(types, list)
            else ""
        )
    else:
        event_type, event_id = record.get("type"), record.get("event_id")
        details = record
        summary = record.get("status") or record.get("title") or ""
    if event_type != key or not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("Lark event type or event ID did not match the subscription")
    field = LARK_REFERENCE_FIELDS[key]
    reference = details.get(field)
    summary = summary[:800] if isinstance(summary, str) else ""
    if isinstance(reference, str) and reference:
        summary = " · ".join(filter(None, (f"{field}={reference[:150]}", summary)))
    # Only project fields needed for triage. The stream can contain other personal data.
    return ExternalEvent(
        source="lark",
        event_type=key,
        dedupe_key=event_id,
        severity=severity,
        title=LARK_EVENTS[key],
        summary=summary,
    )


async def _bounded_lines(stream: asyncio.StreamReader, label: str) -> AsyncIterator[bytes]:
    """Skip an entire oversized record, even when it spans multiple pipe reads."""
    dropping = False
    while True:
        try:
            line = await stream.readuntil(b"\n")
        except asyncio.LimitOverrunError as error:
            if not dropping:
                logger.warning("Skipping oversized Lark %s", label)
            dropping = True
            await stream.readexactly(error.consumed)
            continue
        except asyncio.IncompleteReadError as error:
            if error.partial and not dropping:
                yield error.partial
            return
        if not dropping:
            yield line
        dropping = False


async def _read_stderr(stream: asyncio.StreamReader, key: str, ready: asyncio.Future[bool]) -> None:
    marker = f"[event] ready event_key={key}".encode()
    try:
        async for line in _bounded_lines(stream, f"diagnostic for {key}"):
            if line.strip() == marker and not ready.done():
                ready.set_result(True)
                logger.info("Lark subscription ready for %s", key)
            elif b"WARN" in line or b"drop" in line.lower():
                logger.warning("Lark reported a warning or dropped events for %s", key)
            elif line.startswith(b"{"):
                # Log the error classification without dumping CLI credentials or event data.
                try:
                    error = json.loads(line)["error"]
                    fields = [error.get(name, "") for name in ("type", "subtype")]
                    category = "/".join(field for field in fields if isinstance(field, str))
                except (KeyError, AttributeError, TypeError, ValueError):
                    category = "unknown"
                logger.error("Lark CLI reported an error for %s (%r)", key, category[:120])
    finally:
        if not ready.done():
            ready.set_result(False)


async def consume_lark(
    key: str,
    send: EventSender,
    *,
    executable: str = "lark-cli",
    profile: str = "",
    ready_timeout: float = 30,
    urgent: bool = False,
) -> None:
    if key not in LARK_EVENTS:
        raise ValueError("Unsupported Lark user event key")
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            executable,
            "event",
            "consume",
            key,
            "--as",
            "user",
            *(["--profile", profile] if profile else []),
            env={
                key: value
                for key, value in os.environ.items()
                if key != "CLAUDE_HUB_DOT_EVENTS_TOKEN"
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_EVENT_BYTES,
            # The bridge handles the supervisor's signal and explicitly terminates its children.
            start_new_session=True,
        )
    )
    cancelled_during_spawn = False
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        # Once the OS has begun launching the child, acquire its handle before stopping.
        cancelled_during_spawn = True
        process = await spawn
    assert process.stdin and process.stdout and process.stderr
    ready = asyncio.get_running_loop().create_future()
    stderr = asyncio.create_task(_read_stderr(process.stderr, key, ready))
    try:
        if cancelled_during_spawn:
            raise asyncio.CancelledError
        if not await asyncio.wait_for(ready, timeout=ready_timeout):
            raise RuntimeError(f"Lark subscription exited before ready: {key}")
        async for line in _bounded_lines(process.stdout, f"NDJSON record for {key}"):
            try:
                event = parse_lark_event(key, line, severity="urgent" if urgent else "normal")
            except (ValueError, RecursionError):
                logger.warning("Skipping invalid Lark NDJSON record for %s", key)
                continue
            try:
                await send(event)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 429:
                    raise
                logger.warning("Lark event rejected by the gateway quota for %s", key)
        code = await process.wait()
        raise RuntimeError(f"Lark subscription exited unexpectedly ({code}): {key}")
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        process.stdin.close()
        # Drain pipes concurrently with exit so a verbose child can complete unsubscribe.
        await asyncio.gather(process.stdout.read(), process.wait(), stderr)


def callback_event(
    source: str,
    event_type: str,
    delivery_id: str,
    title: str,
    *,
    summary: str = "",
    url: HttpUrl | None = None,
    severity: str = "normal",
) -> ExternalEvent:
    if event_type not in CALLBACK_TYPES.get(source, ()):
        raise ValueError("Unsupported Codebase/Merlin callback type")
    return ExternalEvent.model_validate(
        dict(
            source=source,
            event_type=event_type,
            dedupe_key=delivery_id,
            severity=severity,
            title=title,
            summary=summary,
            url=url,
        )
    )


async def send_event(
    client: httpx.AsyncClient, endpoint: str, event: ExternalEvent, *, token: str | None = None
) -> None:
    url = httpx.URL(endpoint)
    if url.scheme != "https" and not (
        url.scheme == "http" and url.host in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("Dot events require HTTPS for remote gateways")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = event.model_dump(mode="json")
    for attempt in range(3):
        try:
            response = await client.post(url, json=body, headers=headers, follow_redirects=False)
        except httpx.TransportError:
            if attempt == 2:
                raise
        else:
            if response.status_code < 500 or attempt == 2:
                response.raise_for_status()
                return
        await asyncio.sleep(2**attempt)


async def _run(args: argparse.Namespace) -> None:
    token = os.environ.get("CLAUDE_HUB_DOT_EVENTS_TOKEN")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:

        async def send(event: ExternalEvent) -> None:
            await send_event(client, args.endpoint, event, token=token)

        if args.command == "callback":
            await send(
                callback_event(
                    args.source,
                    args.event_type,
                    args.delivery_id,
                    args.title,
                    summary=args.summary,
                    url=args.url,
                    severity=args.severity,
                )
            )
            return

        loop = asyncio.get_running_loop()
        runner = asyncio.current_task()
        assert runner is not None
        signals = (signal.SIGTERM, signal.SIGINT)

        def stop() -> None:
            # Repeated stop signals must not interrupt child cleanup.
            for sig in signals:
                loop.add_signal_handler(sig, lambda: None)
            runner.cancel()

        for sig in signals:
            loop.add_signal_handler(sig, stop)
        try:
            async with asyncio.TaskGroup() as group:
                for key in dict.fromkeys(args.events or LARK_EVENTS):
                    group.create_task(
                        consume_lark(
                            key, send, profile=args.profile, urgent=key in args.urgent_event
                        )
                    )
        except asyncio.CancelledError:
            logger.info("Lark bridge stopped")
        finally:
            for sig in signals:
                loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("CLAUDE_HUB_DOT_EVENTS_URL", "http://127.0.0.1:8173/api/dot/events"),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    lark = sub.add_parser("lark", help="Subscribe to personal Lark events")
    lark.add_argument("--profile", default=os.environ.get("CLAUDE_HUB_DOT_LARK_PROFILE", ""))
    lark.add_argument("--event", action="append", dest="events", choices=LARK_EVENTS)
    lark.add_argument("--urgent-event", action="append", default=[], choices=LARK_EVENTS)
    callback = sub.add_parser("callback", help="Send a Codebase/Merlin transition")
    callback.add_argument("source", choices=CALLBACK_TYPES)
    callback.add_argument("event_type", choices=sum(CALLBACK_TYPES.values(), ()))
    callback.add_argument("delivery_id", help="Stable producer event ID across retries")
    callback.add_argument("title")
    callback.add_argument("--summary", default="")
    callback.add_argument("--url", type=HttpUrl)
    callback.add_argument("--severity", choices=("urgent", "normal", "digest"), default="normal")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parser.parse_args()
    if args.command == "lark" and args.events and set(args.urgent_event) - set(args.events):
        lark.error("--urgent-event must be included in the selected --event keys")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
