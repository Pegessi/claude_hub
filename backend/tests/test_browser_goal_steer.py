"""Concurrent Goal visual checks retain one lifecycle owner and fail together."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_goal_phases_share_cleanup_and_propagate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    directory = Path(__file__).resolve().parents[2] / "frontend" / "tests"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(
        "goal_steer_probe", directory / "browser_chat_goal_steer.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.sys, "argv", ["probe", "http://127.0.0.1:5292", str(tmp_path), "browser"]
    )

    appearance_context = SimpleNamespace(close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=appearance_context))
    context = SimpleNamespace(browser=browser)
    owner_closed = False

    @asynccontextmanager
    async def owned_context(**kwargs):
        nonlocal owner_closed
        try:
            yield context
        finally:
            owner_closed = True

    started: set[bool] = set()
    cancelled: set[bool] = set()
    both_started = asyncio.Event()

    async def scenario(actual_context, *, appearance):
        assert actual_context is (appearance_context if appearance else context)
        started.add(appearance)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        if outcome == "success":
            return {"result": "PASS", "screenshots": [str(appearance)]}
        if outcome == "failure" and not appearance:
            raise ValueError("behavior failed")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.add(appearance)
            raise

    monkeypatch.setattr(module, "_scenario", scenario)
    run = SimpleNamespace(context=owned_context)
    if outcome == "success":
        await module.main(run)
        report = json.loads((tmp_path / "report.json").read_text())
        assert report["phases"] == {"behavior": "PASS", "appearance": "PASS"}
        assert set(report["screenshots"]) == {"False", "True"}
    elif outcome == "failure":
        with pytest.raises(ExceptionGroup) as caught:
            await module.main(run)
        assert any(isinstance(error, ValueError) for error in caught.value.exceptions)
        assert cancelled == {True}
    else:
        task = asyncio.create_task(module.main(run))
        await both_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == {False, True}
    assert started == {False, True}
    appearance_context.close.assert_awaited_once()
    assert owner_closed
    if outcome != "success":
        assert not (tmp_path / "report.json").exists()
