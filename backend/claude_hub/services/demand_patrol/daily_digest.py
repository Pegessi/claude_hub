"""Deterministic InfoWall, Codebase and Hub attention digest; no model calls."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from claude_hub.models import TaskLifecycle, WorkspaceTask
from claude_hub.services.demand_patrol.commands import (
    CommandError,
    CommandRunner,
    infowall_env,
    run_command,
)
from claude_hub.services.demand_patrol.models import MRStateSnapshot, PatrolRunRecord, PatrolState

_MR_URL = re.compile(r"https://code\.byted\.org/(?:[A-Za-z0-9_.-]+/)+merge_requests/\d+\b")
_OPEN_DEMAND_STATUSES = frozenset({"active", "waiting", "planned"})
_ACTIVE_TASK_STATUSES = frozenset({"todo", "queued", "working", "review"})
_FAILURES = frozenset({"failed", "failure", "error", "timed_out", "timeout"})
_MAX_MRS = 15


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _display(value: Any, fallback: str = "未提供", limit: int = 180) -> str:
    text = " ".join(_text(value).split()) or fallback
    text = text[:limit]
    return re.sub(r"([\\`*_[\]<>])", r"\\\1", text)


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # InfoWall's historical unzoned timestamps are UTC, not the patrol timezone.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _records(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _demands(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    rows = payload.get("demands") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CommandError("InfoWall demand list returned an invalid demands collection")
    return rows


def demand_mr_urls(demand: Mapping[str, Any]) -> set[str]:
    """Extract canonical MR identities only from explicit source/progress evidence."""
    values: list[str] = []
    for source in _records(demand.get("sources")):
        values.extend((_text(source.get("url")), _text(source.get("source_url"))))
    for progress in _records(demand.get("progress")):
        for link in _records(progress.get("links")):
            values.append(_text(link.get("url")))
    return {match.group() for value in values for match in _MR_URL.finditer(value)}


def _last_touch(demand: Mapping[str, Any]) -> datetime | None:
    timestamps = [demand.get("updated_at"), demand.get("created_at")]
    for progress in _records(demand.get("progress")):
        timestamps.extend((progress.get("created_at"), progress.get("updated_at")))
    parsed = [stamp for value in timestamps if (stamp := _timestamp(value)) is not None]
    return max(parsed) if parsed else None


def _matching_tasks(
    demand: Mapping[str, Any], tasks: Sequence[WorkspaceTask]
) -> list[WorkspaceTask]:
    title = " ".join(_text(demand.get("title")).casefold().split())
    urls = demand_mr_urls(demand)
    matches: list[WorkspaceTask] = []
    for task in tasks:
        if task.status.value not in _ACTIVE_TASK_STATUSES:
            continue
        task_text = " ".join(f"{task.title} {task.prompt}".casefold().split())
        title_match = bool(title) and (
            task.title.casefold().strip() == title or (len(title) >= 6 and title in task_text)
        )
        if title_match or urls.intersection(_MR_URL.findall(f"{task.title} {task.prompt}")):
            matches.append(task)
    return matches


def _mr_snapshot(payload: Any, url: str) -> MRStateSnapshot:
    if not isinstance(payload, dict):
        raise CommandError("Codebase MR result is not an object")
    if payload.get("status") == "error" or payload.get("ok") is False:
        raise CommandError("Codebase MR query reported failure")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise CommandError("Codebase MR data is not an object")
    row = data.get("merge_request", data.get("MergeRequest", data))
    if not isinstance(row, dict):
        raise CommandError("Codebase MR metadata is not an object")
    state = _text(row.get("Status") or row.get("state") or row.get("status")).lower()
    if not state:
        raise CommandError("Codebase MR metadata contains no state")
    state = {"opened": "open"}.get(state, state)
    ci_status = _text(
        row.get("CheckRunSummaryStatus") or row.get("ci_status") or row.get("pipeline_status")
    ).lower()
    if not ci_status:
        pipeline = row.get("head_pipeline") or row.get("pipeline")
        if isinstance(pipeline, dict):
            ci_status = _text(pipeline.get("status")).lower()
    if ci_status in _FAILURES:
        ci_status = "failed"
    elif ci_status in {"passed", "succeeded", "successful"}:
        ci_status = "success"
    return MRStateSnapshot(
        url=url,
        state=state,
        ci_status=ci_status or "unknown",
        updated_at=_text(row.get("UpdatedAt") or row.get("updated_at")),
        title=_text(row.get("Title") or row.get("title")),
    )


async def _read_mr(
    url: str, runner: CommandRunner, gate: asyncio.Semaphore
) -> tuple[MRStateSnapshot | None, str | None]:
    async with gate:
        try:
            async with asyncio.timeout(10):
                payload = await runner(
                    ["bytedcli", "--json", "codebase", "mr", "get", url], timeout=10
                )
            return _mr_snapshot(payload, url), None
        except (CommandError, OSError, TimeoutError, ValueError):
            # Do not expose CLI stderr, which can include authentication material.
            return None, f"MR 查询不可用（权限、超时或响应格式）：{url}"


def _state_changes(
    snapshot: MRStateSnapshot,
    previous: MRStateSnapshot | None,
    demands: Sequence[Mapping[str, Any]],
) -> list[str]:
    label = f"[{_display(snapshot.title, 'MR')}]({snapshot.url})"
    lines = []
    for demand in demands:
        if snapshot.state == "merged" and demand.get("status") in _OPEN_DEMAND_STATUSES:
            lines.append(
                f"- **已合入待关单**：{label} → {_display(demand.get('title'))} "
                f"（{_display(demand.get('status'))}）；请核对验收并关闭/更新 InfoWall，合入不等于验收通过。"
            )
    if previous and previous.state != "unknown" and snapshot.state != previous.state:
        lines.append(
            f"- **MR 状态跳变**：{label}，{_display(previous.state)} → {_display(snapshot.state)}。"
        )
    if snapshot.ci_status == "failed" and (previous is None or previous.ci_status != "failed"):
        kind = "CI 回归" if previous and previous.ci_status != "unknown" else "首次发现 CI 失败"
        before = _display(previous.ci_status) if previous else "未观测"
        lines.append(f"- **{kind}**：{label}，{before} → failed；请查看当前流水线。")
    return lines


def _stall_lines(
    demands: Sequence[Mapping[str, Any]],
    tasks: Sequence[WorkspaceTask],
    state: PatrolState,
    now: datetime,
) -> list[str]:
    lines: list[str] = []
    for demand in sorted(demands, key=lambda item: _text(item.get("priority"))):
        priority = _text(demand.get("priority")).lower()
        if priority not in {"p0", "p1"} or demand.get("status") not in _OPEN_DEMAND_STATUSES:
            continue
        touched = _last_touch(demand)
        hours = max(0.0, (now - touched).total_seconds() / 3600) if touched else None
        matches = _matching_tasks(demand, tasks)
        stalled = hours is not None and hours >= state.config.stalled_hours
        unassigned = demand.get("status") == "planned" and not matches
        if not stalled and not unassigned:
            continue
        reasons = []
        if stalled:
            reasons.append("停滞")
        if unassigned:
            reasons.append("待派工（未匹配活跃 Hub Task）")
        age = f"{hours:.0f}h 未更新" if hours is not None else "更新时间未知"
        associated = (
            "；关联 Task " + ", ".join(f"`{task.id}`" for task in matches) if matches else ""
        )
        lines.append(
            f"- **{priority.upper()} · {' / '.join(reasons)}** · {age} · {_display(demand.get('title'))}"
            f"{associated}\n  下一步：{_display(demand.get('next_action'), '请明确下一步行动')}"
        )
    return lines


def _attention_lines(
    tasks: Sequence[WorkspaceTask], lifecycles: Mapping[str, TaskLifecycle]
) -> list[str]:
    lines: list[str] = []
    for task in tasks:
        lifecycle = lifecycles.get(task.id)
        if lifecycle is None or not lifecycle.attention:
            continue
        lines.append(
            f"- **{_display(task.title)}** · {_display(lifecycle.phase.value)} · "
            f"{_display(lifecycle.reason_text)}\n"
            f"  Workspace `{task.workspace_id}` · Task `{task.id}`"
        )
    return lines


def _pending_lines(demands: Sequence[Mapping[str, Any]], top_n: int) -> tuple[int, list[str]]:
    pending = [demand for demand in demands if demand.get("status") == "pending"]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    pending.sort(key=lambda item: _timestamp(item.get("created_at")) or epoch, reverse=True)
    lines = [f"待确认 **{len(pending)}** 项；展示最新 {min(top_n, len(pending))} 项。"]
    for demand in pending[:top_n]:
        lines.append(
            f"- {_display(demand.get('title'))} · `{_display(demand.get('id'))}`\n"
            f"  下一步：{_display(demand.get('next_action'), '确认是否跟进并设置优先级')}"
        )
    return len(pending), lines


async def build_daily_digest(
    state: PatrolState,
    *,
    now: datetime,
    tasks: Sequence[WorkspaceTask],
    lifecycles: Mapping[str, TaskLifecycle],
    runner: CommandRunner = run_command,
    slot_label: str = "manual",
    dry_run: bool = False,
) -> PatrolRunRecord:
    """Collect facts and update successful MR snapshots, leaving delivery to the owner.

    The service owns the working state copy and commits it only after delivery.
    A dry run never changes snapshots, slot receipts, or external state.
    """
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    record = PatrolRunRecord(channel="daily", started_at=now.isoformat(), dry_run=dry_run)
    demand_rows: list[Mapping[str, Any]] = []
    demands_available = True
    try:
        payload = await runner(["infowall", "demand", "list", "--json"], env=infowall_env())
        demand_rows = _demands(payload)
    except (CommandError, OSError, TimeoutError, ValueError):
        demands_available = False
        record.errors.append("InfoWall 需求读取失败；需求数量、停滞及收件箱状态未知。")

    urls_to_demands: dict[str, list[Mapping[str, Any]]] = {}
    for demand in demand_rows:
        if demand.get("status") not in _OPEN_DEMAND_STATUSES:
            continue
        for url in sorted(demand_mr_urls(demand)):
            urls_to_demands.setdefault(url, []).append(demand)
    urls = sorted(urls_to_demands)[:_MAX_MRS]
    gate = asyncio.Semaphore(4)
    results = await asyncio.gather(*(_read_mr(url, runner, gate) for url in urls))
    changes: list[str] = []
    queried = 0
    for snapshot, error in results:
        if error:
            record.errors.append(error)
        if snapshot is None:
            continue
        queried += 1
        changes.extend(
            _state_changes(
                snapshot, state.mr_snapshots.get(snapshot.url), urls_to_demands[snapshot.url]
            )
        )
        if not dry_run:
            state.mr_snapshots[snapshot.url] = snapshot

    stalls = _stall_lines(demand_rows, tasks, state, now)
    attention = _attention_lines(tasks, lifecycles)
    pending_count, pending = _pending_lines(demand_rows, state.config.pending_top_n)
    sections = [f"# 研发需求与 Work 巡检 · {_display(slot_label)}", f"观测时间：{now.isoformat()}"]
    for heading, lines, empty in (
        ("🚨 状态跳变与待关单", changes, "已成功查询的 MR 未发现状态跳变或待关单。"),
        ("🔥 P0·P1 停滞与待派工", stalls, "未发现达到阈值的停滞或计划内待派工事项。"),
        ("⏳ Hub 待你拍板任务", attention, "无待你拍板的 Hub Task。"),
        (
            "📥 InfoWall 待确认收件箱",
            pending if demands_available else [],
            "InfoWall 不可用，收件箱状态未知。",
        ),
    ):
        if not demands_available and heading.startswith(("🚨", "🔥")):
            empty = "InfoWall 不可用，无法核查。"
        sections.extend((f"## {heading}", "\n".join(lines) if lines else empty))
    omitted = max(0, len(urls_to_demands) - len(urls))
    sections.append(f"MR 覆盖：成功 {queried}/{len(urls)}；本轮上限 {_MAX_MRS}，未查询 {omitted}。")
    if record.errors:
        sections.extend(("### 数据缺口", "\n".join(f"- {error}" for error in record.errors)))
    record.markdown = "\n\n".join(sections)
    record.candidates = len(changes) + len(stalls) + len(attention)
    record.details = {
        "demands_available": demands_available,
        "demands": len(demand_rows) if demands_available else None,
        "mrs_queried": queried,
        "mrs_requested": len(urls),
        "mrs_omitted": omitted,
        "state_changes": len(changes),
        "stalled_or_unassigned": len(stalls),
        "attention_tasks": len(attention),
        "pending": pending_count if demands_available else None,
    }
    return record
