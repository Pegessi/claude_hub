"""Unit tests for quota-aware heterogeneous cross-reviewer routing."""

from __future__ import annotations

import pytest

from claude_hub.models import AgentType, ProviderQuota, QuotaLimit, StreamModelOption
from claude_hub.services.cross_reviewer import (
    DEFAULT_ARGON_REVIEW_MODEL,
    DEFAULT_ASTRA_REVIEW_MODEL,
    CrossReviewCandidate,
    canonical_maso_base_model,
    is_candidate_quota_exhausted,
    resolve_cross_reviewer_target,
    select_cross_reviewer_candidates,
)


def _maso_option(model_id: str) -> StreamModelOption:
    return StreamModelOption(
        id=model_id,
        label=model_id,
        provider_model_id=model_id,
    )


def test_is_candidate_quota_exhausted_respects_enforcing_and_freshness() -> None:
    codex_cand = CrossReviewCandidate(provider="codex", effort="high")
    traex_cand = CrossReviewCandidate(provider="traex", effort="high")
    argon_cand = CrossReviewCandidate(
        provider="maso",
        model=DEFAULT_ARGON_REVIEW_MODEL,
    )
    astra_cand = CrossReviewCandidate(
        provider="maso",
        model=DEFAULT_ASTRA_REVIEW_MODEL,
    )

    # Codex over & fresh -> exhausted for both codex and traex
    quota_codex_over = {
        "codex": ProviderQuota(
            provider="codex",
            available=True,
            state="over",
            stale=False,
            limits=[QuotaLimit(scope="5h", label="5-hour limit", state="over", used_percent=100)],
        )
    }
    assert is_candidate_quota_exhausted(codex_cand, quota_codex_over) is True
    assert is_candidate_quota_exhausted(traex_cand, quota_codex_over) is True

    # Stale or near Codex reading -> NOT exhausted
    quota_codex_stale = {
        "codex": ProviderQuota(
            provider="codex",
            available=False,
            state="unavailable",
            stale=True,
            limits=[QuotaLimit(scope="5h", label="5-hour limit", state="over", used_percent=100)],
        )
    }
    assert is_candidate_quota_exhausted(codex_cand, quota_codex_stale) is False

    quota_codex_near = {
        "codex": ProviderQuota(
            provider="codex",
            available=True,
            state="near",
            stale=False,
            limits=[QuotaLimit(scope="5h", label="5-hour limit", state="near", used_percent=88)],
        )
    }
    assert is_candidate_quota_exhausted(codex_cand, quota_codex_near) is False

    # MASO enforcing=True with Argon over and Astra within
    quota_maso_enforcing = {
        "maso": ProviderQuota(
            provider="maso",
            available=True,
            state="over",
            stale=False,
            enforcing=True,
            limits=[
                QuotaLimit(
                    scope="[redacted]",
                    label="[redacted]",
                    state="over",
                    used_percent=100,
                ),
                QuotaLimit(
                    scope="[redacted]",
                    label="[redacted]",
                    state="within",
                    used_percent=25,
                ),
            ],
        )
    }
    assert is_candidate_quota_exhausted(argon_cand, quota_maso_enforcing) is True
    assert is_candidate_quota_exhausted(astra_cand, quota_maso_enforcing) is False

    # MASO enforcing=False -> never treated as exhausted even if limit says over
    quota_maso_advisory = {
        "maso": ProviderQuota(
            provider="maso",
            available=True,
            state="over",
            stale=False,
            enforcing=False,
            limits=[
                QuotaLimit(
                    scope="[redacted]",
                    label="[redacted]",
                    state="over",
                    used_percent=100,
                )
            ],
        )
    }
    assert is_candidate_quota_exhausted(argon_cand, quota_maso_advisory) is False


def test_maso_distinct_endpoints_in_same_family_do_not_false_exhaust() -> None:
    assert canonical_maso_base_model("[redacted]-reasoning-high") == "[redacted]"
    assert canonical_maso_base_model("[redacted]-reasoning-high") == "[redacted]"
    assert canonical_maso_base_model("[redacted]_reasoning_high") == "[redacted]"
    assert canonical_maso_base_model("[redacted]_reasoning_xhigh") == "[redacted]"

    quota_seed_nightly_over = {
        "maso": ProviderQuota(
            provider="maso",
            available=True,
            state="over",
            stale=False,
            enforcing=True,
            limits=[
                QuotaLimit(
                    scope="ep-[redacted]",
                    label="[redacted]",
                    state="over",
                    used_percent=100,
                ),
                QuotaLimit(
                    scope="ep-[redacted]",
                    label="[redacted]",
                    state="within",
                    used_percent=20,
                ),
            ],
        )
    }
    seed_stable_cand = CrossReviewCandidate(
        provider="maso",
        model="[redacted]-reasoning-high",
    )
    seed_nightly_cand = CrossReviewCandidate(
        provider="maso",
        model="[redacted]-reasoning-high",
    )
    assert is_candidate_quota_exhausted(seed_nightly_cand, quota_seed_nightly_over) is True
    assert is_candidate_quota_exhausted(seed_stable_cand, quota_seed_nightly_over) is False


