"""Bounded (<2 KB) AI handoff and context-recovery brief for a single WorkspaceTask."""

from __future__ import annotations

from typing import Any, Sequence

from ..models.schemas import AgentReport, TaskLifecycle, Workspace, WorkspaceTask
from ..models.task_mailbox import TaskEvent

MAX_BRIEF_CHARS = 2048


def _clean_line(text: Any, limit: int = 320) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def build_task_brief_payload(
    *,
    workspace: Workspace,
    task: WorkspaceTask,
    lifecycle: TaskLifecycle,
    reports: Sequence[AgentReport],
    events: Sequence[TaskEvent],
    blockers: Sequence[str],
) -> dict[str, Any]:
    """Build a compact structured + Markdown handoff packet (<2 KB) for AI or human review."""
    latest_report = reports[-1] if reports else None
    prog = task.latest_progress

    if latest_report is not None:
        raw_summary = latest_report.message_zh or latest_report.message or latest_report.message_en
        validation = latest_report.validation or ""
        risks = latest_report.risks or ""
        artifacts = list(
            dict.fromkeys([*latest_report.artifact_refs, *latest_report.changed_files])
        )
    elif prog is not None:
        raw_summary = prog.summary
        validation = prog.validation or ""
        risks = prog.risks or ""
        artifacts = list(prog.artifact_refs)
    else:
        raw_summary = ""
        validation = ""
        risks = ""
        artifacts = []

    objective = _clean_line(
        task.goal_packet.objective if task.goal_packet else task.prompt,
        limit=360,
    )
    summary_text = _clean_line(raw_summary, limit=420)
    validation_text = _clean_line(validation, limit=260)
    risks_text = _clean_line(risks, limit=220)
    artifact_list = [str(item).strip() for item in artifacts if str(item).strip()][:6]

    milestones: list[str] = []
    if reports:
        meaningful = [r for r in reports if r.state.value not in {"started", "review_started"}]
        for r in meaningful[-4:]:
            msg = _clean_line(r.message_zh or r.message or r.message_en, limit=110)
            milestones.append(f"[{r.state.value}] {msg}")
    else:
        prog_events = [
            e
            for e in events
            if e.type.value in {"progress", "completed", "failed"}
            and isinstance(e.payload.get("summary"), str)
            and str(e.payload.get("summary")).strip()
        ]
        if len(prog_events) > 5:
            sampled = [prog_events[0], *prog_events[-3:]]
        else:
            sampled = list(prog_events)
        for ev in sampled:
            st = str(ev.payload.get("state") or ev.type.value)
            milestones.append(f"[{st}] {_clean_line(ev.payload.get('summary'), limit=110)}")

    md_lines = [
        f"# Task Handoff Brief — {task.title}",
        (
            f"- Task: `{task.id}` | Workspace: `{workspace.name}` (`{workspace.id}`) "
            f"| Path: `{workspace.path}`"
        ),
        (
            f"- State: status=`{task.status.value}`, phase=`{lifecycle.phase.value}` "
            f"({lifecycle.reason}), control=`{task.execution_control.value}`, "
            f"epoch={task.execution_epoch}, rev={task.progress_revision}"
        ),
    ]
    if task.source and task.source.tab_id:
        md_lines.append(f"- Source Chat Tab: `{task.source.tab_id}`")
    if blockers:
        md_lines.append(f"- Blockers: {_clean_line('; '.join(blockers), limit=200)}")
    if lifecycle.attention and lifecycle.reason_text:
        md_lines.append(f"- Attention: {_clean_line(lifecycle.reason_text, limit=180)}")
    md_lines.append(f"- Objective: {objective}")
    if summary_text:
        md_lines.append(f"- Latest Outcome: {summary_text}")
    if validation_text:
        md_lines.append(f"- Verified Checks: {validation_text}")
    if risks_text:
        md_lines.append(f"- Caveats / Unverified: {risks_text}")
    if artifact_list:
        md_lines.append("- Deliverables: " + ", ".join(f"`{a}`" for a in artifact_list))
    if milestones:
        md_lines.append("- Recent Checkpoints:")
        for item in milestones:
            md_lines.append(f"  - {item}")
    md_lines.append(
        f"- Inspect full record: `claude-hub task status {task.id} --workspace-id {workspace.id}`"
    )

    markdown = "\n".join(md_lines)
    if len(markdown) > MAX_BRIEF_CHARS:
        markdown = markdown[: MAX_BRIEF_CHARS - 20].rstrip() + "\n… [bounded]"

    return {
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "workspace_path": workspace.path,
        "task_id": task.id,
        "title": task.title,
        "status": task.status.value,
        "phase": lifecycle.phase.value,
        "phase_reason": lifecycle.reason,
        "attention": lifecycle.attention,
        "reason_text": lifecycle.reason_text,
        "execution_control": task.execution_control.value,
        "execution_epoch": task.execution_epoch,
        "progress_revision": task.progress_revision,
        "source_tab_id": task.source.tab_id if task.source else None,
        "objective": objective,
        "latest_outcome": summary_text,
        "validation": validation_text,
        "risks": risks_text,
        "deliverables": artifact_list,
        "milestones": milestones,
        "markdown": markdown,
    }
