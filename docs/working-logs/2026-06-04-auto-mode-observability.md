# Auto Mode Observability

## System Overview

Recent ZZZ gen autonomous image tasks showed that long ChatGPT Web, Doubao, and
Feishu loops could run for hours while the Claude Hub board only showed sparse
agent reports. The underlying reports had timestamps, but delegated role work
and external API/tool waits were embedded in prose and subagent-ledger text,
making the task look silent or single-agent even when later handoff evidence
claimed multi-agent execution.

## Module Design

- `workspace_manager.py` now adds derived timing fields to archived task-record
  timeline events: elapsed seconds/text from task creation and duration
  seconds/text since the previous timeline event.
- The Auto Mode orchestrator contract now requires heartbeat-style `working`
  reports around long delegated, remote, or external steps. Those reports must
  include role, primitive, elapsed time, current evidence, and next action.
- Autonomous `blocked` and `needs_input` reports are only acceptable when no
  autonomous next action remains, and they must include blocker evidence and the
  exact required decision.
- `AgentWorkspaceView.vue` derives live task timing from existing task/report
  timestamps and surfaces total elapsed, working elapsed, latest report age, a
  Progress overview timeline, and per-report progress deltas. The overview
  updates when board polling receives new task/report data; active queued,
  working, and review tasks also show a live `Now` endpoint that advances on a
  local timer.

## Key Issues/Pitfalls

- This is not full structured span tracing. Child agent/tool spans still need a
  future schema if Claude Hub should validate every delegated role as first-class
  data instead of report text.
- `started_at` can represent the latest active start after resume/abort flows,
  so the UI intentionally separates total elapsed from working elapsed.
- Bare `needs_input` text is not enough for autonomous image workflows because
  reviewers cannot tell whether generation, visual review, export, or delivery
  is blocked.
