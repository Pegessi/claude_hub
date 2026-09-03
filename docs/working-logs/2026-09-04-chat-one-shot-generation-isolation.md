# Chat one-shot stdout generation isolation

Date: 2026-09-04

## System overview

Top-level Claude and Cursor Chat sessions launch one provider subprocess per
turn. A long-lived `SessionTailer` consumes every subprocess through one
transport queue and assigns normalized records to the currently active
`turn_id` and `run_epoch`.

## Root cause

Cancelling a one-shot stdout reader deliberately queued `None` to wake the
tailer. The same untagged queue was reused by the replacement process. If the
old EOF was consumed after the tailer installed the next turn's identity, the
tailer synthesized `provider exited without a completion record` for the new
turn, cleared its identity, and then persisted the still-running provider's
real output with `turn_id=null`.

The same boundary existed after an authoritative provider completion: the
turn guard could be released while the one-shot process still held stdout
open, and starting the next turn cancelled that lingering reader and queued an
EOF indistinguishable from the new process's EOF.

## Module design

- `ProviderSession` assigns each Claude/Cursor one-shot reader a monotonically
  increasing stdout generation.
- Parsed records and EOF sentinels carry the generation that produced them.
- `read_line()` returns only records from the current generation and discards
  stale output from cancelled or completed readers.
- Cancellation invalidates the active generation before terminating its
  reader. A provider completion invalidates the generation before releasing
  the turn guard. Starting a new one-shot process advances the generation
  before terminating any lingering predecessor.
- Codex keeps its existing persistent JSON-RPC notification queue and fatal
  EOF semantics; it does not use the one-shot generation path.

## Key issues and pitfalls

- A bare EOF is not sufficient provenance when multiple process lifecycles
  share one consumer queue.
- Publishing a synthetic cancelled completion does not drain already-buffered
  output. Old records must be rejected by generation, not merely reordered.
- A guard-release test alone is insufficient. Regression coverage must start
  a second turn while the prior reader is still alive or being cancelled and
  prove the first delivered record belongs to the replacement process.

## Validation

- Red phase: both cancellation-to-next-turn and
  completion-with-lingering-process-to-next-turn tests read stale `None` on the
  unfixed implementation.
- Green phase: both regressions pass after generation isolation.
- Native transport and agent-stream suites: 110 passed.
- Full backend suite: 1263 passed, 13 skipped, 2 failed, 2 rerun. The two
  failures are unchanged runtime tests outside this code path: the real ttyd
  seven-tab recovery marker missed its five-second deadline, and the
  Playwright terminal replay input assertion missed its one-second deadline.
- `mypy claude_hub`: no issues in 91 source files.
- Black and isort pass for both changed Python files. The repository-wide
  Black check remains red on two unchanged Cursor transcript files.
