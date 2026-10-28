"""Native provider transport sessions for the Paseo Chat surface.

A :class:`ProviderSession` owns the provider subprocess (or persistent
JSON-RPC connection) and exposes a uniform ``send_message`` / ``read_line``
interface to the tailer. ``send_message`` is the single input entry point;
text and images are delivered atomically under one turn guard.

Provider-specific lifecycle:

* **Claude / Cursor** — ``--print`` invocations are one-shot turns. Each
  ``send_message`` spawns a fresh streaming subprocess with the prompt on
  stdin; the provider's conversation id (from the first ``message_start``
  event) is captured so the next turn can ``--resume`` it. No persistent
  process sits idle between turns. Cursor has no structured image-input
  flag, so attached images are staged to temp files and referenced by
  absolute path in a sentinel-wrapped prompt block (the model reads them
  with its Read tool); the files are deleted when the process exits.

* **Codex** — ``codex app-server --stdio`` is a persistent JSON-RPC server.
  ``start`` launches it and runs the ``initialize`` handshake (with
  ``clientInfo``) followed by the ``initialized`` notification, then creates
  or resumes a thread. ``send_message`` issues a ``turn/start`` request with
  ``input: [{type: "text", text: ...}]``. Responses are dispatched to
  per-request Futures; notifications (``AgentMessageDelta``,
  ``ReasoningTextDelta``, ``TurnStarted``, ``TurnCompleted``, ...) are
  forwarded to the notification queue consumed by :meth:`read_line`.

Fail-closed contract:

* If the binary is missing, the handshake times out, or the process exits
  before a recognized event, the session reports ``structured=False`` and
  the API surfaces the last error.
* Image staging raises ``NotImplementedError`` only when the provider does
  not support image input; otherwise images are injected into the turn.
* ``structured`` is only ``True`` after the provider has emitted a
  recognized event (handshake / first delta), never merely because the
  process launched.
* A send lock prevents concurrent turns; a second ``send_message`` while a
  turn is in flight raises rather than cancelling the active turn.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple, TypedDict
from uuid import uuid4

import claude_hub.services.agent_stream.providers.guidance as provider_guidance
import claude_hub.services.agent_stream.providers.model_options as provider_model_options

from ...config import settings
from ...models import (
    AgentType,
    ChatMode,
    ManagedSession,
    StreamCapabilities,
    StreamModelOption,
    StreamModeOption,
    StreamReasoningEffortOption,
)
from ..agent_execution_policy import EXECUTION_POLICY as EXECUTION_POLICY
from ..provider_proxy import provider_subprocess_env, redact_provider_proxy_diagnostic
from .fork_seed import wrap_fork_seed_history
from .provider_network import (
    PROVIDER_NETWORK_MODE_ENV,
    PROXY_ENV_NAMES,
    ProviderEndpointUnknownError,
    ProviderNetworkError,
    provider_network_configuration_fingerprint,
    resolve_provider_endpoint,
    select_provider_subprocess_env,
)
from .providers import images as provider_images
from .providers.claude import ClaudeSession
from .providers.codex_rpc import CodexProtocolSession
from .providers.contracts import ModelCatalog
from .providers.contracts import ProviderGoal as ProviderGoal
from .providers.contracts import ProviderRuntime
from .providers.cursor import CursorSession
from .providers.process import ProcessSession

# Preserve the legacy import and monkeypatch targets used by adapters/sessions.
from .providers.questions import codex_normalize_questions as codex_normalize_questions
from .providers.questions import parse_ask_question_response as parse_ask_question_response
from .providers.spec import TransportKind, get_spec
from .providers.traex import TraexSession

logger = logging.getLogger(__name__)


# How long to wait for the provider's first recognized event before declaring
# the transport unavailable.
_STARTUP_GRACE_S = 10.0

# Maximum stderr bytes buffered per session (sanitized, bounded).
_STDERR_BUFFER_MAX = 64 * 1024

# Maximum image size accepted (20 MiB). Larger payloads are rejected before
# being base64-encoded into the user message envelope.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Codex app-server server→client request methods that ask the user a question.
# The turn blocks until the client answers with a matching JSON-RPC response.
# ``item/tool/requestUserInput`` is the current method; ``tool/requestUserInput``
# is the legacy alias used by older builds.
_CODEX_QUESTION_METHODS = ("item/tool/requestUserInput", "tool/requestUserInput")

# Keep native's call-time lookup surface for transport and adapter patches.
# Helpers receive these values explicitly instead of importing native back.
QUESTION_PROTOCOL_GUIDANCE = provider_guidance.QUESTION_PROTOCOL_GUIDANCE
_HUB_QUESTION_PROTOCOL_START = provider_guidance._HUB_QUESTION_PROTOCOL_START
_HUB_QUESTION_PROTOCOL_END = provider_guidance._HUB_QUESTION_PROTOCOL_END
_HUB_IMAGE_ATTACHMENT_START = provider_guidance._HUB_IMAGE_ATTACHMENT_START
_HUB_IMAGE_ATTACHMENT_END = provider_guidance._HUB_IMAGE_ATTACHMENT_END
_IMAGE_ATTACHMENT_GUIDANCE = provider_guidance._IMAGE_ATTACHMENT_GUIDANCE
HUB_RUNTIME_GUIDANCE = provider_guidance.HUB_RUNTIME_GUIDANCE
_HUB_RUNTIME_START = provider_guidance._HUB_RUNTIME_START
_HUB_RUNTIME_END = provider_guidance._HUB_RUNTIME_END
_HUB_RUNTIME_MARKERS = provider_guidance._HUB_RUNTIME_MARKERS


def wrap_question_protocol_guidance(text: str) -> str:
    """Prefix Cursor question guidance, stripped on transcript read."""
    return provider_guidance.wrap_guidance_block(
        text,
        body=QUESTION_PROTOCOL_GUIDANCE,
        opening=_HUB_QUESTION_PROTOCOL_START,
        closing=_HUB_QUESTION_PROTOCOL_END,
    )


def strip_question_protocol_guidance(text: str) -> str:
    """Strip the first question block without truncating malformed user text."""
    return provider_guidance.strip_guidance_block(
        text, markers=((_HUB_QUESTION_PROTOCOL_START, _HUB_QUESTION_PROTOCOL_END),)
    )


def wrap_image_attachment_guidance(text: str, image_paths: List[Path]) -> str:
    """Prefix staged Cursor image paths, stripped on transcript read."""
    return provider_guidance.wrap_image_attachment_guidance(
        text,
        image_paths,
        guidance=_IMAGE_ATTACHMENT_GUIDANCE,
        opening=_HUB_IMAGE_ATTACHMENT_START,
        closing=_HUB_IMAGE_ATTACHMENT_END,
    )


def strip_image_attachment_guidance(text: str) -> str:
    """Strip the first image block without truncating malformed user text."""
    return provider_guidance.strip_guidance_block(
        text, markers=((_HUB_IMAGE_ATTACHMENT_START, _HUB_IMAGE_ATTACHMENT_END),)
    )


def wrap_hub_runtime_guidance(
    text: str, *, guidance: Optional[str] = HUB_RUNTIME_GUIDANCE, context: str = ""
) -> str:
    """Prefix hidden runtime advice and optional per-turn context."""
    return provider_guidance.wrap_hub_runtime_guidance(
        text,
        guidance=guidance,
        context=context,
        opening=_HUB_RUNTIME_START,
        closing=_HUB_RUNTIME_END,
        markers=_HUB_RUNTIME_MARKERS,
    )


def strip_hub_runtime_guidance(text: str) -> str:
    """Strip the first current or legacy runtime block, preserving malformed text."""
    return provider_guidance.strip_guidance_block(text, markers=_HUB_RUNTIME_MARKERS)


_DEFAULT_MODE = StreamModeOption(
    id=ChatMode.DEFAULT.value,
    label="Default",
    description="Use the provider's normal execution mode.",
)
_PLAN_MODE = StreamModeOption(
    id=ChatMode.PLAN.value,
    label="Plan",
    description="Analyze and propose a plan without modifying workspace files.",
)


def native_provider_binary(agent_type: AgentType) -> Optional[str]:
    """Return the native Chat executable for a provider, if supported."""

    return get_spec(agent_type).binary


def native_provider_binary_available(agent_type: AgentType) -> bool:
    # HTTP providers need no local executable. Their capabilities probe verifies
    # connectivity; a cold tab can be opened before that asynchronous probe.
    # The fail-closed unknown/terminal spec carries no binary, so this stays
    # False for anything without an explicitly registered executable.
    spec = get_spec(agent_type)
    if spec.transport is TransportKind.HTTP_SSE:
        return True
    return spec.binary is not None and shutil.which(spec.binary) is not None


@lru_cache(maxsize=8)
def _help_confirms_plan_mode(binary: str, flag: str) -> bool:
    """Probe installed CLI help once; absence/timeouts fail closed."""

    try:
        completed = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    return completed.returncode == 0 and flag in output and "plan" in output


# ── available models ────────────────────────────────────────────────────────
#
# Cursor's ``agent`` CLI can list its current model catalog (``--list-models``),
# so the picker is discovered at runtime instead of hardcoded. Claude uses the
# selected tab's model environment because a custom Anthropic-compatible
# gateway may expose ids that no global catalog can know. Providers without a
# discovered or configured catalog fall back to a curated static list. Shared
# catalogs are cached per agent type with a TTL so we do not spawn a probe on
# every capabilities fetch.

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_MARKER_RE = re.compile(r"\s*\((default|current)\)\s*$", re.IGNORECASE)
_MODELS_POSITIVE_TTL_S = 600.0  # successful probe: 10 minutes
_MODELS_NEGATIVE_TTL_S = 60.0  # failed probe (static fallback): 1 minute
_REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_REASONING_LABELS = {
    "none": "None",
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
    "ultra": "Ultra",
}

_STATIC_MODELS: Dict[str, List[str]] = {
    "claude": [
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "doubao-seed-2.0-code",
    ],
    "codex": [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
    ],
    "traex": [
        # Slugs as reported by ``traex models`` / thread/start ``model``.
        "Seed-Evolving",
        "Seed-2.1-Pro",
        "Seed-2.1-Turbo",
        "Seed-Code",
        "Seed-Dogfooding-2.0",
        "GPT-6-Astra",
        "GPT-5.6-Sol",
        "GPT-5.6-Terra",
        "GPT-5.6-Luna",
        "GPT-5.5",
        "GPT-5.4",
        "GPT-5.2",
        "DeepSeek-V4-Pro",
        "DeepSeek-V4-Flash",
        "Gemini-3.1-Pro-Preview",
        "Gemini-3-Flash-Preview",
        "openrouter-3o",
        "openrouter-2o",
        "openrouter-1o",
        "openrouter-1",
    ],
    "cursor": [
        "claude-opus-4-8-thinking-high",
        "claude-opus-4-8-high",
        "claude-4.6-sonnet-medium-thinking",
        "claude-4.6-sonnet-medium",
        "claude-4.5-sonnet",
        "gpt-5.2",
        "gpt-5.3-codex",
        "gemini-3.7-flash-high",
        "cursor-grok-4.6-high",
    ],
}
_CLAUDE_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)


def _static_options(agent_type: str) -> List[StreamModelOption]:
    return provider_model_options.model_options_from_ids(_STATIC_MODELS.get(agent_type, []))


def _claude_env_options(env: Dict[str, str]) -> List[StreamModelOption]:
    return provider_model_options.claude_env_options(env, model_env_keys=_CLAUDE_MODEL_ENV_KEYS)


def _parse_list_models(raw: str) -> List[StreamModelOption]:
    return provider_model_options.parse_list_models(
        raw, osc_re=_OSC_RE, ansi_re=_ANSI_RE, marker_re=_MARKER_RE
    )


def _group_cursor_model_options(options: List[StreamModelOption]) -> List[StreamModelOption]:
    return provider_model_options.group_cursor_model_options(
        options, reasoning_efforts=_REASONING_EFFORTS, reasoning_labels=_REASONING_LABELS
    )


def _safe_kill(proc: Any) -> None:
    """Kill a probe subprocess, ignoring the race where it already exited."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _probe_cursor_models() -> Tuple[List[StreamModelOption], bool]:
    """Run ``agent --list-models`` and return ``(options, is_fallback)``.

    ``is_fallback=True`` means the probe failed (binary missing, timeout, or
    unparseable output) and the static curated list was returned instead; the
    caller applies the short negative TTL so recovery is quick.

    The probe uses the backend process plus deployment Cursor proxy defaults,
    rather than the session env: the catalog is cached globally per agent type, so a
    session-specific ``agent`` binary or credentials would leak one session's
    models into another's. The backend runs as the user, so ``agent`` auth
    works.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "agent",
            "--list-models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=provider_subprocess_env("cursor", os.environ),
        )
    except (OSError, NotImplementedError):
        return _group_cursor_model_options(_static_options("cursor")), True
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=settings.cursor_models_probe_timeout_seconds
        )
    except asyncio.CancelledError:
        # The calling task was cancelled mid-probe. ``wait_for`` propagates
        # this without killing the process, so do so explicitly to avoid a
        # leaked (possibly hung) probe, then re-raise.
        _safe_kill(proc)
        raise
    except Exception:
        # Timeout or other failure: ``wait_for`` cancels the communicate
        # coroutine but does not kill the underlying process.
        _safe_kill(proc)
        return _group_cursor_model_options(_static_options("cursor")), True
    parsed = _group_cursor_model_options(
        _parse_list_models(stdout.decode("utf-8", errors="replace"))
    )
    return (
        (parsed, False)
        if parsed
        else (
            _group_cursor_model_options(_static_options("cursor")),
            True,
        )
    )


@dataclass
class _ModelsCacheEntry:
    options: List[StreamModelOption]
    expires_at: float
    metadata_cacheable: bool = True


_models_cache: Dict[str, _ModelsCacheEntry] = {}
_models_lock = asyncio.Lock()


async def model_catalog_for(agent_type: str) -> ModelCatalog:
    """Return the model catalog and whether long-lived metadata caching is safe."""
    entry = _models_cache.get(agent_type)
    now = time.monotonic()
    if entry is not None and entry.expires_at > now:
        return ModelCatalog(entry.options, metadata_cacheable=entry.metadata_cacheable)
    async with _models_lock:
        entry = _models_cache.get(agent_type)
        now = time.monotonic()
        if entry is not None and entry.expires_at > now:
            return ModelCatalog(entry.options, metadata_cacheable=entry.metadata_cacheable)
        metadata_cacheable = True
        if agent_type == "cursor":
            options, is_fallback = await _probe_cursor_models()
            ttl = _MODELS_NEGATIVE_TTL_S if is_fallback else _MODELS_POSITIVE_TTL_S
            metadata_cacheable = not is_fallback
        else:
            options = _static_options(agent_type)
            ttl = _MODELS_POSITIVE_TTL_S
        _models_cache[agent_type] = _ModelsCacheEntry(
            options, now + ttl, metadata_cacheable=metadata_cacheable
        )
        return ModelCatalog(options, metadata_cacheable=metadata_cacheable)


async def available_models_for(agent_type: str) -> List[StreamModelOption]:
    """Return the available models for an agent type, cached with a TTL.

    Cursor is discovered at runtime via ``agent --list-models``; other agent
    types return the curated static list. A single global lock with
    double-checked checking dedups concurrent probes (contention is negligible:
    one probe per TTL per agent type).
    """
    return (await model_catalog_for(agent_type)).options


# Keep the runtime-home and per-provider directory lookups at the public seam.
_CODEX_IMAGE_TEMP_DIR_NAME = "codex-images"
_CURSOR_IMAGE_TEMP_DIR_NAME = "cursor-images"


def _detect_image_mime(data: bytes) -> Optional[str]:
    return provider_images.detect_image_mime(data, max_image_bytes=_MAX_IMAGE_BYTES)


def _mime_extension(mime: str) -> str:
    return provider_images.mime_extension(mime)


def _runtime_home() -> Path:
    """Return the runtime home directory (isolated per worktree)."""
    from ...services.runtime_isolation import resolve_runtime_home

    return resolve_runtime_home()


def _codex_image_temp_dir() -> Path:
    return provider_images.image_temp_dir(_runtime_home, _CODEX_IMAGE_TEMP_DIR_NAME)


def _cursor_image_temp_dir() -> Path:
    return provider_images.image_temp_dir(_runtime_home, _CURSOR_IMAGE_TEMP_DIR_NAME)


def cleanup_codex_temp_dir(max_age_seconds: Optional[float] = None) -> int:
    return provider_images.cleanup_image_temp_dir(_codex_image_temp_dir(), max_age_seconds)


def cleanup_cursor_temp_dir(max_age_seconds: Optional[float] = None) -> int:
    return provider_images.cleanup_image_temp_dir(_cursor_image_temp_dir(), max_age_seconds)


HUB_CHAT_NETWORK_ENV = "HUB_CHAT_ALLOW_NETWORK"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on", "y"})


def _env_flag(env: Dict[str, str], key: str) -> bool:
    value = env.get(key)
    return isinstance(value, str) and value.strip().lower() in _TRUTHY_ENV_VALUES


# Frozen callbacks retain old module lookup semantics without reverse imports.
_NATIVE_RUNTIME = ProviderRuntime(
    default_mode=lambda: _DEFAULT_MODE,
    plan_mode=lambda: _PLAN_MODE,
    stderr_buffer_max=lambda: _STDERR_BUFFER_MAX,
    startup_grace_s=lambda: _STARTUP_GRACE_S,
    question_methods=lambda: _CODEX_QUESTION_METHODS,
    reasoning_efforts=lambda: _REASONING_EFFORTS,
    reasoning_labels=lambda: _REASONING_LABELS,
    question_guidance=lambda: QUESTION_PROTOCOL_GUIDANCE,
    chat_network_env=lambda: HUB_CHAT_NETWORK_ENV,
    network_mode_env=lambda: PROVIDER_NETWORK_MODE_ENV,
    proxy_env_names=lambda: PROXY_ENV_NAMES,
    logger=lambda: logger,
    claude_env_options=lambda env: _claude_env_options(env),
    model_catalog_for=lambda agent_type: model_catalog_for(agent_type),
    provider_subprocess_env=lambda provider, parent, overrides: provider_subprocess_env(
        provider, parent, overrides
    ),
    redact_diagnostic=lambda text: redact_provider_proxy_diagnostic(text),
    select_provider_env=lambda provider, env: select_provider_subprocess_env(provider, env),
    network_fingerprint=lambda provider, env: provider_network_configuration_fingerprint(
        provider, env
    ),
    wrap_fork_seed_history=lambda text: wrap_fork_seed_history(text),
    runtime_home=lambda: _runtime_home(),
    help_confirms_plan_mode=lambda binary, flag: _help_confirms_plan_mode(binary, flag),
    detect_image_mime=lambda data: _detect_image_mime(data),
    codex_image_temp_dir=lambda: _codex_image_temp_dir(),
    cursor_image_temp_dir=lambda: _cursor_image_temp_dir(),
    mime_extension=lambda mime: _mime_extension(mime),
    resolve_endpoint=lambda provider, env: resolve_provider_endpoint(provider, env),
    env_flag=lambda env, key: _env_flag(env, key),
    normalize_questions=lambda raw: codex_normalize_questions(raw),
    wrap_image_guidance=lambda text, paths: wrap_image_attachment_guidance(text, paths),
    wrap_question_guidance=lambda text: wrap_question_protocol_guidance(text),
    wrap_runtime_guidance=lambda: wrap_hub_runtime_guidance,
)


class ProviderSession(ProcessSession):
    """Public native session contract with call-time compatibility dependencies."""

    @property
    def _runtime(self) -> ProviderRuntime:
        return _NATIVE_RUNTIME


class ClaudeNativeSession(ClaudeSession, ProviderSession):
    """Claude one-shot transport."""


class CodexNativeSession(CodexProtocolSession, ProviderSession):
    """Codex app-server transport."""


class TraexNativeSession(TraexSession, CodexNativeSession):
    """TraeX app-server transport with Codex-compatible ancestry."""


class CursorNativeSession(CursorSession, ProviderSession):
    """Cursor one-shot transport."""


# ── factory ──────────────────────────────────────────────────────────────────

# Dispatch by registry-declared provider; MASO stays a lazy import because
# maso_http imports symbols from this module at load time. Unknown transports
# raise in create_native_session below.
_NATIVE_SESSION_CLASSES: Dict[AgentType, type["ProviderSession"]] = {
    AgentType.CLAUDE: ClaudeNativeSession,
    AgentType.CODEX: CodexNativeSession,
    AgentType.TRAEX: TraexNativeSession,
    AgentType.CURSOR: CursorNativeSession,
}


def create_native_session(
    session: ManagedSession,
    conversation_id_persist: Optional[Callable[[str], None]] = None,
    seed_history: Optional[str] = None,
    on_seed_consumed: Optional[Callable[[], Awaitable[None]]] = None,
) -> ProviderSession:
    """Return the native transport for ``session``'s agent type.

    ``seed_history`` / ``on_seed_consumed`` carry a forked tab's truncated
    transcript. Every supported transport (Claude / Codex / TraeX / Cursor)
    accepts ordinary prompt text, so the seed is delivered as a sentinel
    text block on the first turn for all of them — no provider needs a
    special prefill field and there is no silent "UI has history, model does
    not" gap.

    Raises ``ValueError`` for unsupported agent types (fail-closed).
    """
    kwargs: Dict[str, Any] = {
        "conversation_id_persist": conversation_id_persist,
        "seed_history": seed_history,
        "on_seed_consumed": on_seed_consumed,
    }
    session_cls = _NATIVE_SESSION_CLASSES.get(session.agent_type)
    if session_cls is None:
        if session.agent_type == AgentType.MASO:
            from .maso_http import MasoHttpSession

            session_cls = MasoHttpSession
        else:
            raise ValueError(f"no native transport for agent_type={session.agent_type}")
    return session_cls(session, **kwargs)
