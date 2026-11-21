"""The daily radar is factual, bounded and transport-injected."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentType,
    TaskLifecycle,
    TaskOwner,
    TaskOwnerKind,
    TaskPhase,
    WorkspaceTask,
    WorkspaceTaskStatus,
)
from claude_hub.models.schema_domains.execution import TaskProgressSnapshot, TaskProgressState
from claude_hub.services.demand_patrol.commands import CommandError
from claude_hub.services.demand_patrol.daily_digest import (
    build_daily_digest as collect_daily_digest,
)
from claude_hub.services.demand_patrol.daily_digest import (
    demand_mr_urls,
)
from claude_hub.services.demand_patrol.models import (
    MRStateSnapshot,
    PatrolConfig,
    PatrolRunRecord,
    PatrolState,
)
from claude_hub.services.task_lifecycle import LifecycleFacts, derive_task_lifecycle

NOW = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
MR = "https://git.example.com/seed/example/merge_requests/42"


class DigestRunner:
    def __init__(self, demands: Any, mrs: dict[str, Any] | None = None) -> None:
        self.demands = demands
        self.mrs = mrs or {}
        self.calls: list[tuple[list[str], dict[str, str] | None, float]] = []
        self.running = 0
        self.max_running = 0

    async def __call__(
        self,
        argv: list[str],
        *,
        timeout: float = 30,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> Any:
        assert input_text is None
        self.calls.append((argv, env, timeout))
        if argv == ["infowall", "demand", "list", "--json"]:
            if isinstance(self.demands, Exception):
                raise self.demands
            return self.demands
        assert argv[:5] == ["bytedcli", "--json", "codebase", "mr", "get"]
        assert timeout == 10
        self.running += 1
        self.max_running = max(self.running, self.max_running)
        try:
            await asyncio.sleep(0)
            result = self.mrs[argv[-1]]
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self.running -= 1


def _demand(title: str = "修复批量预填充失败", **changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "d-1",
        "title": title,
        "status": "active",
        "priority": "p1",
        "next_action": "核查回归并确认发布结果",
        "created_at": (NOW - timedelta(hours=100)).isoformat(),
        "updated_at": (NOW - timedelta(hours=49)).isoformat(),
        "sources": [{"url": MR}],
        "progress": [],
    }
    result.update(changes)
    return result


def _task(title: str, *, prompt: str = "", **changes: Any) -> WorkspaceTask:
    fields = {
        "id": "task-1",
        "workspace_id": "workspace-1",
        "title": title,
        "prompt": prompt,
        "agent_type": AgentType.CLAUDE,
        "status": WorkspaceTaskStatus.TODO,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(changes)
    return WorkspaceTask.model_validate(fields)


async def build_daily_digest(state: PatrolState, **kwargs: Any) -> PatrolRunRecord:
    kwargs.setdefault("decision_model", AsyncMock(side_effect=CommandError("offline")))
    digest = await collect_daily_digest(state, **kwargs)
    if not digest.record.dry_run:
        state.daily_decision_receipts.update(digest.receipts)
    return digest.record


@pytest.mark.asyncio
async def test_merged_and_failed_codebase_pascal_case_envelope() -> None:
    state = PatrolState(
        mr_snapshots={MR: MRStateSnapshot(url=MR, state="open", ci_status="success")}
    )
    runner = DigestRunner(
        {"demands": [_demand()]},
        {
            MR: {
                "status": "success",
                "data": {
                    "merge_request": {
                        "Status": "merged",
                        "Title": "修复服务",
                        "CheckRunSummaryStatus": "failure",
                        "UpdatedAt": NOW.isoformat(),
                    }
                },
            }
        },
    )
    record = await build_daily_digest(
        state, now=NOW, tasks=[], lifecycles={}, runner=runner, slot_label="11:00"
    )
    assert "CI 失败" in record.markdown
    assert MR in record.markdown
    assert state.mr_snapshots[MR].state == "merged"
    assert state.mr_snapshots[MR].ci_status == "failed"
    assert record.llm_invoked == 1 and record.notified == 0
    assert runner.calls[0][1] is not None
    assert runner.calls[0][1]["INFOWALL_URL"]
    model = AsyncMock()
    second = await build_daily_digest(
        state, now=NOW, tasks=[], lifecycles={}, runner=runner, decision_model=model
    )
    assert second.markdown == "" and second.llm_invoked == 0
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_dry_run_preserves_snapshots_and_reports_initial_failure() -> None:
    state = PatrolState()
    runner = DigestRunner(
        {"demands": [_demand()]}, {MR: {"state": "opened", "head_pipeline": {"status": "failed"}}}
    )
    before = state.model_dump()
    record = await build_daily_digest(
        state, now=NOW, tasks=[], lifecycles={}, runner=runner, dry_run=True
    )
    assert state.model_dump() == before
    assert "CI 失败" in record.markdown
    assert record.llm_invoked == 0
    assert "MR 状态跳变" not in record.markdown
    assert record.dry_run


@pytest.mark.asyncio
async def test_new_threshold_crossing_and_cross_workspace_attention() -> None:
    demands = [
        _demand(id="old", title="长时间停滞", sources=[]),
        _demand(
            id="unassigned",
            title="计划但无人接手",
            status="planned",
            updated_at=(NOW - timedelta(hours=47)).isoformat(),
            sources=[],
        ),
        _demand(
            id="assigned", title="有活跃任务的计划", status="planned", updated_at=NOW.isoformat()
        ),
        _demand(
            id="recent-progress",
            title="最近追加了证据",
            sources=[],
            progress=[{"created_at": NOW.isoformat()}],
        ),
        _demand(id="low", title="低优先级不告警", priority="p2", sources=[]),
        _demand(id="done", title="已完成不告警", status="done", sources=[]),
    ]
    task = _task("实现关联修复", prompt=f"跟进 {MR}")
    orphan = _task(
        "另一工作区等待接手",
        id="task-2",
        workspace_id="workspace-2",
        status=WorkspaceTaskStatus.WORKING,
    )
    lifecycles = {
        item.id: derive_task_lifecycle(LifecycleFacts(task=item, now=NOW))
        for item in [task, orphan]
    }
    runner = DigestRunner({"demands": demands}, {MR: {"state": "open"}})
    state = PatrolState()
    record = await build_daily_digest(
        state, now=NOW, tasks=[task, orphan], lifecycles=lifecycles, runner=runner
    )
    assert record.candidates == 2
    assert "49 小时未更新" in record.markdown
    assert "计划但无人接手" not in record.markdown
    assert "有活跃任务的计划" not in record.markdown
    assert "最近追加了证据" not in record.markdown
    assert "低优先级不告警" not in record.markdown
    assert "已完成不告警" not in record.markdown
    assert "Task 等待重新接手" in record.markdown
    orphan.updated_at = NOW + timedelta(minutes=30)
    lifecycles[orphan.id] = derive_task_lifecycle(LifecycleFacts(task=orphan, now=NOW))
    second = await build_daily_digest(
        state,
        now=NOW + timedelta(hours=1),
        tasks=[task, orphan],
        lifecycles=lifecycles,
        runner=runner,
    )
    assert second.candidates == 1
    assert "计划但无人接手" in second.markdown
    assert "仍待派工" in second.markdown


@pytest.mark.asyncio
async def test_pending_inbox_does_not_open_gate_or_render_sections() -> None:
    demands = [
        _demand(
            id=str(index),
            title=f"待确认-{index}",
            status="pending",
            priority="none",
            sources=[],
            created_at=(NOW - timedelta(hours=index)).isoformat(),
        )
        for index in [4, 2, 0, 3, 1]
    ]
    model = AsyncMock()
    record = await build_daily_digest(
        PatrolState(config=PatrolConfig(pending_top_n=2)),
        now=NOW,
        tasks=[],
        lifecycles={},
        runner=DigestRunner({"demands": demands}),
        decision_model=model,
    )
    assert record.markdown == "" and record.candidates == 0
    assert record.details["decision_mode"] == "skipped"
    model.assert_not_awaited()


@pytest.mark.asyncio
async def test_queries_are_deduplicated_capped_and_concurrency_bounded() -> None:
    urls = [f"https://git.example.com/team/repo/merge_requests/{index}" for index in range(20)]
    demand = _demand(
        sources=[{"url": url + "#discussion"} for url in urls],
        progress=[{"links": [{"url": urls[0]}]}],
    )
    runner = DigestRunner({"demands": [demand]}, {url: {"state": "open"} for url in urls})
    record = await build_daily_digest(
        PatrolState(), now=NOW, tasks=[], lifecycles={}, runner=runner
    )
    assert len(runner.calls) == 16
    assert 1 < runner.max_running <= 4
    assert record.details["mrs_requested"] == record.details["mrs_queried"] == 15
    assert record.details["mrs_omitted"] == 5


@pytest.mark.parametrize("terminal_status", ["done", "dismissed"])
@pytest.mark.parametrize("priority", ["p0", "p1"])
async def test_terminal_demands_do_not_consume_mr_query_slots(
    terminal_status: str,
    priority: str,
) -> None:
    closed_urls = [f"https://git.example.com/a/history/merge_requests/{i}" for i in range(20)]
    open_urls = [f"https://git.example.com/z/active/merge_requests/{i}" for i in range(15)]
    closed = _demand(
        title="历史需求不应巡检",
        status=terminal_status,
        priority=priority,
        sources=[{"url": url} for url in closed_urls],
    )
    active = [
        _demand(
            status=["active", "waiting", "planned"][index % 3],
            priority="none",
            sources=[{"url": url}],
        )
        for index, url in enumerate(open_urls)
    ]
    state = PatrolState(
        mr_snapshots={
            closed_urls[0]: MRStateSnapshot(
                url=closed_urls[0], state="merged", ci_status="success"
            ),
        }
    )
    runner = DigestRunner(
        {"demands": [closed, *active]},
        {
            **{url: {"state": "merged", "ci_status": "failed"} for url in closed_urls},
            **{url: {"state": "open", "ci_status": "success"} for url in open_urls},
        },
    )
    record = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    queried_urls = {argv[-1] for argv, _, _ in runner.calls if argv[0] == "bytedcli"}
    assert queried_urls == set(open_urls)
    assert record.details["mrs_requested"] == record.details["mrs_queried"] == 15
    assert record.details["mrs_omitted"] == 0
    assert record.candidates == 0 and not record.errors
    assert not any(url in record.markdown for url in closed_urls)
    assert state.mr_snapshots[closed_urls[0]].ci_status == "success"


@pytest.mark.asyncio
async def test_source_errors_are_unknown_not_empty_and_preserve_prior_snapshot() -> None:
    state = PatrolState(
        mr_snapshots={MR: MRStateSnapshot(url=MR, state="open", ci_status="success")}
    )
    before = state.model_dump()
    runner = DigestRunner(
        {"demands": [_demand(priority="p2")]},
        {MR: CommandError("secret stderr must not be reflected")},
    )
    record = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    assert state.mr_snapshots[MR].model_dump() == before["mr_snapshots"][MR]
    assert state.daily_mr_cursor == MR
    before = state.model_dump()
    assert record.details["mrs_queried"] == 0
    assert record.markdown == "" and MR in record.errors[0]
    assert "secret stderr" not in record.errors[0]
    unavailable = await build_daily_digest(
        state, now=NOW, tasks=[], lifecycles={}, runner=DigestRunner(CommandError("offline"))
    )
    assert unavailable.details["demands"] is None
    assert unavailable.markdown == "" and unavailable.errors
    assert state.model_dump() == before


@pytest.mark.asyncio
async def test_malformed_demands_and_mr_are_not_treated_as_empty_success() -> None:
    malformed = await build_daily_digest(
        PatrolState(), now=NOW, tasks=[], lifecycles={}, runner=DigestRunner({"error": "offline"})
    )
    assert malformed.errors and malformed.details["demands_available"] is False
    state = PatrolState()
    record = await build_daily_digest(
        state,
        now=NOW,
        tasks=[],
        lifecycles={},
        runner=DigestRunner(
            {"demands": [_demand(priority="p2")]},
            {MR: {"data": {"merge_request": {"Title": "missing status"}}}},
        ),
    )
    assert record.errors and not state.mr_snapshots


def test_evidence_url_extraction_preserves_identity_and_excludes_lookalike_hosts() -> None:
    demand = _demand(
        sources=[
            {"url": MR + "?foo=1"},
            {"url": "https://git.example.com.evil.test/a/merge_requests/1"},
        ],
        progress=[{"links": [{"url": MR + "#note"}, {"url": MR + "0"}]}],
    )
    assert demand_mr_urls(demand) == {MR, MR + "0"}


@pytest.mark.asyncio
async def test_exact_title_matches_active_task_but_not_completed_task() -> None:
    demand = _demand(title="标题关联的待办", status="planned", sources=[])
    task = _task(demand["title"])
    runner = DigestRunner({"demands": [demand]})
    active = await build_daily_digest(
        PatrolState(), now=NOW, tasks=[task], lifecycles={}, runner=runner
    )
    assert active.candidates == 1 and "仍待派工" not in active.markdown
    task.status = WorkspaceTaskStatus.DONE
    completed = await build_daily_digest(
        PatrolState(), now=NOW, tasks=[task], lifecycles={}, runner=runner
    )
    assert completed.candidates == 1 and "仍待派工" in completed.markdown


@pytest.fixture(autouse=True)
def _configure_mr_host(monkeypatch):
    monkeypatch.setenv("CLAUDE_HUB_PATROL_MR_HOSTS", "git.example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer", ['[[1,"B"],[2,"A"],[3,"A"]]', '[[1,"B"],[2,"B"],[3,"B"],[4,"B"]]', TimeoutError()]
)
async def test_top_three_model_choices_and_deterministic_fallback(answer: str | Exception) -> None:
    demands = [
        _demand(
            id=str(index), title=f"<事项|{index}>", priority="p0" if index < 3 else "p1", sources=[]
        )
        for index in reversed(range(6))
    ]
    model = AsyncMock()
    if isinstance(answer, Exception):
        model.side_effect = answer
    else:
        model.return_value = answer
    state = PatrolState()
    runner = DigestRunner({"demands": demands})
    record = await build_daily_digest(
        state, now=NOW, tasks=[], lifecycles={}, runner=runner, decision_model=model
    )
    assert record.candidates == 6 and record.details["selected"] == 3
    assert record.markdown.count("快捷回复") == 3 and record.llm_invoked == 1
    assert record.markdown.count("（推荐）") == 3
    assert "快捷回复：「需求 0，请" in record.markdown
    assert "＜事项｜0＞" in record.markdown and "＜事项｜3＞" not in record.markdown
    assert all(marker not in record.markdown for marker in ("未发现", "收件箱", "<", "|"))
    assert model.await_args is not None
    prompt, workspace_id = model.await_args.args
    assert (
        "＜事项" not in prompt
        and "<事项" not in prompt
        and workspace_id == state.config.workspace_id
    )
    assert record.details["decision_mode"] == (
        "model" if answer == '[[1,"B"],[2,"A"],[3,"A"]]' else "deterministic"
    )
    assert ("B（推荐）" in record.markdown) is (answer == '[[1,"B"],[2,"A"],[3,"A"]]')
    second = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    assert second.candidates == 3 and "＜事项｜0＞" not in second.markdown
    assert "＜事项｜3＞" in second.markdown


@pytest.mark.asyncio
async def test_ci_recovery_rearms_and_incomplete_observation_keeps_receipt() -> None:
    state = PatrolState()
    runner = DigestRunner(
        {"demands": [_demand(priority="p2")]}, {MR: {"state": "open", "ci_status": "failed"}}
    )
    assert (
        await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    ).candidates == 1
    for status in ("unknown", "pending", "running", "failed"):
        runner.mrs[MR] = {"state": "open", "ci_status": status}
        quiet = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
        assert quiet.candidates == quiet.llm_invoked == 0
        assert f"ci:{MR}" in state.daily_decision_receipts
    runner.mrs[MR] = {"state": "open", "ci_status": "success"}
    assert (
        await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    ).candidates == 0
    assert f"ci:{MR}" not in state.daily_decision_receipts
    runner.mrs[MR] = {"state": "open", "ci_status": "failed"}
    assert (
        await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    ).candidates == 1


@pytest.mark.asyncio
async def test_new_mr_closure_is_reported_even_if_ci_becomes_unavailable() -> None:
    state = PatrolState(
        mr_snapshots={MR: MRStateSnapshot(url=MR, state="open", ci_status="failed")}
    )
    runner = DigestRunner({"demands": [_demand(priority="p2")]}, {MR: {"state": "closed"}})
    record = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    assert "MR 已关闭" in record.markdown and "CI 失败" not in record.markdown
    assert (
        await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    ).candidates == 0


@pytest.mark.asyncio
async def test_daily_provider_uses_low_cost_health_route_and_archives(monkeypatch) -> None:
    from types import SimpleNamespace

    from claude_hub.services.agent_stream.maso_http import _model_options
    from claude_hub.services.demand_patrol import digest_provider, feishu_triage

    models = _model_options(
        [
            {
                "key": "test-model",
                "selector_variants": [
                    {"mode": "default", "key": "test-model_reasoning_low"},
                    {"mode": "default", "key": "test-model_reasoning_high"},
                ],
            }
        ]
    )
    monkeypatch.delenv("CLAUDE_HUB_PATROL_MODEL", raising=False)
    monkeypatch.setattr(feishu_triage, "local_setting", lambda key, default="": default)
    discover = AsyncMock(
        return_value=("http://127.0.0.1:19999", models, "test-model_reasoning_high")
    )
    monkeypatch.setattr(feishu_triage, "discover_maso_service", discover)
    session = SimpleNamespace(workspace_id="ws", id="daily-chat")
    ensure = AsyncMock(return_value=session)
    monkeypatch.setattr(
        digest_provider, "workspace_manager", SimpleNamespace(ensure_workspace_agent=ensure)
    )
    monkeypatch.setattr(
        digest_provider, "_resolve_patrol_workspace_id", lambda workspace_id: workspace_id
    )
    monkeypatch.setattr(digest_provider, "_find_retained_patrol_session", lambda *_: None)
    turn = AsyncMock(return_value='["A"]')
    archive = AsyncMock()
    monkeypatch.setattr(digest_provider, "_run_workspace_triage_turn", turn)
    monkeypatch.setattr(digest_provider, "_archive_patrol_session", archive)

    assert await digest_provider.recommend_daily_decisions("recommend", "ws") == '["A"]'
    assert ensure.await_args is not None
    workspace_id, request = ensure.await_args.args
    assert workspace_id == "ws" and request.agent_type == AgentType.MASO
    assert request.env["MASO_MODEL"] == "test-model_reasoning_low"
    assert request.ephemeral and request.caller_owned_ephemeral and not request.solo_mode
    assert ensure.await_args.kwargs == {"bootstrap": False}
    turn.assert_awaited_once_with(session, "recommend", timeout_seconds=25)
    archive.assert_awaited_once_with("ws", "daily-chat")


@pytest.mark.asyncio
@pytest.mark.parametrize("third", [4, 5])
async def test_model_can_select_among_tied_priorities_but_cannot_displace_p0(third: int) -> None:
    demands = [
        _demand(
            id=str(index), title=f"候选-{index}", priority="p0" if index < 4 else "p1", sources=[]
        )
        for index in range(5)
    ]
    record = await build_daily_digest(
        PatrolState(),
        now=NOW,
        tasks=[],
        lifecycles={},
        runner=DigestRunner({"demands": demands}),
        decision_model=AsyncMock(return_value=f'[[1,"A"],[2,"A"],[{third},"B"]]'),
    )
    if third == 4:
        assert record.details["decision_mode"] == "model"
        assert "候选-3" in record.markdown and "候选-2" not in record.markdown
    else:
        assert record.details["decision_mode"] == "deterministic"
        assert "候选-2" in record.markdown and "候选-4" not in record.markdown


@pytest.mark.asyncio
async def test_bounded_mr_rotation_reaches_omitted_failure_and_dry_run_keeps_cursor() -> None:
    urls = [f"https://git.example.com/team/repo/merge_requests/{index}" for index in range(10, 26)]
    mr_status = {url: {"state": "open", "ci_status": "success"} for url in urls}
    mr_status[urls[-1]] = {"state": "open", "ci_status": "failed"}
    runner = DigestRunner(
        {"demands": [_demand(priority="p2", sources=[{"url": url} for url in urls])]}, mr_status
    )
    state = PatrolState()
    first = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    assert first.candidates == first.llm_invoked == 0
    assert state.daily_mr_cursor == urls[-2]
    state = PatrolState.model_validate_json(state.model_dump_json())
    before = state.model_dump()
    second = await build_daily_digest(
        state, now=NOW, tasks=[], lifecycles={}, runner=runner, dry_run=True
    )
    assert state.model_dump() == before
    assert urls[-1] in second.markdown and second.llm_invoked == 0
    third = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    assert third.candidates == third.llm_invoked == 1
    assert urls[-1] in third.markdown
    assert third.details["mrs_requested"] == third.details["mrs_queried"] == 15


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "detail", "expected_option"),
    [
        ("progress", "发布策略待定。A：复用旧实现；B：启用新路径。推荐 B。", "B"),
        ("report", "发布策略待定，需要在复用旧实现和启用新路径之间选择。", "A"),
    ],
)
async def test_task_decision_uses_current_blocker_and_concrete_reply(
    source: str, detail: str, expected_option: str
) -> None:
    task = _task("发布策略")
    lifecycle = TaskLifecycle(
        task_id=task.id,
        phase=TaskPhase.NEEDS_USER,
        reason="agent_needs_input",
        reason_text="等待用户输入",
        owner=TaskOwner(kind=TaskOwnerKind.SESSION),
        attention=True,
        since=NOW,
        legacy_status="working",
    )
    task_details = {}
    if source == "progress":
        task.latest_progress = TaskProgressSnapshot(
            state=TaskProgressState.NEEDS_INPUT,
            summary=detail,
            call_id="task-1-needs-input",
            execution_epoch=task.execution_epoch,
            reported_at=NOW,
        )
    else:
        task_details[task.id] = detail
    model = AsyncMock(return_value='[[1,"A"]]')
    state = PatrolState()
    kwargs = dict(
        now=NOW,
        tasks=[task],
        lifecycles={task.id: lifecycle},
        runner=DigestRunner({"demands": []}),
        task_details=task_details,
        decision_model=model,
    )
    record = await build_daily_digest(state, **kwargs)
    assert "发布策略待定" in record.markdown
    assert "选择复用旧实现" in record.markdown and "选择启用新路径" in record.markdown
    assert f"{expected_option}（推荐）" in record.markdown
    assert (
        f"快捷回复：「Task {task.id}，请选择{'启用新路径' if expected_option == 'B' else '复用旧实现'}」"
        in record.markdown
    )
    assert model.await_args is not None and "新路径" not in model.await_args.args[0]
    assert (await build_daily_digest(state, **kwargs)).candidates == 0
    updated = detail.replace("启用新路径", "先开灰度")
    if task.latest_progress:
        task.latest_progress.summary = updated
    else:
        task_details[task.id] = updated
    assert "先开灰度" in (await build_daily_digest(state, **kwargs)).markdown


def test_hub_task_details_select_current_reviewer_question(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from claude_hub import services
    from claude_hub.models import AgentReport, AgentReportState
    from claude_hub.services.demand_patrol.service import _hub_tasks

    task = _task("发布策略", review_cycle=2)
    lifecycle = TaskLifecycle(
        task_id=task.id,
        phase=TaskPhase.NEEDS_USER,
        reason="reviewer_needs_input",
        owner=TaskOwner(kind=TaskOwnerKind.SESSION, id="worker"),
        attention=True,
        since=NOW,
        legacy_status="review",
    )
    question = AgentReport(
        id="current",
        task_id=task.id,
        workspace_id=task.workspace_id,
        session_id="reviewer",
        state=AgentReportState.REVIEW_NEEDS_INPUT,
        message="Choose a rollout.",
        message_zh="A：先灰度；B：直接发布。",
        review_cycle=2,
        created_at=NOW,
    )
    stale = question.model_copy(
        update={"id": "stale", "review_cycle": 1, "created_at": NOW + timedelta(seconds=1)}
    )
    worker = question.model_copy(
        update={
            "id": "worker",
            "session_id": "worker",
            "state": AgentReportState.WORKING,
            "created_at": NOW + timedelta(seconds=1),
        }
    )
    manager = SimpleNamespace(
        tasks={task.id: task},
        task_lifecycles=lambda _: {task.id: lifecycle},
        reports={report.id: report for report in (question, stale, worker)},
    )
    monkeypatch.setattr(services, "workspace_manager", manager)
    assert _hub_tasks() == ([task], {task.id: lifecycle}, {task.id: question.message_zh})
