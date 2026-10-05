# Chat-linked durable work

## System overview

The Chat UI remains the entry point. `claude-hub work` exposes a compact create,
inspect, control and report surface using the source tab from
`CLAUDE_HUB_TAB_ID`. A stable request key makes retries return the same linked
work. One-shot work and recurring monitoring share the existing ScheduledTask
parent; every execution uses a normal WorkspaceTask with a persisted
`source_work_id`. There is no second scheduler or task lifecycle store.

- One-shot work defaults to the existing reviewed mode. Explicit direct,
  subagent or autonomous modes use their existing acceptance rules.
- Monitoring uses the existing internal scheduled-task completion/ephemeral
  cleanup path. Interval is at least 60 seconds; due checks coalesce while an
  execution is active. No LLM is kept alive merely waiting for the next check.
- Provider and cwd default to the source Chat. Claude/Codex explicit models map
  to the actual launch variables ANTHROPIC_MODEL/CODEX_MODEL. Unsupported model
  overrides and remote source Chats fail clearly. A named environment preset is
  forwarded to the actual session launcher; credentials are never added to the
  work view. Same-provider inheritance reads the source Chat environment at
  launch. Hard deletion or provider change of that source fails closed; selecting
  an explicit preset avoids that dependency. Archiving a Chat retains its tab.

## API and reporting

`GET/POST /api/tabs/{tab_id}/work`, `GET/PATCH .../{work_id}`, and
`POST .../{work_id}/report` return ChatWorkView. A view contains one stable work
id, its current execution, the latest notable report with provenance and at most
20 execution rows. Notable results remain visible after more than 20 quiet
checks. The API never injects polling results into the provider transcript.

The assigned worker uses one reporting command:

```sh
claude-hub work report WORK_ID --tab-id SOURCE_TAB --task-id TASK_ID \
  --session-id ASSIGNED_SESSION --kind no_change \
  --summary 'Still running' --validation 'Endpoint returned RUNNING'
```

This writes the canonical task report with its outcome before terminal cleanup.
`no_change` completes one unchanged check; `completed` ends the entire monitor.
`anomaly` completes a check and retains a notable result. `progress` keeps the
execution active, and `decision` uses the existing needs-input report state.
Existing reports can also be classified with `--report-id`. Results must cite a
report from the linked execution; the ordinary assignment validator still runs
for new reports. A report/call id cannot be reused with conflicting contents.
Worker reports are evidence-bearing claims, not proof that model prose is true.

## Lifecycle and failure behavior

Creation saves the schedule intent, then execution ownership before session
creation awaits. The schedule lock serializes firing, controls and reporting;
workspace locking remains authoritative for canonical report intake. Creation
persistence failures cannot be replayed as successful phantom work. Interrupted
pre-dispatch launches fail closed, preserving the execution for inspection.
Linked dead workers are not silently replayed on another model; explicit resume
is required. Existing task timeout/report handling remains active.

Pause disables future checks while the current check finishes; the card reports
paused with its active task id retained. Resume may re-enable a running monitor's
future cadence without duplicating its active check. Stop persists the stop
intent, uses the existing abort lifecycle and cleans only caller-owned ephemeral
sessions. Interruption is best-effort and cannot undo external side effects.
No worktree, report, shared session or accepted task evidence is deleted.

## Validation

Focused tests cover concurrent create retries, no overlapping checks, actual
session launch settings, canonical completion and cleanup, report retry after
worker deletion, one-shot review preservation, wrong-tab/foreign-task rejection,
pause/resume/stop, failed-save rollback, interrupted launch recovery, retained
notable evidence, post-report restart recovery, report/stop serialization, API
and CLI environment scoping. Terminal I/O is mocked; manager, persistence,
dispatch and canonical report lifecycle remain real. Real-provider/browser
acceptance belongs to the combined Chat workflow integration.
