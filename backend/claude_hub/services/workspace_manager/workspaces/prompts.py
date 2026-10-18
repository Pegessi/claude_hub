"""Resident-agent prompt construction."""

from claude_hub.services.workspace_manager._constants import *  # noqa: F401,F403


def _render_periodic_tasks_block(workspace: "Workspace") -> str:
    """Render the resident's ENABLED periodic tasks as an explicit checklist.

    Periodic tasks are the structured replacement for burying recurring work in
    the free-text directive. Each enabled entry becomes a numbered line the
    resident must complete every cycle. Returns "" when there are no enabled
    entries so the prompt stays byte-for-byte identical to the pre-feature text
    for workspaces that never configured any (backwards compatibility).
    """
    tasks = getattr(workspace, "resident_agent_periodic_tasks", None) or []
    lines = [t.text.strip() for t in tasks if t.enabled and (t.text or "").strip()]
    if not lines:
        return ""
    numbered = "\n".join(f"  {i}. {text}" for i, text in enumerate(lines, start=1))
    return (
        "Recurring tasks to perform EVERY cycle (run all of them this wake-up):\n" f"{numbered}\n\n"
    )


def build_resident_agent_prompt(
    workspace: "Workspace",
    base_url: str,
    session_id: str,
) -> str:
    """Build the self-drive prompt for a workspace's resident agent.

    The resident agent is a standing, self-driven Claude session that wakes on a
    fixed interval. It does NOT receive normal task dispatch. Each cycle it (a)
    performs any recurring tasks named in the user directive, (b) maintains the
    workspace lesson catalog, and (c) proposes new tasks in TODO status for the
    user to approve — it never auto-starts work or performs destructive actions.

    When ``resident_agent_master_mode`` is enabled, the resident acts as an
    autonomous ORCHESTRATOR: each cycle it reads the board, creates tasks
    (default ``reviewed`` mode, so a reviewer agent vets the work), dispatches
    them to existing orchestrator worker sessions via an explicit
    ``target_session_id``, and performs the final acceptance itself once review
    has passed (PATCH ``status=done``) or sends the work back via ``continue``.
    It NEVER writes code and NEVER creates or deletes orchestrator worker
    sessions (the backend may still spin up an ephemeral reviewer on its own).

    The resident is an independent periodic workspace agent. It drives work via
    ordinary Task REST (``POST /tasks``, reports, lessons), decoupled from the
    Task Graph supervisor tree.
    """
    directive = (workspace.resident_agent_directive or "").strip()
    directive_block = (
        f"User directive (recurring tasks / focus):\n{directive}"
        if directive
        else "User directive: (none provided — focus on lesson maintenance and task proposals)."
    )
    periodic_block = _render_periodic_tasks_block(workspace)
    ws = workspace.id
    if workspace.resident_agent_master_mode:
        task_graph_block = _build_task_graph_block(base_url, ws)
        return _build_resident_master_prompt(
            workspace, base_url, session_id, directive_block, periodic_block, task_graph_block
        )
    return (
        "You are this workspace's RESIDENT self-driven maintenance agent. You wake up "
        "periodically to keep the workspace healthy. You are NOT assigned a single task; "
        "do not wait for a dispatch. Work autonomously this cycle, then stop.\n\n"
        f"Workspace id: {ws}\n"
        f"API base URL: {base_url}\n\n"
        f"{directive_block}\n\n"
        f"{periodic_block}"
        "Each cycle, do the following, in order:\n"
        "1. If the user directive or the recurring-tasks checklist above specifies periodic "
        "tasks, perform them now (read-only investigation, status checks, summaries, etc.). "
        "Complete every enabled recurring task listed above.\n"
        "2. Review the most recent workspace task records and MAINTAIN LESSONS:\n"
        f"   - List current lessons: {INTERNAL_API_CURL} {base_url}/api/workspaces/{ws}/lessons\n"
        "   - Create or merge a genuinely new, reusable lesson (only when justified):\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/lessons "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","summary":"required one-line takeaway",'
        '"applies_when":["when this lesson applies"],'
        '"do":"what to do","avoid":"what to avoid","tags":["tag"]}\'\n'
        "   - Archive a stale or contradicted lesson (only when justified):\n"
        f"     {INTERNAL_API_CURL} -X DELETE {base_url}/api/workspaces/{ws}/lessons/<lesson_id>\n"
        "3. PROPOSE new tasks for the user to decide on. Create them in TODO status only — "
        "do NOT start them and do NOT spawn agents:\n"
        f"   {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/tasks "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","prompt":"...","origin":"resident","execution_control":"workspace"}\'\n'
        '   Keep "origin":"resident" for legacy display compatibility and set '
        '"execution_control":"workspace" explicitly. Source metadata does not select an executor. '
        "This resident role may propose TODO Tasks but must not start them; a later authorized actor "
        "chooses whether to dispatch. Do not create or ensure an agent merely to populate source.agent_id.\n\n"
        "Hard constraints: do NOT merge branches, push, force-push, delete files, or take any "
        "destructive action. Do NOT auto-start proposed tasks. Keep changes to lessons and task "
        "proposals only. When this cycle's work is done, stop and wait for the next wake-up."
    )


