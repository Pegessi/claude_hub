"""Behavioral regressions for call-time overrides on legacy service entrypoints."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_hub.models import ScheduledTaskCreate, ScheduledTaskUpdate
from claude_hub.services import feedback_lessons, feishu_bot_pool, workspace_manager
from claude_hub.services.dot import service as dot_service
from claude_hub.services.dot.store import DotStore


@pytest.mark.parametrize("patch_before_creation", [False, True])
def test_feedback_clock_override_reaches_catalog_summary_and_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    patch_before_creation: bool,
) -> None:
    original_time = datetime(2000, 1, 2, 3, 4, 5)
    patched_time = datetime(2001, 2, 3, 4, 5, 6)
    explicit_time = datetime(1999, 9, 9, 9, 9, 9)
    monkeypatch.setattr(feedback_lessons, "_now", lambda: original_time)
    store = None if patch_before_creation else feedback_lessons.FeedbackLessonStore(tmp_path)
    payload = feedback_lessons.FeedbackLessonCreate(
        summary="Check evidence before reporting",
        applies_when=["code review"],
        do="Verify the result",
        avoid="Guessing the outcome",
        evidence_task_ids=["task-1"],
    )

    with monkeypatch.context() as patch:
        patch.setattr(feedback_lessons, "_now", lambda: patched_time)
        if store is None:
            store = feedback_lessons.FeedbackLessonStore(tmp_path)
        lesson = store.create_lesson("workspace", payload, enforce_iteration_signal=False)
        assert lesson.created_at == patched_time
        index = json.loads((tmp_path / "workspace/feedback/lesson-index.json").read_text())
        assert index["updated_at"] == patched_time.isoformat()

        summary = store.prepare_summary_input(
            "workspace",
            tmp_path / "empty-records",
            mode=feedback_lessons.FeedbackSummaryMode.INCREMENTAL,
            limit=1,
            force=False,
        )
        assert summary["_now"] == patched_time
        store.commit_summary_input(summary, [])
        index = json.loads((tmp_path / "workspace/feedback/index.json").read_text())
        assert index["updated_at"] == patched_time.isoformat()

        archived = store.archive_lesson("workspace", lesson.id, now=explicit_time)
        assert archived.updated_at == explicit_time

    # The same instance must observe restoration, not keep a constructor snapshot.
    restored = store.create_lesson("workspace", payload, enforce_iteration_signal=False)
    assert restored.created_at == original_time


@pytest.mark.parametrize("patch_before_creation", [False, True])
def test_dot_wake_override_applies_to_new_schedules_and_restores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    patch_before_creation: bool,
) -> None:
    original_interval = dot_service.WAKE_INTERVAL_SECONDS
    schedules: dict[str, SimpleNamespace] = {}
    updates: list[ScheduledTaskUpdate] = []

    def create(payload: ScheduledTaskCreate) -> SimpleNamespace:
        item = SimpleNamespace(id=f"schedule-{len(schedules)}", **payload.model_dump())
        schedules[item.id] = item
        return item

    def update(schedule_id: str, payload: ScheduledTaskUpdate) -> None:
        updates.append(payload)
        schedules[schedule_id].__dict__.update(payload.model_dump(exclude_unset=True))

    monkeypatch.setattr(workspace_manager, "scheduled_tasks", schedules)
    monkeypatch.setattr(workspace_manager, "create_scheduled_task", create)
    monkeypatch.setattr(workspace_manager, "update_scheduled_task", update)
    service = None if patch_before_creation else dot_service.DotService(DotStore(tmp_path))

    with monkeypatch.context() as patch:
        patch.setattr(dot_service, "WAKE_INTERVAL_SECONDS", 123)
        if service is None:
            service = dot_service.DotService(DotStore(tmp_path))
        service.store.ensure()
        service.ensure_schedules("tab")
        state = service.store.read_state()
        wake = schedules[state["wake_schedule_id"]]
        assert wake.interval_seconds == 123
        assert schedules[state["tidy_schedule_id"]].cron == dot_service.TIDY_CRON

    # Existing user-selected timing is preserved; only missing schedules use defaults.
    service.ensure_schedules("tab")
    assert wake.interval_seconds == 123
    assert updates == []
    schedules.clear()
    service.ensure_schedules("tab")
    state = service.store.read_state()
    assert schedules[state["wake_schedule_id"]].interval_seconds == original_interval


@pytest.mark.parametrize("patch_before_creation", [False, True])
def test_bot_capacity_override_is_late_bound_and_restores(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    patch_before_creation: bool,
) -> None:
    def new_pool() -> feishu_bot_pool.FeishuBotPoolStore:
        return feishu_bot_pool.FeishuBotPoolStore(
            path=tmp_path / "pool.json",
            legacy_path=tmp_path / "legacy.json",
            now=lambda: 1.0,
        )

    pool = None if patch_before_creation else new_pool()
    first = feishu_bot_pool.FeishuBotConfig(app_id="first", app_secret="test-secret")
    second = feishu_bot_pool.FeishuBotConfig(app_id="second", app_secret="test-secret")

    with monkeypatch.context() as patch:
        patch.setattr(feishu_bot_pool, "MAX_BOTS", 0)
        if pool is None:
            pool = new_pool()
        with pytest.raises(feishu_bot_pool.FeishuPoolFull, match="pool"):
            pool.create_bot(name="Blocked", config=first, environ={})
        assert pool.snapshot(environ={}).bots == ()

    first_id = pool.create_bot(name="First", config=first, environ={})
    with monkeypatch.context() as patch:
        patch.setattr(feishu_bot_pool, "MAX_BOTS", 1)
        with pytest.raises(feishu_bot_pool.FeishuPoolFull, match="pool"):
            pool.create_bot(name="Blocked second", config=second, environ={})
        assert [bot.bot_id for bot in pool.snapshot(environ={}).bots] == [first_id]

    second_id = pool.create_bot(name="Second", config=second, environ={})
    assert {bot.bot_id for bot in pool.snapshot(environ={}).bots} == {first_id, second_id}
