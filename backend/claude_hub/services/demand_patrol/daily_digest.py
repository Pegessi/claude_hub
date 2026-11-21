"""Gate daily recommendations on fresh, verifiable decision candidates."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit

from claude_hub.models import TaskLifecycle, TaskPhase, WorkspaceTask
from claude_hub.services.demand_patrol.commands import (
    CommandError,
    CommandRunner,
    infowall_env,
    run_command,
)
from claude_hub.services.demand_patrol.models import MRStateSnapshot, PatrolRunRecord, PatrolState
from claude_hub.services.local_runtime_settings import local_setting

_MR_URL = re.compile(r"https://[A-Za-z0-9.-]+/(?:[A-Za-z0-9_.-]+/)+merge_requests/\d+\b")
_OPEN_DEMAND_STATUSES = frozenset({"active", "waiting", "planned"})
_ACTIVE_TASK_STATUSES = frozenset({"todo", "queued", "working", "review"})
_FAILURES = frozenset({"failed", "failure", "error", "timed_out", "timeout"})
_MAX_MRS = 15


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _display(value: Any, fallback: str = "未提供", limit: int = 180) -> str:
    text = " ".join(_text(value).split()) or fallback
    text = text[:limit].replace("<", "＜").replace(">", "＞").replace("|", "｜")
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


def _mr_urls(text: str) -> set[str]:
    hosts = {
        item.strip().lower()
        for item in local_setting("CLAUDE_HUB_PATROL_MR_HOSTS", "").split(",")
        if item.strip()
    }
    return {
        match.group()
        for match in _MR_URL.finditer(text)
        if (urlsplit(match.group()).hostname or "").lower() in hosts
    }


def demand_mr_urls(demand: Mapping[str, Any]) -> set[str]:
    """Extract canonical MR identities only from explicit source/progress evidence."""
    values: list[str] = []
    for source in _records(demand.get("sources")):
        values.extend((_text(source.get("url")), _text(source.get("source_url"))))
    for progress in _records(demand.get("progress")):
        for link in _records(progress.get("links")):
            values.append(_text(link.get("url")))
    return {url for value in values for url in _mr_urls(value)}


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
        if title_match or urls.intersection(_mr_urls(f"{task.title} {task.prompt}")):
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


DecisionModel = Callable[[str, str], Awaitable[str]]
_MAX_DECISIONS = 3
_MAX_MODEL_CANDIDATES = 8
_PRIORITIES = {"p0": 0, "p1": 1}
_REPLY_SUBJECTS = {"mr": "MR", "demand": "需求", "task": "Task"}


@dataclass(frozen=True)
class Decision:
    key: str
    fingerprint: str
    rank: tuple[int, int, float]
    kind: str
    subject: str
    situation: str
    actions: tuple[str, str]
    next_action: str = ""


@dataclass(frozen=True)
class DailyDigest:
    record: PatrolRunRecord
    receipts: dict[str, str]


def _fingerprint(*fields: str | int) -> str:
    return sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def _mr_decision(
    snapshot: MRStateSnapshot, demands: Sequence[Mapping[str, Any]]
) -> Decision | None:
    priority = min(
        (_PRIORITIES.get(_text(demand.get("priority")).lower(), 2) for demand in demands),
        default=2,
    )
    subject = f"[{_display(snapshot.title, 'MR')}]({snapshot.url})"
    if snapshot.ci_status == "failed":
        kind = "CI 失败"
        actions = ("指派 MR 负责人排查并重跑 CI", "暂缓该 MR 并保留失败记录")
    elif snapshot.state == "closed":
        kind = "MR 已关闭"
        actions = ("确认是否需要重新打开或替换 MR", "确认关闭并更新关联需求")
    elif snapshot.state == "merged":
        kind = "MR 已合入，关联需求仍待验收"
        actions = ("安排核对验收证据并更新需求", "继续保持需求开启，等待验收证据")
    else:
        return None
    return Decision(
        key=f"mr:{snapshot.url}",
        fingerprint=_fingerprint(kind, snapshot.state),
        rank=(priority, 0, 0),
        kind=kind,
        subject=subject,
        situation=kind,
        actions=actions,
    )


def _stall_decision(
    demand: Mapping[str, Any], tasks: Sequence[WorkspaceTask], state: PatrolState, now: datetime
) -> Decision | None:
    demand_id = _text(demand.get("id"))
    status = _text(demand.get("status"))
    priority = _text(demand.get("priority")).lower()
    touched = _last_touch(demand)
    if not demand_id or priority not in _PRIORITIES or status not in _OPEN_DEMAND_STATUSES:
        return None
    if (
        touched is None
        or (age := (now - touched).total_seconds() / 3600) < state.config.stalled_hours
    ):
        return None
    unassigned = status == "planned" and not _matching_tasks(demand, tasks)
    action = "指定负责人并确认处理时限" if unassigned else "指定负责人核查阻塞并更新进展"
    return Decision(
        key=f"demand:{demand_id}",
        fingerprint=_fingerprint(
            priority, status, touched.astimezone(timezone.utc).isoformat(), int(unassigned)
        ),
        rank=(_PRIORITIES[priority], 2, -age),
        kind="需求停滞待派工" if unassigned else "需求停滞",
        subject=f"{_display(demand.get('title'))}（需求 {_display(demand_id, limit=80)}）",
        situation=f"{priority.upper()} 已 {age:.0f} 小时未更新"
        + ("，仍待派工" if unassigned else ""),
        actions=(action, "明确延期日期并更新需求"),
        next_action=_text(demand.get("next_action")),
    )


_TASK_ACTIONS = {
    TaskPhase.NEEDS_USER: (
        "Task 等待用户决定",
        ("查看最新阻塞并给出决定", "明确暂缓并约定复查时间"),
    ),
    TaskPhase.AWAITING_ACCEPTANCE: ("Task 等待验收", ("核对产物后验收", "退回并补充验收缺口")),
    TaskPhase.ORPHANED: ("Task 等待重新接手", ("指定接手者继续任务", "暂缓任务并记录原因")),
}


def _task_decision(task: WorkspaceTask, lifecycle: TaskLifecycle | None) -> Decision | None:
    if not lifecycle or not lifecycle.attention or lifecycle.phase not in _TASK_ACTIONS:
        return None
    kind, actions = _TASK_ACTIONS[lifecycle.phase]
    return Decision(
        key=f"task:{task.id}",
        fingerprint=_fingerprint(
            lifecycle.phase.value,
            lifecycle.reason,
            lifecycle.owner.id or "",
            task.review_cycle,
            task.execution_epoch,
        ),
        rank=(1, 1, 0),
        kind=kind,
        subject=f"{_display(task.title)}（Task {_display(task.id, limit=80)}）",
        situation=f"{kind}（{_display(lifecycle.reason_text, '待处理')}）",
        actions=actions,
    )


async def _recommend(
    decisions: Sequence[Decision], model: DecisionModel, workspace_id: str
) -> list[tuple[Decision, str]]:
    # Titles, URLs, and free text from InfoWall or Task records stay local.
    # The model ranks factual candidate types and chooses among fixed actions.
    context = [
        {
            "id": index,
            "kind": item.kind,
            "priority_rank": item.rank[0],
            "stalled_hours": round(-item.rank[2]) if item.rank[2] else None,
            "options": dict(zip(("A", "B"), item.actions, strict=True)),
        }
        for index, item in enumerate(decisions, start=1)
    ]
    count = min(len(decisions), _MAX_DECISIONS)
    prompt = (
        "你是每日巡检的只读决策简报助手。根据事实类型和候选动作，提炼最需要用户拍板的事项。"
        "仅做文本归纳，禁止使用任何工具、访问文件或外部系统、执行动作或通知他人。"
        f"从下方恰好选 {count} 项，优先选择 priority_rank 较小的事项；同级按阻塞紧迫性取舍，并按优先级顺序输出。"
        '仅输出 JSON 数组，每项为 [事项 id, 推荐选项 A 或 B]，例如 [[1,"A"],[2,"B"]]。\n'
        + json.dumps(context, ensure_ascii=False)
    )
    async with asyncio.timeout(40):
        answer = (await model(prompt, workspace_id)).strip()
    answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer)
    if len(answer) > 100:
        raise ValueError("Daily recommendation output exceeds its budget")
    choices = json.loads(answer)
    if not isinstance(choices, list) or len(choices) != count:
        raise ValueError("Invalid daily recommendation length")
    selected: list[tuple[Decision, str]] = []
    seen: set[int] = set()
    for choice in choices:
        if not isinstance(choice, list) or len(choice) != 2:
            raise ValueError("Invalid daily recommendation entry")
        index, option = choice
        if type(index) is not int or not 1 <= index <= len(decisions) or index in seen:
            raise ValueError("Invalid daily recommendation index")
        if option not in ("A", "B"):
            raise ValueError("Invalid daily recommendation option")
        seen.add(index)
        selected.append((decisions[index - 1], option))
    if [decision.rank[0] for decision, _ in selected] != [
        decision.rank[0] for decision in decisions[:count]
    ]:
        raise ValueError("Daily recommendation omitted higher priority decisions")
    return selected


def _render(selected: Sequence[tuple[Decision, str]], slot_label: str) -> str:
    lines = [f"**研发需求巡检 · {_display(slot_label)}**"]
    for item, recommended in selected:
        options = [
            f"{letter}{'（推荐）' if letter == recommended else ''} {action}"
            for letter, action in zip(("A", "B"), item.actions, strict=True)
        ]
        scope, _, identity = item.key.partition(":")
        reply_subject = f"{_REPLY_SUBJECTS[scope]} {_display(identity)}"
        reply_action = item.actions[0 if recommended == "A" else 1]
        if item.next_action:
            options[0] += f"（已有下一步：{_display(item.next_action, limit=80)}）"
        lines.append(
            f"- {item.subject}：{item.situation}。\n"
            f"  选项：{'；'.join(options)}。\n"
            f"  快捷回复：「{reply_subject}，请{reply_action}」。"
        )
    return "\n\n".join(lines)


async def build_daily_digest(
    state: PatrolState,
    *,
    now: datetime,
    tasks: Sequence[WorkspaceTask],
    lifecycles: Mapping[str, TaskLifecycle],
    runner: CommandRunner = run_command,
    decision_model: DecisionModel,
    slot_label: str = "manual",
    dry_run: bool = False,
) -> DailyDigest:
    """Refresh observed state and return receipts to commit after delivery."""
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    record = PatrolRunRecord(channel="daily", started_at=now.isoformat(), dry_run=dry_run)
    demand_rows: list[Mapping[str, Any]] = []
    demands_available = True
    try:
        payload = await runner(["infowall", "demand", "list", "--json"], env=infowall_env())
        demand_rows = _demands(payload)
    except (CommandError, OSError, TimeoutError, ValueError):
        demands_available = False
        record.errors.append("InfoWall 需求读取失败，无法核查关联 MR 和需求停滞。")

    urls_to_demands: dict[str, list[Mapping[str, Any]]] = {}
    for demand in demand_rows:
        if demand.get("status") in _OPEN_DEMAND_STATUSES:
            for url in demand_mr_urls(demand):
                urls_to_demands.setdefault(url, []).append(demand)
    urls = sorted(urls_to_demands)[:_MAX_MRS]
    gate = asyncio.Semaphore(4)
    results = await asyncio.gather(*(_read_mr(url, runner, gate) for url in urls))
    receipts = state.daily_decision_receipts.copy()
    decisions: dict[str, Decision] = {}
    queried = 0
    for snapshot, error in results:
        if error:
            record.errors.append(error)
        if snapshot is None:
            continue
        queried += 1
        previous = state.mr_snapshots.get(snapshot.url)
        # An absent CI field is an incomplete observation. Preserve the last
        # known status so its disappearance does not re-arm an alert.
        if (
            snapshot.ci_status == "unknown"
            and previous is not None
            and snapshot.state == previous.state
        ):
            snapshot.ci_status = previous.ci_status
            if previous.ci_status == "failed":
                continue
        decision = _mr_decision(snapshot, urls_to_demands[snapshot.url])
        if decision is not None:
            decisions[decision.key] = decision
        else:
            receipts.pop(f"mr:{snapshot.url}", None)
        if not dry_run:
            state.mr_snapshots[snapshot.url] = snapshot

    for demand in demand_rows:
        if (decision := _stall_decision(demand, tasks, state, now)) is not None:
            decisions[decision.key] = decision
    for task in tasks:
        if (decision := _task_decision(task, lifecycles.get(task.id))) is not None:
            decisions[decision.key] = decision
    for key in list(receipts):
        scope, _, identity = key.partition(":")
        if key in decisions:
            continue
        if scope == "task" or (
            demands_available
            and (scope == "demand" or (scope == "mr" and identity not in urls_to_demands))
        ):
            del receipts[key]
    if not dry_run:
        state.daily_decision_receipts = receipts

    fresh = [item for item in decisions.values() if receipts.get(item.key) != item.fingerprint]
    fresh.sort(key=lambda item: (item.rank, item.key))
    selected = [(item, "A") for item in fresh[:_MAX_DECISIONS]]
    record.candidates = len(fresh)
    record.details = {
        "demands_available": demands_available,
        "demands": len(demand_rows) if demands_available else None,
        "mrs_queried": queried,
        "mrs_requested": len(urls),
        "mrs_omitted": len(urls_to_demands) - len(urls),
        "selected": len(selected),
        "decision_mode": "skipped",
    }
    if selected:
        record.details["decision_mode"] = "deterministic"
        if not dry_run:
            record.llm_invoked = 1
            try:
                selected = await _recommend(
                    fresh[:_MAX_MODEL_CANDIDATES], decision_model, state.config.workspace_id
                )
                record.details["decision_mode"] = "model"
            except Exception as exc:
                record.details["provider_fallback"] = type(exc).__name__
        record.markdown = _render(selected, slot_label)
    return DailyDigest(record, {item.key: item.fingerprint for item, _ in selected})
