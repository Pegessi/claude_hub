# Chat thinking presentation

## Scope and status

Candidate prepared from source commit `525b84e` in a task-local source snapshot.
No primary-checkout files, shared service, provider configuration, or persisted
conversation events were changed. This is a frontend candidate, not a deployment.

## Display contract

- Consecutive adapter-authored reasoning updates render as one compact row.
  The labels are `Thinking…`, `Thought`, and `Thinking interrupted`.
- When that uninterrupted region contains plaintext thinking, the indicator is
  inside its expandable card; there is no second status row. The first source
  key remains stable through text deltas and completion snapshots, preserving
  an open card. Thinking remains plain text, never Markdown parsing.
- Text, tools, approvals, ordinary notices, errors, and child regions remain
  display boundaries. An interrupted opaque attempt stays distinct from a
  preceding successfully completed attempt.
- Parent and child lifecycles are independent. A trailing child region does not
  finish its parent's thinking, and parent completion does not stop a live child.
- Plaintext-only providers have no universal thinking-end event. Only the last
  uninterrupted thinking region is inferred active; explicit terminal/error
  evidence stops the animation. Providers with neither plaintext nor reasoning
  events retain the existing response-waiting behavior; no reasoning is invented.
- Active indicators use a small rotating ring. Completion is static. Reduced
  motion disables rotation and the card-chevron transition.

## Implementation

`thinkingPresentation.ts` is a pure display projection over the existing event
reducer output. The reducer now preserves `provider_status` on status parts;
matching labels alone never suppress an ordinary notice. Original events and
compatibility text/status aggregates remain unchanged.

Unchanged child projections are cached by thread identity, child render revision,
and part count. A parent instruction can append a part without a child event,
so count participates in invalidation. Tools remain shared objects; completion
updates are visible without copying. The main-turn projection remains a simple
linear scan. Independent measurement found approximately 0.1–0.2 ms per 13,500
parts on this host; incremental display mutation was not justified by that result.

## Verification

- Frontend Node unit suite: 898/898 passed; TypeScript, ESLint, and Vite build passed.
- Added reducer/projection tests for repeated statuses, stable card keys, metadata
  snapshots, interruption, boundaries, history folding, replay, and a 13,500-part
  sibling-cache case.
- Extended the existing full-app composer browser scenario: active/completed
  opaque reasoning, ten completed statuses compressed into one row, open
  plaintext card continuity, tool boundaries, child isolation, error/cancelled
  outcomes, reduced motion, mobile containment, and unobscured text.
- All eleven mocked browser scenarios passed. The final evidence directory is
  `chat-thinking-ui/browser-diag2-5308` in the task workspace; it includes
  plaintext active/completed and unobscured mobile screenshots. The owned Vite
  exited normally and its port was closed, without forced cleanup.
- Mobile visibility checks explicitly open child details before hit-testing
  their readable content and wait for the sidebar's closing transition.
- The first browser run caught a parent phase incorrectly finishing when a child
  region appeared. This was fixed and covered by a focused unit test. A child
  cancellation fixture was also corrected to use a real nonempty lifecycle
  status rather than one that the existing reducer intentionally ignores.
- Independent read-only review rechecked cache invalidation, lifecycle isolation,
  and keyed details after the fixes; no unresolved finding remained.

The repository-wide `verify.sh all` was attempted but could not complete: this
snapshot has no backend `.venv`, and `pnpm` is absent from PATH. Frontend checks
were run directly using the copied, lockfile-matched installed dependencies.
Browser verification used the repository's isolated owner script with an
existing Python 3.11.16 / Playwright 1.58.0 environment, Node 24.21.0, and Chromium.
No tool or browser installation and no live-provider call was performed.

The Vite bundle retains the existing chunk-size warning. Backend tests, terminal
HMR/replay, live-provider behavior, and production deployment are not covered by
this candidate's verification.
