"""Boundary regressions for the Feishu message clock allowance."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from claude_hub.config import Settings
from claude_hub.services.feishu_bot import (
    FeishuMessageTimeStatus,
    feishu_message_time_is_valid,
    feishu_message_time_status,
)

_SKEW_ENV = "CLAUDE_HUB_FEISHU_MESSAGE_FUTURE_SKEW_SECONDS"


@pytest.mark.parametrize(
    ("now", "created_at_ms"),
    [(math.nextafter(1_800_000_000.0, -math.inf), 1_800_000_000_000), (0.0009999999999999998, 1)],
    ids=["epoch-boundary", "sub-millisecond-boundary"],
)
def test_zero_allowance_never_rounds_a_future_message_into_the_present(
    now: float, created_at_ms: int
) -> None:
    assert not feishu_message_time_is_valid(created_at_ms, activated_at=0, now=now)


def test_decimal_allowance_has_an_exact_millisecond_boundary() -> None:
    arguments = {
        "activated_at": 1_799_999_999.0,
        "now": 1_800_000_000.0,
        "future_skew_seconds": 0.152,
    }
    assert (
        feishu_message_time_status(1_800_000_000_152, **arguments) == FeishuMessageTimeStatus.VALID
    )
    assert (
        feishu_message_time_status(1_800_000_000_153, **arguments)
        == FeishuMessageTimeStatus.MESSAGE_FUTURE_OVER_SKEW
    )


def test_finite_large_allowance_does_not_overflow_timestamp_arithmetic() -> None:
    assert (
        feishu_message_time_status(
            1_800_000_000_001,
            activated_at=1_799_999_999.0,
            now=1_800_000_000.0,
            future_skew_seconds=1e308,
        )
        == FeishuMessageTimeStatus.VALID
    )


def test_default_and_environment_allowance_are_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SKEW_ENV, raising=False)
    assert Settings(_env_file=None).feishu_message_future_skew_seconds == 5.0
    monkeypatch.setenv(_SKEW_ENV, "0.152")
    assert Settings(_env_file=None).feishu_message_future_skew_seconds == 0.152
    monkeypatch.setenv(_SKEW_ENV, "0")
    assert Settings(_env_file=None).feishu_message_future_skew_seconds == 0.0


@pytest.mark.parametrize("value", ["nan", "inf", "-0.1"])
def test_invalid_configured_allowance_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(_SKEW_ENV, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
