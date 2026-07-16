"""Render key prompts and report token sizes. Used to verify compaction targets."""
import sys, os, asyncio, uuid
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
os.environ["CLAUDE_HUB_DATA_DIR"] = "/tmp/ch_measure_" + uuid.uuid4().hex[:8]

try:
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    def tok(s): return len(enc.encode(s))
except Exception:
    def tok(s): return len(s)//4

from claude_hub.models import schemas as S
from claude_hub.services.workspace_manager import WorkspaceManager
from datetime import datetime, timezone


def dt(): return datetime.now(timezone.utc)


def make_task(mgr, w, worker, **overrides):
    defaults = dict(
        id="t"+uuid.uuid4().hex[:6], workspace_id=w.id, title="Measure",
        prompt="Measure prompt sizes and identify bloat across autonomous/complex flows.",
        task_mode=S.WorkspaceTaskMode.AUTONOMOUS,
        execution_complexity=S.WorkspaceTaskExecutionComplexity.COMPLEX,
        autonomy_policy=S.AutonomyPolicy(),
        created_at=dt(), updated_at=dt(), status=S.WorkspaceTaskStatus.WORKING,
        session_id=worker.id, agent_type=S.AgentType.CLAUDE,
        clear_context=True, origin=S.WorkspaceTaskOrigin.HUMAN,
        dispatch_reason="measure", review_cycle=1,
    )
    defaults.update(overrides)
    t = S.WorkspaceTask(**defaults)
    if t.task_mode == S.WorkspaceTaskMode.AUTONOMOUS and t.autonomous_run is None:
        t.autonomous_run = S.AutonomousRun(id=str(uuid.uuid4()), phase=S.AutonomousRunPhase.INTAKE)
    mgr.tasks[t.id] = t
    return t


