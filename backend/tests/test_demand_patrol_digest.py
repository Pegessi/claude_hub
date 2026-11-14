"""The daily radar is factual, bounded and transport-injected."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from claude_hub.models import AgentType, WorkspaceTask, WorkspaceTaskStatus
from claude_hub.services.demand_patrol.commands import CommandError
from claude_hub.services.demand_patrol.daily_digest import build_daily_digest, demand_mr_urls
from claude_hub.services.demand_patrol.models import MRStateSnapshot, PatrolConfig, PatrolState
from claude_hub.services.task_lifecycle import LifecycleFacts, derive_task_lifecycle

NOW = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
MR = "https://code.byted.org/seed/example/merge_requests/42"


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
    assert "已合入待关单" in record.markdown
    assert "MR 状态跳变" in record.markdown
    assert "CI 回归" in record.markdown
    assert "success → failed" in record.markdown
    assert "合入不等于验收通过" in record.markdown
    assert MR in record.markdown
    assert state.mr_snapshots[MR].state == "merged"
    assert state.mr_snapshots[MR].ci_status == "failed"
    assert record.details["state_changes"] == 3
    assert record.llm_invoked == record.notified == 0
    assert runner.calls[0][1] is not None
    assert runner.calls[0][1]["INFOWALL_URL"]
    second = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    assert "CI 回归" not in second.markdown
    assert "MR 状态跳变" not in second.markdown
    assert "已合入待关单" in second.markdown


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
    assert "首次发现 CI 失败" in record.markdown
    assert "MR 状态跳变" not in record.markdown
    assert record.dry_run


@pytest.mark.asyncio
async def test_stall_unassigned_and_cross_workspace_attention() -> None:
    demands = [
        _demand(id="old", title="长时间停滞", sources=[]),
        _demand(
            id="unassigned",
            title="计划但无人接手",
            status="planned",
            updated_at=NOW.isoformat(),
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
    record = await build_daily_digest(
        PatrolState(), now=NOW, tasks=[task, orphan], lifecycles=lifecycles, runner=runner
    )
    assert record.details["stalled_or_unassigned"] == 2
    assert "49h 未更新" in record.markdown
    assert "计划但无人接手" in record.markdown
    assert "有活跃任务的计划" not in record.markdown
    assert "最近追加了证据" not in record.markdown
    assert "低优先级不告警" not in record.markdown
    assert "已完成不告警" not in record.markdown
    assert record.details["attention_tasks"] == 1
    assert "orphaned" in record.markdown
    assert "workspace-2" in record.markdown


@pytest.mark.asyncio
async def test_pending_top_n_uses_created_time_and_not_input_order() -> None:
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
    record = await build_daily_digest(
        PatrolState(config=PatrolConfig(pending_top_n=2)),
        now=NOW,
        tasks=[],
        lifecycles={},
        runner=DigestRunner({"demands": demands}),
    )
    assert record.details["pending"] == 5
    assert "待确认 **5** 项" in record.markdown
    assert "待确认-0" in record.markdown and "待确认-1" in record.markdown
    assert "待确认-2" not in record.markdown
    assert record.markdown.index("待确认-0") < record.markdown.index("待确认-1")


@pytest.mark.asyncio
async def test_queries_are_deduplicated_capped_and_concurrency_bounded() -> None:
    urls = [f"https://code.byted.org/team/repo/merge_requests/{index}" for index in range(20)]
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


@pytest.mark.asyncio
async def test_source_errors_are_unknown_not_empty_and_preserve_prior_snapshot() -> None:
    state = PatrolState(
        mr_snapshots={MR: MRStateSnapshot(url=MR, state="open", ci_status="success")}
    )
    before = state.model_dump()
    runner = DigestRunner(
        {"demands": [_demand()]}, {MR: CommandError("secret stderr must not be reflected")}
    )
    record = await build_daily_digest(state, now=NOW, tasks=[], lifecycles={}, runner=runner)
    assert state.model_dump() == before
    assert record.details["mrs_queried"] == 0
    assert "数据缺口" in record.markdown and MR in record.markdown
    assert "secret stderr" not in record.markdown
    unavailable = await build_daily_digest(
        state, now=NOW, tasks=[], lifecycles={}, runner=DigestRunner(CommandError("offline"))
    )
    assert unavailable.details["demands"] is None
    assert unavailable.details["pending"] is None
    assert "未知" in unavailable.markdown
    assert "待确认 **0** 项" not in unavailable.markdown
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
            {"demands": [_demand()]}, {MR: {"data": {"merge_request": {"Title": "missing status"}}}}
        ),
    )
    assert record.errors and not state.mr_snapshots


def test_evidence_url_extraction_preserves_identity_and_excludes_lookalike_hosts() -> None:
    demand = _demand(
        sources=[
            {"url": MR + "?foo=1"},
            {"url": "https://code.byted.org.evil.test/a/merge_requests/1"},
        ],
        progress=[{"links": [{"url": MR + "#note"}, {"url": MR + "0"}]}],
    )
    assert demand_mr_urls(demand) == {MR, MR + "0"}


@pytest.mark.asyncio
async def test_exact_title_matches_active_task_but_not_completed_task() -> None:
    demand = _demand(
        title="标题关联的待办", status="planned", updated_at=NOW.isoformat(), sources=[]
    )
    task = _task(demand["title"])
    runner = DigestRunner({"demands": [demand]})
    active = await build_daily_digest(
        PatrolState(), now=NOW, tasks=[task], lifecycles={}, runner=runner
    )
    assert active.details["stalled_or_unassigned"] == 0
    task.status = WorkspaceTaskStatus.DONE
    completed = await build_daily_digest(
        PatrolState(), now=NOW, tasks=[task], lifecycles={}, runner=runner
    )
    assert completed.details["stalled_or_unassigned"] == 1
