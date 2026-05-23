# Workspace Goal Packet v1

## System Overview

Agent Workspace tasks now support an optional task-level Goal Packet. The packet captures the worker's structured interpretation of the user's prompt: objective, acceptance criteria, validation plan, assumptions, out-of-scope boundaries, and final handoff requirements.

Goal Packets are additive metadata on `WorkspaceTask`. Existing tasks without a packet remain valid, and malformed legacy packet data is normalized away during state loading instead of breaking workspace startup.

## Module Design

- `backend/claude_hub/models/schemas.py` defines `GoalPacket`, `GoalPacketStatus`, `AcceptanceCheck`, and `AcceptanceCheckStatus`.
- `backend/claude_hub/services/workspace_manager.py` persists task-level packets during task create/update and when a worker includes `goal_packet` in a report.
- Worker assignment prompts now ask the agent to derive a Goal Packet before substantive implementation.
- Report payloads can include `acceptance_check`, which stays attached to the report as handoff evidence.
- Reviewer prompts include the original task prompt, stored Goal Packet, trigger report, recent reports, changed files, validation, risks, and acceptance checks.
- Completion reports that request `review_decision=skip` must have a stored Goal Packet and non-empty acceptance-check evidence; otherwise the backend sends a supplement prompt and keeps the task working.
- `review_decision=skip` only bypasses AI reviewer assignment. It does not complete the task; the task stays in the review column awaiting human acceptance.
- `human_acceptance_requested_at` is set after AI review passes or AI review is skipped. `human_accepted_at` is set when a human marks the task done/accepted.
- Request-changes only re-engages the original agent when that session is still safe to reuse. If the agent has already been dispatched to another active task, the backend rejects the continuation instead of overwriting the active assignment.
- `frontend/src/components/AgentWorkspaceView.vue` renders a compact read-only Goal Packet section in task detail and shows acceptance-check evidence in report cards.

## Key Issues/Pitfalls

- Keep Goal Packet generation as a control-plane audit feature. Do not treat it as autonomous continuation or a replacement for the original task prompt.
- Reviewer prompts must explicitly check goal fidelity because a weak agent-generated packet can narrow the user's request.
- Goal Packet and acceptance-check fields are optional to preserve old task and report records.
- Optional storage does not mean optional evidence for review skip. Low-risk skips are only allowed after the agent supplies Goal Packet audit data and acceptance evidence.
- The human acceptance gate is deliberately layered on the existing `review` column to avoid a broader task state-machine rewrite in v1.
- The review column can represent both "awaiting human acceptance" and "reviewer blocked/failed" states. UI actions must be derived from the latest reviewer report, not from `status=review` alone.
- Report-state compatibility is the boundary: `ready_for_review` and `completed` still drive review creation; `review_*` verdict states are unchanged.
