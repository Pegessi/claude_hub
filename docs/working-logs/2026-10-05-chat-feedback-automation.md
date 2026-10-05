# Bounded automatic feedback for Chat work

## Behavior and ownership

Feedback automation reuses the workspace's existing Feedback Reaper task,
summary lock, staged input package and completion commit. It does not create a
second task lifecycle. The monitor starts one tracked asynchronous check and
continues foreground scheduling without awaiting agent startup; shutdown cancels
and joins that check. A typed callback lets the Chat adapter defer new work while
a native Chat turn is active.

New installations start enabled with a process-start fresh-only watermark. Old
archives are not replayed automatically. Eligible new evidence consists of a
non-internal task archive with a review failure or repeated needs-input signal,
or an explicitly captured Chat correction. Internal reaper archives are excluded
from both automatic and manual selection to avoid self-summary loops. Empty
checks create no model session or summary run.

Each workspace persists its enable flag, minimum one-hour cooldown, last attempt
and result, and scan cursor. An automatic check launches at most one reaper and
defers while any existing reaper is running. Manually queued reapers retain manual
ownership. Failed launches consume the cooldown too; changing settings during an
awaited launch is preserved. Disabling prevents future launches and does not
interrupt an already started reaper.

## Bounds and source contracts

- Default automatic input: five records (configurable one to ten), with a 24,000
  character prompt ceiling. These limits are not token or billing caps.
- At most 1,000 fresh archive/correction files are read per automatic scan,
  at most 1 MiB plus one oversize sentinel byte each and 8 MiB in aggregate.
  Oversized or invalid records are skipped. A durable rotating cursor prevents
  hashed filenames or large archives from permanently hiding later evidence.
  Directory enumeration and mtime checks still cover the workspace's record
  filenames; file-content reading runs in a worker thread.
- Chat source discovery reads only the last 256 KiB of the persisted event log
  and returns at most twenty recent user-message references. Synthetic schedule
  and Goal turns, redacted events and machine metadata are excluded. An exact
  quote must match a returned persisted user message; one message produces one
  deduplicated correction record regardless of repeated calls or excerpts.
- Capture validates the Chat tab and workspace execution identity, including
  linked local Git worktrees or the exact remote profile and cwd. Lesson creation
  verifies correction records belong to the cited workspace. Source provenance
  does not certify an agent's interpretation of a correction.
- Evidence becomes processed only after successful existing reaper completion.
  Prompt-budget-dropped records remain eligible. Unrelated and empty queries
  return no lesson index instead of injecting popular but irrelevant lessons.

## API and CLI integration

The feedback router exposes automation status/configuration, compact relevant
context, recent user-message sources and exact correction capture. The `feedback`
CLI group mirrors those operations; `sources` and `capture` obtain the Chat tab
from `CLAUDE_HUB_TAB_ID` unless explicitly supplied. Router/CLI registration and
the native-Chat busy callback are integrated by the Chat workflow branch.

Capture should be invoked only for an explicit reusable user correction, not as
a transcript harvester. The correction quote remains evidence until the reaper
derives a scoped lesson with an applicable condition and observable check.

## Validation and limits

Focused tests cover fresh-only bootstrap, no-model empty checks, persisted
cooldown, failed-launch retry, active/manual ownership, native Chat busy gating,
settings changes during launch, tracked-task shutdown, scan fairness beyond
1,000 filenames, actual read bounds and oversized-file recovery, exact source
capture, API workspace validation, CLI environment selection, evidence staging,
confidence limits and irrelevant-context suppression. Existing feedback-store
and provenance tests remain green. The manual summary lifecycle checks retain
the prior strict requirement that every cited task has its own archive; one old
fixture now supplies its second cited task record.

Targeted mypy reports only the existing duplicate `sub_thread` declaration in
`services/agent_stream/codex_jsonl.py`. Live model behavior, long-running resource
cleanup and combined Chat/browser acceptance belong to integration validation;
this branch has not changed or restarted the main Hub.
