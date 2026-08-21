"""Workspace CRUD and the background monitor."""

import claude_hub.services.workspace_manager as _wm  # noqa: F401  (call-time patch lookup)

from ..agent_tree import _request_fingerprint
from ._constants import *  # noqa: F401,F403


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
    root_run_id: Optional[str] = None,
    ack_sequence: int = 0,
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

    The resident is also the root supervisor of the workspace's agent tree. It
    can spawn child runs (managed tasks) via the agent tree API and receive
    directed events from its subtree.
    """
    directive = (workspace.resident_agent_directive or "").strip()
    directive_block = (
        f"User directive (recurring tasks / focus):\n{directive}"
        if directive
        else "User directive: (none provided — focus on lesson maintenance and task proposals)."
    )
    periodic_block = _render_periodic_tasks_block(workspace)
    ws = workspace.id
    agent_tree_block = _build_agent_tree_block(base_url, ws, root_run_id, ack_sequence)
    if workspace.resident_agent_master_mode:
        return _build_resident_master_prompt(
            workspace, base_url, session_id, directive_block, periodic_block, agent_tree_block
        )
    return (
        "You are this workspace's RESIDENT self-driven maintenance agent. You wake up "
        "periodically to keep the workspace healthy. You are NOT assigned a single task; "
        "do not wait for a dispatch. Work autonomously this cycle, then stop.\n\n"
        f"Workspace id: {ws}\n"
        f"API base URL: {base_url}\n\n"
        f"{agent_tree_block}\n\n"
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
        '-d \'{"title":"...","prompt":"...","origin":"resident"}\'\n'
        '   Always include "origin":"resident" so the UI tags the proposal as '
        "agent-created. "
        "Newly created tasks stay in TODO; the user chooses whether to start them.\n\n"
        "Hard constraints: do NOT merge branches, push, force-push, delete files, or take any "
        "destructive action. Do NOT auto-start proposed tasks. Keep changes to lessons and task "
        "proposals only. When this cycle's work is done, stop and wait for the next wake-up."
    )


def _build_agent_tree_block(
    base_url: str,
    workspace_id: str,
    root_run_id: Optional[str],
    ack_sequence: int = 0,
) -> str:
    """Build the agent tree API instructions block for the resident prompt.

    Informs the resident of its root run id, its persisted ACK cursor
    (``ack_sequence``), and how to spawn child runs, wait for directed
    subtree events, and acknowledge them.

    ``ack_sequence`` is the resident root run's persisted ACK cursor. The
    resident should use it as the starting ``since_sequence`` for ``wait``
    calls so it only receives events it has not yet processed. After
    processing events, the resident calls ``ack`` to advance the cursor.
    """
    if not root_run_id:
        return (
            "## Agent Tree\n"
            "Your agent tree root run is not yet available. It will be created "
            "on the next cycle."
        )
    return (
        "## Agent Tree (supervisor role)\n"
        f"You are the root supervisor of this workspace's agent tree. Your root run id is:\n"
        f"  {root_run_id}\n"
        f"Your persisted ACK cursor (last processed event sequence): {ack_sequence}\n\n"
        "You can delegate work to child runs (managed tasks) and receive directed "
        "events from your subtree. Use the agent tree API:\n\n"
        "1. Spawn a child run (creates a managed task and dispatches it to a worker):\n"
        f"   {INTERNAL_API_CURL} -X POST {base_url}/api/agent-tree/spawn "
        "-H 'Content-Type: application/json' "
        f'-d \'{{"workspace_id":"{workspace_id}","parent_id":"{root_run_id}",'
        '"executor_kind":"managed_task","title":"...","initial_message":"...",'
        '"call_id":"<unique-call-id>","session_id":"<orchestrator-session-id>"}\'\n'
        "   The response is the child run. Save its `id` for tracking. "
        "`session_id` is optional: when provided, the task is dispatched to that "
        "specific orchestrator worker session (use this to route work to an "
        "existing worker). When omitted, the backend picks an available worker.\n\n"
        "2. Wait for directed events from your subtree (blocks until events arrive "
        "or timeout). Start from your persisted ACK cursor so you only get new "
        "events:\n"
        f"   {INTERNAL_API_CURL} -X POST {base_url}/api/agent-tree/wait "
        "-H 'Content-Type: application/json' "
        f'-d \'{{"workspace_id":"{workspace_id}","recipient_id":"{root_run_id}",'
        f'"since_sequence":{ack_sequence},"subtree":true,"timeout_seconds":30}}\'\n'
        "   Returns events addressed to you (recipient == your root run id). "
        "Child runs address their reports to you (their supervisor), so you "
        "receive their progress/completed/failed events. After processing, "
        "call `ack` with the highest sequence you saw to advance your "
        "persisted cursor.\n\n"
        "3. Acknowledge events up to a sequence cursor (persists your progress "
        "so a restarted resident resumes from where it left off):\n"
        f"   {INTERNAL_API_CURL} -X POST '{base_url}/api/agent-tree/ack?"
        f"workspace_id={workspace_id}&run_id={root_run_id}&sequence=<max_seq>'\n\n"
        "4. Send a follow-up message to a child run AND resume its turn (use this "
        "to send reviewed work back to the worker with feedback; maps to "
        "continue_task):\n"
        f"   {INTERNAL_API_CURL} -X POST {base_url}/api/agent-tree/followup "
        "-H 'Content-Type: application/json' "
        f'-d \'{{"workspace_id":"{workspace_id}","recipient_id":"<child_run_id>",'
        f'"author_id":"{root_run_id}","message":"what is wrong and what to fix",'
        '"call_id":"<unique-call-id>"}\'\n\n'
        "5. Interrupt a child run (preserves context for later resume):\n"
        f"   {INTERNAL_API_CURL} -X POST {base_url}/api/agent-tree/interrupt "
        "-H 'Content-Type: application/json' "
        f'-d \'{{"workspace_id":"{workspace_id}","run_id":"<child_run_id>",'
        '"call_id":"<unique-call-id>"}\'\n\n'
        "Child runs emit events (progress, blocked, completed, failed) that are "
        "delivered to your mailbox. Use `wait` to observe them and `ack` to mark "
        "them processed. A `completed` event means the child run finished "
        "successfully; a `failed` event means it failed."
    )


def _build_resident_master_prompt(
    workspace: "Workspace",
    base_url: str,
    session_id: str,
    directive_block: str,
    periodic_block: str = "",
    agent_tree_block: str = "",
) -> str:
    """Master-mode resident prompt: an autonomous ORCHESTRATOR / product-owner.

    Each cycle the resident reads the board, creates a small number of tasks
    (default ``reviewed`` mode — a reviewer agent vets the work), dispatches them
    to EXISTING orchestrator worker sessions via the agent tree ``spawn`` action
    (passing ``session_id`` for explicit worker routing), and performs the final
    acceptance itself once review has passed (PATCH ``status=done``) or sends the
    work back via the agent tree ``followup`` action. It NEVER writes code and
    NEVER creates or deletes orchestrator worker sessions (the backend may
    auto-spawn an ephemeral reviewer to vet a task — that is allowed; the
    resident just never provisions worker agents itself).
    """
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
        f"{agent_tree_block}\n\n"
        f"{directive_block}\n\n"
        f"{periodic_block}"
        "## Each cycle, in order\n\n"
        "1. Read the board to understand current state:\n"
        f"     {INTERNAL_API_CURL} {base_url}/api/workspaces/{ws}/board\n"
        "   Inspect `tasks` (id, title, status, session_id, task_mode) and `sessions` (id, role, "
        "status, runtime_status). Review recent task outcomes and the user directive to decide "
        "what the workspace still needs next. Iterate on the requirements — refine the goal, do "
        "not just repeat finished work. If a recurring-tasks checklist was provided above, treat "
        "those items as standing objectives to advance every cycle.\n\n"
        "2. Find the EXISTING worker agents you may dispatch to. A usable worker is a session "
        'with `role == "orchestrator"` whose `status` is not stopped and whose `runtime_status` '
        "is idle or working (NOT offline and NOT attention).\n"
        "   - If there are NO such orchestrator sessions, you MUST NOT create one and you MUST "
        "NOT start any task. Instead, degrade to proposal-only: create any tasks you think are "
        "needed in TODO status (step 3, but WITHOUT the spawn call) and say so in your heartbeat "
        '("no worker agents available — proposed N tasks for the user to start"). Then skip to '
        "step 6.\n\n"
        "3. Create and dispatch the tasks you deem necessary this cycle. Create AT MOST 3 "
        "tasks per cycle to avoid a runaway backlog. Use the agent tree `spawn` action to create "
        "a managed task AND dispatch it to an existing orchestrator worker in one call:\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/agent-tree/spawn "
        "-H 'Content-Type: application/json' "
        f'-d \'{{"workspace_id":"{ws}","parent_id":"<your-root-run-id>",'
        '"executor_kind":"managed_task","title":"...","initial_message":"detailed instructions '
        'for the worker","call_id":"<unique-call-id>",'
        '"session_id":"<existing-orchestrator-session-id>"}\'\n'
        "   This creates a managed task (default reviewed mode — a reviewer agent vets the work "
        "before it returns to you) and dispatches it to the specified orchestrator session. "
        "The response is the child run; save its `id` (the run id) and `context_ref` (the task "
        "id) for tracking. Always pass an explicit `session_id` so the backend never auto-creates "
        "an agent. If the spawn call returns an error (e.g. the agent went offline), leave the "
        "task undone and note it in the heartbeat — do NOT retry against a different role and do "
        "NOT create an agent.\n\n"
        "4. Observe child run progress via the agent tree event stream instead of polling the "
        "board. Use `wait` to fetch directed events from your subtree since your last acknowledged "
        "sequence, then `ack` to advance your cursor:\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/agent-tree/wait "
        "-H 'Content-Type: application/json' "
        f'-d \'{{"workspace_id":"{ws}","recipient_id":"<your-root-run-id>",'
        '"since_sequence":<last_acked_seq>,"subtree":true,"timeout_seconds":5}}\'\n'
        f"     {INTERNAL_API_CURL} -X POST '{base_url}/api/agent-tree/ack?"
        f"workspace_id={ws}&run_id=<your-root-run-id>&sequence=<max_seq_seen>'\n"
        "   A `completed` event on a child run means the task finished (review passed). A "
        "`failed` event means review failed or the task errored. A `blocked` event means the "
        "worker needs input.\n\n"
        "5. Accept reviewed work or send it back. For each child run that reached `completed` "
        "(review passed), read the worker's latest report/output and the reviewer's verdict, then "
        "validate it against what you asked for:\n"
        "   - If satisfactory, accept the task (this moves it from review to done):\n"
        f"       {INTERNAL_API_CURL} -X PATCH {base_url}/api/workspaces/tasks/<task_id> "
        "-H 'Content-Type: application/json' "
        '-d \'{"status":"done"}\'\n'
        "   - If NOT satisfactory, send it back to the SAME worker with concrete feedback using "
        "the agent tree `followup` action (this re-dispatches to the original agent, not a new "
        "one):\n"
        f"       {INTERNAL_API_CURL} -X POST {base_url}/api/agent-tree/followup "
        "-H 'Content-Type: application/json' "
        f'-d \'{{"workspace_id":"{ws}","recipient_id":"<child_run_id>",'
        f'"author_id":"<your-root-run-id>","message":"what is wrong and what to fix",'
        '"call_id":"<unique-call-id>"}\'\n'
        "   Only ever accept or continue tasks YOU created. Never accept or modify tasks a human "
        "created or dispatched. Only accept a task after review has finished (the child run "
        "emitted a `completed` event).\n\n"
        "6. (Optional, as before) Maintain workspace lessons when genuinely justified:\n"
        f"     {INTERNAL_API_CURL} {base_url}/api/workspaces/{ws}/lessons\n"
        f"     {INTERNAL_API_CURL} -X POST {base_url}/api/workspaces/{ws}/lessons "
        "-H 'Content-Type: application/json' "
        '-d \'{"title":"...","summary":"one-line takeaway",'
        '"applies_when":["when this applies"],"do":"...","avoid":"...","tags":["tag"]}\'\n\n'
        "## Hard constraints (never violate)\n"
        "- NEVER create or delete orchestrator worker sessions. Never call any agent-spawn "
        "endpoint to add a worker, never DELETE a session. You may ONLY dispatch to "
        "already-existing orchestrator sessions. If none exist, you propose tasks and stop. "
        "(The backend may auto-spawn a short-lived REVIEWER to vet a reviewed task — that is the "
        "backend's doing and is allowed; you never provision agents yourself.)\n"
        "- ALWAYS pass an explicit `session_id` when spawning a task, and NEVER spawn a task "
        "when no orchestrator session exists (so the backend never auto-creates a default "
        "worker agent).\n"
        "- NEVER write code, edit files, commit, merge, push, or run destructive git commands. "
        "You are an orchestrator; the worker agents do the implementation.\n"
        "- Only accept/continue tasks YOU created; never touch human-driven tasks. Only accept a "
        "task after review has finished (the child run emitted a `completed` event).\n\n"
        "## Heartbeat report (REQUIRED at the END of EVERY cycle)\n"
        "Post one workspace-level heartbeat summarizing this cycle. task_id is omitted for a "
        "workspace-level heartbeat:\n"
        f"    {INTERNAL_API_CURL} -X POST {reports_endpoint} "
        "-H 'Content-Type: application/json' "
        '-d \'{"state":"working","message":"Resident orchestrator cycle: <summary>",'
        '"message_en":"Resident orchestrator cycle: <summary>",'
        '"message_zh":"常驻编排周期：<摘要>",'
        '"call_id":"resident-heartbeat-<cycle_count>"}\'\n'
        "Replace <summary> with: requirements identified, tasks created, tasks dispatched (and to "
        "which agents), and tasks accepted this cycle (or 'no actionable work this cycle'). "
        "Replace <cycle_count> with a monotonically increasing integer for this resident session "
        "(e.g. 1, 2, 3, ...) so each heartbeat has a unique call_id. "
        "Always include message_en (concise English) and message_zh (concise 中文).\n\n"
        "When this cycle's bounded orchestration pass is done and the heartbeat is posted, STOP "
        "and wait for the next wake-up."
    )


class _WorkspacesMixin:
    # Initialized in _StateMixin.__init__; annotation-only declaration (no value,
    # so no runtime attribute is created) lets mypy type the rebind below.
    _monitor_task: "asyncio.Task[None] | None"
    # Initialized in _StateMixin.__init__; annotation-only declarations let mypy
    # type the dict-comprehension rebinds in delete_workspace below.
    tasks: "dict[str, WorkspaceTask]"
    reports: "dict[str, AgentReport]"

    def list_workspaces(self) -> list[Workspace]:
        return sorted(self.workspaces.values(), key=lambda item: item.created_at)

    def start_background_monitor(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._background_monitor_loop())

    async def stop_background_monitor(self) -> None:
        if not self._monitor_task:
            return
        self._monitor_task.cancel()
        try:
            await self._monitor_task
        except asyncio.CancelledError:
            pass
        finally:
            self._monitor_task = None

    async def _background_monitor_loop(self) -> None:
        # One-time agent tree crash recovery: retry any PENDING runs whose
        # adapter spawn was lost (process crashed after the run was persisted
        # but before the executor context was created). Runs that already have
        # a context_ref are advanced to RUNNING.
        for workspace_id in list(self.workspaces):
            try:
                await self.agent_tree.recover_pending_runs(workspace_id)
            except Exception:
                logger.exception("Agent tree recovery failed for workspace %s", workspace_id)
        while True:
            try:
                await self._refresh_session_statuses(run_auto_continue=True)
                # Expire stale processing call_ids for sessions whose tmux
                # inbox is gone (STOPPED). A call_id in processing means the
                # pump successfully sent it to tmux; for live sessions the
                # message is already in the tmux input buffer and will be
                # ACKed by the worker, so we must NOT re-deliver it (that
                # would produce a duplicate turn). Only when the tmux session
                # itself is destroyed is the input buffer lost, at which
                # point expiry moves the stranded call_ids back to pending
                # for re-delivery.
                for session_id in list(self.sessions):
                    try:
                        self._expire_processing_leases(session_id)
                    except Exception:
                        logger.exception("Lease expiry failed for session %s", session_id)
                # Receipt-based cold recovery for live sessions: query the
                # tmux-server receipt for each processing call_id. If the
                # receipt is absent, the paste never ran and we can safely
                # return the call_id to pending. If the session is gone,
                # move to uncertain.
                for session_id in list(self.sessions):
                    sess = self.sessions.get(session_id)
                    if sess is None or sess.status == ManagedSessionStatus.STOPPED:
                        continue
                    if not sess.processing_call_ids:
                        continue
                    try:
                        await self._recover_processing_via_receipt(session_id)
                    except Exception:
                        logger.exception(
                            "Receipt-based processing recovery failed for session %s",
                            session_id,
                        )
                # Pump any pending call_ids that were not yet delivered to
                # tmux (e.g. the pump failed on a previous cycle, or a new
                # message was enqueued). This is the live counterpart to the
                # cold-recovery pump in recover_pending_runs.
                for session_id in list(self.sessions):
                    sess = self.sessions.get(session_id)
                    if sess is None or not sess.pending_call_ids:
                        continue
                    try:
                        await self._pump_session_messages(session_id)
                    except Exception:
                        logger.exception("Live pump failed for session %s", session_id)
                for workspace_id in list(self.workspaces):
                    await self.dispatch_workspace(workspace_id, refresh_sessions=False)
                await self._tick_resident_agents()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Workspace background monitor failed")
            await asyncio.sleep(WORKSPACE_MONITOR_INTERVAL_SECONDS)

    def get_workspace(self, workspace_id: str) -> Optional[Workspace]:
        return self.workspaces.get(workspace_id)

    def create_workspace(self, payload: WorkspaceCreate) -> Workspace:
        source_path = Path(payload.path).expanduser().resolve()
        if not source_path.exists() or not source_path.is_dir():
            raise ValueError(f"Local workspace dir does not exist: {source_path}")
        if payload.target == ExecutionTarget.REMOTE:
            if not payload.remote_profile_id:
                raise ValueError("Remote workspace requires remote_profile_id")
            if not remote_profile_manager.get_profile(payload.remote_profile_id):
                raise ValueError(f"Remote profile not found: {payload.remote_profile_id}")

        workspace_id = str(uuid.uuid4())
        now = _wm._now()
        prefix = payload.session_prefix or _slug(payload.name)
        interval_minutes = max(1, payload.resident_agent_interval_minutes)
        directive = (payload.resident_agent_directive or "").strip() or None
        resident_title = (payload.resident_agent_title or "").strip() or None
        resident_cwd = (payload.resident_agent_cwd or "").strip() or None
        periodic_tasks = self._normalize_periodic_tasks(payload.resident_agent_periodic_tasks)
        workspace = Workspace(
            id=workspace_id,
            name=payload.name,
            path=str(source_path),
            default_branch=payload.default_branch,
            session_prefix=prefix,
            dispatcher_session_id=None,
            target=payload.target,
            remote_profile_id=payload.remote_profile_id,
            remote_cwd=payload.remote_cwd,
            remote_reconnect=payload.remote_reconnect,
            resident_agent_enabled=payload.resident_agent_enabled,
            resident_agent_paused=payload.resident_agent_paused,
            resident_agent_interval_minutes=interval_minutes,
            resident_agent_session_id=None,
            resident_agent_directive=directive,
            resident_agent_periodic_tasks=periodic_tasks,
            resident_agent_last_run_at=None,
            resident_agent_type=payload.resident_agent_type,
            resident_agent_env=dict(payload.resident_agent_env or {}),
            resident_agent_solo_mode=payload.resident_agent_solo_mode,
            resident_agent_master_mode=payload.resident_agent_master_mode,
            resident_agent_title=resident_title,
            resident_agent_target=payload.resident_agent_target,
            resident_agent_remote_profile_id=payload.resident_agent_remote_profile_id,
            resident_agent_cwd=resident_cwd,
            resident_agent_remote_reconnect=payload.resident_agent_remote_reconnect,
            created_at=now,
            updated_at=now,
        )
        self.workspaces[workspace_id] = workspace
        self._save_state()
        return workspace

    def update_workspace(self, workspace_id: str, payload: WorkspaceUpdate) -> Workspace:
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)

        update_kwargs: dict[str, Any] = {}
        if payload.name is not None:
            name = payload.name.strip()
            if not name:
                raise ValueError("Workspace name cannot be empty")
            update_kwargs["name"] = name
        if payload.path is not None:
            new_path = payload.path.strip()
            if not new_path:
                raise ValueError("Local workspace dir cannot be empty")
            resolved = Path(new_path).expanduser().resolve()
            if not resolved.exists() or not resolved.is_dir():
                raise ValueError(f"Local workspace dir does not exist: {resolved}")
            update_kwargs["path"] = str(resolved)
        if payload.default_branch is not None:
            branch = payload.default_branch.strip()
            if not branch:
                raise ValueError("Default branch cannot be empty")
            update_kwargs["default_branch"] = branch
        if payload.remote_cwd is not None:
            value = payload.remote_cwd.strip()
            update_kwargs["remote_cwd"] = value or None
        if payload.remote_reconnect is not None:
            update_kwargs["remote_reconnect"] = payload.remote_reconnect
        if payload.resident_agent_enabled is not None:
            update_kwargs["resident_agent_enabled"] = payload.resident_agent_enabled
        if payload.resident_agent_paused is not None:
            update_kwargs["resident_agent_paused"] = payload.resident_agent_paused
        if payload.resident_agent_interval_minutes is not None:
            if payload.resident_agent_interval_minutes < 1:
                raise ValueError("resident_agent_interval_minutes must be >= 1")
            update_kwargs["resident_agent_interval_minutes"] = (
                payload.resident_agent_interval_minutes
            )
        if payload.resident_agent_directive is not None:
            directive = payload.resident_agent_directive.strip()
            update_kwargs["resident_agent_directive"] = directive or None
        if payload.resident_agent_periodic_tasks is not None:
            update_kwargs["resident_agent_periodic_tasks"] = self._normalize_periodic_tasks(
                payload.resident_agent_periodic_tasks
            )
        if payload.resident_agent_type is not None:
            update_kwargs["resident_agent_type"] = payload.resident_agent_type
        if payload.resident_agent_env is not None:
            update_kwargs["resident_agent_env"] = dict(payload.resident_agent_env)
        if payload.resident_agent_solo_mode is not None:
            update_kwargs["resident_agent_solo_mode"] = payload.resident_agent_solo_mode
        if payload.resident_agent_master_mode is not None:
            update_kwargs["resident_agent_master_mode"] = payload.resident_agent_master_mode
        if payload.resident_agent_title is not None:
            title = payload.resident_agent_title.strip()
            update_kwargs["resident_agent_title"] = title or None
        if payload.resident_agent_target is not None:
            update_kwargs["resident_agent_target"] = payload.resident_agent_target
        if payload.resident_agent_remote_profile_id is not None:
            profile_id = payload.resident_agent_remote_profile_id.strip()
            update_kwargs["resident_agent_remote_profile_id"] = profile_id or None
        if payload.resident_agent_cwd is not None:
            cwd = payload.resident_agent_cwd.strip()
            update_kwargs["resident_agent_cwd"] = cwd or None
        if payload.resident_agent_remote_reconnect is not None:
            update_kwargs["resident_agent_remote_reconnect"] = (
                payload.resident_agent_remote_reconnect
            )

        # Resident launch-config invalidation
        # ------------------------------------
        # The resident's agent_type/env/solo_mode are LAUNCH-TIME properties: they
        # are only applied on the CREATE path (inside the EnsureWorkspaceAgentRequest
        # in _run_resident_agent). The reuse path re-drives whatever live session is
        # tracked by resident_agent_session_id and does NOT rebuild the request, so a
        # config change here would otherwise be silently ignored while a session is
        # alive — worst case claude->terminal keeps prompting the stale claude session
        # forever. To make any of type/env/solo_mode changes actually take effect, we
        # clear resident_agent_session_id (and drop the old ManagedSession row) so the
        # next tick recreates the resident with the new launch config.
        #
        # Tab teardown: delete_session / delete_workspace tear down the old tab via
        # `await ttyd_manager.delete_tab(...)` — but BOTH are async and update_workspace
        # is SYNC, so we cannot await an async teardown here. Per design we therefore do
        # NOT call delete_tab from this sync path; instead we only drop the ManagedSession
        # row, which makes the old tab a session-less orphan that the existing
        # _prune_orphan_workspace_tabs reconciler (run on the monitor loop) cleans up.
        # This keeps sync code sync-safe and reuses the established orphan-tab pruner.
        #
        # Disable teardown: when resident_agent_enabled flips True -> False in this
        # update, we tear the resident down the SAME way (clear the pointer + drop the
        # ManagedSession so the orphan-tab pruner removes the tab) and additionally
        # reset resident_agent_last_run_at so a future re-enable starts clean. This is
        # the ENABLE master switch: OFF means "stop AND tear down", no orphan left
        # running. PAUSE (resident_agent_paused) deliberately does NOT come through
        # here — pausing keeps resident_agent_session_id and the ManagedSession intact
        # so the user can still open the resident terminal and chat manually; it only
        # stops automatic scheduling (handled in _resident_agent_due).
        disabling_resident = (
            workspace.resident_agent_enabled is True
            and update_kwargs.get("resident_agent_enabled") is False
        )
        old_resident_session_id = workspace.resident_agent_session_id
        if old_resident_session_id is not None and (
            disabling_resident or self._resident_launch_config_changed(workspace, update_kwargs)
        ):
            update_kwargs["resident_agent_session_id"] = None
            self.sessions.pop(old_resident_session_id, None)
            if disabling_resident:
                update_kwargs["resident_agent_last_run_at"] = None

        if not update_kwargs:
            return workspace

        updated = workspace.model_copy(update={**update_kwargs, "updated_at": _wm._now()})
        # Recompute the UI next-run hint whenever anything that feeds it may have
        # changed (interval, enable, pause, or last_run reset on disable). This is
        # advisory only; the authoritative trigger is _resident_agent_due.
        updated = updated.model_copy(
            update={
                "resident_agent_next_run_at": self._resident_next_run_at(
                    updated, updated.resident_agent_last_run_at
                )
            }
        )
        self.workspaces[workspace_id] = updated
        self._save_state()
        return updated

    def request_resident_run(self, workspace_id: str) -> Workspace:
        """Flag the resident to run on the next monitor tick (manual "run now").

        Stamps ``resident_agent_run_requested_at``; ``_resident_agent_due`` then
        returns True on the next tick (within WORKSPACE_MONITOR_INTERVAL_SECONDS),
        and ``_run_resident_agent`` consumes/clears the flag when the cycle fires.
        The override respects Enable but bypasses Pause and the interval/activity
        gates — an explicit user request is a deliberate one-off.

        Raises ``KeyError`` if the workspace is missing, ``ValueError`` if the
        resident is not enabled (nothing to run). If the resident session is
        currently WORKING the flag still stays set and simply fires on the next
        idle tick (the WORKING-skip in ``_run_resident_agent`` defers it without
        clearing the request), so the manual run is not lost.
        """
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)
        if not workspace.resident_agent_enabled:
            raise ValueError("Resident agent is not enabled for this workspace")
        updated = workspace.model_copy(
            update={
                "resident_agent_run_requested_at": _wm._now(),
                "updated_at": _wm._now(),
            }
        )
        self.workspaces[workspace_id] = updated
        self._save_state()
        return updated

    @staticmethod
    def _resident_launch_config_changed(
        workspace: "Workspace", update_kwargs: dict[str, Any]
    ) -> bool:
        """True when this update changes the resident's launch config to a DIFFERENT value.

        Launch config = agent_type / env / solo_mode (the three properties applied only
        when the resident session/tab is created). We compare the proposed new value
        (present in ``update_kwargs`` only when the caller supplied a non-None field)
        against the current workspace value, so a no-op write of the same value does NOT
        trigger a needless recreation.
        """
        for field in (
            "resident_agent_type",
            "resident_agent_env",
            "resident_agent_solo_mode",
            "resident_agent_target",
            "resident_agent_remote_profile_id",
            "resident_agent_cwd",
            "resident_agent_remote_reconnect",
        ):
            if field in update_kwargs and update_kwargs[field] != getattr(workspace, field):
                return True
        return False

    @staticmethod
    def _normalize_periodic_tasks(
        tasks: Optional[list["ResidentPeriodicTask"]],
    ) -> list["ResidentPeriodicTask"]:
        """Normalize a periodic-task list: trim text, drop empties, keep order.

        Each entry keeps its client-supplied id (or the model default) so the UI
        can stably identify rows across edits; entries whose text is blank after
        trimming are dropped. ``None`` means "not provided" and yields an empty
        list (safe default; callers only pass ``None`` on create where absence
        means no periodic tasks).
        """
        result: list["ResidentPeriodicTask"] = []
        for task in tasks or []:
            text = (task.text or "").strip()
            if not text:
                continue
            result.append(task.model_copy(update={"text": text}))
        return result

    def _resident_next_run_at(
        self, workspace: "Workspace", last_run: Optional[datetime]
    ) -> Optional[datetime]:
        """Compute the resident's next overdue-backstop wake time for the UI.

        This mirrors the backstop arm in ``_resident_agent_due``: ``last_run +
        interval + stable jitter``. Returns ``None`` when the resident is
        disabled or paused (no automatic scheduling) or when ``last_run`` is
        ``None`` (bootstrap — it is due immediately, so there is no future time
        to show). The activity fast-path can still wake the resident EARLIER
        than this; the UI copy makes that clear. This value is advisory only —
        the authoritative trigger remains ``_resident_agent_due``.
        """
        if not workspace.resident_agent_enabled or workspace.resident_agent_paused:
            return None
        if last_run is None:
            return None
        interval_seconds = max(1, workspace.resident_agent_interval_minutes) * 60
        jitter = self._resident_jitter_seconds(workspace, interval_seconds)
        return last_run + timedelta(seconds=interval_seconds + jitter)

    @staticmethod
    def _resident_jitter_seconds(workspace: "Workspace", interval_seconds: int) -> int:
        """Stable, deterministic per-workspace jitter in ``[0, interval_seconds)``.

        Spreads resident wake-ups across the interval so that many workspaces
        sharing the same interval do not all fire on the same monitor tick (the
        classic thundering-herd / synchronized-poll problem). The offset is
        derived from a SHA-256 of the workspace id, NOT Python's builtin
        ``hash()`` (which is randomized per process via PYTHONHASHSEED) nor any
        time/random source — so it is identical across processes and restarts,
        and unit-testable.
        """
        if interval_seconds <= 0:
            return 0
        digest = hashlib.sha256(workspace.id.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % interval_seconds

    def _workspace_activity_since(self, workspace_id: str, since: Optional[datetime]) -> bool:
        """True when this workspace saw a real task OUTCOME or external progress.

        "Activity" deliberately means a real *outcome* to learn from, NOT mere
        task creation/update. For a NON ``system_internal`` task we look ONLY at
        its terminal/progress timestamps — ``completed_at``, ``reviewed_at`` and
        ``human_accepted_at`` — and treat the task as activity when any of those
        is newer than ``since``. A freshly-proposed TODO task has all three set
        to ``None``, so it does NOT trip the gate. This is what prevents the
        resident self-retrigger loop: the resident's prompt makes it PROPOSE
        tasks via ``POST /tasks`` (non-``system_internal`` tasks whose
        ``created_at``/``updated_at`` are newer than the just-stamped
        ``last_run``); gating on outcomes rather than creations means those
        proposals never re-arm the activity fast-path.

        A non-resident report created after ``since`` also counts as activity:
        worker agents post reports (the resident's prompt uses ``/tasks`` and
        ``/lessons``, not ``/sessions/{id}/reports``), so a fresh report is
        genuine progress. As defense-in-depth we still exclude reports and tasks
        whose ``session_id`` matches the workspace's
        ``resident_agent_session_id`` in case a future prompt makes the resident
        emit them. ``system_internal`` tasks are excluded entirely. When
        ``since`` is ``None`` any existing outcome/report counts.

        Agent-tree events in the resident's subtree (progress, blocked, failed,
        completed, etc.) also count as activity: the resident's child runs
        (managed tasks) emit directed events that the resident should react to.
        """
        workspace = self.workspaces.get(workspace_id)
        resident_session_id = workspace.resident_agent_session_id if workspace is not None else None

        def _after(value: Optional[datetime]) -> bool:
            if value is None:
                return False
            return since is None or value > since

        for task in self.tasks.values():
            if task.workspace_id != workspace_id or task.system_internal:
                continue
            if resident_session_id is not None and task.session_id == resident_session_id:
                # Defense-in-depth: ignore tasks owned by the resident itself so
                # its own proposals can never count as activity.
                continue
            # Gate on real outcomes only (completed/reviewed/accepted), never on
            # creation/update — a freshly-proposed TODO has these all None.
            if (
                _after(task.completed_at)
                or _after(task.reviewed_at)
                or _after(task.human_accepted_at)
            ):
                return True
        for report in self.reports.values():
            if report.workspace_id != workspace_id:
                continue
            if resident_session_id is not None and report.session_id == resident_session_id:
                # The resident does not post reports, but guard anyway so a
                # future prompt change cannot let it re-trigger itself.
                continue
            if _after(report.created_at):
                return True
        # Agent-tree directed events: any event in the resident root run's
        # subtree created after ``since`` counts as activity. This lets the
        # resident wake on child progress/blocked/failed/completed without
        # scanning global task/report APIs.
        if resident_session_id is not None:
            root_run = self.agent_tree.get_run_by_context_ref(workspace_id, resident_session_id)
            if root_run is not None:
                since_seq = 0
                # Map the ``since`` timestamp to a sequence cursor: find the
                # last event at or before ``since`` and use its sequence.
                if since is not None:
                    all_events = self.agent_tree.get_events(
                        workspace_id, root_run.id, since_sequence=0, subtree=True
                    )
                    for ev in all_events:
                        if ev.created_at <= since:
                            since_seq = max(since_seq, ev.sequence)
                new_events = self.agent_tree.get_events(
                    workspace_id, root_run.id, since_sequence=since_seq, subtree=True
                )
                if new_events:
                    return True
        return False

    def _resident_agent_due(self, workspace: Workspace, now: datetime) -> bool:
        """Return True when a resident agent should run this tick.

        Event-gated ("Option C") trigger. The cheap 5s monitor tick is only the
        wakeup; whether the resident actually fires is decided here:

        * **Disabled** -> never due.
        * **Manual run-now** -> if ``resident_agent_run_requested_at`` is set (the
          run-now endpoint stamped it), fire on the next tick regardless of the
          interval or activity gate. It still respects Enable (a disabled
          resident is never driven) but overrides Pause: an explicit user "run
          now" is an intentional one-off even while auto-scheduling is paused.
          The flag is cleared by ``_run_resident_agent`` once the cycle fires.
        * **Bootstrap** (``last_run_at is None``) -> due once. The first run
          establishes the activity/timer baseline; it does not fire instantly on
          every empty boot because once it runs the baseline is stamped.
        * **Activity-gated fast path** -> if there has been real workspace
          activity since the last run AND at least
          ``RESIDENT_ACTIVITY_DEBOUNCE_SECONDS`` have elapsed, fire now. The
          debounce floor coalesces bursts so a flurry of task updates triggers at
          most one run per debounce window instead of one per event.
        * **Overdue backstop** -> even with no activity, fire once the full
          ``resident_agent_interval_minutes`` (plus a stable per-workspace jitter
          offset) have elapsed, so idle-but-enabled workspaces still get a
          periodic pass. This is the legacy fixed-interval path, demoted to a
          backstop.

        Net: ``due = enabled AND (run_requested OR last_run is None
                                  OR (activity_since AND elapsed >= debounce)
                                  OR elapsed >= interval + jitter)``.
        """
        if not workspace.resident_agent_enabled:
            return False
        # Manual run-now override: an explicit user request fires on the next
        # tick even while paused (it is a deliberate one-off), but never while
        # disabled (guarded above). Checked BEFORE the paused early-return.
        if workspace.resident_agent_run_requested_at is not None:
            return True
        # Paused = keep the session alive for manual chat, but stop automatic
        # scheduling (no self-drive runs). disabled OR paused -> not due.
        if workspace.resident_agent_paused:
            return False

        last_run = workspace.resident_agent_last_run_at
        if last_run is None:
            # Bootstrap: run once to establish the baseline.
            return True

        elapsed = now - last_run
        interval_seconds = max(1, workspace.resident_agent_interval_minutes) * 60

        # Activity-gated fast path: react to real work, but no more than once per
        # debounce window.
        debounce = timedelta(seconds=RESIDENT_ACTIVITY_DEBOUNCE_SECONDS)
        if elapsed >= debounce and self._workspace_activity_since(workspace.id, last_run):
            return True

        # Overdue backstop: fixed interval + stable jitter keeps idle workspaces
        # ticking and desynchronizes wake-ups across workspaces.
        jitter = self._resident_jitter_seconds(workspace, interval_seconds)
        backstop = timedelta(seconds=interval_seconds + jitter)
        return elapsed >= backstop

    async def _tick_resident_agents(self) -> None:
        """Fire due resident agents across all workspaces.

        Wrapped per-workspace so one failure cannot abort the rest of the tick.
        Skips a workspace whose resident session is currently working.
        """
        now = _wm._now()
        for workspace_id in list(self.workspaces):
            workspace = self.workspaces.get(workspace_id)
            if workspace is None or not self._resident_agent_due(workspace, now):
                continue
            try:
                await self._run_resident_agent(workspace)
            except Exception:
                logger.exception("Resident agent tick failed for workspace_id=%s", workspace_id)

    async def _run_resident_agent(self, workspace: Workspace) -> None:
        """Create or reuse the workspace's resident agent and self-drive it.

        The resident is created with the workspace's configured
        ``resident_agent_type`` / ``resident_agent_env`` / ``resident_agent_solo_mode``
        (parity with normal workspace agents) rather than a hardcoded CLAUDE
        session with no env.

        TERMINAL edge case: a TERMINAL resident is a plain user shell with no LLM
        agent listening, so the self-drive prompt is pointless/harmful (it would
        be dumped as literal shell input). For TERMINAL we still create/track an
        openable tab and advance ``resident_agent_last_run_at`` (so it does not
        churn every tick), but we do NOT send the self-drive prompt on either the
        create path (suppressed in _build_session_bootstrap_prompt) or the reuse
        path (guarded below). CLAUDE/CURSOR/CODEX are CLI LLM agents and receive
        the same curl-based resident prompt as normal.
        """
        existing = self.sessions.get(workspace.resident_agent_session_id or "")
        if existing is not None and existing.status == ManagedSessionStatus.STOPPED:
            existing = None
        if existing is not None and existing.runtime_status == AgentRuntimeStatus.WORKING:
            # Busy from a prior cycle: skip without advancing the timer so it
            # retries on the next monitor tick.
            return

        if existing is not None:
            reused = True
            session = existing
        else:
            reused = False
            # Create the resident's root run BEFORE the session bootstrap so
            # the bootstrap prompt can include the root run id and the
            # resident can act as a supervisor from its first cycle. The
            # context_ref (session id) is set after the session is created.
            self._ensure_resident_root_run(workspace.id, session_id=None)

            # reuse_existing is False on purpose: the generic reuse path only
            # matches ORCHESTRATOR sessions, so a resident session must be
            # tracked and reused via workspace.resident_agent_session_id here.
            # NOTE: ensure_workspace_agent already sends the bootstrap prompt,
            # which for the RESIDENT role IS build_resident_agent_prompt (see
            # _prompts._build_session_bootstrap_prompt routing). So a freshly
            # created resident has already received the self-drive prompt this
            # cycle and must NOT be sent a second copy below. (For a TERMINAL
            # resident the bootstrap is suppressed to an empty string, so no
            # prompt is sent on create either.)
            session = await self.ensure_workspace_agent(
                workspace.id,
                EnsureWorkspaceAgentRequest(
                    agent_type=workspace.resident_agent_type,
                    env=dict(workspace.resident_agent_env or {}),
                    solo_mode=workspace.resident_agent_solo_mode,
                    role=WorkspaceSessionRole.RESIDENT,
                    reuse_existing=False,
                    title=(workspace.resident_agent_title or f"{workspace.name} Resident"),
                    target=workspace.resident_agent_target,
                    cwd=workspace.resident_agent_cwd,
                    remote_profile_id=workspace.resident_agent_remote_profile_id,
                    remote_cwd=workspace.resident_agent_cwd,
                    remote_reconnect=workspace.resident_agent_remote_reconnect,
                ),
            )
            # Now that the session exists, link the root run to it via
            # context_ref so we can find the root run by session id later.
            self._ensure_resident_root_run(workspace.id, session_id=session.id)

        # Persist the session id and advance the timer BEFORE sending so that a
        # failure in send_session_message does not leave resident_agent_session_id
        # unset (which would respawn a brand-new session/tab every monitor tick)
        # nor leave last_run_at stale (which would retry immediately every tick).
        # We also clear resident_agent_run_requested_at (the manual run-now flag
        # is a one-off — consume it now that the cycle is firing) and recompute
        # resident_agent_next_run_at from the new last_run for the UI countdown.
        now = _wm._now()
        stamped = workspace.model_copy(
            update={
                "resident_agent_session_id": session.id,
                "resident_agent_last_run_at": now,
                "resident_agent_run_requested_at": None,
            }
        )
        self.workspaces[workspace.id] = stamped.model_copy(
            update={
                "resident_agent_next_run_at": self._resident_next_run_at(stamped, now),
                "updated_at": now,
            }
        )
        self._save_state()

        # Ensure the resident has a root run in the agent tree so it can act as
        # a supervisor that spawns child runs and receives directed events from
        # its subtree. The root run's context_ref is the resident session id.
        self._ensure_resident_root_run(workspace.id, session.id)
        root_run = self.agent_tree.get_run_by_context_ref(workspace.id, session.id)
        root_run_id = root_run.id if root_run else None

        # A TERMINAL resident has no LLM agent to drive: advance the timer (done
        # above) and keep the tab, but never send the self-drive prompt.
        if workspace.resident_agent_type == AgentType.TERMINAL:
            return

        if not reused:
            # Bootstrap already delivered the resident prompt this cycle.
            return

        base_url = (
            f"http://127.0.0.1:{session.remote_forward_port}"
            if session.remote_forward_port
            else f"http://localhost:{settings.port}"
        )
        prompt = build_resident_agent_prompt(
            workspace,
            base_url,
            session.id,
            root_run_id,
            root_run.ack_sequence if root_run else 0,
        )

        # Inject recent directed subtree events so the resident can observe
        # child progress/blocked/failed/completed via the unified mailbox
        # instead of scanning global task/report APIs. Use the root run's
        # ack_sequence cursor so only unprocessed events are injected.
        #
        # We use ``agent_tree.wait`` (the directed cursor ACK mechanism)
        # rather than ``get_events`` directly: ``wait`` enforces the
        # Hub-side receiver cursor (``max(since_sequence, ack_sequence)``)
        # so ACKed events are never re-delivered.
        #
        # **Delivery-call_id binding (fail-closed cursor):**
        #
        # We do NOT advance the resident root run's ``ack_sequence`` cursor
        # here. Instead we bind the delivered event batch to a persistent
        # delivery call_id and store the batch's ``max_sequence`` in the
        # call record. The prompt is sent to the resident's tmux inbox via
        # ``send_session_message`` with that call_id. Only when the resident
        # worker ACKs that call_id (lists it in ``acked_call_ids`` of a
        # report) does ``_ack_call_ids`` → ``_advance_resident_ack_on_delivery``
        # advance the cursor to ``max_sequence``.
        #
        # This closes the loss window: if ``send_session_message`` fails or
        # the Hub crashes before the resident processes the events, the
        # cursor is NOT advanced, so the next cycle's ``wait`` returns the
        # same events again (at-least-once). If the resident processes them
        # and ACKs, the cursor advances exactly once.
        delivery_call_id: Optional[str] = None
        if root_run is not None:
            from claude_hub.models.agent_tree import AgentEventType, WaitRequest

            wait_req = WaitRequest(
                workspace_id=workspace.id,
                recipient_id=root_run.id,
                since_sequence=root_run.ack_sequence,
                subtree=True,
                timeout_seconds=1.0,
            )
            subtree_events = await self.agent_tree.wait(wait_req)
            if subtree_events:
                # Deliver the OLDEST unprocessed page (first 20 events), not
                # the most recent 20. Using the tail would silently drop the
                # older events the resident never saw. max_sequence binds
                # ONLY to the delivered page so the ACK cursor advances
                # exactly as far as the resident processed; the remaining
                # events are delivered in subsequent cycles.
                page = subtree_events[:20]
                event_lines = []
                for ev in page:
                    payload_msg = (ev.payload or {}).get("message", "")
                    event_lines.append(
                        f"  - seq={ev.sequence} type={ev.type.value} "
                        f"author={ev.author} recipient={ev.recipient or 'broadcast'} "
                        f"msg={payload_msg}"
                    )
                prompt += (
                    "\n\n## Recent child activity (agent tree events)\n"
                    "The following directed events from your subtree arrived since "
                    "your last cycle. Use them to decide what to do next instead of "
                    "scanning the board:\n" + "\n".join(event_lines) + "\n"
                )
                max_seq = max(ev.sequence for ev in page)
                # Bind the event batch to a delivery call_id. Record the
                # max_sequence in the call record so the ACK handler can
                # advance the cursor. The call_id is embedded in the prompt
                # so the resident can ACK it.
                delivery_call_id = f"resident-delivery:{root_run.id}:{max_seq}"
                try:
                    self.agent_tree._append_event(
                        workspace_id=workspace.id,
                        agent_run_id=root_run.id,
                        event_type=AgentEventType.PROGRESS,
                        author=root_run.id,
                        recipient=root_run.id,
                        call_id=delivery_call_id,
                        action="resident_delivery",
                        target=root_run.id,
                        fingerprint=_request_fingerprint(
                            "resident_delivery",
                            {"run_id": root_run.id, "max_sequence": max_seq},
                        ),
                        payload={"max_sequence": max_seq},
                        rollback_on_error=False,
                    )
                except Exception:
                    logger.exception(
                        "Failed to record resident delivery call_id=%s max_sequence=%s",
                        delivery_call_id,
                        max_seq,
                    )
                    # Fail closed: do NOT send the event-bearing prompt
                    # without a call_id. Without a call_id the resident
                    # cannot ACK the batch, so the cursor would never
                    # advance and the events would be silently lost after
                    # the resident processes them. Leave the cursor and
                    # events in place for the next cycle to retry.
                    return

        # Recovery contract: the prompt above is rebuilt each cycle (event
        # lines, lesson context, etc. may drift). The delivery call_id is
        # stable until ACK (resident-delivery:{root_run.id}:{max_seq}), so a
        # repeated cycle before ACK must NOT overwrite the persisted envelope
        # with a drifted prompt. Use resume_existing_call to pump the stored
        # payload (pending) or no-op (processing/delivered); only if the
        # call_id is absent do we commit the freshly built prompt.
        #
        # When there are no new events, delivery_call_id is None — this is an
        # ordinary self-drive prompt with no durable delivery contract, so we
        # send it directly without a call_id.
        if delivery_call_id is None:
            await self.send_session_message(session.id, prompt)
        else:
            resumed = await self.resume_existing_call(session.id, delivery_call_id)
            if not resumed:
                await self.send_session_message(session.id, prompt, call_id=delivery_call_id)

    def _ensure_resident_root_run(self, workspace_id: str, session_id: Optional[str]) -> None:
        """Ensure the resident has a root run in the agent tree.

        The resident acts as the root supervisor: it can spawn child runs
        (managed tasks) and receive directed events from its subtree. The
        root run's ``context_ref`` is the resident session id so we can
        locate it later.

        When ``session_id`` is ``None`` (called before the resident session
        is created), the root run is created without a context_ref. When
        ``session_id`` is provided later, the existing root run's
        context_ref is updated to link it to the session.
        """
        from claude_hub.models.agent_tree import ExecutorKind

        # Find the resident root run: the unique root (parent_id is None) in
        # this workspace.
        root_run = None
        for run in self.agent_tree._runs.values():
            if run.workspace_id == workspace_id and run.parent_id is None:
                root_run = run
                break

        if root_run is None:
            # No root run yet — create one. context_ref may be None if the
            # session hasn't been created yet.
            self.agent_tree.create_root_run(
                workspace_id=workspace_id,
                executor_kind=ExecutorKind.RESIDENT_ROOT,
                title="Resident Agent",
                context_ref=session_id,
            )
            return

        # Root run exists. If a session_id was provided and the root run's
        # context_ref doesn't match, update it (links the root run to the
        # resident session).
        if session_id is not None and root_run.context_ref != session_id:
            root_run.context_ref = session_id
            root_run.updated_at = _wm._now()
            self._save_state()

    async def delete_workspace(self, workspace_id: str) -> None:
        """Delete a workspace and all of its in-memory and on-disk state.

        Tears down every managed session's terminal tab (unconditionally — unlike
        ``delete_session`` there is no non-DONE-task guard), purges the
        workspace's tasks/sessions/reports, removes its on-disk state directory,
        then rewrites the index.
        """
        workspace = self.workspaces.get(workspace_id)
        if workspace is None:
            raise KeyError(workspace_id)

        for session in [s for s in self.sessions.values() if s.workspace_id == workspace_id]:
            self.sessions.pop(session.id, None)
            try:
                await ttyd_manager.delete_tab(session.tab_id)
            except Exception:
                logger.exception(
                    "Failed to delete terminal tab while deleting workspace "
                    "workspace_id=%s session_id=%s tab_id=%s",
                    workspace_id,
                    session.id,
                    session.tab_id,
                )

        self.tasks = {
            task_id: task
            for task_id, task in self.tasks.items()
            if task.workspace_id != workspace_id
        }
        self.reports = {
            report_id: report
            for report_id, report in self.reports.items()
            if report.workspace_id != workspace_id
        }
        self.workspaces.pop(workspace_id, None)

        shutil.rmtree(_wm.STATE_ROOT / workspace_id, ignore_errors=True)
        self._save_state()
