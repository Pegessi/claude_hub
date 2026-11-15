"""Heterogeneous, quota-aware cross-reviewer routing for Workspace tasks and Chat Goals.

Selects an independent reviewer from a different model family or provider than
the worker that produced the implementation, preventing same-model self-review
and skipping quota-exhausted reviewer targets when readings are available.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Sequence

from claude_hub.models import AgentType, ProviderQuota, SessionKind, StreamModelOption
from claude_hub.services import maso_discovery
from claude_hub.services.maso_discovery import discover_maso_service

logger = logging.getLogger(__name__)

DEFAULT_ARGON_REVIEW_MODEL = "gemini-4-argon_reasoning_high"
DEFAULT_ASTRA_REVIEW_MODEL = "gpt-6-astra_reasoning_high"
_ORIGINAL_DISCOVER_MASO_SERVICE = maso_discovery.discover_maso_service
_QUOTA_READ_TIMEOUT_SECONDS = 1.5
_MASO_VARIANT_SUFFIX_RE = re.compile(
    r"(?:[_-]reasoning(?:[_-](?:xhigh|high|medium|low|max))?"
    r"|[_-]thinking(?:[_-](?:xhigh|high|medium|low|max))?)$",
    re.IGNORECASE,
)

MasoProbeFn = Callable[
    [dict[str, str]],
    Awaitable[tuple[str, list[StreamModelOption], str | None]],
]


@dataclass(frozen=True)
class CrossReviewerTarget:
    """Resolved heterogeneous reviewer launch configuration for Workspace tasks."""

    agent_type: AgentType
    session_kind: SessionKind
    env: dict[str, str]
    default_maso_model: str | None = None


@dataclass(frozen=True)
class CrossReviewCandidate:
    """One heterogeneous cross-review candidate in priority order."""

    provider: str
    model: str | None = None
    effort: str | None = None
    env: dict[str, str] | None = None


StrictReviewerTarget = CrossReviewCandidate


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


def canonical_maso_base_model(model: str) -> str:
    """Strip reasoning/thinking effort variant suffixes without conflating distinct endpoints."""
    cleaned = model.strip().lower()
    if not cleaned:
        return ""
    return _MASO_VARIANT_SUFFIX_RE.sub("", cleaned)


def _maso_limit_matches_model(limit_label: str, limit_scope: str, model: str) -> bool:
    raw_model = model.strip().lower()
    if not raw_model:
        return False
    base_model = canonical_maso_base_model(raw_model)
    label = limit_label.strip().lower()
    scope = limit_scope.strip().lower()
    label_base = canonical_maso_base_model(label)
    scope_base = canonical_maso_base_model(scope)
    if label in {raw_model, base_model} or scope in {raw_model, base_model}:
        return True
    if base_model and (label_base == base_model or scope_base == base_model):
        return True
    return False


def is_candidate_quota_exhausted(
    candidate: CrossReviewCandidate,
    quota_by_provider: Mapping[str, ProviderQuota] | None,
) -> bool:
    """Return True only when an authoritative, non-stale reading shows the candidate is over quota."""
    if not quota_by_provider:
        return False
    provider = (candidate.provider or "").strip().lower()
    if provider in {"codex", "traex"}:
        quota = quota_by_provider.get("codex")
        if quota is None or quota.stale or not quota.available:
            return False
        if quota.state == "over":
            return True
        return any(limit.state == "over" for limit in (quota.limits or []))

    if provider == "maso":
        quota = quota_by_provider.get("maso")
        if quota is None or quota.stale or not quota.available:
            return False
        if quota.enforcing is False:
            return False
        if not quota.limits:
            return bool(quota.state == "over" and quota.enforcing is not False)
        if quota.enforcing is not True:
            return False
        raw_model = str(candidate.model or (candidate.env or {}).get("MASO_MODEL") or "").strip()
        if not raw_model:
            return False
        return any(
            limit.state == "over" and _maso_limit_matches_model(limit.label, limit.scope, raw_model)
            for limit in quota.limits
        )

    if provider == "antigravity":
        quota = quota_by_provider.get("antigravity")
        if quota is None or quota.stale or not quota.available:
            return False
        if not quota.limits:
            return quota.state == "over"
        raw_model = str(candidate.model or "").strip().lower()
        target_group = (
            "3p"
            if any(tag in raw_model for tag in ("claude", "gpt", "3p", "sonnet", "opus"))
            else "gemini"
        )
        for limit in quota.limits:
            if limit.state != "over":
                continue
            haystack = f"{limit.scope} {limit.label}".lower()
            if target_group == "gemini" and "gemini" in haystack:
                return True
            if target_group == "3p" and any(
                tag in haystack for tag in ("3p", "claude", "gpt", "third")
            ):
                return True
        return False

    return False


async def read_cached_quota_map() -> dict[str, ProviderQuota]:
    """Read cached provider quota non-blockingly for routing decisions."""
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        and os.environ.get("CLAUDE_HUB_ENABLE_CROSS_REVIEW_PROBE_IN_TESTS") != "1"
    ):
        return {}
    try:
        from claude_hub.services.provider_quota import read_provider_quota

        response = await asyncio.wait_for(
            read_provider_quota(refresh=False),
            timeout=_QUOTA_READ_TIMEOUT_SECONDS,
        )
        return {item.provider: item for item in response.providers}
    except Exception:
        logger.debug("Quota snapshot unavailable during cross-reviewer selection", exc_info=True)
        return {}


def _advertised_model_ids(models: Sequence[str | StreamModelOption]) -> set[str]:
    ids: set[str] = set()
    for option in models:
        if isinstance(option, str):
            if option.strip():
                ids.add(option.strip())
            continue
        if option.id:
            ids.add(option.id)
        if option.provider_model_id:
            ids.add(option.provider_model_id)
        for effort in option.supported_reasoning_efforts or []:
            if effort.provider_model_id:
                ids.add(effort.provider_model_id)
    return ids


def _pick_family_model(
    models: Sequence[str | StreamModelOption],
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
    models: Sequence[str | StreamModelOption], excluded_family: str
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


def select_cross_reviewer_candidates(
    parent_agent_type: AgentType,
    parent_env: Mapping[str, str] | None = None,
    *,
    maso_base_url: str | None = None,
    maso_models: Sequence[str | StreamModelOption] | None = None,
    default_maso_model: str | None = None,
    quota_by_provider: Mapping[str, ProviderQuota] | None = None,
) -> list[CrossReviewCandidate]:
    """Choose strictly heterogeneous, quota-filtered reviewer candidates for ``parent_agent_type``."""
    env = dict(parent_env or {})
    candidates: list[CrossReviewCandidate] = []
    model_seq: Sequence[str | StreamModelOption] = list(maso_models or [])
    parent_family = maso_model_family(env, default_model=default_maso_model)

    def add_maso(preferred_base: str, preferred_variant: str, target_family: str) -> None:
        if not maso_base_url:
            return
        picked = _pick_family_model(
            model_seq,
            preferred_full_id=preferred_variant,
            base_prefix=preferred_base,
            target_family=target_family,
        )
        if not picked:
            return
        if parent_agent_type == AgentType.MASO and parent_family:
            picked_family = maso_model_family({"MASO_MODEL": picked})
            if picked_family == parent_family:
                return
        if any(c.provider == AgentType.MASO.value and c.model == picked for c in candidates):
            return
        candidates.append(
            CrossReviewCandidate(
                provider=AgentType.MASO.value,
                model=picked,
                env={"MASO_BASE_URL": maso_base_url},
            )
        )

    if parent_agent_type == AgentType.MASO:
        if parent_family == "argon":
            add_maso("gpt-6-astra", DEFAULT_ASTRA_REVIEW_MODEL, "astra")
            if maso_base_url and model_seq and not candidates:
                alt = _pick_alternate_non_family_model(model_seq, "argon")
                if alt:
                    candidates.append(
                        CrossReviewCandidate(
                            provider=AgentType.MASO.value,
                            model=alt,
                            env={"MASO_BASE_URL": maso_base_url},
                        )
                    )
            candidates.append(CrossReviewCandidate(provider=AgentType.CODEX.value, effort="high"))
            candidates.append(CrossReviewCandidate(provider=AgentType.CLAUDE.value))
        else:
            add_maso("gemini-4-argon", DEFAULT_ARGON_REVIEW_MODEL, "argon")
            if parent_family not in {"astra", ""}:
                add_maso("gpt-6-astra", DEFAULT_ASTRA_REVIEW_MODEL, "astra")
            if maso_base_url and model_seq and not candidates:
                alt = _pick_alternate_non_family_model(model_seq, parent_family or "other")
                if alt:
                    candidates.append(
                        CrossReviewCandidate(
                            provider=AgentType.MASO.value,
                            model=alt,
                            env={"MASO_BASE_URL": maso_base_url},
                        )
                    )
            candidates.append(CrossReviewCandidate(provider=AgentType.CODEX.value, effort="high"))
            candidates.append(CrossReviewCandidate(provider=AgentType.CLAUDE.value))
    elif parent_agent_type in {AgentType.CODEX, AgentType.TRAEX}:
        add_maso("gemini-4-argon", DEFAULT_ARGON_REVIEW_MODEL, "argon")
        add_maso("gpt-6-astra", DEFAULT_ASTRA_REVIEW_MODEL, "astra")
        candidates.append(CrossReviewCandidate(provider=AgentType.CLAUDE.value))
    elif parent_agent_type == AgentType.CLAUDE:
        add_maso("gemini-4-argon", DEFAULT_ARGON_REVIEW_MODEL, "argon")
        add_maso("gpt-6-astra", DEFAULT_ASTRA_REVIEW_MODEL, "astra")
        candidates.append(CrossReviewCandidate(provider=AgentType.CODEX.value, effort="high"))
    else:
        add_maso("gemini-4-argon", DEFAULT_ARGON_REVIEW_MODEL, "argon")
        add_maso("gpt-6-astra", DEFAULT_ASTRA_REVIEW_MODEL, "astra")
        candidates.append(CrossReviewCandidate(provider=AgentType.CODEX.value, effort="high"))
        candidates.append(CrossReviewCandidate(provider=AgentType.CLAUDE.value))

    return [
        candidate
        for candidate in candidates
        if not is_candidate_quota_exhausted(candidate, quota_by_provider)
    ]


async def resolve_cross_reviewer_target(
    worker_agent_type: AgentType,
    worker_env: Mapping[str, str] | None = None,
    *,
    maso_probe: MasoProbeFn | None = None,
    quota_by_provider: Mapping[str, ProviderQuota] | None = None,
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

    effective_quota = (
        dict(quota_by_provider) if quota_by_provider is not None else await read_cached_quota_map()
    )

    candidate_models: list[str] = []
    if worker_agent_type == AgentType.MASO:
        worker_family = maso_model_family(env, default_model=default_model)
        if worker_family == "argon":
            for maybe_model in (
                _pick_family_model(
                    models,
                    preferred_full_id=DEFAULT_ASTRA_REVIEW_MODEL,
                    base_prefix="gpt-6-astra",
                    target_family="astra",
                ),
                _pick_alternate_non_family_model(models, "argon"),
            ):
                if maybe_model and maybe_model not in candidate_models:
                    candidate_models.append(maybe_model)
        else:
            effective_excluded = worker_family or "other"
            ordered = [
                _pick_family_model(
                    models,
                    preferred_full_id=DEFAULT_ARGON_REVIEW_MODEL,
                    base_prefix="gemini-4-argon",
                    target_family="argon",
                ),
                (
                    _pick_family_model(
                        models,
                        preferred_full_id=DEFAULT_ASTRA_REVIEW_MODEL,
                        base_prefix="gpt-6-astra",
                        target_family="astra",
                    )
                    if worker_family not in {"astra", ""}
                    else None
                ),
                _pick_alternate_non_family_model(models, effective_excluded),
            ]
            for maybe_model in ordered:
                if maybe_model and maybe_model not in candidate_models:
                    candidate_models.append(maybe_model)
    else:
        for maybe_model in (
            _pick_family_model(
                models,
                preferred_full_id=DEFAULT_ARGON_REVIEW_MODEL,
                base_prefix="gemini-4-argon",
                target_family="argon",
            ),
            _pick_family_model(
                models,
                preferred_full_id=DEFAULT_ASTRA_REVIEW_MODEL,
                base_prefix="gpt-6-astra",
                target_family="astra",
            ),
            _pick_alternate_non_family_model(models, ""),
        ):
            if maybe_model and maybe_model not in candidate_models:
                candidate_models.append(maybe_model)

    selected_model: str | None = None
    for candidate_model in candidate_models:
        cand = CrossReviewCandidate(
            provider=AgentType.MASO.value,
            model=candidate_model,
            env={"MASO_BASE_URL": base_url, "MASO_MODEL": candidate_model},
        )
        if not is_candidate_quota_exhausted(cand, effective_quota):
            selected_model = candidate_model
            break

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
