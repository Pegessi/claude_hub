"""MASO session HTTP transport. No MASO CLI or local trajectory access.

Each Hub turn subscribes before posting its message. Initial replay/snapshots
establish a baseline, not UI backfill. MASO owns model context and condensation;
Hub's event store owns the immutable display timeline. A broken SSE connection
fails the turn rather than guessing continuity from MASO's bounded, resettable
event IDs or retrying a possibly accepted prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import httpx

from ...config import settings
from ...models import StreamCapabilities, StreamModelOption, StreamReasoningEffortOption
from .native import ProviderSession, _detect_image_mime
from .store import AgentStreamStore

DEFAULT_MASO_BASE_URL = "http://127.0.0.1:9796"
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_CHUNK_BYTES = 512 * 1024
_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_UNCERTAIN_MESSAGE = (
    "MASO message acceptance is uncertain. Further sends are refused for this Chat. "
    "The service may still execute the message; inspect and stop the session in MASO."
)


def configured_maso_base_url(env: Dict[str, str]) -> Optional[str]:
    """Chat override > process override > service startup configuration."""
    return env.get("MASO_BASE_URL") or os.environ.get("MASO_BASE_URL") or settings.maso_base_url


def maso_base_url(env: Dict[str, str]) -> str:
    value = configured_maso_base_url(env)
    if not value:
        # Port shorthand is a startup setting; Chat presets use a URL so the
        # existing UI preflight and admission select the same service.
        port_value = os.environ.get("MASO_PORT")
        if port_value:
            try:
                if (
                    not port_value.isascii()
                    or not port_value.isdecimal()
                    or not 1 <= int(port_value) <= 65535
                ):
                    raise ValueError
            except ValueError:
                raise ValueError("MASO_PORT must be an integer from 1 to 65535") from None
            value = f"http://127.0.0.1:{int(port_value)}"
        else:
            value = DEFAULT_MASO_BASE_URL
    value = value.rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        httpx.URL(value)
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid port")
    except (ValueError, httpx.InvalidURL):
        raise ValueError("MASO_BASE_URL must be a valid HTTP(S) URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("MASO_BASE_URL must be an HTTP(S) URL without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("MASO_BASE_URL must not contain a query or fragment")
    return value


def _model_options(rows: Any) -> List[StreamModelOption]:
    if not isinstance(rows, list):
        raise RuntimeError("MASO returned an invalid model catalog")
    options: List[StreamModelOption] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("deprecated") is True:
            continue
        key = row.get("selector_primary_key") or row.get("key")
        if not isinstance(key, str) or not key or key in seen:
            continue
        variants = row.get("selector_variants", [])
        efforts: List[StreamReasoningEffortOption] = []
        effort_ids: set[str] = set()
        if isinstance(variants, list):
            for variant in variants:
                if not isinstance(variant, dict):
                    continue
                mode, model = variant.get("mode"), variant.get("key")
                if not isinstance(mode, str) or not isinstance(model, str) or mode in effort_ids:
                    continue
                effort_ids.add(mode)
                efforts.append(
                    StreamReasoningEffortOption(
                        id=mode,
                        label=str(variant.get("label") or mode),
                        provider_model_id=model,
                    )
                )
        seen.add(key)
        options.append(
            StreamModelOption(
                id=key,
                label=key,
                provider_model_id=key,
                description=str(row.get("model_name") or ""),
                default_reasoning_effort=next(
                    (item.id for item in efforts if item.provider_model_id == key), None
                ),
                supported_reasoning_efforts=efforts if len(efforts) > 1 else [],
            )
        )
    return options


class MasoServiceUnavailable(RuntimeError):
    """A bounded, read-only readiness check failed without creating a session."""


async def maso_model_catalog(
    client: httpx.AsyncClient,
) -> Tuple[List[StreamModelOption], str]:
    try:
        # Bound the entire probe, not just each individual request. Neither
        # endpoint invokes a model, creates a session, or changes MASO state.
        async with asyncio.timeout(3.0):
            response = await client.get("/api/models", timeout=2.0)
            response.raise_for_status()
            models = _model_options(response.json())
            if not models:
                raise MasoServiceUnavailable(
                    "MASO has no usable models configured. Configure a model, then retry."
                )
            response = await client.get("/api/models/default", timeout=2.0)
            response.raise_for_status()
            default = response.json()
            key = default.get("key") if isinstance(default, dict) else None
            keys = {model.provider_model_id for model in models}
            keys.update(
                effort.provider_model_id
                for model in models
                for effort in model.supported_reasoning_efforts
            )
            if not isinstance(key, str) or not key or key not in keys:
                raise MasoServiceUnavailable(
                    "MASO has no usable default model. Configure a default model, then retry."
                )
            return models, key
    except MasoServiceUnavailable:
        raise
    except httpx.HTTPStatusError as exc:
        raise MasoServiceUnavailable(
            f"MASO service is unavailable (HTTP {exc.response.status_code}). "
            "Check the MASO service, then retry."
        ) from None
    except (httpx.HTTPError, TimeoutError):
        raise MasoServiceUnavailable(
            "MASO service is unavailable and cannot be used. "
            "Start MASO or check its service address, then retry."
        ) from None
    except (ValueError, RuntimeError):
        raise MasoServiceUnavailable(
            "The configured endpoint is not a usable MASO service. "
            "Check its address, then retry."
        ) from None


async def check_maso_service(
    env: Dict[str, str],
) -> Tuple[List[StreamModelOption], str]:
    async with httpx.AsyncClient(
        base_url=maso_base_url(env), trust_env=False, timeout=2.0
    ) as client:
        return await maso_model_catalog(client)


class MasoHttpSession(ProviderSession):
    adapter_id = "maso_http"
    supports_images = True
    supports_tool_timeline = True
    supports_approval_ui = True
    supports_goals = False
    goal_execution_owner = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self._conversation_id_verified and not self.session.env.get("MASO_BASE_URL"):
            raise ValueError("MASO session endpoint is unverified; create a new MASO Chat")
        self._base = maso_base_url(self.session.env)
        self._client: Optional[httpx.AsyncClient] = None
        self._root_agent_id: Optional[str] = None
        self._effective_model: Optional[str] = None
        self._default_model: Optional[str] = None
        self._stream_ready: Optional[asyncio.Future[None]] = None
        self._accept_events = False
        self._root_started = False
        self._stream_failed_generation: Optional[int] = None
        self._pending_confirmations: Dict[str, str] = {}
        self._needs_abort = False
        self._delivery_path = AgentStreamStore(
            self.session.workspace_id, self.session.id
        ).path.with_suffix(".maso-delivery.json")
        self._message_id: Optional[str] = None
        self._delivery_uncertain = False
        self._staged_images: List[bytes] = []
        self._guidance_in_this_send = False
        if self._delivery_path.exists():
            try:
                delivery = json.loads(self._delivery_path.read_text())
                if delivery["base_url"] != self._base or (
                    self._conversation_id_verified
                    and delivery["session_id"] != self._conversation_id
                ):
                    raise ValueError("delivery identity mismatch")
                self._message_id = str(uuid.UUID(delivery["message_id"]))
                self._delivery_uncertain = delivery.get("uncertain") is True
                if not self._conversation_id_verified:
                    self._persist_conversation_id(delivery["session_id"])
                self._needs_abort = True
            except (OSError, ValueError, KeyError, TypeError):
                raise ValueError("MASO pending delivery identity could not be verified") from None

    def _build_command(self) -> List[str]:
        raise NotImplementedError("MASO uses HTTP, not a provider executable")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            # The local service must not inherit Hub's upstream-model proxy.
            self._client = httpx.AsyncClient(base_url=self._base, trust_env=False, timeout=10.0)
        return self._client

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http().request(method, path, **kwargs)
            response.raise_for_status()
            return None if response.status_code == 204 else response.json()
        except httpx.HTTPStatusError as exc:
            # Provider error bodies/URLs may contain credentials or prompts.
            raise RuntimeError(
                f"MASO {method} {path} returned HTTP {exc.response.status_code}"
            ) from None
        except (httpx.HTTPError, ValueError):
            raise RuntimeError(f"MASO {method} request failed") from None

    def _path(self, suffix: str) -> str:
        if not self._conversation_id or not self._conversation_id_verified:
            raise RuntimeError("MASO session has not been created")
        return f"/api/sessions/{quote(self._conversation_id, safe='')}/{suffix}"

    async def start(self) -> None:
        # Opening a Chat or fetching its model catalog must not create a MASO
        # session. Creation/resume happens only when a user actually sends.
        self._started = True

    async def _prepare_for_send(self) -> None:
        if self._current_mode != "default":
            raise ValueError("MASO Chat only supports default mode")
        if self._delivery_uncertain:
            raise RuntimeError(_UNCERTAIN_MESSAGE)
        if self._needs_abort:
            # Resume first so a persisted MASO session has an active broker.
            await self._request("GET", self._path("status"))
            await self._abort_and_wait()

    async def prepare_capabilities(self) -> None:
        try:
            self._available_models, self._default_model = await maso_model_catalog(self._http())
            self._last_error = None
        except RuntimeError as exc:
            self._available_models = []
            self._last_error = str(exc)

    def current_model(self) -> Optional[str]:
        return self.session.env.get("MASO_MODEL") or self._effective_model or self._default_model

    def current_reasoning_effort(self) -> Optional[str]:
        model = self.current_model()
        return next(
            (
                effort.id
                for option in self._available_models
                for effort in option.supported_reasoning_efforts
                if effort.provider_model_id == model
            ),
            None,
        )

    def capabilities(self) -> StreamCapabilities:
        return StreamCapabilities(
            structured=self._last_error is None and self._current_mode == "default",
            adapter_id=self.adapter_id,
            schema_version=self.schema_version,
            supports_tool_timeline=True,
            supports_approval_ui=True,
            supports_images=self.supports_images,
            available_models=self._available_models,
            available_modes=self.available_modes(),
            current_mode=self._current_mode,
            current_model=self.current_model(),
            current_reasoning_effort=self.current_reasoning_effort(),
            unavailable_reason=self._last_error,
        )

    async def _with_first_turn_prefixes(self, text: str) -> Tuple[str, bool]:
        self._guidance_in_this_send = bool(text) and not self._hub_runtime_guidance_injected
        prompt, seeded = await super()._with_first_turn_prefixes(text)
        if prompt != text and text:
            # A service-side tool process cannot inherit this Hub tab's env.
            prompt = (
                "MASO HTTP transport: for Hub CLI tool calls, first set "
                f"CLAUDE_HUB_TAB_ID={shlex.quote(self.session.tab_id)} in that shell command. "
                "This is the current Chat's ID supplied by Hub.\n\n" + prompt
            )
        return prompt, seeded

    async def _ensure_session(self) -> Dict[str, Any]:
        if not self._conversation_id_verified:
            created = await self._request(
                "POST",
                "/api/sessions",
                json={
                    "agent_name": "MASOAgent",
                    "working_dir": self._cwd or None,
                    "model": self.session.env.get("MASO_MODEL") or None,
                    "isolation": "shared",
                    "inject_memory": False,
                },
            )
            if not isinstance(created, dict) or not isinstance(created.get("session_id"), str):
                raise RuntimeError("MASO returned an invalid session identity")
            self._persist_conversation_id(created["session_id"])
        status: Dict[str, Any] = await self._request("GET", self._path("status"))
        if not isinstance(status, dict) or not isinstance(status.get("agent_id"), str):
            raise RuntimeError("MASO returned an invalid session status")
        self._root_agent_id = status["agent_id"]
        self._effective_model = status.get("preferred_model") or status.get("model")
        # After a Hub crash the old display turn is interrupted by the tailer.
        # Stop its still-running service-side execution before a new prompt.
        if status.get("status") in {"running", "waiting", "aborting"} or self._message_id:
            status = await self._abort_and_wait()
        self._handshake_complete = True
        return status

    async def _abort_and_wait(self) -> Dict[str, Any]:
        self._needs_abort = True
        # MASO abort leaves broker messages queued and can re-wake them. Remove
        # this Hub-owned message before aborting; UUID correlation is not POST
        # idempotency. Retain the delivery sidecar until both steps succeed.
        if self._message_id:
            await self._request(
                "DELETE",
                self._path(f"messages/pending/{quote(self._message_id, safe='')}"),
            )
        await self._request("POST", self._path("control"), json={"action": "abort"})
        # MASO sets registry IDLE before its deferred cancellation finalizer
        # emits agent_end. Polling status (including indicator.running) cannot
        # fence that event from the next turn. exit closes the old SSE plugin
        # and awaits despawn's cancellation finalizers and persistence. The
        # next status request resumes this same saved session, with a fresh
        # plugin. A failed exit keeps recovery blocked before any new POST.
        await self._request("POST", self._path("exit"))
        status = await self._request("GET", self._path("status"))
        # A concurrent exit can return 200 while an earlier close is still
        # draining. Successful resume cannot register this same agent ID until
        # its old finalizers finish. An ended/non-live fallback is not proof.
        if not isinstance(status, dict) or not isinstance(status.get("agent_id"), str):
            raise RuntimeError("MASO did not confirm a live resumed session after exit")
        self._root_agent_id = status["agent_id"]
        self._effective_model = status.get("preferred_model") or status.get("model")
        if not self._delivery_uncertain:
            self._clear_delivery()
        self._needs_abort = False
        return status

    def _save_delivery(self, message_id: str, *, uncertain: bool = False) -> None:
        self._delivery_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._delivery_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "base_url": self._base,
                    "session_id": self._conversation_id,
                    "message_id": message_id,
                    "uncertain": uncertain,
                }
            )
        )
        temporary.replace(self._delivery_path)
        self._message_id = message_id
        self._delivery_uncertain = uncertain

    def _clear_delivery(self) -> None:
        self._delivery_path.unlink(missing_ok=True)
        self._message_id = None
        self._delivery_uncertain = False

    def acknowledge_turn_complete(self) -> None:
        # A completed root run consumed its message. A transport error may
        # still need an abort; keep that recovery identity until it succeeds.
        if not self._needs_abort:
            self._clear_delivery()
        super().acknowledge_turn_complete()

    async def _close_stream(self) -> None:
        self._accept_events = False
        self._pending_confirmations.clear()
        self._invalidate_stdout_stream()
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _stage_images(self, images: List[bytes]) -> None:
        # Validate the whole turn before creating a session or uploading any
        # image. MASO's limits are narrower than Hub's original-image input.
        for image in images:
            if len(image) > _MAX_IMAGE_BYTES:
                raise ValueError("MASO image input is limited to 8 MiB per image")
            if _detect_image_mime(image) not in _IMAGE_MIME_TYPES:
                raise ValueError("MASO image input requires PNG, JPEG, GIF or WebP")
        self._staged_images = list(images)

    def _clear_staged_images(self) -> None:
        self._staged_images = []

    async def _message_content(self, text: str, images: List[bytes]) -> Any:
        if not images:
            return text
        parts: List[Dict[str, str]] = [{"type": "text", "text": text}] if text else []
        try:
            async with asyncio.timeout(120.0):
                for image in images:
                    upload_id = uuid.uuid4().hex
                    uploaded: Any = None
                    for sequence, offset in enumerate(range(0, len(image), _IMAGE_CHUNK_BYTES)):
                        chunk = image[offset : offset + _IMAGE_CHUNK_BYTES]
                        uploaded = await self._request(
                            "POST",
                            self._path("images/chunk"),
                            params={
                                "upload_id": upload_id,
                                "seq": sequence,
                                "final": int(offset + len(chunk) == len(image)),
                            },
                            headers={"Content-Type": "application/octet-stream"},
                            content=chunk,
                            timeout=30.0,
                        )
                    ref = uploaded.get("ref") if isinstance(uploaded, dict) else None
                    if (
                        ref != upload_id
                        or uploaded.get("media_type") != _detect_image_mime(image)
                        or uploaded.get("bytes") != len(image)
                    ):
                        raise RuntimeError("MASO returned invalid image upload metadata")
                    parts.append({"type": "image", "image_ref": ref})
        except TimeoutError:
            raise RuntimeError("MASO image upload timed out") from None
        return parts

    async def _send_text(self, text: str) -> None:
        images, self._staged_images = self._staged_images, []
        try:
            await self._ensure_session()
            selected = self.session.env.get("MASO_MODEL")
            if selected and selected != self._effective_model:
                await self._request("PATCH", self._path("model"), json={"model": selected})
                self._effective_model = selected
            await self._close_stream()
            generation = self._stdout_generation
            if not self.turn_in_flight:
                raise RuntimeError("MASO turn was cancelled before message delivery")
            # Uploads cannot start a model turn. Finish them before subscribing
            # or recording uncertain delivery. Unused refs expire in MASO.
            content = await self._message_content(text, images)
            if not self.turn_in_flight or generation != self._stdout_generation:
                raise RuntimeError("MASO turn was cancelled before message delivery")
        except BaseException:
            # No prompt was posted, including when cancellation interrupts an
            # upload. Release the guard and retry first-turn guidance later.
            self._end_turn()
            if self._guidance_in_this_send:
                self._hub_runtime_guidance_injected = False
            raise
        self._root_started = False
        self._stream_ready = asyncio.get_running_loop().create_future()
        self._reader_task = asyncio.create_task(self._consume_stream(generation))
        post_started = False
        try:
            await asyncio.wait_for(asyncio.shield(self._stream_ready), 10.0)
            if not self.turn_in_flight or generation != self._stdout_generation:
                raise RuntimeError("MASO turn was cancelled before message delivery")
            message_id = str(uuid.uuid4())
            # Until the response arrives, a crash cannot distinguish rejection
            # from a request still enqueueing on the service. Persist that fact.
            self._save_delivery(message_id, uncertain=True)
            self._accept_events = True
            post_started = True
            await self._request(
                "POST",
                self._path("messages"),
                json={
                    "content": content,
                    "message_id": message_id,
                    "send_timing": "next_turn",
                },
            )
            if self._stream_failed_generation == generation:
                raise RuntimeError("MASO stream failed before message acceptance was confirmed")
            if self.turn_in_flight:
                self._save_delivery(message_id)
            else:
                self._clear_delivery()
        except (Exception, asyncio.CancelledError):
            await self._close_stream()
            if post_started:
                # DELETE cannot tombstone a message the server has not queued
                # yet. Keep uncertain identity even after best-effort abort;
                # a later send/restart must not silently reuse this session.
                try:
                    await self._abort_and_wait()
                except RuntimeError:
                    pass
                self._end_turn()
                raise RuntimeError(_UNCERTAIN_MESSAGE) from None
            self._end_turn()
            if self._guidance_in_this_send:
                self._hub_runtime_guidance_injected = False
            raise

    async def _consume_stream(self, generation: int) -> None:
        kind = ""
        data_lines: List[str] = []
        baseline = False
        try:
            async with self._http().stream("GET", self._path("stream"), timeout=None) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                    elif line == "":
                        if data_lines:
                            data = json.loads("\n".join(data_lines))
                            if isinstance(data, dict):
                                if kind == "indicator_snapshot":
                                    baseline = True
                                    if (
                                        self._stream_ready is not None
                                        and not self._stream_ready.done()
                                    ):
                                        self._stream_ready.set_result(None)
                                elif baseline and self._accept_events:
                                    root = data.get("agent_id") == self._root_agent_id
                                    if root and kind in {
                                        "agent_start",
                                        "turn_start",
                                        "agent_error",
                                    }:
                                        self._root_started = True
                                    if self._root_started:
                                        call_id = data.get("tool_call_id")
                                        agent_id = data.get("agent_id")
                                        if isinstance(call_id, str) and isinstance(agent_id, str):
                                            if kind == "confirmation_required":
                                                self._pending_confirmations[call_id] = agent_id
                                            elif kind == "confirmation_resolved":
                                                self._pending_confirmations.pop(call_id, None)
                                        await self._stdout_queue.put(
                                            (
                                                generation,
                                                {
                                                    "type": kind,
                                                    "data": data,
                                                    "root_agent_id": self._root_agent_id,
                                                    "generation": generation,
                                                },
                                            )
                                        )
                                        if kind == "agent_end" and root:
                                            return
                        kind, data_lines = "", []
            raise RuntimeError("MASO stream closed before the run ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stream_failed_generation = generation
            message = "MASO event stream disconnected; the turn was stopped"
            if self._stream_ready is not None and not self._stream_ready.done():
                self._stream_ready.set_exception(
                    RuntimeError("MASO event stream could not be opened")
                )
            elif self._accept_events and generation == self._stdout_generation:
                try:
                    await self._abort_and_wait()
                except RuntimeError:
                    message = (
                        "MASO event stream disconnected; execution stop could not be confirmed"
                    )
                    self._last_error = message
                await self._stdout_queue.put(
                    (
                        generation,
                        {
                            "type": "hub_transport_error",
                            "data": {"error": message},
                            "root_agent_id": self._root_agent_id,
                            "generation": generation,
                        },
                    )
                )

    def accepts_notification(self, record: Dict[str, Any]) -> bool:
        return record.get("generation") == self._stdout_generation

    async def answer_pending_question(
        self,
        answers: List[Dict[str, Any]],
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        if expected_call_id is not None and expected_call_id not in self._pending_confirmations:
            return False
        if not self._pending_confirmations:
            # Structured card answers must never fall through as new prompts,
            # including after Stop, completion, or a backend restart.
            raise ValueError("This MASO tool confirmation is no longer pending")
        decisions: List[Tuple[str, bool]] = []
        if not answers:
            decisions = [
                (call_id, False)
                for call_id in self._pending_confirmations
                if expected_call_id is None or call_id == expected_call_id
            ]
        for entry in answers:
            question_id = entry.get("questionId")
            if not isinstance(question_id, str) or not question_id.startswith("maso-confirm:"):
                continue
            call_id = question_id[len("maso-confirm:") :]
            if expected_call_id is not None and call_id != expected_call_id:
                continue
            if call_id not in self._pending_confirmations:
                continue
            selected = entry.get("selected")
            if selected not in (["Approve"], ["Reject"]):
                raise ValueError("Select Approve or Reject for a MASO tool confirmation")
            decisions.append((call_id, selected == ["Approve"]))
        if not decisions:
            raise ValueError("This answer does not match a pending MASO tool confirmation")
        for call_id, confirmed in decisions:
            await self._request(
                "POST",
                self._path("confirm"),
                json={
                    "tool_call_id": call_id,
                    "confirmed": confirmed,
                    "agent_id": self._pending_confirmations[call_id],
                },
            )
            self._pending_confirmations.pop(call_id, None)
        return True

    async def cancel_active_turn(self) -> None:
        if not self.turn_in_flight:
            return
        # Stop retires the Hub turn before service-side abort completes. Fence
        # late records immediately so they cannot reopen an unattributed turn.
        await self._close_stream()
        try:
            await self._abort_and_wait()
        finally:
            # Failed abort stays recorded in _needs_abort and the sidecar.
            # The next send retries only cleanup before submitting anything.
            self._end_turn()

    async def stop(self) -> None:
        try:
            if self.turn_in_flight and self._conversation_id_verified:
                await self.cancel_active_turn()
        finally:
            await self._close_stream()
            if self._client is not None:
                await self._client.aclose()
            self._started = False
            self._end_turn()

    def update_env(self, env: Dict[str, str]) -> None:
        env = dict(env)
        if not env.get("MASO_BASE_URL"):
            env["MASO_BASE_URL"] = self._base
        if maso_base_url(env) != self._base:
            raise ValueError("Create a new MASO Chat to change MASO_BASE_URL")
        super().update_env(env)
