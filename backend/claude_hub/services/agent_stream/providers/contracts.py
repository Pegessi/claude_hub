"""Typed dependency contract between compatibility sessions and implementations."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Set,
    Tuple,
    TypedDict,
)

from ....models import StreamModelOption, StreamModeOption

ProviderGoalOrigin = Literal["user", "automatic"]


class ProviderGoal(TypedDict, total=False):
    """Provider-authored Goal data; the provider owns its lifecycle and accounting."""

    id: str
    threadId: str
    objective: str
    status: str
    tokenBudget: Optional[int]
    tokensUsed: int
    timeUsedSeconds: int
    createdAt: int
    updatedAt: int
    metadata: Dict[str, Any]


class ProviderGoalSnapshot(TypedDict):
    """A supported native Goal API, including a confirmed absence of a Goal."""

    thread_id: str
    goal: Optional[ProviderGoal]


class ProviderRpcError(RuntimeError):
    """Keep protocol error codes separate from transport failures and prose."""

    def __init__(self, message: str, *, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class RuntimeGuidance(Protocol):
    def __call__(self, text: str, *, guidance: Optional[str] = ..., context: str = "") -> str: ...


@dataclass(frozen=True)
class ProviderRuntime:
    """Stateless callbacks, explicitly provided by each concrete session owner."""

    default_mode: Callable[[], StreamModeOption]
    plan_mode: Callable[[], StreamModeOption]
    stderr_buffer_max: Callable[[], int]
    startup_grace_s: Callable[[], float]
    question_methods: Callable[[], Tuple[str, ...]]
    reasoning_efforts: Callable[[], Set[str]]
    reasoning_labels: Callable[[], Dict[str, str]]
    question_guidance: Callable[[], str]
    chat_network_env: Callable[[], str]
    network_mode_env: Callable[[], str]
    proxy_env_names: Callable[[], Tuple[str, ...]]
    logger: Callable[[], logging.Logger]
    claude_env_options: Callable[[Dict[str, str]], List[StreamModelOption]]
    available_models_for: Callable[[str], Awaitable[List[StreamModelOption]]]
    provider_subprocess_env: Callable[[str, Mapping[str, str], Mapping[str, str]], Dict[str, str]]
    redact_diagnostic: Callable[[str], str]
    select_provider_env: Callable[[str, Mapping[str, str]], Awaitable[Dict[str, str]]]
    network_fingerprint: Callable[[str, Mapping[str, str]], str]
    wrap_fork_seed_history: Callable[[str], str]
    wrap_runtime_guidance: Callable[[], RuntimeGuidance]
    runtime_home: Callable[[], Path]
    help_confirms_plan_mode: Callable[[str, str], bool]
    detect_image_mime: Callable[[bytes], Optional[str]]
    codex_image_temp_dir: Callable[[], Path]
    cursor_image_temp_dir: Callable[[], Path]
    mime_extension: Callable[[str], str]
    resolve_endpoint: Callable[[str, Mapping[str, str]], Optional[str]]
    env_flag: Callable[[Dict[str, str], str], bool]
    normalize_questions: Callable[[Any], List[Dict[str, Any]]]
    wrap_image_guidance: Callable[[str, List[Path]], str]
    wrap_question_guidance: Callable[[str], str]