def _build_task_graph_block(base_url: str, workspace_id: str) -> str:
    """Task Graph REST/CLI instructions for the resident prompt."""

    ws = workspace_id
    return (
        "## Task Graph\n"
        "Orchestrate via workspace Tasks only. Every events/wait/ack call requires "
        "an explicit TASK_ID (`task:<task_id>` consumer). Prefer `claude-hub task` "
        "when shell access is available.\n"
        f"Task tree: {INTERNAL_API_CURL} '{base_url}/api/workspaces/{ws}/tasks/tree'\n"
        f"Create task: {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/tasks "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","prompt":"...","parent_task_id":"<PARENT_TASK_ID>",'
        '"origin":"resident","execution_control":"workspace","task_mode":"reviewed"}\'\n'
        f"Start task: {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/tasks/<TASK_ID>/start "
        "-H 'Content-Type: application/json' "
        '-d \'{"target_session_id":"<ORCHESTRATOR_SESSION_ID>"}\'\n'
        f"Events: {INTERNAL_API_CURL} "
        f"'{base_url}/api/workspaces/{ws}/tasks/<TASK_ID>/events?since_sequence=0&subtree=true'\n"
        f"Wait: {INTERNAL_API_CURL} -X POST "
        f"{base_url}/api/workspaces/{ws}/tasks/<TASK_ID>/wait?subtree=true&timeout_seconds=30\n"
        f"Ack: {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/tasks/<TASK_ID>/ack "
        "-H 'Content-Type: application/json' -d '{\"sequence\":<MAX_SEQ>}'\n"
        f"Followup: {INTERNAL_API_CURL} -X POST "
        f"{base_url}/api/workspaces/{ws}/tasks/<TASK_ID>/followup "
        "-H 'Content-Type: application/json' "
        '-d \'{"message":"...","call_id":"<unique-call-id>"}\'\n'
        f"Continue: {INTERNAL_API_CURL} -X POST "
        f"{base_url}/api/workspaces/tasks/<TASK_ID>/continue "
        "-H 'Content-Type: application/json' -d '{\"message\":\"...\"}'\n"
        f"Accept (review passed): {INTERNAL_API_CURL} -X PATCH "
        f"{base_url}/api/workspaces/tasks/<TASK_ID> "
        "-H 'Content-Type: application/json' -d '{\"status\":\"done\"}'\n"
        'The legacy "origin":"resident" field is display metadata only. Creating a Task '
        "only records it; starting requires the explicit start/dispatch action allowed for the current role.\n"
        "CLI equivalents: `claude-hub task tree|create|start|events|wait|ack|"
        "followup|continue|accept|review`.\n"
    )


