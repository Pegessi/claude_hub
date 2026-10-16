# Claude result ownership on resume

## Failure and cause

Two user inputs were marked completed without output within half a second. The
provider transcript continued for minutes and contained a final answer absent
from the Hub event store. The original stdout was not retained, so its exact
first result cannot be recovered retrospectively.

The installed Claude Code 2.1.277 reproduces the relevant protocol sequence.
After interrupting a session with two background tasks and resuming it with a
new input, stdout contains:

1. A successful, empty `result`, `num_turns: 0`,
   `origin: {"kind": "task-notification"}`.
2. The new user request's assistant response.
3. The user request's successful result, with its input UUID.

This matches the [documented result provenance and batching contract](https://code.claude.com/docs/en/agent-sdk/python#resultmessage).
The [SDK changelog](https://github.com/anthropics/claude-agent-sdk-typescript/blob/main/CHANGELOG.md)
records batched empty task-notification results in 0.3.274.

Previously every result cleared adapter state, completed the active Hub turn and
retired its stdout generation. The real answer following the first result was
then discarded. A subsequent user send could also terminate the still-working
one-shot process.

## Change boundary

`ClaudeNativeSession` gives each submitted input a fresh UUID. Before adapter
normalization, it admits a result with explicit singular/plural input references
only when their union includes that UUID. Without references, results explicitly
attributed to task notifications or peers cannot complete the pending user input.
Other records continue through the existing stream path.

Empty and zero-turn results are not inherently failures: local commands and owned
errors still complete normally. Unknown or absent provenance keeps the legacy
behavior. Emitters without both input correlation and known provenance remain
ambiguous. Generic completion retirement, cancellation, process generations,
timers and other providers are unchanged.

## Verification

- Baseline regression: the native-transport/tailer test completed or failed the
  user turn on a background result before its actual reply.
- Fixed regression: real `ClaudeNativeSession` with fake process streams preserves
  the answer, keeps the guard until the owned result and persists one completion.
- Ownership cases cover singular/plural references, merged references, background
  failures, local commands, owned errors and older/unclassified/unknown results.
- The two existing native/stream test modules pass all 330 cases.
- `scripts/verify.sh all` passes: documentation, formatting, backend types,
  3534 backend tests (7 skipped), frontend lint/types, 843 frontend tests and build.
  That run selected pnpm 12.5.1 instead of the declared 9.15.9; all four frontend
  targets were then rerun successfully with pnpm 9.15.9 and strict version checks.
- Independent review found no actionable issues and independently passed the
  16 new cases. The reviewed production/test file hashes match the final files.
- A separate experiment uses the installed CLI, an isolated HOME/config/state,
  and a deterministic loopback-only fake Anthropic API. No real model credentials
  or inference are used. With the candidate transport and tailer, the first
  background result is rejected and the correlated result accepted, producing
  `turn_started`, the actual `text_delta`, and one `turn_completed`.

The private experiment captures are kept outside Git. This change does not
rewrite the affected conversation, deploy a candidate or restart the primary Hub.
