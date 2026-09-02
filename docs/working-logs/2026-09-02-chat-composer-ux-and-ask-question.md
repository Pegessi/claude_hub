# Chat composer UX and Cursor AskQuestion cards

Date: 2026-09-02

## Overview

Structured Chat composer gains Codex-style turn control (stop, queue, steer) and
Cursor `AskQuestion` turns render as interactive approval cards instead of
invisible tool JSON.

## Backend

- `POST .../stream/cancel` terminates the active native turn and publishes
  `turn_completed(status=cancelled)`.
- `POST .../stream/send` accepts `delivery=normal|steer`. Steer cancels an
  in-flight turn under the tailer send lock, then delivers the new message.
- Cursor adapter normalizes assistant `tool_use` from stream-json; `AskQuestion`
  also emits `approval_required` with parsed question/options.

## Frontend

- Composer: Stop, in-memory queue (Enter while busy), Cmd/Ctrl+Enter steer,
  Shift+Enter newline, IME-safe Enter, textarea autoresize (240px cap).
- Timeline: `approval_required`/`approval_resolved` → `approval` parts;
  `AskQuestion` tool rows are hidden when an approval card is shown.
- Selection submit sends JSON `ask_question_response` via composer (steer when
  a turn is still in flight).
- TDZ fix: composer refs (`turnInFlight`, `draftQueue`, …) must be declared
  before any `immediate` watcher; queue flush runs from a watcher registered
  after `flushDraftQueue`.

## Limits

- Cursor native uses one-shot `--print` subprocesses; AskQuestion answers are
  delivered as steer + follow-up message, not as a blocking tool result on the
  same process stdin. Raw terminal remains the escape hatch for edge cases.

## Validation

- Backend: agent stream native/cursor/API tests (133 passed in targeted run).
- Frontend: composer/question/timeline unit tests + `vue-tsc`.