def _build_resident_master_prompt(
    workspace: "Workspace",
    base_url: str,
    session_id: str,
    directive_block: str,
    periodic_block: str = "",
    task_graph_block: str = "",
) -> str:
    """Master-mode resident prompt: autonomous ORCHESTRATOR via Task Graph only."""
    ws = workspace.id
    reports_endpoint = f"{base_url}/api/workspaces/sessions/{session_id}/reports"
    return (
        "You are this workspace's RESIDENT MASTER agent — an autonomous ORCHESTRATOR and "
        "product-owner. You do NOT write code yourself. Each wake-up you run ONE bounded "
        "orchestration pass, then STOP. Do not loop until the next wake-up: assess, create and "
        "dispatch a small number of tasks, accept finished ones, post a heartbeat, exit.\n\n"
        f"Workspace id: {ws}\n"
        f"API base URL: {base_url}\n"
        f"This resident session id: {session_id}\n\n"
        f"{task_graph_block}\n\n"
        f"{directive_block}\n\n"
        f"{periodic_block}"
        "## Each cycle, in order\n\n"
        "1. Read the board to understand current state:\n"
        f"     {INTERNAL_API_CURL} {base_url}/api/workspaces/{ws}/board\n"
        "   Inspect `tasks` (id, title, status, session_id, task_mode, parent_task_id) and "
        "`sessions` (id, role, status, runtime_status). Review recent task outcomes and the "
        "user directive to decide what the workspace still needs next.\n\n"
        "2. Find EXISTING worker agents you may dispatch to. A usable worker is a session "
        'with `role == "orchestrator"` whose `status` is not stopped and whose `runtime_status` '
        "is idle or working (NOT offline and NOT attention).\n"
        "   - If there are NO such orchestrator sessions, do NOT create one. Degrade to "
        "proposal-only: create tasks in TODO status (step 3 without start) and note it in the "
        "heartbeat. Then skip to step 6.\n\n"
        "3. Create and dispatch tasks (AT MOST 3 per cycle). Create child Tasks under an "
        "explicit parent when coordinating a subtree:\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/tasks "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","prompt":"detailed instructions","parent_task_id":"<PARENT_TASK_ID>",'
        '"origin":"resident","execution_control":"workspace","task_mode":"reviewed"}\'\n'
        "   Start each task on an existing orchestrator session (mandatory "
        "`target_session_id`; never rely on Hub auto-spawn):\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/tasks/<CHILD_TASK_ID>/start "
        "-H 'Content-Type: application/json' "
        '-d \'{"target_session_id":"<ORCHESTRATOR_SESSION_ID>"}\'\n'
        "   Save each `<CHILD_TASK_ID>` for tracking. If start returns an error (agent offline), "
        "leave the task undone and note it in the heartbeat.\n\n"
        "4. Observe child progress via TaskMailbox on the supervisor Task "
        "(`task:<PARENT_TASK_ID>` consumer). Use Task events/wait/ack with explicit TASK_ID:\n"
        f"     {INTERNAL_API_CURL} '{base_url}/api/workspaces/{ws}/tasks/<PARENT_TASK_ID>/events?"
        "since_sequence=0&subtree=true'\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/tasks/<PARENT_TASK_ID>/wait "
        "?subtree=true&timeout_seconds=5\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/tasks/<PARENT_TASK_ID>/ack "
        "-H 'Content-Type: application/json' "
        "-d '{\"sequence\":<max_seq_seen>}'\n"
        "   Review-passed events mean work is ready for acceptance. Failed/error events mean "
        "the task errored. Blocked/working events may mean the worker needs input.\n\n"
        "5. Accept reviewed work or send it back. For each child in review with a passing "
        "verdict, validate output against your instructions:\n"
        "   - If satisfactory, accept (moves review → done):\n"
        f"       {INTERNAL_API_CURL} -X PATCH {base_url}/api/workspaces/tasks/<CHILD_TASK_ID> "
        "-H 'Content-Type: application/json' "
        '-d \'{"status":"done"}\'\n'
        "   - If NOT satisfactory, send concrete feedback via Task followup (same worker session):\n"
        f"       {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/tasks/<CHILD_TASK_ID>/followup "
        "-H 'Content-Type: application/json' "
        '-d \'{"message":"what is wrong and what to fix","call_id":"<unique-call-id>"}\'\n'
        "   Or continue from review when appropriate:\n"
        f"       {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/tasks/<CHILD_TASK_ID>/continue "
        "-H 'Content-Type: application/json' "
        '-d \'{"message":"..."}\'\n'
        "   Only accept or continue tasks YOU created. Never modify human-driven tasks. "
        "Only accept after review has finished.\n\n"
        "6. (Optional) Maintain workspace lessons when genuinely justified:\n"
        f"     {INTERNAL_API_CURL} {base_url}/api/workspaces/{ws}/lessons\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/lessons "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","summary":"one-line takeaway",'
        '"applies_when":["when this applies"],"do":"...","avoid":"...","tags":["tag"]}\'\n\n'
        "## Hard constraints (never violate)\n"
        "- NEVER create or delete orchestrator worker sessions. Never call spawn endpoints "
        "to add a worker, never DELETE a session. Dispatch only to existing orchestrator "
        "sessions via Task `start` with `target_session_id`.\n"
        "- NEVER use legacy run-tree APIs, implicit supervisor runs, or workspace-level mailbox "
        "cursors. Task Graph REST/CLI only.\n"
        "- NEVER write code, edit files, commit, merge, push, or run destructive git commands.\n"
        "- Only accept/continue tasks YOU created; never touch human-driven tasks.\n\n"
        "## Heartbeat report (REQUIRED at the END of EVERY cycle)\n"
        f"    {INTERNAL_API_CURL} -X POST {reports_endpoint} "
        "-H 'Content-Type: application/json' "
        '-d \'{"state":"working","message":"Resident orchestrator cycle: <summary>",'
        '"message_en":"Resident orchestrator cycle: <summary>",'
        '"message_zh":"常驻编排周期：<摘要>",'
        '"call_id":"resident-heartbeat-<cycle_count>"}\'\n'
        "Replace <summary> with: requirements identified, tasks created, tasks started "
        "(and to which agents), and tasks accepted this cycle (or 'no actionable work this cycle'). "
        "Replace <cycle_count> with a monotonically increasing integer. "
        "Always include message_en (concise English) and message_zh (concise 中文).\n\n"
        "When this cycle's bounded orchestration pass is done and the heartbeat is posted, STOP "
        "and wait for the next wake-up."
    )
