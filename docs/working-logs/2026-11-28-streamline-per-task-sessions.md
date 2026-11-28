# 2026-11-28 — Streamlined Task Contracts, Subagent Blind Reviews & Per-Task Sessions

## Summary

Replaced the multi-role shared workspace agent pool and pre-implementation Goal
Packet stall with contract-first per-task isolated sessions and seatless
`SubagentService` blind reviews:

1. **First-class lightweight task contracts**: Added `goal`, `done_when`, and
   `out_of_scope` across task models, CLI (`task create`, `dot delegate`), and
   Web UI, synthesizing an `APPROVED` `GoalPacket` automatically unless
   `pending_review` is explicitly requested.
2. **Seatless blind review & 2-round rework cap**: Local implementation reviews
   run via one-shot `SubagentService` Chat subagents (`P0`/`P1` blocking only;
   evidence-backed false-positive rebuttal rules; 2-round automated rework cap
   before parking for human review).
3. **Per-task isolated sessions with archive-on-done**: Normal task starts
   provision dedicated `ephemeral` sessions (local Chat-capable providers
   default to `SessionKind.CHAT`) without queueing behind unrelated busy tasks.
   Marking a Chat task `Done` archives its session so transcripts and
   `conversation_id` stay inspectable and automatically unarchive when a
   follow-up task specifies `related_task_id`; `delete_task` / `task cleanup`
   performs physical removal.
4. **Retired shared-pool UI & `dot delegate` fast lane**: Removed `Manage Agents`,
   `WorkspaceAgentDialog`, `AgentDirectoryDialog`, and manual seat/`Clear context`
   controls; `dot delegate` routes directly through the same contract-driven
   pipeline with non-pushing `auto_worktree` on local Git repositories.

## Verification

- `./scripts/verify.sh docs`, `checkout-safety`, `backend-format`, `backend-types`
  (734 files), `frontend-lint`, `frontend-types`, `frontend-tests`,
  `frontend-build`.
- Focused backend suites covering per-task session lifecycle, archive/unarchive
  continuity, subagent blind review + rework cap, work pipeline, managed Chat
  orchestration, CLI lifecycle, and Dot delegation (`643 passed`).
- Multi-round independent `codex (high)` blind reviews resolved concurrency and
  lock-ordering edge cases cleanly.