async def main():
    mgr = WorkspaceManager()
    # Clear any disk-loaded state so measurements are reproducible and isolated.
    mgr.workspaces.clear()
    mgr.tasks.clear()
    mgr.sessions.clear()
    mgr.reports.clear()
    w = S.Workspace(
        id="w1", name="measure", path="/tmp", target=S.ExecutionTarget.LOCAL,
        default_agent_type=S.AgentType.CLAUDE, default_branch="main",
        session_prefix="cb", created_at=dt(), updated_at=dt(),
    )
    mgr.workspaces[w.id] = w
    worker = S.ManagedSession(
        id="cb-w", workspace_id=w.id, title="w", agent_type=S.AgentType.CLAUDE,
        role=S.WorkspaceSessionRole.WORKER, target=S.ExecutionTarget.LOCAL,
        workspace_path="/tmp", tab_id="t1", created_at=dt(), updated_at=dt(),
        status=S.ManagedSessionStatus.IDLE, tmux_session="x",
        runtime_status=S.AgentRuntimeStatus.IDLE,
    )
    reviewer = S.ManagedSession(
        id="cb-r", workspace_id=w.id, title="r", agent_type=S.AgentType.CLAUDE,
        role=S.WorkspaceSessionRole.REVIEWER, target=S.ExecutionTarget.LOCAL,
        workspace_path="/tmp", tab_id="t2", created_at=dt(), updated_at=dt(),
        status=S.ManagedSessionStatus.IDLE, tmux_session="y",
        runtime_status=S.AgentRuntimeStatus.IDLE,
    )
    mgr.sessions[worker.id] = worker
    mgr.sessions[reviewer.id] = reviewer

    def show(name, text):
        print(f"{name:55s}  chars={len(text):6d}  tok~={tok(text):5d}  lines={text.count(chr(10))+1:4d}")

    t_auto_complex = make_task(mgr, w, worker)
    t_auto_simple = make_task(mgr, w, worker,
                              execution_complexity=S.WorkspaceTaskExecutionComplexity.SIMPLE,
                              id="ts")
    t_rev_complex = make_task(mgr, w, worker,
                              task_mode=S.WorkspaceTaskMode.REVIEWED,
                              autonomous_run=None, id="trc")
    t_direct_simple = make_task(mgr, w, worker,
                                task_mode=S.WorkspaceTaskMode.DIRECT,
                                execution_complexity=S.WorkspaceTaskExecutionComplexity.SIMPLE,
                                autonomous_run=None, id="tds")
    t_iter2 = make_task(mgr, w, worker, id="t2", review_cycle=3)
    t_iter2.autonomous_run = S.AutonomousRun(id=str(uuid.uuid4()), phase=S.AutonomousRunPhase.REVISING, iteration=3)
    # Seed some prior reports for t_iter2 so the tiered serializer is exercised
    for i in range(10):
        r = S.AgentReport(
            id=str(uuid.uuid4()), workspace_id=w.id, task_id=t_iter2.id,
            session_id=worker.id if i % 2 == 0 else reviewer.id,
            state=[S.AgentReportState.WORKING, S.AgentReportState.REVIEW_FAILED][i % 2],
            message=f"Iteration progress/failure {i}",
            message_en="x", message_zh="x",
            changed_files=[f"backend/file_{i}.py"] if i%2==0 else [],
            validation=("subagent-ledger:\n  - role.id=impl primitive=P-EXECUTE goal=do thing "
                        "decision=accepted evidence=backend/file.py, some/other/path.py with a "
                        "bunch of output "*30),  # long validation, like real ledger
            risks="some risks " * 40,
            acceptance_check=[S.AcceptanceCheck(criterion=f"crit {j}", status=S.AcceptanceCheckStatus.PASSED,
                                                    evidence="ev"*20) for j in range(4)],
            review_decision=S.ReviewDecision.AUTO, review_reason="",
            risk_level="low", review_cycle=i//2+1, created_at=dt(),
        )
        mgr.reports[r.id] = r
    # Trigger report
    trigger = S.AgentReport(
        id=str(uuid.uuid4()), workspace_id=w.id, task_id=t_auto_complex.id, session_id=worker.id,
        state=S.AgentReportState.COMPLETED,
        message="done", message_en="done", message_zh="完成",
        changed_files=["backend/claude_hub/services/workspace_manager/_prompts.py"],
        validation="subagent-ledger:\n  - role.id=impl primitive=P-EXECUTE decision=accepted\n",
        risks=None, review_decision=S.ReviewDecision.AUTO, review_reason="",
        risk_level="low", review_cycle=1, created_at=dt(),
    )
    mgr.reports[trigger.id] = trigger
    trigger2 = S.AgentReport(
        id=str(uuid.uuid4()), workspace_id=w.id, task_id=t_iter2.id, session_id=worker.id,
        state=S.AgentReportState.COMPLETED,
        message="iteration 3 complete", message_en="done", message_zh="完成",
        changed_files=["backend/x.py"], validation="ledger here"*50, risks=None,
        review_decision=S.ReviewDecision.AUTO, review_reason="", risk_level="low",
        review_cycle=3, created_at=dt(),
    )
    mgr.reports[trigger2.id] = trigger2

    print("== cold-start prompts ==")
    show("bootstrap worker", mgr._build_workspace_agent_prompt(w, worker))
    show("ASSIGN autonomous+complex claude",
         mgr._build_task_assignment_prompt(w, t_auto_complex, worker, lesson_context=[]))
    show("ASSIGN autonomous+simple claude",
         mgr._build_task_assignment_prompt(w, t_auto_simple, worker, lesson_context=[]))
    show("ASSIGN reviewed+complex claude",
         mgr._build_task_assignment_prompt(w, t_rev_complex, worker, lesson_context=[]))
    show("ASSIGN direct+simple claude",
         mgr._build_task_assignment_prompt(w, t_direct_simple, worker, lesson_context=[]))
    show("REVIEW autonomous+complex (1 prior report)",
         mgr._build_review_prompt(w, t_auto_complex, reviewer, trigger, lesson_context=[]))
    show("CONTINUE (autonomous)",
         mgr._build_continue_prompt(t_auto_complex,
                                    S.ContinueTaskRequest(message="Fix the blocking issues."),
                                    worker))
    show("HARD-RECOVERY worker (auto+complex, iter=1)",
         mgr._build_hard_recovery_worker_prompt(w, t_auto_complex, worker, "API error 529"))
    show("HARD-RECOVERY worker (auto+complex, iter=3)  [resume briefing]",
         mgr._build_hard_recovery_worker_prompt(w, t_iter2, worker, "API error 529"))
    print()
    print("== history scaling test ==")
    show("REVIEW autonomous+complex (10 prior verbose reports, iter=3) [tiered]",
         mgr._build_review_prompt(w, t_iter2, reviewer, trigger2, lesson_context=[]))

asyncio.run(main())
