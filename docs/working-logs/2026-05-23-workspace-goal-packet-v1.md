# Workspace Goal Packet v1

## System Overview

Agent Workspace tasks support a task-level Goal Packet. The packet captures the worker's structured interpretation of the user's prompt: objective, acceptance criteria, validation plan, assumptions, out-of-scope boundaries, and final handoff requirements.

Goal Packets are additive metadata on `WorkspaceTask`. Existing tasks without a packet remain valid, and malformed legacy packet data is normalized away during state loading instead of breaking workspace startup.

For reviewed tasks, the initial worker `working` report that includes a Goal Packet is a pre-implementation approval gate. The backend stores that packet as `pending_review`, dispatches a reviewer to judge only the packet, and continues the original worker only after `review_passed`. A failed packet review returns the worker to revise and resubmit the packet without starting implementation.

## Module Design

- `backend/claude_hub/models/schemas.py` defines `GoalPacket`, `GoalPacketStatus`, `AcceptanceCheck`, and `AcceptanceCheckStatus`.
- `backend/claude_hub/services/workspace_manager.py` persists task-level packets during task create/update and when a worker includes `goal_packet` in a report.
- Worker assignment prompts now ask the agent to derive a Goal Packet before substantive implementation and to stop after posting it until the approval review passes.
- `GoalPacketStatus` includes `pending_review`, `approved`, and `rejected` so the existing task `review` column can represent packet approval without adding a new persisted task status.
- A `pending_review` packet dispatches a special Goal Packet approval review prompt. Reviewers check goal fidelity, editable/non-editable boundaries, execution order, validation plan, and handoff requirements. They explicitly do not judge implementation completeness at this stage.
- `review_passed` on a pending packet marks it `approved` and sends the original worker a continuation prompt to begin implementation. `review_failed` marks it `rejected` and sends a packet-revision prompt. Final implementation review still uses the ordinary `ready_for_review`/`completed` path.
- Report payloads can include `acceptance_check`, which stays attached to the report as handoff evidence.
- Reviewer prompts include the original task prompt, stored Goal Packet, trigger report, recent reports, changed files, validation, risks, and acceptance checks.
- Completion reports that request `review_decision=skip` must have a stored Goal Packet and non-empty acceptance-check evidence; otherwise the backend sends a supplement prompt and keeps the task working.
- `review_decision=skip` only bypasses AI reviewer assignment. It does not complete the task; the task stays in the review column awaiting human completion.
- `human_acceptance_requested_at` is set after AI review passes or AI review is skipped. `human_accepted_at` is set when a human marks the task done/accepted.
- Manual Request review accepts a human note and sends it to the reviewer as the review trigger. It is not a duplicate path for sending implementation feedback to the original agent.
- Request-changes only re-engages the original agent when that session is still safe to reuse. If the agent has already been dispatched to another active task, the backend rejects the continuation instead of overwriting the active assignment.
- `frontend/src/components/AgentWorkspaceView.vue` renders a compact read-only Goal Packet section in task detail and shows acceptance-check evidence in report cards.

## Key Issues/Pitfalls

- Keep Goal Packet generation as a control-plane audit feature. Do not treat it as autonomous continuation or a replacement for the original task prompt.
- Packet approval is not implementation approval. A `review_passed` verdict on a `pending_review` packet only unlocks development; it must not request human acceptance.
- Reviewer prompts must explicitly check goal fidelity because a weak agent-generated packet can narrow the user's request.
- Goal Packet and acceptance-check fields are optional to preserve old task and report records.
- Optional storage does not mean optional evidence for review skip. Low-risk skips are only allowed after the agent supplies Goal Packet audit data and acceptance evidence.
- The human acceptance gate is deliberately layered on the existing `review` column to avoid a broader task state-machine rewrite in v1.
- The review column can represent both "awaiting human acceptance" and "reviewer blocked/failed" states. UI actions must be derived from the latest reviewer report, not from `status=review` alone.
- Report-state compatibility is the boundary: `ready_for_review` and `completed` still drive review creation; `review_*` verdict states are unchanged.
