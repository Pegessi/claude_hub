"""Regression coverage for the reader gate's CSS transition synchronization."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def wait_for_fade(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    # Load the actual standalone scenario without starting a browser or its CLI.
    monkeypatch.setattr(sys, "path", sys.path.copy())
    scenario = Path(__file__).resolve().parents[2] / "frontend/tests/browser_markdown_reader.py"
    return cast(Callable[..., Any], runpy.run_path(str(scenario))["wait_code_block_state"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("intermediate", "target"), [(0.99858, 1), (0.00142, 0), (None, 0)])
async def test_code_fade_wait_requires_exact_endpoint(
    wait_for_fade: Callable[..., Any], intermediate: float | None, target: int
) -> None:
    pending = [{"hint_opacity": intermediate}]
    settled = [{"hint_opacity": target}]
    blocks = SimpleNamespace(evaluate_all=AsyncMock(side_effect=[pending, settled]))
    page = SimpleNamespace(wait_for_timeout=AsyncMock())
    result: Any = await wait_for_fade(page, blocks, [target])
    assert result == settled
    assert blocks.evaluate_all.await_count == 2
    page.wait_for_timeout.assert_awaited_once_with(50)


@pytest.mark.asyncio
async def test_code_fade_wait_rejects_unsettled_transition(
    wait_for_fade: Callable[..., Any],
) -> None:
    blocks = SimpleNamespace(evaluate_all=AsyncMock(return_value=[{"hint_opacity": 0.99858}]))
    page = SimpleNamespace(wait_for_timeout=AsyncMock())
    with pytest.raises(AssertionError, match="code block fade did not settle"):
        await wait_for_fade(page, blocks, [1])
    assert blocks.evaluate_all.await_count == 40
    assert page.wait_for_timeout.await_count == 40