def test_select_cross_reviewer_candidates_filters_quota_and_fixes_implicit_maso_default() -> None:
    # 1. Implicit MASO default model (MASO_MODEL unset in tab.env, service default is Argon)
    # Must route to MASO Astra -> Codex -> Claude, NEVER Argon self-review!
    implicit_argon_candidates = select_cross_reviewer_candidates(
        AgentType.MASO,
        {},
        maso_base_url="http://127.0.0.1:11004",
        maso_models=["[redacted]", "[redacted]"],
        default_maso_model="[redacted]_reasoning_high",
    )
    assert [c.provider for c in implicit_argon_candidates] == ["maso", "codex", "claude"]
    assert implicit_argon_candidates[0].model == DEFAULT_ASTRA_REVIEW_MODEL
    assert all(c.model != DEFAULT_ARGON_REVIEW_MODEL for c in implicit_argon_candidates)

    # 2. Parent Claude when MASO Argon is over (enforcing=True) and Codex is over:
    # Must skip both Argon and Codex, selecting MASO Astra!
    quota_map = {
        "maso": ProviderQuota(
            provider="maso",
            available=True,
            state="over",
            enforcing=True,
            limits=[
                QuotaLimit(scope="ep-1", label="[redacted]", state="over"),
                QuotaLimit(scope="ep-2", label="[redacted]", state="within"),
            ],
        ),
        "codex": ProviderQuota(
            provider="codex",
            available=True,
            state="over",
            limits=[QuotaLimit(scope="5h", label="5-hour limit", state="over")],
        ),
    }
    claude_candidates = select_cross_reviewer_candidates(
        AgentType.CLAUDE,
        {},
        maso_base_url="http://127.0.0.1:11004",
        maso_models=["[redacted]", "[redacted]"],
        quota_by_provider=quota_map,
    )
    assert len(claude_candidates) == 1
    assert claude_candidates[0].provider == "maso"
    assert claude_candidates[0].model == DEFAULT_ASTRA_REVIEW_MODEL

    # 3. Zero silent same-model fallback when all heterogeneous candidates are exhausted
    all_exhausted_quota = {
        "maso": ProviderQuota(
            provider="maso",
            available=True,
            state="over",
            enforcing=True,
            limits=[
                QuotaLimit(scope="ep-1", label="[redacted]", state="over"),
                QuotaLimit(scope="ep-2", label="[redacted]", state="over"),
            ],
        ),
        "codex": ProviderQuota(
            provider="codex",
            available=True,
            state="over",
            limits=[QuotaLimit(scope="5h", label="5-hour limit", state="over")],
        ),
    }
    empty_candidates = select_cross_reviewer_candidates(
        AgentType.CLAUDE,
        {},
        maso_base_url="http://127.0.0.1:11004",
        maso_models=["[redacted]", "[redacted]"],
        quota_by_provider=all_exhausted_quota,
    )
    assert empty_candidates == []


@pytest.mark.asyncio
async def test_resolve_cross_reviewer_target_skips_exhausted_maso_family() -> None:
    async def fake_probe(_env: dict[str, str]):
        return (
            "http://127.0.0.1:11004",
            [_maso_option("[redacted]"), _maso_option("[redacted]")],
            "[redacted]_reasoning_high",
        )

    # Implicit Argon MASO worker -> picks Astra
    target_for_maso = await resolve_cross_reviewer_target(
        AgentType.MASO,
        {},
        maso_probe=fake_probe,
        quota_by_provider={},
    )
    assert target_for_maso is not None
    assert target_for_maso.env["MASO_MODEL"] == DEFAULT_ASTRA_REVIEW_MODEL

    # Claude worker when Argon is quota-exhausted -> picks Astra instead of Argon
    quota_argon_over = {
        "maso": ProviderQuota(
            provider="maso",
            available=True,
            state="over",
            enforcing=True,
            limits=[
                QuotaLimit(scope="ep-1", label="[redacted]", state="over"),
                QuotaLimit(scope="ep-2", label="[redacted]", state="within"),
            ],
        )
    }
    target_for_claude = await resolve_cross_reviewer_target(
        AgentType.CLAUDE,
        {},
        maso_probe=fake_probe,
        quota_by_provider=quota_argon_over,
    )
    assert target_for_claude is not None
    assert target_for_claude.env["MASO_MODEL"] == DEFAULT_ASTRA_REVIEW_MODEL
