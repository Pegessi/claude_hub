"""Heterogeneous cross-reviewer selection and MASO model-family pairing."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from ..models import AgentType, SessionKind, StreamModelOption
from . import maso_discovery
from .maso_discovery import discover_maso_service

logger = logging.getLogger(__name__)

_ORIGINAL_DISCOVER_MASO_SERVICE = maso_discovery.discover_maso_service

MasoProbeFn = Callable[[dict[str, str]], Awaitable[tuple[str, list[StreamModelOption], str]]]

DEFAULT_ARGON_REVIEW_MODEL = "gemini-4-argon_reasoning_high"
DEFAULT_ASTRA_REVIEW_MODEL = "gpt-6-astra_reasoning_high"


@dataclass(frozen=True)
class CrossReviewerTarget:
    """Resolved heterogeneous reviewer configuration."""

    agent_type: AgentType
    session_kind: SessionKind
    env: dict[str, str]
    reason: str = ""


def maso_model_family(env: Mapping[str, str] | None) -> str:
    """Classify a MASO_MODEL identifier into a canonical model family."""
    if not env:
        return ""
    raw = str(env.get("MASO_MODEL") or "").strip().lower()
    if not raw:
        return ""
    if "argon" in raw or "gemini" in raw:
        return "argon"
    if "astra" in raw or "gpt" in raw:
        return "astra"
    if "seed" in raw:
        return "seed"
    if "deepseek" in raw:
        return "deepseek"
    if "qwen" in raw:
        return "qwen"
    if "orange" in raw or "grape" in raw:
        return "orange"
    return raw.split("_", 1)[0]


def is_heterogeneous_reviewer(
    worker_agent_type: AgentType,
    worker_env: Mapping[str, str] | None,
    reviewer_agent_type: AgentType,
    reviewer_env: Mapping[str, str] | None,
) -> bool:
    """Return True when the reviewer uses a different provider or MASO model family."""
    if worker_agent_type != reviewer_agent_type:
        return True
    if worker_agent_type == AgentType.MASO and reviewer_agent_type == AgentType.MASO:
        worker_family = maso_model_family(worker_env)
        reviewer_family = maso_model_family(reviewer_env)
        if worker_family and reviewer_family and worker_family != reviewer_family:
            return True
    return False


def _advertised_model_ids(models: list[StreamModelOption]) -> set[str]:
    advertised: set[str] = set()
    for option in models:
        if option.id:
            advertised.add(option.id)
        if option.provider_model_id:
            advertised.add(option.provider_model_id)
        for effort in option.supported_reasoning_efforts:
            if effort.provider_model_id:
                advertised.add(effort.provider_model_id)
            elif effort.id and option.id:
                advertised.add(f"{option.id}_{effort.id}")
    return advertised


def _family_is_advertised(models: list[StreamModelOption], family_prefix: str) -> bool:
    if not models:
        return True
    prefix = family_prefix.lower()
    for model_id in _advertised_model_ids(models):
        if model_id.lower().startswith(prefix) or prefix in model_id.lower():
            return True
    return False


def _pick_alternate_non_family_model(
    models: list[StreamModelOption], excluded_family: str
) -> str | None:
    candidates = sorted(_advertised_model_ids(models))
    # Prefer high-reasoning variants from another family first.
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
    allow_maso_probe: bool = True,
    maso_probe: MasoProbeFn | None = None,
) -> CrossReviewerTarget | None:
    """Resolve a heterogeneous MASO reviewer target when a local MASO service is ready.

    Automatically skips live network probing during pytest runs unless
    ``CLAUDE_HUB_ENABLE_CROSS_REVIEW_PROBE_IN_TESTS=1``, an explicit ``maso_probe``
    callback is passed, or ``discover_maso_service`` has been monkeypatched.
    """
    if not allow_maso_probe:
        return None

    probe_fn = maso_probe
    if probe_fn is None:
        is_patched = (
            discover_maso_service is not _ORIGINAL_DISCOVER_MASO_SERVICE
            or maso_discovery.discover_maso_service is not _ORIGINAL_DISCOVER_MASO_SERVICE
        )
        in_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        enable_in_tests = os.environ.get("CLAUDE_HUB_ENABLE_CROSS_REVIEW_PROBE_IN_TESTS") == "1"
        if in_pytest and not enable_in_tests and not is_patched:
            return None
        probe_fn = (
            discover_maso_service
            if discover_maso_service is not _ORIGINAL_DISCOVER_MASO_SERVICE
            else maso_discovery.discover_maso_service
        )

    probe_env: dict[str, str] = {}
    if worker_env:
        for key in ("MASO_BASE_URL", "MASO_PORT"):
            if key in worker_env and worker_env[key]:
                probe_env[key] = str(worker_env[key])

    try:
        base_url, models, _default_model = await probe_fn(probe_env)
    except Exception:
        logger.debug("Cross-reviewer MASO discovery unavailable; falling back", exc_info=True)
        return None

    worker_family = maso_model_family(worker_env) if worker_agent_type == AgentType.MASO else ""
    chosen_model: str | None = None

    if worker_agent_type == AgentType.MASO and worker_family == "argon":
        if _family_is_advertised(models, "gpt-6-astra"):
            chosen_model = DEFAULT_ASTRA_REVIEW_MODEL
        else:
            chosen_model = _pick_alternate_non_family_model(models, "argon")
    else:
        if _family_is_advertised(models, "gemini-4-argon"):
            chosen_model = DEFAULT_ARGON_REVIEW_MODEL
        elif _family_is_advertised(models, "gpt-6-astra") and not (
            worker_agent_type == AgentType.MASO and worker_family == "astra"
        ):
            chosen_model = DEFAULT_ASTRA_REVIEW_MODEL
        else:
            chosen_model = _pick_alternate_non_family_model(models, worker_family or "astra")

    if not chosen_model:
        return None

    reviewer_env = {
        "MASO_BASE_URL": base_url,
        "MASO_MODEL": chosen_model,
    }
    if not is_heterogeneous_reviewer(
        worker_agent_type, worker_env or {}, AgentType.MASO, reviewer_env
    ):
        return None

    return CrossReviewerTarget(
        agent_type=AgentType.MASO,
        session_kind=SessionKind.CHAT,
        env=reviewer_env,
        reason=f"heterogeneous cross-review via maso:{chosen_model}",
    )
