# 2026-10-12 Goal steer, question answers, and resume

Tasks: `fcef1458` (Goal mode small issues) and `43736691` (resume after an
interactive question).

## Symptoms

1. While a Goal ran, the composer was disabled, so ⌘/Ctrl+Enter could not
   steer it. Codex lets the user interject without leaving the Goal.
2. After a Goal ended, ordinary replies in the same Chat still ended with the
   `<goal-checkpoint>` JSON and `<goal-status>`. Markdown hid the tags but
   rendered the JSON.
3. On Claude/Cursor, answering a question card paused the Goal and ran the
   answer as a long manual turn. Resume returned 409 while that turn ran, so the
   user had to Stop the turn and then Resume.

Reproduced on Chat `6729d0ac` (Cursor): the answer turn carried no Goal
metadata, so the tailer did not sanitize it, and the Goal stayed paused.

## Root causes

- The composer treated "Goal active" as "no input"; the backend only accepted a
  Codex/TraeX in-turn question answer while a Goal was active.
- Claude/Cursor have no in-turn answer channel. The fallback paused the Goal and
  sent a manual turn, and resume refused any running turn.
- The protocol sanitizer was enabled only for Goal continuation turns. The agent
  keeps echoing the protocol learned from earlier Goal prompts.

## Behavior now

- **Goal steer.** Three inputs become the next Goal turn and keep the Goal
  active: a ⌘/Ctrl+Enter (or Send button) steer, a Claude/Cursor question answer,
  and any text reply to a blocked Goal. The Hub records the text in
  `GoalRun.pending_input`, cancels the running Goal turn, and dispatches a
  continuation that tells the agent to address the message first. That turn
  reuses the composer's `client_turn_id`, shows the user's text as the visible
  message, and uses steer delivery. `pending_input` is cleared only after
  provider acceptance, so a failed dispatch keeps it (Goal paused + uncertain;
  the API returns 503 and the next resume carries it). Steering is idempotent
  per `client_turn_id`. Steer text is limited to the persisted 32,000-character
  `pending_input` bound; longer text gets 422 before any card or Goal changes,
  because an unvalidated value would make the Goal snapshot fail to load.
- **Codex/TraeX** keep answering questions in-turn first.
- **Enter during an active Goal** queues the draft locally; it flushes when the
  Goal pauses, blocks, or ends. Goal turns cannot carry images.
- **Answered cards** for Claude/Cursor are closed with `approval_resolved`
  before the Goal turn starts.
- **Deferred resume.** Resuming while an ordinary Chat turn runs no longer
  returns 409. The Goal becomes active with `resume_after_turn` and the status
  message "resumes when the current Chat turn finishes"; the next turn boundary
  dispatches it. Pause, complete, clear, and restart recovery clear the flag. The
  API re-checks the turn guard after storing the deferral to close the race with
  a turn that ended meanwhile.
- **Protocol hiding.** Ordinary turns in any Chat that has ever had a Goal pass
  `strip_goal_protocol` to the tailer, which hides control blocks from visible
  text. Only Goal continuation turns may synthesize a completion from a trailing
  `<goal-status>`; ordinary turns never end early. The frontend also hides a
  complete trailing envelope in history recorded before this change; mid-text
  examples and earlier unmatched `<goal-status>` openers stay visible.

## Validation

- Controller: steer replaces the running turn, replay does not resend, paused
  Goals reject steer, failed dispatch keeps the input, blocked Goals resume with
  the reply, deferred resume releases once at the next boundary.
- API: question answer, ⌘/Ctrl+Enter steer, and blocked reply routing; image and
  plain-Enter refusal; busy blocked reply 409; failed steer 503; deferred resume
  and its race re-check; `strip_goal_protocol` only after Goal history.
- Tailer: an ordinary turn with the flag hides the envelope and is not completed
  by Goal synthesis.
- Frontend: policy test for trailing-envelope stripping; wiring test for the
  steer/queue composer contract.
- Browser: `frontend/tests/browser_chat_goal_steer.py` against a task-owned
  Vite server with mocked APIs. With an active Goal turn in flight, the composer
  stays enabled and shows the steer hints, Enter queues without sending,
  ⌘+Enter sends `delivery: "steer"`, and a recorded envelope is hidden.
- Full backend suite: `test_service_restart.py::test_frontend_build_timeout_keeps_current_backend_and_dist`
  fails only under full-suite load on this macOS host (also on a branch without
  these changes), most likely because `killpg` returns EPERM during timeout
  cleanup and only `ProcessLookupError` is handled, so the outer `OSError`
  branch reports "could not start". It passes alone; the rest of the suite
  passes with it deselected. This launcher path is outside this change.
