"""Heterogeneous cross-reviewer routing for Workspace tasks.

Selects an independent reviewer from a different model family or provider than
the worker that produced the implementation, preventing same-model self-review
when a cross-model reviewer is available.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

from claude_hub.models import AgentType, SessionKind, StreamModelOption
from claude_hub.services import maso_discovery
from claude_hub.services.maso_discovery import discover_maso_service

logger = logging.getLogger(__name__)

DEFAULT_ARGON_REVIEW_MODEL = "gemini-4-argon_reasoning_high"
DEFAULT_ASTRA_REVIEW_MODEL = "gpt-6-astra_reasoning_high"
_ORIGINAL_DISCOVER_MASO_SERVICE = maso_discovery.discover_maso_service

MasoProbeFn = Callable[
    [dict[str, str]],
    Awaitable[tuple[str, list[StreamModelOption], str | None]],
]


@dataclass(frozen=True)
class CrossReviewerTarget:
    """Resolved heterogeneous reviewer launch configuration."""

    agent_type: AgentType
    session_kind: SessionKind
    env: dict[str, str]
    default_maso_model: str | None = None


def maso_model_family(
    env: Mapping[str, str] | None,
    *,
    default_model: str | None = None,
) -> str:
    """Classify a MASO_MODEL string (or service default_model fallback) into a canonical model family."""
    raw = str((env or {}).get("MASO_MODEL") or default_model or "").strip().lower()
    if not raw:
        return ""
    if "argon" in raw or "gemini" in raw:
        return "argon"
    if "astra" in raw:
        return "astra"
    if "seed" in raw:
        return "seed"
    if "deepseek" in raw:
        return "deepseek"
    if "qwen" in raw:
        return "qwen"
    if "orange" in raw or "grape" in raw:
        return "orange"
    if "gpt" in raw:
        return "gpt"
    return raw.split("_", 1)[0].split("-", 1)[0]


def is_heterogeneous_reviewer(
    worker_agent_type: AgentType,
    worker_env: Mapping[str, str] | None,
    reviewer_agent_type: AgentType,
    reviewer_env: Mapping[str, str] | None,
    *,
    default_maso_model: str | None = None,
) -> bool:
    """Return True iff the reviewer uses a different provider or MASO model family than the worker."""
    if worker_agent_type != reviewer_agent_type:
        return True
    if worker_agent_type == AgentType.MASO and reviewer_agent_type == AgentType.MASO:
        worker_family = maso_model_family(worker_env, default_model=default_maso_model)
        reviewer_family = maso_model_family(reviewer_env, default_model=default_maso_model)
        if not worker_family or not reviewer_family:
            return False
        return worker_family != reviewer_family
    return False


def _advertised_model_ids(models: list[StreamModelOption]) -> set[str]:
    ids: set[str] = set()
    for option in models:
        if option.id:
            ids.add(option.id)
        if option.provider_model_id:
            ids.add(option.provider_model_id)
        for effort in option.supported_reasoning_efforts or []:
            if effort.provider_model_id:
                ids.add(effort.provider_model_id)
    return ids


def _pick_family_model(
    models: list[StreamModelOption],
    *,
    preferred_full_id: str,
    base_prefix: str,
    target_family: str,
) -> str | None:
    if not models:
        return preferred_full_id
    advertised = _advertised_model_ids(models)
    if preferred_full_id in advertised or base_prefix in advertised:
        return preferred_full_id
    for candidate in sorted(advertised):
        if maso_model_family({"MASO_MODEL": candidate}) == target_family:
            if candidate == base_prefix:
                return preferred_full_id
            return candidate
    return None


def _pick_alternate_non_family_model(
    models: list[StreamModelOption], excluded_family: str
) -> str | None:
    candidates = sorted(_advertised_model_ids(models))
    for preferred_tag in (
        "_reasoning_high",
        "-reasoning-high",
        "_reasoning",
        "-reasoning",
        "_thinking",
        "-thinking",
        "",
    ):
        for model_id in candidates:
            if preferred_tag and preferred_tag not in model_id:
                continue
            if maso_model_family({"MASO_MODEL": model_id}) != excluded_family:
                return model_id
    return None


async def resolve_cross_reviewer_target(
    worker_agent_type: AgentType,
    worker_env: Mapping[str, str] | None = None,
    *,
    maso_probe: MasoProbeFn | None = None,
) -> CrossReviewerTarget | None:
    """Resolve a heterogeneous Chat reviewer target, or return None if unavailable.

    Pairing matrix:
    - Worker is ``AgentType.MASO`` with Argon/Gemini family (explicit or via MASO
      service ``default_model``) -> MASO ``gpt-6-astra_reasoning_high`` (or alternate
      non-Argon model).
    - Worker is ``AgentType.MASO`` with Astra/GPT/Seed/DeepSeek/other family (or
      empty default model) -> MASO ``gemini-4-argon_reasoning_high`` (or alternate
      non-worker-family model).
    - Worker is ``claude`` / ``codex`` / ``cursor`` / ``traex`` / ``antigravity`` ->
      MASO ``gemini-4-argon_reasoning_high`` (falling back to ``gpt-6-astra_reasoning_high``).
    """
    env = dict(worker_env or {})
    if (
        maso_probe is None
        and os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("CLAUDE_HUB_ENABLE_CROSS_REVIEW_PROBE_IN_TESTS") != "1"
        and discover_maso_service is _ORIGINAL_DISCOVER_MASO_SERVICE
        and maso_discovery.discover_maso_service is _ORIGINAL_DISCOVER_MASO_SERVICE
    ):
        return None
    if maso_probe is not None:
        probe = maso_probe
    elif discover_maso_service is not _ORIGINAL_DISCOVER_MASO_SERVICE:
        probe = discover_maso_service
    else:
        probe = maso_discovery.discover_maso_service

    probe_env: dict[str, str] = {}
    if env.get("MASO_BASE_URL"):
        probe_env["MASO_BASE_URL"] = env["MASO_BASE_URL"]

    try:
        base_url, models, default_model = await probe(probe_env)
    except Exception:
        logger.debug(
            "Local MASO service unavailable for heterogeneous cross-review; falling back",
            exc_info=True,
        )
        return None

    selected_model: str | None = None
    if worker_agent_type == AgentType.MASO:
        worker_family = maso_model_family(env, default_model=default_model)
        if worker_family == "argon":
            selected_model = _pick_family_model(
                models,
                preferred_full_id=DEFAULT_ASTRA_REVIEW_MODEL,
                base_prefix="gpt-6-astra",
                target_family="astra",
            ) or _pick_alternate_non_family_model(models, "argon")
        else:
            effective_excluded = worker_family or "other"
            selected_model = _pick_family_model(
                models,
                preferred_full_id=DEFAULT_ARGON_REVIEW_MODEL,
                base_prefix="gemini-4-argon",
                target_family="argon",
            ) or _pick_alternate_non_family_model(models, effective_excluded)
    else:
        selected_model = (
            _pick_family_model(
                models,
                preferred_full_id=DEFAULT_ARGON_REVIEW_MODEL,
                base_prefix="gemini-4-argon",
                target_family="argon",
            )
            or _pick_family_model(
                models,
                preferred_full_id=DEFAULT_ASTRA_REVIEW_MODEL,
                base_prefix="gpt-6-astra",
                target_family="astra",
            )
            or _pick_alternate_non_family_model(models, "")
        )

    if not selected_model:
        return None

    reviewer_env = {
        "MASO_BASE_URL": base_url,
        "MASO_MODEL": selected_model,
    }
    if not is_heterogeneous_reviewer(
        worker_agent_type,
        env,
        AgentType.MASO,
        reviewer_env,
        default_maso_model=default_model,
    ):
        if worker_agent_type == AgentType.MASO and not maso_model_family(
            env, default_model=default_model
        ):
            # Worker had no explicit or advertised default model; setting an explicit
            # MASO_MODEL on the reviewer still gives a deterministic reviewer target.
            return CrossReviewerTarget(
                agent_type=AgentType.MASO,
                session_kind=SessionKind.CHAT,
                env=reviewer_env,
                default_maso_model=default_model,
            )
        return None

    return CrossReviewerTarget(
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
        env=reviewer_env,
        default_maso_model=default_model,
    )
