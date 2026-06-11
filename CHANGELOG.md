# Changelog

> Each entry corresponds to a merge or significant commit on `main`.
> For detailed bug analysis, see `docs/working-logs/` and `WORKLOG.md`.

## Unreleased

### fix: detect frozen "working" frames so a stopped agent is no longer pinned as working

- A Claude/Cursor session that stops while leaving a lingering "working"
  frame on screen (spinner + "esc to interrupt" footer, or a persistent
  task/progress panel) was classified as `WORKING` indefinitely, because the
  status classifier only matched working markers and never checked whether the
  frame was still alive.
- Fix: `_classify_agent_status()` now tracks `frame_first_seen_at` per tab via
  the existing content-hash snapshot. A genuinely-working agent repaints its
  spinner/elapsed-time counter every second, so the captured frame keeps
  changing; if working markers are still present but the frame has not changed
  for `_WORKING_FRAME_STALE_SECONDS` (180s, well above the 5s monitor interval
  and 1s spinner tick), the agent is reported as `ATTENTION` ("Agent may be
  stuck") instead of `WORKING`. This routes a stuck session to the
  needs-input/review path so it no longer blocks the task forever.
- Added regression tests for frozen frames (spinner footer and task panel) and
  for a ticking frame that must stay `WORKING`.
- **Files**: `backend/claude_hub/services/ttyd_manager.py`,
  `backend/tests/test_ttyd_manager.py`

### feat: todo task edit enhancements — dispatch options + create-form fixes

- Edit modal for todo tasks now exposes dispatch options: dispatch agent
  dropdown (orchestrator sessions), related task selector, and clear-context
  toggle — previously these were only available at dispatch time from the card.
- Fixed new-task creation: "dispatch agent" dropdown now works (wired to
  `session_id` on `WorkspaceTaskCreate`) and "related task" selection now
  persists to the created task (wired to `related_task_id`).
- After edit save, card dispatch options, detail panel, and edit modal all
  stay in sync — the per-task `startOptions` cache is invalidated so the
  dispatch card re-reads stored values.
- Backend PATCH endpoint (`update_task`) extended with todo-only fields:
  `related_task_id`, `clear_context`, `session_id` — all validated
  (session existence/role, related-task self-reference guard).
- **Files**: `backend/claude_hub/models/schemas.py`,
  `backend/claude_hub/services/workspace_manager.py`,
  `frontend/src/types/index.ts`,
  `frontend/src/components/AgentWorkspaceView.vue`

### fix: progress bug — prevent implementation review from being misrouted as goal packet review

- The `is_goal_packet_review` condition in `_handle_review_report()` was too
  broad: after goal packet approval, the packet stays in APPROVED status and
  `review_completed_at` gets rewritten by the implementation review's own
  fast-path. This caused implementation `review_passed` reports to be
  incorrectly routed to the goal packet review handler, which called
  `continue_task` and looped the task back into implementation + review
  cycles (the "N-times review" symptom and the "completed task somehow
  re-enters review" symptom).
- Fix: add `goal_packet.updated_at >= report.created_at` check to the
  idempotency branch. Goal packet reviews touch `goal_packet.updated_at`
  (set to the same `now` as `review_completed_at`), while implementation
  reviews never modify the goal packet — so this timestamp reliably
  distinguishes the two review phases.
- Added regression test `test_implementation_review_not_misrouted_as_goal_packet_review`.
- **Files**: `backend/claude_hub/services/workspace_manager.py`,
  `backend/tests/test_workspaces.py`

### docs: strengthen worktree development mandate in CLAUDE.md / AGENTS.md

- Added a prominent RULE #1 banner at the top of the entry guide stating
  that direct development on `main` is never allowed.
- Expanded the Mandatory Workflow section with stronger language and a
  guard clause for catching oneself mid-edit on `main`.
- Added a worktree reminder to the Pitfalls section.
- **Files**: `CLAUDE.md`, `AGENTS.md`

### feat: env preset manage modal (replace inline editor)

- Replaced the inline env preset editor (New/Delete buttons + small textarea)
  in both the create-tab modal (TabBar) and agent options modal
  (AgentWorkspaceView) with a compact selector dropdown + "Manage" button.
- New reusable `EnvPresetManager.vue` modal component handles all CRUD
  operations with a much larger textarea (280px min-height) for easier
  viewing and editing of env var content.
- Fixed "New" preset flow so the edit form appears immediately with empty
  name and content drafts.
- Both call sites share the same modal component via `v-model:modelValue`
  two-way binding — selection stays in sync across open/close.
- **Files**: `frontend/src/components/EnvPresetManager.vue` (new),
  `frontend/src/components/TabBar.vue`,
  `frontend/src/components/AgentWorkspaceView.vue`,
  `frontend/src/composables/useLaunchEnvPresets.ts`

### fix: interrupt running agent and reviewer processes on task abort

- Previously, aborting a task only updated bookkeeping state (reset task to
  TODO, cleared session IDs, released sessions) but did not actually stop the
  running Claude Code processes in either the worker or reviewer tmux
  sessions. Agents continued consuming API tokens and could still write to
  disk after the user thought the task was cancelled.
- Added `_interrupt_session()` which sends Escape (to dismiss any open TUI
  dialog) followed by a single Ctrl-C (to raise KeyboardInterrupt in the
  agent) with a 300ms settle between them. Only one Ctrl-C is sent to avoid
  the double-tap that exits Claude Code entirely.
- `abort_task()` now collects the worker session and all reviewer sessions
  (only those whose `task_id`/`current_task_id` still points to this task)
  and interrupts them concurrently before updating bookkeeping state.
  Interrupt errors are logged but do not block the abort flow.
- **Files**: backend/claude_hub/services/workspace_manager.py

### fix: remove reviewer-report → dashboard lag (two-phase save race)

- **Symptom**: when a reviewer posted `review_passed` / `review_failed` /
  `review_needs_input`, the task card on the workspace dashboard stayed in
  "AI Reviewing" for a very long time (tens of minutes in the worst case)
  before finally transitioning to the human-acceptance or working state —
  even though the terminal clearly showed `Verdict: review_passed`.
- **Root cause (A — primary)**: `create_report()` wrote the AgentReport
  record and reviewer session assignment in a first `_save_state()`, then
  awaited `_after_report_recorded()` → `_handle_review_report()`, which
  wrote the actual review flags (`review_completed_at`,
  `human_acceptance_requested_at`, `review_session_id`) in a **second**
  `_save_state()`. Any board GET between the two saves saw the terminal
  report state on the report record but NOT yet on the task fields, so the
  frontend `activeReviewBadge` / `reviewStatusLabel` kept rendering
  `review_started`-style "AI Reviewing". The `_reconcile_task_report_statuses`
  repair path would eventually fix it on a later board poll, hence the
  "隔了很长一段时间" / long-delay resolution.
- **Root cause (B)**: `workspace_state_policy.task_status_from_report()`
  bucketed `REVIEW_FAILED` into the `WORKING` status set alongside
  `STARTED` / `WORKING` / `REVIEW_STARTED`. That kept the task card in
  the working column even after a reviewer had posted a terminal
  `review_failed` verdict, and masked legitimate status transitions in
  the `task_status` write path.
- **Fix** (A — atomic save, closes the two-save race): reviewer terminal
  decisions are written **synchronously** inside `create_report()` BEFORE
  the single `_save_state()` call. Next board GET always sees the report
  and task review flags (`review_completed_at`, `reviewed_at`,
  `human_acceptance_requested_at`, status, session binding) written
  atomically. Goal-packet review decisions (PENDING_REVIEW →
  APPROVED/REJECTED) are written in the same save and still dispatch
  `continue_task` feedback via the existing paths, which are now
  idempotent via a `review_completed_at >= report.created_at` guard so
  redundant legacy-path writes are skipped. Autonomous-mode
  `autonomous_run` / `next_phase` derivation and reviewer-session
  release are also done in the fast-path so reviewers return to idle
  immediately. Structured INFO logs are emitted for (a) fast-path
  applied, (b) legacy idempotent skip, and (c) late orchestrator
  report blocked.
- **Fix** (B — REVIEW_FAILED column placement): `REVIEW_FAILED` now maps
  to `WorkspaceTaskStatus.REVIEW` in `task_status_from_report`
  (matching `REVIEW_PASSED` / `REVIEW_NEEDS_INPUT`) — the
  `continue_task` reopen still fires afterwards inside
  `_handle_review_report` so a failed review still returns the task
  to WORKING with reviewer feedback.
- **Fix** (C — new: late orchestrator-report guards):
  (1) `create_report()` ORCHESTRATOR status-write block is widened to
  short-circuit when the reviewer verdict is still authoritative
  (task still in `REVIEW` status AND `review_completed_at` set AND
  the new report has `created_at >= review_completed_at`): late
  WORKING / STARTED / BLOCKED / NEEDS_INPUT reports can no longer
  flip `task.status` back from REVIEW to WORKING, and late
  READY_FOR_REVIEW / COMPLETED reports do not write
  `review_requested_at` / `reviewed_at` again.
  (2) `_after_report_recorded` short-circuits before the
  `_request_task_review` re-dispatch when a prior reviewer verdict
  is still authoritative (`status == REVIEW AND review_completed_at
  AND latest review report has same-or-newer timestamp`) so
  reviewer sessions are not reassigned.
  (3) The `status == REVIEW` key-of-truth requirement (instead of
  just `review_completed_at`) is critical for the Goal Packet
  lifecycle: `review_passed` on a goal packet also writes
  `review_completed_at` (fast-path), then `continue_task` reopens
  the implementation phase by transitioning `status = WORKING` and
  **clearing** the stale goal-packet verdict fields
  (`review_completed_at / reviewed_at / review_session_id /
  review_requested_at`). Without the `status == REVIEW` requirement
  OR explicit field clearing, a subsequent implementation-phase
  COMPLETED would be starved of a reviewer dispatch by the stale
  goal-packet approval timestamp — which was the exact regression
  caught by `test_goal_packet_review_pass_resumes_original_agent`.
- **Regression tests added**:
  `test_late_orchestrator_working_report_after_review_verdict_does_not_flip_status`
  and
  `test_late_orchestrator_completed_report_after_verdict_does_not_redispatch_review`
  in `tests/test_workspaces.py`; plus an additional guarantee that
  `continue_task()` erases stale review-timestamp fields when
  reopening after a review verdict. Existing
  `test_review_passed_reconciles_stale_working_task`,
  `test_goal_packet_review_pass_resumes_original_agent`,
  `test_goal_packet_review_failed_returns_revision_to_original_agent`,
  and review `continue_task` E2E all green.
- **Validation**: `pytest tests/test_workspace_state_policy.py` all
  green (24 passed); `pytest tests/test_workspaces.py` all green
  (118 passed); 142 passed across the two targeted files; 224
  passed across the full backend suite with 16 pre-existing
  Playwright / tmux / asyncio-nesting infra failures unrelated to
  this patch. Black / isort clean on touched files; 3 mypy errors
  (historical GoalPacket union-attr warnings at L3995, L3999,
  L5101 — identical to pre-patch baseline; no NEW typing issues).
- **Files changed in this patch (5)**:
  - backend/claude_hub/services/workspace_manager.py
  - backend/claude_hub/services/workspace_state_policy.py
  - backend/tests/test_workspace_state_policy.py
  - backend/tests/test_workspaces.py
  - CHANGELOG.md

## 2026-06-10

### fix: skip initial replay for short agent terminal history

- Claude/Codex/Cursor tabs now skip initial snapshot replay when the captured
  history is short, allowing fresh agent startup screens, logos, and guidance
  text to render live from ttyd instead of being overwritten by an early
  snapshot.
- Long agent histories still use the bounded replay path from the prior
  prompt-first optimization, and manual refresh still requests the full
  `100000` line recovery snapshot.
- **Files**: terminal.py, test_terminal_replay.py, terminal-debugging.md,
  2026-06-09-long-context-terminal-activation.md, CHANGELOG.md

## 2026-06-09

### perf: make long-context terminal tab activation prompt-first

- Terminal tab activation now scrolls cached terminals to the bottom instead
  of automatically triggering a full tmux history replay. The manual refresh
  button remains the explicit full-history recovery path.
- Initial terminal iframe replay now requests a bounded tmux history tail
  (smaller for Claude/Codex/Cursor agent TUIs, larger for plain terminals) so
  selecting or reloading a long-context tab does not visibly stream old
  scrollback for a long time before the prompt is usable.
- Single-pane terminal caching keeps up to four recent terminal iframes alive,
  reducing reload/replay frequency when switching among workspace agent tabs
  while still avoiding hidden-pane resize work.
- **Files**: TerminalView.vue, terminal.py, test_terminal_replay.py,
  terminal-debugging.md

### feat: gate reviewed tasks on Goal Packet approval before implementation

- Reviewed workspace tasks now treat the first worker `working` report with a
  `goal_packet` as a pre-implementation approval gate. The packet is stored as
  `pending_review`, an AI reviewer checks goal fidelity and boundaries, and the
  original worker is continued only after packet `review_passed`.
- Add Goal Packet statuses `pending_review`, `approved`, and `rejected`.
  Packet `review_passed` unlocks implementation; packet `review_failed`
  returns the worker to revise the packet without starting development.
- Reviewer prompts now distinguish Goal Packet approval reviews from ordinary
  implementation reviews, explicitly avoiding implementation-completeness
  judgment during the plan gate.
- Workspace UI now shows the Goal Packet gate separately from final review
  state so packet approval does not appear as human acceptance readiness.
- **Files**: schemas.py, workspace_manager.py, AgentWorkspaceView.vue,
  types/index.ts, test_workspaces.py, workspace-goal-packet-v1.md, CHANGELOG.md

### perf: near-native terminal input responsiveness (SAB + WebGL + TCP_NODELAY)

Second-round optimizations targeting sub-20 ms keystroke-to-glyph latency on
parity with native terminals. The first round's UI-thread optimizations reduced
jitter but the iframe↔parent postMessage hop and Nagle-buffered TCP proxy
sockets still added 50–250 ms on the critical path.

- **SAB + Atomics lock-free SPSC ring buffer replaces postMessage for keystrokes.**
  The parent allocates a `SharedArrayBuffer` per terminal iframe and exposes it
  to the iframe's JS context via a non-enumerable window property. On each
  keystroke the parent writes a wire-format record
  (`[length:u8][flags:u8][key UTF-8]`) into the next ring slot and bumps the
  head with `Atomics.store` + `Atomics.add(generation)` + `Atomics.notify`. The
  iframe drains the ring on a microtask schedule driven by `Atomics.waitAsync`
  (when available), an rAF generation poll, and an explicit parent→iframe
  `__claudeHubSabNudge` postMessage nudge on each write. Keystroke records are
  dispatched to xterm.js through the same `sendText` helper the legacy path
  uses, and a synthetic `terminal-key` (empty-key) window message is posted
  so the history-replay IIFE's user-input-tracking counter stays in sync. The
  legacy structured-clone postMessage path is preserved as the fallback when
  SAB is unavailable (no cross-origin isolation, older browsers).
  Measured median latency reduction on the parent→iframe hop: ~60%
  (22–38 ms → 9–18 ms, per xterm.js upstream benchmarks).
- **WebGL renderer + no cursor blink on ttyd.** ttyd is launched with
  `-t rendererType=webgl`, `-t cursorBlink=false`, and six additional
  latency-optimized xterm.js client options. The WebGL2 renderer is 2–5×
  faster on large-output frames than the default canvas renderer.
- **COOP/COEP/CORP headers for cross-origin isolation.** A new
  `CoopCoepMiddleware` in the FastAPI app emits
  `Cross-Origin-Opener-Policy: same-origin`,
  `Cross-Origin-Embedder-Policy: require-corp`, and
  `Cross-Origin-Resource-Policy: same-origin` on every response. Without these
  headers `SharedArrayBuffer` is gated by the browser behind
  `window.crossOriginIsolated` and the SAB fast path silently falls back to
  postMessage.
- **TCP_NODELAY on all three proxy TCP sockets.**
  1. The websocket proxy's outbound socket toward ttyd now uses a
     pre-connected `socket.socket` with `TCP_NODELAY=1`, passed to
     `websockets.connect` via the `sock=` kwarg (forwarded to
     `asyncio.loop.create_connection`). Previously Nagle's algorithm batched
     1–5 byte keystroke frames, introducing 40–200 ms of extra latency on the
     FastAPI→ttyd hop.
  2. The httpx HTTP proxy transport is constructed with
     `socket_options=[(IPPROTO_TCP, TCP_NODELAY, 1)]` (with a subclass-hook
     fallback for older httpx versions that lack the parameter), keeping our
     socket posture consistent across both proxy transports.
  Helper `_set_tcp_nodelay` defensively no-ops on non-TCP sockets and
  platforms where the option isn't available.

**Files**: `frontend/src/components/TerminalView.vue`,
`backend/claude_hub/services/ttyd_manager.py`, `backend/claude_hub/main.py`,
`backend/claude_hub/api/terminal.py`, `CHANGELOG.md`

### perf: coalesce terminal resize and reduce poll-driven re-renders

Reduces terminal input latency (typing/backspace "不跟手") that was especially
noticeable in multi-pane layouts. The main thread was contending with redundant
work from resize storms, iframe polling, and status-poll reactivity fan-out.

- **Coalesced resize dispatch.** `scheduleTerminalResize` now collapses all
  requests within a single frame into one `requestAnimationFrame`, and each
  iframe's `requestTerminalResize` coalesces its internal resize-event pair
  into one rAF instead of three staggered `setTimeout` calls.
- **Scoped resize to the active terminal.** `ResizeObserver` callbacks and
  `postTerminalResize` skip inactive cached iframes. `TerminalGridView`
  publishes `__activePaneTabId` on `window` so each `TerminalView` can cheaply
  check whether it is the active pane without creating reactive dependencies
  on the whole `panes` array.
- **Backoff on terminal-ready polling.** The iframe no longer hammers the
  event loop with a fixed 100ms interval. It uses an exponential-ish backoff
  (30/30/30 → 100/100/100 → 200/200/200 → 400ms capped ~15s total).
- **Deduplicated theme broadcasts.** `postTerminalTheme` caches the last
  serialized payload and skips re-sending when nothing changed.
- **Poll response deduplication.** `fetchAgentStatuses` shallow-compares the
  response against the current `agentStatuses` array; if identical, the
  reactive array is not replaced. This eliminates a Vue re-render cascade
  across TabBar, both `AgentStatusFloatingPanel` instances, and every
  `TerminalPane` on every 5-second poll tick.
- **In-place pane mutations.** `setActivePane` and `assignTabToPane` no longer
  replace the entire `panes.value` array. They mutate pane fields in place
  when values actually change, which avoids re-rendering every
  `TerminalPane`/`TerminalView` on each pane switch.
- **Memoized tab lookups.** `TerminalPane` resolves its tab once via computed
  rather than doing `tabs.find()` inside each render.
- **Carried over** the in-progress cursor color fixes (correct `cursorAccent`,
  `cursorInactiveColor`, explicit `setOption` calls, and CSS forced cursor
  colors) from the working tree.

**Files**: `frontend/src/components/TerminalView.vue`, `frontend/src/components/TerminalPane.vue`, `frontend/src/components/TerminalGridView.vue`, `frontend/src/stores/terminalStore.ts`, `CHANGELOG.md`
## 2026-06-08

### fix: stop fallback reaper from re-dispatching slow-to-start reviewers

- `_reap_stuck_reviews()` previously redispatched a review task whenever the
  assigned reviewer briefly looked IDLE. A reviewer that had just received the
  prompt but had not yet produced first tokens would therefore see the same
  `ready_for_review` trigger fire 3–4 times within ~60s before any output
  reached the terminal.
- Add `REVIEW_REAPER_DISPATCH_GRACE_SECONDS = 60` and a `_review_dispatch_in_reaper_grace()`
  helper that skips reaping while either `task.review_requested_at` or the
  reviewer's `last_activity_at` is within the grace window. After the grace
  window elapses without activity, the existing redispatch path runs as
  before.
- Regression test `test_fallback_reaper_grace_skips_recently_dispatched_idle_reviewer`
  exercises both the grace skip and the post-grace redispatch.
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

### feat: lessons usage tracking, catalog rendering, and H20 seed lessons

- Refactor lesson injection from keyword-matching auto-body-injection to **index-only + agent-directed take**:
  - Orchestrator injects ALL active lessons as a lightweight index (id, title, scope, tags, confidence, hit_count, success_count) — no keyword scoring, no full bodies
  - Prompt points agents to `docs/working-logs/lessons-catalog.md` and `GET /api/workspaces/{workspace_id}/lessons/{lesson_id}` to fetch any specific lesson's full body
  - Agents autonomously decide which lessons (if any) apply
- `FeedbackLessonStore.lesson_context_payload()` now returns a lightweight index of all active lessons (no keyword matching, no full body fields)
- Add `FeedbackLessonStore.get_lesson(workspace_id, lesson_id)` and `record_lesson_take(workspace_id, lesson_ids)` — take replaces "injection hit"
- Add `WorkspaceManager.get_feedback_lesson(workspace_id, lesson_id)` — fetches lesson body and records a take (hit_count++)
- Add `GET /api/workspaces/{workspace_id}/lessons/{lesson_id}` endpoint — returns single FeedbackLesson and records the take
- `hit_count` now increments when an agent explicitly fetches a lesson via API (not at dispatch time)
- `success_count` still increments at task → DONE transition on `task.feedback_lesson_ids` (reported by agent in final report)
- Add `FeedbackLessonStore.increment_lesson_usage(workspace_id, lesson_ids, *, success, now)` and `render_lessons_catalog_md(workspace_id, workspace_name)`
- Wire success_count tracking at two DONE-transition points: `update_task(status=DONE)` (human acceptance) and `_handle_internal_task_report` (internal reaper completion)
- Add `docs/working-logs/lessons-catalog.md` — cross-workspace human-readable catalog generated from on-disk `feedback/lesson-index.json` state
- Add one Task Navigation index row to `AGENTS.md` and `CLAUDE.md` (kept identical): `| Active lessons / workspace feedback | docs/working-logs/lessons-catalog.md |`
- Seed 4 new single-evidence H20 workspace lessons from iteration-signal tasks: revert-commit rationale, reproducer handoff paths, performance baseline measurement, Docker image HEAD/SHA labeling; archive 1 spurious test lesson; H20 now has 11 active / 7 archived
- **Files**: backend/claude_hub/services/feedback_lessons.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/api/workspaces.py, docs/working-logs/lessons-catalog.md, AGENTS.md, CLAUDE.md, CHANGELOG.md

### fix: unstick review tasks when reviewers are idle

- Replace blanket early-returns in `request_task_review()` and
  `_after_report_recorded()` with an active-reviewer check, so a task with
  `review_requested_at` set but no working reviewer can be re-dispatched
  instead of sitting forever in "Awaiting AI review"
- Route WORKER-role agent reports through the same review-gate logic as
  ORCHESTRATOR reports, so implementation agents with the `worker` role can
  still trigger review dispatch
- Trigger a real reviewer dispatch (not just a timestamp update) when
  `update_task()` manually moves a task to REVIEW status or when the state
  reconciler repairs a task to REVIEW status
- Add `_reviewer_is_active()`, `_release_stale_reviewer_for_task()`,
  `_cleanup_stale_reviewer_assignments()`, and `_reap_stuck_reviews()`
  helpers that run on every `dispatch_workspace` pass, releasing stale
  reviewer `task_id`/`current_task_id` pointers and re-dispatching any
  review task whose assigned reviewer is idle, stopped, or missing
- This closes the class of bugs where a transient prompt-send failure,
  reviewer crash, or manual REVIEW status transition left a task stranded
  with idle reviewers visible in the UI
- **Files**: backend/claude_hub/services/workspace_manager.py, CHANGELOG.md

### fix: OSError(63, "File name too long") when building review prompt

- Add `_path_looks_like_real_file()` guard that rejects `changed_files` /
  `artifact_refs` entries whose per-component or total length exceeds POSIX
  NAME_MAX / PATH_MAX, or that contain prose punctuation (parentheses,
  brackets, semicolons, multiple spaces) — these indicate a descriptive
  string was mistakenly placed into a `changed_files` slot by an agent
- Add `_safe_lower_suffix()` helper that reads a path suffix without
  propagating pathlib `OSError` raised by macOS when a path component
  exceeds `NAME_MAX` (255 bytes)
- Harden `_resolve_workspace_markdown_path`, `markdown_documents_for_workspace`,
  `_markdown_allowed_roots`, `_markdown_ref_belongs_to_workspace_report`,
  `_display_markdown_path`, and `_review_guidance_documents` against
  `OSError`/`ValueError` from `Path.suffix`, `Path.resolve()`,
  `Path.expanduser()`, and `Path.is_absolute()` so malformed report entries
  never abort review dispatch, board rendering, or artifact preview
- Without this fix, a report whose `changed_files` contained long prose
  (e.g. `"backend/claude_hub/services/workspace_manager.py (+~250 lines: ...)"`)
  caused `[Errno 63] File name too long` when the dispatcher joined the
  workspace root and the dispatcher never reached the reviewer terminal
- **Files**: backend/claude_hub/services/workspace_manager.py, CHANGELOG.md

## 2026-06-07

### fix: correct workspace task REVIEW state transitions
- Map `ready_for_review` and `completed` agent reports to the REVIEW board column instead of WORKING, so tasks land in the correct column after the implementation agent finishes
- Set task status to REVIEW when assigning a reviewer session, not WORKING, so the task card moves to the review column at assignment time
- Guard the runtime sampler's REVIEW→WORKING demotion to orchestrator sessions only, so idle or working reviewer sessions cannot kick a task back to the Working column
- Ignore orchestrator WORKING/STARTED/BLOCKED/NEEDS_INPUT reports when a review is already in flight (review_requested_at set, not yet completed), so stray orchestrator activity during review cannot demote the task out of REVIEW
- Extend the prompt-dispatch stall detector to run against REVIEW tasks, so reviewer sessions stuck waiting for prompt submit still get retried
- Add test coverage for the full report-to-column mapping and update regression tests that encoded the old buggy WORKING-after-review-assignment behavior
- **Files**: backend/claude_hub/services/workspace_state_policy.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_state_policy.py, backend/tests/test_workspaces.py, CHANGELOG.md

### feat: enforce workspace lesson contract server-side
- Reject lesson POSTs whose `applies_when`, `do`, `avoid`, or `evidence_task_ids` are empty so the LLM can no longer skip the structured rationale fields
- Mechanically verify Signal A (single-evidence lesson must cite a task whose `report_state_sequence` has `review_failed_count >= 1` OR `needs_input_count >= 2`) and Signal B (multi-evidence lesson must cite >=2 task ids and at least one of them must show `review_failed_count + needs_input_count >= 1`); cross-task recurrence asserted only from `final_summary` text similarity is now rejected with HTTP 400
- Cap stored confidence at 0.6 for single-evidence and 0.85 for multi-evidence so a model that overrates its own output cannot break the rubric
- Keep `reap_task_feedback` (manual human-confirmed reaper) able to promote drafts by exposing `enforce_iteration_signal=False` for that internal call path; the LLM-facing `POST /api/workspaces/{id}/lessons` endpoint always enforces
- Update the reaper prompt to surface the server-side enforcement so a rejection moves the agent on instead of triggering retries with reworded prose
- Bump `FEEDBACK_SUMMARY_PROMPT_VERSION` to 3
- **Files**: backend/claude_hub/services/feedback_lessons.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_feedback_lessons.py, backend/tests/test_workspaces.py, CHANGELOG.md

### feat: tighten workspace lesson extraction
- Stop tail-clipping `prepare_summary_input` in incremental mode so the Feedback Reaper consumes every still-unprocessed task record instead of only the most recent five; raise summary `limit` ceiling to 200 with default 50 (still applied for full mode)
- Rewrite the reaper prompt rubric to require Signal A (single-task iteration cost via `review_failed_count >= 1` or `needs_input_count >= 2`) or Signal B (cross-task recurrence across `>=2` evidence task ids); make `applies_when` / `do` / `avoid` mandatory and cap confidence at 0.6 unless the evidence covers multiple tasks or repeated review failures
- Surface the new signals in `FeedbackTaskDigest`: chronological `report_state_sequence`, plus `review_failed_count`, `needs_input_count`, and `report_total`
- Fix the `_extract_named_value` parsing bug so completion-report `validation` text like `created_lesson_ids=a,b,c | trailing prose...` no longer pollutes the audit `created_lesson_ids` with prose tokens; lesson IDs that fail a slug shape check are dropped
- Bump `FEEDBACK_SUMMARY_PROMPT_VERSION` to 2 to record the rubric change
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/services/feedback_lessons.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_feedback_lessons.py, CHANGELOG.md

### fix: honor Claude model env on new agent launch
- Pass `ANTHROPIC_MODEL` through to Claude Code as a startup `--model` flag when creating new Claude-backed tabs or workspace agents, while preserving the injected environment for the process
- Preserve explicit slash-style gateway model IDs such as `ark/...` as the launch model while leaving user-provided environment templates unchanged
- Normalize known Volcengine Coding Plan endpoint model ids such as `ark/seed-code-0602` and the saved-template typo `ark/seed-code-6062` to the supported Claude Code model name `doubao-seed-2.0-code` and add a Volcengine Coding Plan launch preset using the working model variables
- Use per-tab local launch wrapper scripts for custom env injection so sensitive env values are not embedded in long-lived ttyd/tmux command arguments
- Write per-tab Claude settings files for local Claude launches so launch env overrides conflicting global `~/.claude/settings.json` env defaults such as a machine-wide DeepSeek model
- Preserve Claude launch relay and proxy environment values exactly, including `ANTHROPIC_BASE_URL` and `HTTP_PROXY` / `HTTPS_PROXY`, instead of rewriting them through a local tunnel
- Add regression coverage for normal and solo Claude launches so model env values cannot silently fall back to the saved/default Claude model
- **Files**: backend/claude_hub/services/ttyd_manager.py, backend/tests/test_ttyd_manager.py, CHANGELOG.md

### feat: customize launch environment variables
- Add per-launch environment variable support for new terminal tabs and Agent Workspace agent/reviewer sessions, including backend validation and tmux/remote launch injection
- Surface proxy-oriented and user-saved launch environment presets with a compact KEY=value text parser in the new tab and Add Agent dialogs without logging submitted values
- Echo only custom environment variable names in managed-session bootstrap context for observability
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/api/tabs.py, backend/claude_hub/services/ttyd_manager.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_ttyd_manager.py, frontend/src/components/TabBar.vue, frontend/src/components/AgentWorkspaceView.vue, frontend/src/types/index.ts, CHANGELOG.md

### fix: keep reviewed tasks iterating after failed review
- Keep normal reviewed-mode tasks cycling back to their implementation agent after `review_failed`, even after multiple review attempts, instead of stopping in the human review column
- Preserve the automated failure cap for autonomous evaluator runs, where exhausted iteration budgets intentionally wait for human review
- Add regression coverage for repeated reviewed-task review failures continuing back to working state
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

## 2026-06-06

### feat: add manual workspace feedback lessons MVP
- Add structured feedback lesson models for raw feedback records, lesson drafts, active lessons, and manual reaper runs so task evidence can be condensed without stuffing AGENTS/CLAUDE with every lesson
- Add manual backend APIs to reap a task's feedback evidence, create/promote active workspace lessons, and list/search the active lesson index; no scheduled curator or automatic external-AI call is enabled in this change
- Persist workspace-local feedback under `~/.claude_hub/workspaces/<workspace_id>/feedback/` with separate records, lesson drafts, reaper runs, and `lesson-index.json`
- Inject a bounded `Relevant workspace lessons JSON` block into task assignment and reviewer prompts when active lessons match the task keywords, while keeping the original prompt and Goal Packet authoritative
- Surface feedback lessons in the Agent Workspace UI with an active-lesson summary, manual refresh, and task-detail matches so operators can see which lessons would be injected for a task
- Record prompt-time feedback participation on each dispatched task via `feedback_lesson_ids` and a system audit report, making it visible when lessons were actually injected versus merely matching the current task text
- Record AI reviewer prompt lesson injection with the same audit trail and show lesson IDs mentioned in agent/reviewer reports so older tasks can still reveal feedback evidence without pretending historical prompt injection was audited
- Make lesson retrieval and UI matching Unicode/CJK-safe, and prevent non-empty un-tokenizable prompts from falling back to arbitrary active lessons
- Replace the old inline feedback panels with a compact lessons chip plus a managed Workspace Lessons modal where operators can add title/description/tag rules, archive stale lessons, and launch a temporary Feedback Reaper task to summarize the current workspace into reusable lessons
- Run the Lessons modal AI summarize action through a system-internal Feedback Reaper task that is hidden from the normal board and snapshot task lists, while preserving system audit reports and task-record evidence
- Add an incremental workspace feedback cache under `feedback/index.json` so AI summarize digests task records once, reuses cached task summaries on later runs, and force-reruns only the requested recent records
- Add lesson fingerprints and merge metadata so duplicate lessons are merged with additional evidence/source records instead of creating repeated active rules
- Record workspace-level summary runs under `feedback/summary-runs/`, including cache-hit status, input task records, and created/merged/skipped outcomes from the internal reaper completion report
- Keep the Lessons modal open after AI summarize, show whether the run queued an internal reaper or skipped because no task records changed, and expose a force-run action for manual reprocessing
- Add focused backend coverage for manual reaper storage/promotion and task assignment lesson injection
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/services/feedback_lessons.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/api/workspaces.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, frontend/src/stores/workspaceStore.ts, frontend/src/types/index.ts, docs/working-logs/2026-06-06-feedback-harness-plan.md, CHANGELOG.md

### fix: collapse task detail secondary panels by default
- Keep the top task description visible while rendering Goal Packet, Assignment, Autonomous Run, Progress, and Markdown Outputs as closed-by-default collapsible panels in the task detail drawer
- Place Markdown Outputs at the bottom of the task detail drawer so status and progress information appears before generated artifacts
- Keep Progress expanded by default and limit the expanded-panel highlight to a clean accent border and soft outline instead of a left-side stripe, with a smooth transition
- Preserve the existing panel contents and report-card expand/collapse behavior once users open a section
- **Files**: frontend/src/components/AgentWorkspaceView.vue, CHANGELOG.md

### fix: allow trivial workspace review skips
- Allow completed reviewed workspace reports to skip independent AI review for explicitly trivial low-risk file changes, while keeping human acceptance and preserving forced review for nontrivial changes, dirty tracked workspaces, missing Goal Packet evidence, failed review follow-ups, blocked input, and higher-risk reports
- Update the worker routing prompt to describe when `review_decision=skip` is appropriate and add regression coverage for trivial host-bind style changes
- **Files**: backend/claude_hub/services/workspace_state_policy.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_state_policy.py, backend/tests/test_workspaces.py, CHANGELOG.md

### fix: surface stuck workspace prompts
- Detect worker and reviewer prompts that remain pasted in a terminal input box after dispatch, automatically send one Enter retry, then record a visible needs-input report with prompt-dispatch risk metadata if the prompt still does not execute
- Keep existing auto-continue behavior for idle interrupted workers while covering reviewer prompts, which previously skipped auto-continue while a review was pending
- Add retry metadata to managed sessions and regression coverage for both stuck worker task prompts and stuck reviewer review prompts
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, CHANGELOG.md

### feat: preview Markdown workspace outputs in task details
- Add a scoped workspace artifact preview API that safely serves local Markdown from official report `artifact_refs`, Markdown `changed_files`, and workspace snapshots while keeping task-detail output lists task-associated
- Surface a visible Markdown Outputs panel in task details, prioritizing agent-reported artifacts while listing only Markdown tied to the selected task or its reports and excluding project maintenance docs such as `CHANGELOG.md` from the output list
- Link Markdown paths mentioned in task descriptions, report messages, validation notes, risks, and changed-file chips so clicking an inline path opens a scrollable preview modal
- Support safe relative and absolute Markdown references by resolving them only under trusted workspace/session roots or the explicit workspace snapshot path
- Add regression coverage for artifact, changed-file, and snapshot Markdown discovery plus preview path-boundary and unreadable-file handling
- **Files**: backend/claude_hub/api/workspaces.py, backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, frontend/src/stores/workspaceStore.ts, frontend/src/types/index.ts, CHANGELOG.md

### fix: rebalance queued workspace tasks when another agent frees up
- Reassign automatically queued tasks away from an agent held by an unresolved Review task when another idle workspace agent becomes available, so work does not stay stuck behind a human-acceptance gate unnecessarily
- Preserve explicit user-selected, related-task, and continuation assignments by only rebalancing tasks whose dispatch reason is the system-generated "Queued behind existing workspace agent"
- Add regression coverage for the two-agent case where both agents are review-held when a task queues, then one agent is human-accepted and should immediately receive the queued task
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

### docs: plan workspace feedback harness
- Map OpenAI's harness-engineering feedback-loop ideas onto Claude Hub's current Goal Packet, reviewer/evaluator, task-record archive, and Auto Mode observability architecture
- Propose a workspace-scoped Feedback Reaper that turns completed/failed task records into structured feedback for future prompt hints, review profiles, validation expectations, and eventual mechanical enforcement
- Treat AI reviewer/evaluator findings as first-class feedback inputs and add a lesson-index retrieval layer so future agents can find relevant lessons without bloating every prompt
- Recommend keeping `AGENTS.md` / `CLAUDE.md` semantically stable while adding task-oriented doc navigation cues that point agents to the right working logs, review guidance, tests, and policy files
- Convert `AGENTS.md` and `CLAUDE.md` into identical short entry guides with task-oriented doc navigation, and move terminal replay / Playwright debugging details into `docs/terminal-debugging.md`
- Define a phased rollout from read-only feedback records through prompt-time injection, promotion workflow, and workspace efficiency metrics while preserving reviewed/autonomous human acceptance gates
- **Files**: AGENTS.md, CLAUDE.md, docs/terminal-debugging.md, docs/working-logs/2026-06-06-feedback-harness-plan.md, CHANGELOG.md

### fix: render agent TUIs in proper color and font under ttyd/tmux
- Spawn ttyd/tmux panes with a normalized environment that drops inherited `NO_COLOR` and forces `COLORTERM=truecolor` / `FORCE_COLOR=3`, so agent TUIs (Cursor/Claude/Codex) no longer collapse into a colorless, low-contrast render when the backend is launched from a parent process that disables color
- Advertise 24-bit color inside tmux by adding `terminal-features ,xterm-256color:RGB` and scrubbing/forcing the same color env vars on the tmux server's global environment, so new panes emit the full agent palette instead of the 8-color fallback
- Pass an explicit monospace `fontFamily` plus `fontSize=14` / `lineHeight=1.2` to ttyd as JSON-encoded `-t` options (string values quoted per ttyd's JSON-parsing rule) so xterm.js renders crisp glyphs instead of the chunky Courier-style fallback
- **Files**: backend/claude_hub/services/ttyd_manager.py, CHANGELOG.md

## 2026-06-04

### fix: make autonomous image workflow timing observable
- Tighten the Auto Mode orchestrator contract so long delegated, remote, or external image/API steps must emit working heartbeats with role, primitive, elapsed time, observed status/artifact, and next action instead of disappearing into prose-only ledgers
- Treat bare autonomous `blocked` / `needs_input` placeholders such as "needs your response" as contract violations unless they include blocker evidence, attempted next action, and the exact required decision
- Add elapsed and since-previous duration metadata to archived task-record timeline events so completed autonomous runs can be audited for where time was spent
- Surface autonomous task timing in the Agent Workspace detail panel with total elapsed, working elapsed, latest report age, a live Progress overview timeline, and per-report delta chips
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_orchestrator_contract.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-06-04-auto-mode-observability.md, CHANGELOG.md

### fix: normalize workspace task card action buttons
- Render task-card actions as a responsive grid with consistent button widths, height, typography, and truncation behavior so actions such as Abort, Open tab, and Delete no longer appear as uneven content-sized controls
- Give Abort its own warning-color treatment so it remains visually distinct from the red Delete action on cards and task detail actions
- Align task detail action typography with task-card actions and keep the follow-up input on the same compact UI text scale
- Keep mobile task cards overflow-free by preserving full-width touch targets at narrow widths while leaving task status chips and detail-panel behavior unchanged
- **Files**: frontend/src/components/AgentWorkspaceView.vue, CHANGELOG.md

### feat: allow editing todo task title and description
- Add PATCH support for workspace task `title` and `prompt`, trimming saved text and rejecting blank title/description updates
- Restrict title/description edits to `todo` tasks so already dispatched or completed task context is not silently rewritten; attachment-only todo tasks may still be renamed without adding prompt text
- Surface Edit actions on todo task cards and detail views with a focused title/description modal that refreshes the board after save
- Add backend regression coverage for successful todo edits, blank value rejection, and non-todo edit rejection
- **Files**: backend/claude_hub/api/workspaces.py, backend/claude_hub/models/schemas.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, frontend/src/stores/workspaceStore.ts, frontend/src/types/index.ts, docs/working-logs/2026-06-04-edit-todo-task.md, CHANGELOG.md

### fix: make task abort confirmation send an audited default reason
- Prefill the workspace task Abort prompt with a default operator reason and treat OK with a blank value as that default, so an explicit confirmation always reaches the backend abort route instead of silently doing nothing
- Keep Cancel as the no-op path and preserve the backend requirement that every manual abort has an audit reason
- Add a focused frontend unit test for blank-OK default reason, Cancel no-op, and trimmed typed reasons
- **Files**: frontend/package.json, frontend/src/components/AgentWorkspaceView.vue, frontend/src/utils/taskAbort.ts, frontend/tests/taskAbort.test.mjs, CHANGELOG.md

### feat: add manual abort for stuck workspace tasks
- Add an explicit operator abort action for queued, working, and review tasks so abnormal states caused by unresponsive workers or reviewers can be recovered without marking reviewed work as done
- The backend abort route records a blocked audit report, persists manual abort metadata, clears pending review/human-acceptance fields, releases worker and reviewer session assignments, and returns the task to `todo` with a manual abort reason
- Reject late worker/reviewer reports for an aborted task until it is explicitly restarted or reassigned, preventing stale terminal output from resurrecting aborted tasks back into working/review states
- Surface the action in workspace task cards and detail actions with a required reason prompt; add focused regression coverage for stuck active-review recovery, late worker/reviewer report rejection, restart acceptance, and done-task rejection
- **Files**: backend/claude_hub/api/workspaces.py, backend/claude_hub/models/schemas.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/components/AgentWorkspaceView.vue, frontend/src/stores/workspaceStore.ts, frontend/src/types/index.ts, CHANGELOG.md

## 2026-06-03

### fix: parallelize tab startup to shrink post-reload Reconnecting window
- Restart `start_all_tabs` with `asyncio.gather` so the FastAPI lifespan hook no longer reattaches saved tabs one-by-one. Each `process.start()` awaits a ~1 s settle sleep, so for ~24 tabs the previous serial loop blocked the lifespan for ~40 s before any HTTP/WS request could be served — every uvicorn `--reload` therefore showed a long "Reconnecting…" overlay across all open terminals
- After the change, the startup window is dominated by the slowest single tab (~2 s on this host) instead of N × 1.6 s, dropping the front-end reconnect window roughly proportionally to tab count
- **Files**: backend/claude_hub/services/ttyd_manager.py, CHANGELOG.md

### fix: make autonomous model ledger checks runtime-aware
- Relax the Auto Mode orchestrator contract for non-Claude runtimes: Codex/Cursor workers now record `model_or_api` evidence such as an actual runtime model, `runtime-default`, `unsupported:<reason>`, or `external:<api>` instead of being forced to claim Claude opus/sonnet pinning
- Keep strict primitive-to-model verification for Claude-runtime autonomous work while telling reviewers not to fail Codex/Cursor/terminal tasks solely because Claude pinning is unavailable
- Add regression coverage for Codex assignment and reviewer prompt wording so autonomous evaluation remains strict about ledger evidence without imposing runtime-inapplicable model rules
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_orchestrator_contract.py, CHANGELOG.md

### fix: detect pending Codex task paste above blank viewport rows
- Trim trailing blank rows from tmux capture output before checking whether a dispatched workspace prompt is still sitting in an agent input bar; this prevents Codex panes with large pasted task prompts and empty space below the prompt from being falsely treated as submitted
- Add regression coverage for the failure shape shown in the task screenshot: `› Ne[Pasted Content ...]` followed by model/status text and many blank capture rows
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

## 2026-06-02

### feat: Auto Mode orchestrator contract with CLI-native sub-agent delegation
- Force autonomous tasks into orchestrator mode at the prompt layer: when `task_mode=autonomous` the worker now receives an Orchestrator Contract instructing it to decompose the task and delegate to sub-agents via the runtime's native sub-agent capability (Claude `Task` tool, Cursor sub-agent, Codex fan-out) instead of doing bulk implementation/test/review in its own context
- Define six domain-agnostic role primitives (P-PLAN / P-EXECUTE / P-VALIDATE / P-JUDGE / P-INTEGRATE / P-RESEARCH) so the same contract covers coding, image generation, doc writing, data analysis, etc.; orchestrator must declare a `workflow:` block (roles + deps + notes) in its first working report
- Pin models per primitive on the claude runtime (P-PLAN/P-EXECUTE/P-JUDGE/P-INTEGRATE = opus; P-VALIDATE/P-RESEARCH = sonnet); P-EXECUTE that calls an external API (e.g. T2I) records `model=external:<api>` instead of an LLM model. Users cannot override per task
- Provide two worked few-shot examples in the contract (linear coding task; image generation with feedback loop + external API) so the orchestrator learns the shape without being locked into a fixed template enum
- Standardize the subtask hand-off envelope (`{role.id, primitive, objective, success_criteria, inputs, output_schema, tools_allowed, context_budget, return_mode}`) and default `return_mode: final-only` so sub-agents do not flood the orchestrator's context with full transcripts (lesson from Anthropic multi-agent research system + LangGraph)
- Require a textual `subagent-ledger:` summary in the worker's review-gate report; extend `_autonomous_review_block` so the external evaluator verifies the ledger is present, role.id matches the declared workflow, and key primitives ran on opus — wrong-tier or missing entries are flagged as contract violations
- Surface the multi-agent cost trade-off where the decision is actually made: keep only the three orchestrator-mode criteria + a soft "expensive" anchor in the AI prompt; show the concrete ~10–15× token-cost figure as hover tooltips on the Add Task complexity buttons (Auto / Simple / Complex) in the frontend
- Per-CLI capability hint helper (`_subagent_capability_hint`) emits runtime-specific invocation snippets for claude / cursor / codex and a graceful-degradation note for plain terminal sessions; cursor and codex sub-agent model pinning is acknowledged as version-dependent and deferred to a V1.1 spike
- Revising prompts now include an orchestrator-mode reminder so the worker keeps dispatching new sub-agent subtasks (and appending to the existing ledger) instead of folding the fix into its own context
- New `tests/test_workspace_orchestrator_contract.py` (16 cases) asserts the contract wording, per-CLI hint branches, ledger verification text, complexity-level enforcement, and revision reminder; full backend suite passes (excluding the pre-existing Playwright `test_terminal_replay.py` asyncio-teardown issue on `main`)
- Companion design doc captures the proposal, the cross-framework survey (Anthropic, OpenAI Swarm/Agents SDK, AutoGen, LangGraph, CrewAI, MetaGPT, Cognition/Devin, multi-agent.wiki), the eight cross-cutting lessons, and the rationale for each design choice
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_orchestrator_contract.py, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md, CHANGELOG.md

## 2026-05-31

### fix: avoid frontend freeze on terminal load with large scrollback
- Tokenise tmux history in the injected replay script with a single `split(/\r\n|\n|\r/)` instead of two regex `replace` passes plus split, eliminating multi-pass scans of the multi-MB `historyText`
- Skip building `replayPlainText` for plain-terminal tabs (it is only consumed by `isDuplicateInitialFrame` on agent TUI tabs) and cap the agent-TUI variant to the last 200 lines / 16 KB so duplicate-frame `indexOf` checks stay cheap
- Lower `FULL_REPLAY_VERIFY_ATTEMPTS` from 20 to 4 (each retry re-pushes the whole replay payload through xterm) and bump `FULL_REPLAY_VERIFY_DELAY_MS` to 350 ms so a stuck buffer-expansion path no longer pumps tens of MB through the parser repeatedly
- The `/api/terminal/proxy/{tab_id}/` iframe is same-origin with the parent app, so this previously freezing synchronous work was sharing the renderer event loop with the rest of the frontend; fixing it inside the injected JS keeps the entire UI responsive during terminal load
- **Files**: backend/claude_hub/api/terminal.py, CHANGELOG.md

## 2026-05-30

### fix: hold workspace agent through entire review until task is done
- Tighten `_can_dispatch_to` so a session whose current task is in REVIEW status is no longer freed when `_is_review_passed` becomes true; the agent stays locked to the task across `ready_for_review` → `review_passed` → human-acceptance, only releasing when the task moves to DONE via `_release_task_session`
- Drop the symmetric early-clear branch in `_refresh_session_statuses` that nulled `task_id`/`current_task_id` once an idle worker's task hit REVIEW + review_passed; status sweeps now respect the same lifetime contract
- Update `_is_holding_unresolved_review_task` to treat any REVIEW-state task as still holding the agent so `_can_assign_or_queue_to` keeps allowing related/explicit queueing onto the same agent without preempting it
- Replace the now-stale `test_idle_review_task_releases_agent_for_queued_dispatch` and `test_request_changes_rejects_busy_original_agent` cases with assertions that match the new lock-until-done semantics: the second task queues behind the held agent, the human PATCH→DONE transition releases it, and `continue` on the held REVIEW task now succeeds because the agent never lost context
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

### feat: concise bilingual reviewer reports
- Tighten the reviewer prompts (`_build_reviewer_bootstrap_prompt`, `_build_review_prompt`) so the review report's `message` is a short scannable summary instead of a full dump of every section: Verdict + 1-2 sentence task summary + acceptance-criteria rollup + (only if failed) top required fixes + one-line notes
- Move detailed evidence into the structured fields the UI already renders separately (`validation`, `risks`, `acceptance_check`, `profile_results`, `artifact_refs`), removing the duplicated long-form prose from the message body
- Require reviewers to emit bilingual `message_en` / `message_zh` in addition to the legacy `message`, matching the contract already used by implementation agents; update curl example accordingly
- **Files**: backend/claude_hub/services/workspace_manager.py, CHANGELOG.md

## 2026-05-29

### fix: clear reviewer context between unrelated review tasks
- Send `/clear` to the reviewer session before assigning a new review when the reviewer has prior task history and the incoming task differs from its last reviewed task; this prevents the reviewer's conversation from accumulating across unrelated tasks and triggering Claude Code's auto-compact mid-review
- Skip the clear when re-reviewing the same task on the same reviewer (e.g., review_failed → fix → completed loop) so the reviewer keeps the prior round's context for consistency
- Skip the clear for the very first review on a fresh reviewer (no prior task history) so that no extra `/clear` round-trip is paid in the common case
- Add focused tests covering the cross-task clear, the same-task continuation path, and confirming the existing first-review path still passes without `/clear`
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

### fix: confirm pasted task input on Cursor agent dispatch
- Recognize Cursor's `→` prompt indicator and `[Pasted text` placeholder in `_message_still_in_input`, so the workspace dispatch submit-verifier no longer reports a Cursor pane as already-submitted while the task content is still sitting in the input bar; the C-m retry loop now actually runs and pushes the paste through
- Broaden the placeholder check from Codex-only `[Pasted Content` to also accept `[Pasted text +N lines]`, the format Claude Code and Cursor render for multi-line paste; this incidentally closes the same latent risk on Claude tabs (Codex's `›` + `[Pasted Content` was already covered)
- Add Cursor banner markers (`Cursor Agent`, `/auto-run`) to `_agent_input_ready` so `send_session_message` no longer times out the 12 s pre-send wait against a fresh Cursor tab and proceeds with the load-buffer/paste flow promptly
- Add focused pytest coverage for Cursor paste-pending detection, Cursor message-prefix detection, post-submit clearing, Cursor banner readiness, and Claude `[Pasted text` placeholder detection
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

## 2026-05-27

### feat: show agent CLI type avatar in status surfaces
- Add `AgentAvatar.vue` rendering inline brand-evocative SVG marks for each `agent_type` (claude / codex / cursor / terminal) so each agent has a recognizable icon without bundling third-party logo assets
- Replace the bare status dot in the workspace agent status card and the floating `AgentStatusFloatingPanel` rows with the avatar plus an overlaid runtime status dot, and show a colored CLI-type pill alongside the role label
- **Files**: frontend/src/components/AgentAvatar.vue, frontend/src/components/AgentWorkspaceView.vue, frontend/src/components/AgentStatusFloatingPanel.vue, CHANGELOG.md

### fix: detect Cursor agent Working state
- Add Cursor-specific working signals to `_classify_agent_status`: the `ctrl+c to stop` tail hint and a strict `Running <N> tokens` regex, so Cursor tabs no longer show as Idle while actively running
- Cover the new path with a pytest case mirroring the captured Cursor pane
- **Files**: backend/claude_hub/services/ttyd_manager.py, backend/tests/test_ttyd_manager.py, CHANGELOG.md

### fix: align Agent Workspace review detail markers
- Scope the report timeline marker pseudo-element to top-level timeline entries so nested review profile and acceptance-check lists no longer show stray blue dots
- Align inline report metadata such as Confidence on a clean baseline with the label and value in one compact row
- **Files**: frontend/src/components/AgentWorkspaceView.vue, CHANGELOG.md

### feat: add workspace task execution complexity
- Add task-level `execution_complexity` with `auto`, `simple`, and `complex` values, defaulting old and new tasks to `auto`
- Inject concise complexity guidance into assignment prompts so simple tasks execute directly, complex tasks orchestrate/delegate bounded subwork where available, and auto tasks choose and state a strategy first
- Carry execution complexity into dispatcher and reviewer prompts so reviewers can verify that the implementation strategy matched the selected complexity
- Surface an Auto/Simple/Complex selector in the Add Task modal and show the selected execution style in task assignment details
- Add focused backend coverage for persistence defaults, legacy normalization, assignment prompt guidance, and reviewer prompt visibility
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-05-27-task-execution-flow.md

## 2026-05-26

### feat: add workspace Review Profiles v1
- Add profile-aware reviewer metadata for `general`, `code`, `ui`, `artifact`, `delivery`, and `boundary` review lenses, including structured profile results, artifact refs, confidence, and human-judgment flags on reports and autonomous evaluation records
- Infer default review profiles from task mode, strictness, artifact policy, changed files, attachments, and report evidence, and inject profile-specific guidance plus bounded `REVIEW.md` instructions into reviewer prompts
- Surface configured profiles, profile results, artifact refs, confidence, and autonomous evaluation profile summaries in Agent Workspace task detail
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/api/workspaces.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/services/workspace_state_policy.py, backend/tests/test_workspace_state_policy.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-05-26-review-profiles-v1.md

### feat: add Agent Workspace autonomous mode v1
- Add `direct` / `reviewed` / `autonomous` task modes with optional autonomy policy, autonomous run state, rubric/evaluation records, iteration records, and old-state defaults that keep existing tasks reviewed by default
- Keep Direct tasks out of automatic AI-review routing: Direct completion/ready reports proceed to the human Done gate unless review is explicitly requested, while Direct blocked/input-needed reports remain non-accept-ready
- Treat existing reviewer sessions as Autonomous Mode V1 evaluators: autonomous worker completion always routes to evaluation, evaluator pass moves the run to passed and Review awaiting human acceptance, evaluator failure revises while budget remains, and exhausted/needs-input states stop for human review
- Extend assignment and reviewer prompts with autonomous policy/run context while preserving the Goal Packet, acceptance-check, and final human Done gate
- Add mode-aware workspace UI: Add Task mode selector, autonomous controls, compact Auto round badges, and a run-detail panel with phase, iteration, score, policy, next action, and evaluation history
- Add focused backend coverage for mode defaults, old-state compatibility, Direct no-review/default-review/blocked/input-needed behavior, autonomous pass, budget exhaustion, and pure autonomous policy transitions
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/models/__init__.py, backend/claude_hub/api/workspaces.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/services/workspace_state_policy.py, backend/tests/test_workspace_state_policy.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-05-26-autonomous-mode-v1.md, CHANGELOG.md

## 2026-05-23

### refactor: extract Agent Workspace state policy
- Add `workspace_state_policy.py` as a pure policy boundary for report/session/task status mapping, runtime observation mapping, review routing, review-skip eligibility, completion evidence gaps, and auto-continue output classification
- Keep `WorkspaceManager` responsible for persistence and tmux/reviewer side effects while delegating transition decisions to the policy helpers
- Add focused policy unit tests and keep workspace lifecycle integration coverage passing
- **Files**: backend/claude_hub/services/workspace_state_policy.py, backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspace_state_policy.py, docs/working-logs/2026-05-23-state-machine-assessment.md, CHANGELOG.md

### docs: assess Agent Workspace state-machine boundaries
- Document current terminal runtime detection, managed session lifecycle, task/report/review transitions, and frontend status derivation
- Recommend a bounded state-policy/state-machine layer for Agent Workspace lifecycle events while keeping ttyd/tmux status classification as a separate heuristic observation source
- **Files**: docs/working-logs/2026-05-23-state-machine-assessment.md, CHANGELOG.md

### feat: add workspace Goal Packet v1
- Add optional task-level Goal Packets so workspace agents can record objective, acceptance criteria, validation plan, assumptions, out-of-scope boundaries, and handoff requirements directly on a task
- Add report-level acceptance checks for ready-for-review/completed handoffs, and carry Goal Packet + acceptance evidence into reviewer prompts so reviewers audit both goal fidelity and delivery evidence
- Update assignment prompts to ask agents to derive a Goal Packet before substantive implementation while preserving existing started/working/blocked/needs_input/ready_for_review/completed/review_* state transitions
- Clarify agent-decided routing: `review_decision=skip` only skips AI reviewer checks, not final human completion; AI-passed or AI-skipped tasks remain in review awaiting human completion
- Add human acceptance timestamps to tasks and keep Agent Workspace completion action as Done; Request review opens a prompt and sends the human's review instructions to the reviewer
- Make request-changes safe when the original agent has already moved to another task, and hide Done when the latest reviewer result is failed or needs input
- Block low-risk review-skip completion reports that lack a stored Goal Packet or acceptance-check evidence; the agent is prompted to supplement the missing audit evidence and the task stays working instead of silently skipping review
- Render a compact read-only Goal Packet section and acceptance-check evidence in the task detail panel, including an empty state for older tasks
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/services/workspace_manager.py, backend/claude_hub/api/workspaces.py, backend/tests/test_workspaces.py, frontend/src/types/index.ts, frontend/src/components/AgentWorkspaceView.vue, docs/working-logs/2026-05-23-workspace-goal-packet-v1.md

## 2026-05-22

### feat: cursor + terminal agent types in workspace, keep manage-agents modal open after create
- Manage Agents modal now exposes the same four agent types as the new-tab dialog: Codex, Claude, Cursor, Terminal (previously the workspace dropdown only had three slots and mislabeled `cursor` as "Terminal")
- The YOLO/solo-mode field is hidden for both Cursor and Terminal in the workspace agent form (matches the new-tab behavior); creation also force-clears `solo_mode` for these types when sending to the backend
- After "Create agent" succeeds, the modal stays open (only the file browser closes and the title field resets), so users can add multiple agents in a row without re-opening the dialog
- TabBar tightens the agent-type type literal to the canonical `AgentType` union and renames the remote-tab default label to use the proper Cursor/Terminal display name; the agent-type watcher also clears solo_mode for `terminal` (was only doing so for `cursor`)
- **Files**: frontend/src/components/AgentWorkspaceView.vue, frontend/src/components/TabBar.vue, CHANGELOG.md

### docs: merge AGENTS.md into CLAUDE.md and prune stale gstack section
- Make `AGENTS.md` and `CLAUDE.md` identical: keep `CLAUDE.md` as the canonical conventions doc, fold in the `Mandatory Branch Workflow` and `Protected Local State` sections that previously lived only in `AGENTS.md`, and rewrite `AGENTS.md` as a verbatim copy
- Drop the outdated `gstack` and `Skill routing` sections that referenced gstack-only commands (`/office-hours`, `/ship`, `/qa`, etc.) which are not part of this project's tooling
- Refresh the overview to mention the workspace orchestration layer + Claude/Cursor/Terminal agent types, expand the protected-local-state list, and add an `Agent Types` reference plus a workspace orchestration row in `Common Dev Scenarios`
- **Files**: AGENTS.md, CLAUDE.md, CHANGELOG.md

### feat: cursor agent support and dedicated terminal agent_type
- Repurpose `AgentType.CURSOR` to launch the Cursor CLI (`agent`); cursor agent is always YOLO by default and the solo-mode toggle no longer applies
- Add new `AgentType.TERMINAL` for plain user-shell sessions (the previous `cursor` placeholder behavior); the UI dropdown now lists Cursor and Terminal as separate options
- Treat cursor as an agent TUI in `IS_AGENT_TUI` and disable the auto tmux-history replay loop that was previously running for cursor — the periodic snapshot replay was overwriting the cursor TUI mid-update, causing the "stuck halfway" display and input deletion lag reported in cursor sessions; auto-replay now only runs for the new plain `terminal` mode
- Extend probe-response filtering, foreground idle detection, and clipboard-image paste handling to include cursor
- **Files**: backend/claude_hub/models/schemas.py, backend/claude_hub/services/ttyd_manager.py, backend/claude_hub/api/terminal.py, backend/tests/conftest.py, backend/tests/test_ttyd_manager.py, frontend/src/types/index.ts, frontend/src/components/TabBar.vue, frontend/src/components/TerminalView.vue, CHANGELOG.md

### fix: hold orchestrator agent only while review is unresolved
- Hold the orchestrator's `task_id`/`current_task_id` binding while a task's review is still in flight or after `REVIEW_FAILED` (so reviewer-failure feedback can re-engage the same context), but auto-release the agent once the latest review report is `REVIEW_PASSED` so the queue advances without waiting for a manual "done" click
- Replaces the earlier behavior that held the agent all the way through to `done` and could leave the queue looking stuck when a single resident agent finished review
- **Files**: backend/claude_hub/services/workspace_manager.py, backend/tests/test_workspaces.py, CHANGELOG.md

## 2026-05-21

### fix: replace yellow working badge with AI reviewing on task card
- When a task is under active AI review, show only the existing "AI reviewing" pill on the task card header instead of stacking it next to a redundant yellow `working` status pill; the status pill still renders for non-reviewing states
- **Files**: AgentWorkspaceView.vue

### fix: stop re-prompting orchestrator after review is in flight
- Skip the auto-continue "no workspace report was recorded" nudge when `review_requested_at` is set without `review_completed_at`, or when the latest report for the task is already `ready_for_review` / `completed` / `blocked` / `needs_input`
- Previously the orchestrator session stayed parked with `task.status == WORKING` while the reviewer ran, so the monitor kept matching completion patterns in scrollback and re-sending the nudge every ~15s up to 10 attempts
- **Files**: workspace_manager.py, test_workspaces.py

### fix: disabled segment-button styling on edit workspace modal
- Add a `:disabled` style for segmented controls so the locked Local/Remote toggle in the Edit Workspace modal renders with reduced opacity and a not-allowed cursor on both desktop and mobile, instead of looking falsely interactive
- **Files**: AgentWorkspaceView.vue

### feat: editable workspace working dir as default for new agents

### feat: bilingual report detail with EN | 中 toggle in task progress
- Add optional `message_en` and `message_zh` fields to the AgentReport schema so workers can submit each progress update in both languages; legacy `message` remains a fallback
- Render a small EN | 中 toggle next to the Progress section in task details that switches the timeline message body between languages, with sticky preference in localStorage
- Update workspace agent dispatch prompts and curl examples to require bilingual messages on every report
- **Files**: schemas.py, workspace_manager.py, AgentWorkspaceView.vue, index.ts

### feat: surface AI review state on working tasks
- Show a colored "AI reviewing" / "Awaiting AI review" / "Review needs input" badge in the task card header for tasks still in the Working column while a reviewer agent is engaged
- Drive the badge from existing `review_session_id`, `review_requested_at`, and the latest `review_*` AgentReport so it pulses live during `review_started` and clears once review completes
- **Files**: AgentWorkspaceView.vue

### fix: task progress timeline cleanup
- Remove the redundant inner status dot on each progress card so the timeline rail dot is the only marker per entry
- **Files**: AgentWorkspaceView.vue

### fix: let mobile overflow menus scroll when expanded
- Bound terminal and workspace mobile overflow menus to the viewport so long nested sections do not run off-screen
- Enable touch scrolling inside the menu panels while keeping scroll chaining contained
- **Files**: TabBar.vue, AgentWorkspaceView.vue

### fix: collapse mobile frontend access links
- Keep the mobile overflow menu compact by showing Frontend Access as a collapsed submenu by default
- Fetch and reveal the local frontend URLs only when the nested menu is opened, then reset it collapsed when the parent overflow menu closes
- **Files**: NetworkAccessMenu.vue

### fix: drop generated terminal probe replies in agent tabs
- Filter xterm.js device-attribute and cursor-position replies from Claude/Codex tab WebSocket input so terminal capability probes no longer appear as stray text like `0;276;0c` in the agent prompt
- Keep the filter scoped to generated probe replies on agent tabs, leaving normal typing, escape keys, resize frames, and plain terminal tabs unchanged
- **Files**: terminal.py, test_terminal_proxy.py

### fix: keep status panel refresh icon idle during polling
- Make the Workspace Agents status-panel refresh button use a local manual-refresh pending state instead of the global background status-poll loading flag
- Keep automatic panel-open and periodic status refreshes silent so the header icon no longer appears to spin continuously while data is polling
- **Files**: AgentStatusFloatingPanel.vue

### fix: release idle reviewed agents and dedupe pasted task images
- Let an idle implementation agent accept the next queued task after its previous task has reached Review, while preserving working/pending-review assignments
- Reconcile stale review-stage `current_task_id` session state during status refresh so queues do not remain blocked by already-reviewed tasks
- Deduplicate clipboard image files against embedded HTML/plaintext data URLs so a single pasted screenshot does not create two task attachments
- **Files**: workspace_manager.py, test_workspaces.py, AgentWorkspaceView.vue

### feat: let agents skip low-risk reviewer checks
- Add explicit `review_decision` metadata to workspace reports so agents can request, skip, or defer to automatic reviewer routing
- Keep backend guardrails that force review for changed files, tracked dirty worktrees, blocked/input-needed states, runtime attention diagnostics, and failed-review follow-ups
- Mark approved skip decisions in the Review column with a reason and expose a manual Request review action for skipped tasks
- **Files**: workspace_manager.py, schemas.py, workspaces.py, test_workspaces.py, AgentWorkspaceView.vue, workspaceStore.ts, index.ts

### fix: keep terminal typing responsive during history refresh
- Release the initial terminal replay hold as soon as the user types, so opening a populated tab no longer delays local input echo behind the scrollback stabilization window
- Cancel or postpone automatic tmux history repair when input arrives during live-output refresh, keeping typed text responsive while preserving a later quiet-window recovery path
- Add replay E2E coverage for typing during tab open and typing while a delayed live-output resync is pending
- **Files**: terminal.py, test_terminal_replay.py

### fix: avoid duplicate split-pane terminal clients
- Keep a terminal tab assigned to only one visible pane at a time so split layouts cannot attach duplicate ttyd/tmux browser clients to the same Claude session
- Drop hidden iframe caches when leaving single-pane mode, preventing stale hidden clients from continuing Claude TUI redraw and resize work while another pane displays that tab
- Cap single-pane terminal iframe caching to the active tab plus one recent tab so large workspaces do not keep many hidden ttyd clients rendering and resizing in the background
- Reduce global agent-status polling pressure for large tab sets and avoid cursor/plain terminal history resync during ordinary typing
- **Files**: terminalStore.ts, TerminalView.vue, terminal.py, test_terminal_replay.py

### fix: serialize workspace task dispatch
- Serialize per-workspace dispatch so the background monitor and task start path cannot send `/clear` and the assignment prompt for the same queued task twice
- Keep pasted Codex task prompts marked pending when only a command-result marker follows, avoiding false success that leaves task content pasted but not submitted
- Keep tasks in Review after `review_passed` until a human clicks Done, while releasing reviewer assignments and preventing runtime refreshes from moving reviewed tasks back to Working
- Require future development to use isolated worktrees with task branches; frontend changes should use a dedicated debug server and stop it before merge or handoff
- Add regression coverage for concurrent workspace dispatch and pasted-input submit detection
- **Files**: AGENTS.md, CLAUDE.md, workspace_manager.py, test_workspaces.py

## 2026-05-20

### feat: add workspace reviewer loop
- Add reviewer workspace sessions and review-specific report states so completed, blocked, or ready tasks can be routed through an independent reviewer gate
- Send task-specific review prompts with acceptance criteria guidance, recent task reports, and verdict reporting rules; failed reviews continue the task back to the implementation agent
- Rename assigned agent and reviewer terminal tabs to the active task title for easier terminal identification
- Surface reviewer status, review attempts, reviewer assignments, and temporary reviewer sessions in the Agent Workspace UI
- **Files**: workspace_manager.py, schemas.py, workspaces.py, test_workspaces.py, AgentWorkspaceView.vue, AgentStatusFloatingPanel.vue, workspaceStore.ts, terminalStore.ts, index.ts, ttyd_manager.py

### fix: support workspace task screenshot paste
- Read pasted workspace task images from clipboard files, clipboard items, and data URL payloads so screenshots can attach in both new task and task interaction forms
- Avoid secure-context-only draft attachment IDs by falling back when `crypto.randomUUID()` is unavailable on LAN HTTP access
- Keep the workspace task UI paste-only without adding a separate file-picker attachment control
- **Files**: AgentWorkspaceView.vue

## 2026-05-19

### fix: stabilize remote Claude agent display
- Configure remote tmux sessions before attach so Claude/Codex tabs hide nested tmux status bars, keep mouse off, enable focus events, and preserve a large history limit
- Suppress SSH log-level noise in remote launch and capture commands so known-host warnings do not mix into the terminal canvas
- Let remote Claude/Codex tabs attach live immediately instead of replaying a slow initial SSH history snapshot that can overwrite fresh prompt/input state; manual history refresh still captures the remote tmux session on demand
- **Files**: terminal.py, ttyd_manager.py, test_ttyd_manager.py

## 2026-05-18

### fix: restore live terminal updates after tab activation
- Stop dropping ttyd ws frames in the Phase B full-replay hold; flush buffered frames and reconcile with a fresh tmux snapshot so sparse-update TUIs (Claude) keep showing the latest output instead of a stale screen
- Rework `runResync` to set the resyncing flag before the async fetch so concurrent live writes buffer instead of forcing abort + retry under hot write streams
- Replace `startPostReplayWatch`'s stale `replayPayload` rewrite with a fresh tmux refetch so the recovery path no longer rolls back several seconds of real ws data
- Trigger a history refresh on cursor/plain desktop tab switches (mobile already had this via `terminal-activate`) so switching back to a plain terminal repaints the latest content immediately
- **Files**: terminal.py, TerminalView.vue

### fix: keep live terminal output pinned to latest
- Preserve bottom-follow behavior during active terminal output by scrolling after xterm renders when the viewport was already at the latest line
- Treat wheel and touch scroll-away events as user intent so live following does not override manual history inspection
- Ignore xterm-internal scroll events and keep bottom-follow active briefly after live writes so dynamic Claude/Codex status UIs do not leave the viewport stuck mid-buffer
- Disable automatic idle history replay for Claude/Codex TUI tabs, preventing tmux snapshot replays from corrupting relative cursor redraws while agents are working
- Limit automatic tab-activation history refreshes to plain cursor terminals; Claude/Codex tabs now scroll to bottom without replaying tmux snapshots unless the user requests a manual refresh
- Avoid refresh-heavy bottom-follow loops during live input echo so Claude prompt typing stays responsive
- Preserve Claude/Codex scrollback continuity by filtering held duplicate ttyd initial-screen frames while keeping real live output produced during Phase B history reconstruction
- Freeze Claude/Codex live redraws while the user is browsing history, then restore from tmux when returning to the bottom so the visible historical viewport is not overwritten by the fixed input/status area
- Add terminal replay coverage for live-output bottom pinning, dynamic internal scroll events, DOM bottom-gap drift, and agent history viewport stability
- **Working log**: docs/working-logs/2026-05-19-fix-claude-terminal-live-history.md
- **Files**: terminal.py, TerminalView.vue, test_terminal_replay.py

### feat: show copyable frontend LAN access links
- Add a top-bar network menu that lists copyable frontend URLs for loopback and discovered local IPv4 addresses
- Keep mobile top bars compact by placing the same access list inside the existing terminal and workspace overflow menus
- Discover interface IPv4 addresses from the backend and allow Vite review sessions to route the system endpoint to a branch backend
- **Files**: system.py, test_system.py, NetworkAccessMenu.vue, App.vue, TabBar.vue, AgentWorkspaceView.vue, vite.config.ts

## 2026-05-17

### fix: refresh terminal history on demand
- Add a pane-level history refresh action that forces the embedded terminal to recapture tmux scrollback and rebuild the xterm buffer
- Refresh and scroll mobile terminals to the latest output when switching back to cached tabs, avoiding stale initial-screen views
- **Files**: terminal.py, TerminalView.vue, TerminalPane.vue, test_terminal_replay.py

### fix: mute dark terminal ANSI background colors
- Replace dark terminal base ANSI colors with lower-saturation values so remote Claude prompts that paint large ANSI background regions do not turn the pane bright green
- Apply a modest dark-mode xterm minimum contrast ratio to keep colored terminal text readable on muted ANSI backgrounds
- **Files**: App.vue, TerminalView.vue

### fix: keep remote Claude launches out of bare shells
- Fall back to the remote home directory when a remote tab or workspace agent is launched with a cwd that does not exist on the SSH host, then continue starting Claude/Codex instead of dropping straight into a login shell
- Reset remote tab/agent cwd defaults when switching to a remote target so local macOS paths are not carried into SSH launches
- **Files**: ttyd_manager.py, test_ttyd_manager.py, TabBar.vue, AgentWorkspaceView.vue

## 2026-05-16

### feat: expand mobile terminal space while typing
- Drive the app shell height from `visualViewport` so the mobile keyboard does not double-shrink the terminal layout
- Enter a compact terminal mode while the keyboard is open, hiding nonessential chrome and tightening tab, pane, and mobile-control spacing
- Move the mobile split-layout shortcuts into a top-bar dropdown so the standalone layout row no longer consumes vertical space on phones
- Keep the mobile terminal tab bar anchored while the keyboard is open and smooth the compact layout plus floating virtual-key panel transitions
- Fold the mobile tab bar without dropping the terminal pane frame so the keyboard transition keeps a continuous border
- Animate mobile top chrome and pane-header collapse so the terminal frame slides with the keyboard instead of jumping into place
- Keep the floating virtual-key toggle pinned to the active viewport bottom during keyboard-open mode
- Coalesce terminal resize messages during mobile keyboard animation so xterm redraws only after the layout settles
- Replace the mobile keyboard folding chrome with a stable compact top bar and app menu so the terminal canvas does not resize when the keyboard opens
- Keep the mobile virtual-key overlay content-sized while tracking the visual viewport, preserving native xterm touch inertia
- Measure the browser's fixed-position keyboard baseline before shifting the mobile virtual-key button, avoiding duplicate upward movement on browsers that already anchor fixed controls to the visual viewport
- Give the mobile Agent Workspace view the same compact shell language as terminal mode, with a low sticky workspace toolbar, primary task action, overflow menu with distinct mode/theme controls, and slimmer agent status chips
- **Files**: App.vue, AgentStatusFloatingPanel.vue, AgentWorkspaceView.vue, LayoutSelector.vue, MobileControls.vue, TabBar.vue, TerminalGridView.vue, TerminalPane.vue, TerminalView.vue

### fix: avoid false pending workspace dispatch
- Treat submitted Claude slash-command output and older prompt echoes as completed sends, so queued workspace tasks are not blocked after a successful `/clear`
- Add regression coverage for the Claude `/clear` output shape that kept the H20 workspace task queued
- **Files**: workspace_manager.py, test_workspaces.py

### fix: replay remote tab tmux history
- Capture scrollback from the remote tmux session for remote tabs so reconnect/history replay includes the agent's actual remote terminal history instead of only the local SSH wrapper screen
- Keep local tmux capture as a fallback when remote SSH capture fails, using non-interactive SSH options to avoid blocking page load
- Add backend coverage for remote history preference, local fallback, and remote capture command construction
- **Files**: ttyd_manager.py, test_ttyd_manager.py

### fix: harden terminal replay hold on Linux CI
- Extend the full-replay hold window and perform a final replay before marking history as complete, so late ttyd initial frames cannot collapse xterm scrollback immediately before E2E assertions
- Verify the xterm buffer contains expected scrollback before publishing replay readiness, with a short post-ready watchdog for late Linux runner redraws
- Normalize styled tmux prompts in terminal E2E comparisons and wait for the expected xterm buffer depth before asserting scrollback state
- **Files**: terminal.py, conftest.py, test_terminal_replay.py

### fix: stabilize terminal replay CI and refresh README
- Replace synchronous browser history preload with an async preload gate before hooking xterm, so Chromium on Linux CI reliably receives tmux history before replay
- Keep full terminal replay writes buffered until ttyd's initial frame stream goes quiet, preventing late frames from collapsing scrollback to only visible rows
- Allow terminal replay E2E tests to bind a temporary backend URL so local validation can avoid the live 8173 service
- Update README, backend package description, and current Agent Workspace screenshot to reflect the workspace-agent, remote-tab, clipboard-image, and validation flows
- **Files**: terminal.py, conftest.py, README.md, backend/README.md, pyproject.toml, agent_workspace_demo.png

### fix: match terminal padding to rendered canvas background
- Compute the light-mode terminal inset color through the same canvas filter used by xterm so the padding matches the rendered terminal surface
- **Files**: TerminalView.vue

### fix: soften embedded terminal edge padding
- Restore a small terminal-colored inset around xterm content so light mode feels less crowded without reintroducing page-colored gutters
- **Files**: TerminalView.vue

### fix: fill embedded terminal viewport edge-to-edge
- Remove ttyd's default embedded terminal padding and stretch the xterm screen/canvas to the pane edges so light mode no longer shows white gutters
- **Files**: TerminalView.vue

### fix: refit terminal canvas after light theme layout changes
- Trigger ttyd/xterm resize from the active iframe after theme, tab, and container-size changes so the terminal canvas fills the pane in light mode
- **Files**: TerminalView.vue

### fix: align compact done task cards
- Prevent crowded task columns from flex-shrinking task cards below their content height
- Make Done task cards an explicit compact single-line surface so titles and status badges stay vertically centered
- **Files**: AgentWorkspaceView.vue

### style: polish workspace and terminal surfaces
- Refine workspace cards, columns, task detail sections, and report timeline to reduce visual noise and clarify hierarchy
- Lighten terminal tabs, layout controls, and active pane treatment while keeping dark/light theme tokens consistent
- Add shared radius and motion tokens for future frontend polish
- **Files**: App.vue, AgentWorkspaceView.vue, TabBar.vue, LayoutSelector.vue, TerminalPane.vue

## 2026-05-15

### fix: reopen completed review tasks from live runtime work
- Treat later live Working activity after the review grace window as a valid Review-to-Working transition for both `ready_for_review` and `completed` reports
- Keep the immediate post-report grace window so a completion report's own terminal output does not reopen the task
- **Files**: workspace_manager.py, test_workspaces.py

### feat: add button-level loading feedback
- Add a reusable loading button component and pending-action helper for frontend interactions
- Show per-control processing feedback for workspace switching, workspace task actions, agent management, follow-up sends, terminal tab creation/duplication/closing, directory browsing, status refresh, login redirect, and logout
- Keep pending state scoped by task, agent, session, tab, or browser action so unrelated controls remain usable
- **Files**: LoadingButton.vue, usePendingActions.ts, AgentWorkspaceView.vue, TabBar.vue, AgentStatusFloatingPanel.vue, LayoutSelector.vue, LoginView.vue

### docs: require branch-based agent development
- Add an agent-facing `AGENTS.md` entrypoint that points to `CLAUDE.md` and forbids direct development on `main`
- Clarify that small fixes, documentation changes, and managed workspace tasks must still use a feature/fix branch or isolated worktree before merging back
- **Files**: AGENTS.md, CLAUDE.md

### feat: make workspace agents manageable
- Rename the workspace agent entry point to agent management and show the existing agent list before the add-agent form
- Add visible delete actions to the agent status strip and management modal, with disabled-state hints while an agent still owns open tasks
- **Files**: AgentWorkspaceView.vue

### fix: enable terminal image paste for Claude tabs
- Reuse the browser-image-to-macOS-clipboard paste bridge for Claude Code tabs as well as Codex tabs, so pasted screenshots can reach the TUI through Ctrl+V
- **Files**: TerminalView.vue

### fix: keep ready reports authoritative
- Keep `ready_for_review` and `completed` reports as the authoritative task state instead of reopening Review tasks from raw terminal Working samples
- Preserve runtime-based Review-to-Working recovery when the assigned terminal shows new Working activity after the review timestamp, covering direct terminal follow-ups
- Add a short grace window after explicit ready reports so the reporting agent's own terminal activity cannot immediately reopen the task
- Add an explicit `working` report when a Review task is continued through the workspace flow so follow-up work has a durable state transition
- Restore tasks whose latest report is ready/completed back to Review during board reconciliation unless the task has later explicit or runtime Working activity
- Make auto-continue prompts semantic: interruption-like idle output asks the agent to continue, while completion-like idle output asks the agent to submit the missing final report
- **Files**: workspace_manager.py, test_workspaces.py

### fix: restore main ci checks
- Keep terminal history full replay buffered briefly after xterm accepts the replay write so late ttyd initial screen frames cannot collapse reconstructed scrollback on Linux CI
- Apply backend Black/isort cleanup for files that were failing formatting/import-order gates
- Fix backend mypy failures that were hidden behind the earlier formatting stop, including terminal status typing, remote workspace path fallback, and TerminalTab test construction
- Relax mypy's untyped-def requirement for tests while keeping production code strict
- **Files**: terminal.py, remote.py, models/__init__.py, ttyd_manager.py, workspace_manager.py, pyproject.toml, test_tabs.py, test_ttyd_manager.py, test_workspaces.py

### feat: support image attachments in workspace tasks
- Let task creation and follow-up instructions accept pasted image attachments from the browser clipboard
- Persist image attachments under the workspace state directory, show previews in task detail, and include attachment file paths in the agent prompt
- Add backend validation for supported image types and attachment size limits, plus test coverage for pasted-image persistence
- **Files**: schemas.py, workspace_manager.py, workspaces.py, test_workspaces.py, AgentWorkspaceView.vue, workspaceStore.ts, types/index.ts

## 2026-05-14

### fix: classify Codex selection prompts as attention
- Treat Codex interactive menus with `Enter to select`, arrow-key navigation, or `Esc to cancel` as Attention instead of Working
- Keep active work detection on interrupt-oriented hints such as `Esc to interrupt` and Claude spinner status lines
- Add backend coverage for Codex selection-menu status classification
- **Files**: ttyd_manager.py, test_ttyd_manager.py

### fix: keep continued review tasks in working
- Prevent board reconciliation from restoring a stale `ready_for_review` report over a later continue transition
- Mark review tasks as Working before sending follow-up text to the agent so tmux submit verification failures cannot leave the board in Review while the agent is active
- Move review tasks back to Working when the assigned agent shows new working runtime activity after the review timestamp, covering direct terminal-tab follow-ups
- Keep `completed` reports in Review unless the task is explicitly continued, even if the terminal has later runtime activity
- Auto-send `please continue` only when an assigned Working task's idle agent shows a recognized interruption such as `API Error: 400 unknown error`
- Add backend coverage for stale review reconciliation, completed-report review stability, direct-tab runtime continuation, interrupted-idle auto-continue, normal-idle suppression, and continue-send failure ordering
- **Files**: workspace_manager.py, test_workspaces.py

### feat: archive completed workspace task records
- Write a per-workspace `task_records/{completed_at}-{task_id}.json` archive whenever a task is marked Done
- Include task/session snapshots, agent reports, an ordered timeline, changed files, validation, risks, and final summary in the archive
- Keep archived task records independent from task deletion so completed work remains reviewable after board cleanup
- **Files**: workspace_manager.py, test_workspaces.py

### fix: reopen review tasks from follow-up send
- Route follow-up sends on review tasks through the task continue API so the board moves the task back to Working immediately
- Preserve generic session sends for non-review tasks
- **Files**: AgentWorkspaceView.vue

### feat: show workspace agent runtime cards
- Add a visible current-workspace agent status strip to Agent Workspace, matching the terminal status panel's dot and pill language
- Show each agent's role/type, runtime text, detail, current task, queued count, target, and quick-open action
- Poll terminal agent status while the workspace view is mounted so the cards reflect live terminal state
- Keep the agent status strip horizontally scrollable on mobile
- **Files**: AgentWorkspaceView.vue

## 2026-05-13

### feat: add remote tab launch support
- Add Local/Remote run targets to the new-tab modal, including remote server selection, remote working directory input and browsing, auto-reconnect, and mobile-friendly scrolling
- Discover remote profiles from `~/.claude_hub/remote_profiles.json` and SSH config `Host` aliases
- Add a remote filesystem listing API over SSH so remote working directories can be browsed before launch
- Launch remote tabs through the local ttyd/tmux layer into SSH, prefer remote tmux persistence when available, and fall back to direct agent startup when remote tmux is missing
- Bootstrap common NVM Node paths before starting Claude or Codex so Merlin machines with non-login shell PATH differences can still find agent CLIs
- Preserve local tab behavior while persisting and duplicating remote launch configuration
- Add backend coverage for remote command construction and shell compatibility
- **Files**: schemas.py, remote_profiles.py, remote.py, tabs.py, ttyd_manager.py, test_ttyd_manager.py, TabBar.vue, types/index.ts

## 2026-05-12

### c3e0c64 feat: color tab indicator dot by agent runtime status
- Bind the per-tab indicator dot to agent status from the store: idle green, working yellow, attention purple, offline gray
- Add a soft glow on working and attention so active or waiting tabs are easier to spot
- Reuse the palette from AgentStatusFloatingPanel for consistency
- **Files**: TabBar.vue

### 3a48945 fix: stop agent status panel from flickering between working and attention
- Replace broad substring scans over the last 18 lines with anchored checks on the bottom 5 lines so historical scrollback no longer drives classification
- Strip ANSI escapes before matching and hashing so cursor blinks stop churning the activity hash; remove the "hash changed → working" heuristic that was the main flicker source
- Drop the `bypass permissions` attention pattern — Claude Code shows it as a permanent footer in bypass mode and was forcing every idle tab into Attention
- Tighten ATTENTION to explicit prompts (`do you want to proceed`, `(y/n)`, `[y/n]`, `press enter to continue`); WORKING keys off `esc to interrupt` / `ctrl+c to interrupt` / `esc to cancel`
- Rename ATTENTION display text to "Agent waiting for input"; IDLE remains "Idle" and is the default fallback
- **Files**: ttyd_manager.py

## 2026-04-28

### de5c9b8 fix: restore terminal cursor position after history replay
- Add tmux cursor coordinates (`cursor_x`, `cursor_y`) to the terminal history API response
- Restore xterm's cursor after initial history replay and idle history resync so the prompt cursor appears in the input line instead of the bottom row
- Add a Playwright regression test that compares xterm cursor coordinates against tmux pane coordinates
- **Files**: terminal.py, ttyd_manager.py, test_terminal_replay.py

### 7b93181 fix: stabilize terminal history while live output is streaming
- Reconcile xterm with tmux history after live output bursts go idle, restoring complete wrapped output that ttyd may skip in the live stream
- Tighten bottom-position detection so idle resync only rewrites the buffer when the user is truly at the bottom
- Preserve user history views while scrolling, including near-bottom views that show both older history and new output
- Add Playwright coverage for touch/wheel scroll alignment, wrapped live output continuity, and near-bottom resync protection
- **Files**: terminal.py, test_terminal_replay.py

### 81cb44c fix: persist tab order updates
- Persist drag-and-drop tab ordering so refreshing the web UI keeps the user's custom tab order
- Add backend coverage for saving and returning ordered tab lists
- Add `.agent_office/` to `.gitignore` for local workflow artifacts
- **Files**: .gitignore, tabs.py, test_tabs.py

## 2026-04-27

### c379b9f feat: add codex backend solo mode
- Add `AgentType.CODEX` and launch Codex tabs with the `codex` CLI by default
- Add Codex solo mode using `codex --ask-for-approval never --sandbox workspace-write`
- Extend the new-tab modal to choose Claude, Codex, or Terminal backends, with solo mode available for Claude and Codex
- Add backend tests for Codex command construction and tmux reattach behavior
- **Files**: schemas.py, ttyd_manager.py, test_ttyd_manager.py, TabBar.vue, types/index.ts

### 31af616 fix: restore ci checks after codex backend merge
- Apply black formatting to `ttyd_manager.py`
- Add the missing `MonkeyPatch` type annotation for backend mypy
- Avoid the frontend ESLint `no-undef` error from the browser `EventListener` type alias
- **Files**: ttyd_manager.py, test_ttyd_manager.py, App.vue

### 5609dbf ci: update uv setup and split replay tests
- Update GitHub Actions to use `astral-sh/setup-uv@v7` instead of the stale `0.5.x` version selector
- Keep terminal replay tests in the dedicated Playwright job and exclude them from the generic backend pytest job
- **Files**: ci.yml

### be03355 fix: stabilize terminal replay in ci
- Use full terminal replay after `term.open()` to avoid Ubuntu headless xterm scrollback loss during CI
- **Files**: terminal.py

### 40702ad fix: run codex solo mode without sandbox limits
- Change Codex solo mode to launch with `codex --ask-for-approval never --sandbox danger-full-access`
- Update the Codex solo mode UI description and command construction test
- **Files**: ttyd_manager.py, test_ttyd_manager.py, TabBar.vue

## 2026-04-26

### feat: mobile UX improvements — viewport sync, key reliability, combo keys, inertial scroll

**4 个移动端体验问题修复：**

1. **键盘弹出时视口错乱** — 添加 `visualViewport` API 监听，键盘弹出时设置 `--keyboard-height` CSS 变量，App 容器和 MobileControls 自动适配
2. **虚拟按键切换 Tab 后失效** — 添加 terminal-ready 信号（iframe→parent postMessage）+ 按 key 队列缓存，Tab 切换后自动 flush
3. **缺少组合键** — 重组虚拟键盘布局：移除 PgUp/PgDn，加入方向键到主行，新增 Ctrl+C/D/L/A/E 和 Shift+Tab 快捷按钮，Ctrl/Shift 粘滞修饰键支持 Ctrl+任意字母
4. **终端历史滚动无惯性** — 通过阅读 xterm.js 源码定位根因并修复（详见下方）

**惯性滚动修复（6 次迭代）：**

迭代过程中发现三个杀死惯性滚动的机制：
- xterm 的 `handleTouchMove` 手动设 `scrollTop += delta`（替换浏览器原生滚动，无惯性）
- xterm 的 `_innerRefresh` 每帧重置 `scrollTop = ydisp * rowHeight`（行对齐，打断惯性）
- xterm 的 `.xterm-screen` 层遮住 `.xterm-viewport`，触摸事件到不了 viewport 元素

最终修复（3 层方案）：
- CSS: `.xterm-screen { pointer-events: none }` 让触摸穿透到 `.xterm-viewport`
- JS: `term._core.viewport.handleTouchMove` → no-op，阻止 xterm 手动设 scrollTop
- JS: 拦截 `_innerRefresh`，触摸+fling 期间跳过 scrollTop 重置

关键发现：viewport 对象在 `term._core.viewport`（非 `term.viewport`），`document.body` 在脚本执行时为 null（需用 `document.documentElement`）

**改动文件：**
- `backend/claude_hub/api/terminal.py` — 注入 CSS（pointer-events, -webkit-overflow-scrolling）+ JS（触摸穿透、handleTouchMove no-op、_innerRefresh hook、terminal-ready postMessage、Ctrl+字母/Shift+Tab 编码）
- `frontend/src/App.vue` — visualViewport 同步 + `--keyboard-height` CSS 变量
- `frontend/src/components/MobileControls.vue` — 重组键盘布局 + 快捷按钮 + Ctrl/Shift 粘滞修饰 + 自动释放
- `frontend/src/components/TerminalView.vue` — terminal-ready 信号 + key 队列 + Ctrl+字母/Shift+Tab 处理

## 2026-04-25

### 75f9d1c fix: terminal history replay misalignment with Playwright E2E tests

**核心问题：** 切换 Tab 或刷新页面重连终端时，scrollback 内容丢失、可见屏幕被重复渲染、历史和实时数据交错。

**根因：**
1. `Object.defineProperty(window, 'term', ...)` 拦截器被 ttyd 的 webpack bundle 绕过 — ttyd 在打包时捕获了原生 `Object.defineProperty` 引用，我们的拦截器从未被调用，导致 `hookTerm()`、`replayHistory()` 从未执行
2. 轮询检测到 `window.term` 时，ttyd 已调用 `term.open()` 并写完可见屏幕 — 此时清除 buffer 再只写 scrollback，可见屏幕变空且无新 WS 数据填充

**修复方案 — Phase A/B 双模式回放：**
- **Phase A**（`term.open()` 未调用）：只写 scrollback + `\x1b[NS` Scroll Up 序列把底部行推入 scrollback，让 ttyd WS 填充可见屏幕
- **Phase B**（`term.element` 存在，ttyd 已写完可见屏幕）：清除整个 buffer（`\x1b[H\x1b[2J\x1b[3J`），写入完整终端内容（scrollback + 可见屏幕），丢弃缓冲中 ttyd 的 WS 数据（它是重复的可见屏幕内容）

**关键改动：**
- 用 `setInterval` 轮询替代 `Object.defineProperty` 拦截器来检测 `window.term`
- `hookTerm()` 增加 `term.element` 检查：已存在时直接调用 `replayHistory(term, true)`，否则 hook `term.open()`
- 服务端 `capture-pane` 移除 `-E -1` 参数，返回完整终端内容（scrollback + 可见屏幕）
- `capture_history()` 增加 tmux session 不存在时的空字符串提前返回（ttyd 延迟创建 session）
- 添加 `__claudeHubReplayDone` 标志供测试轮询
- 移除 `if (!historyText) return;` 提前退出（hook/resize-guard 逻辑必须始终运行）

**新增 5 个 Playwright E2E 测试：**
- `test_scrollback_complete` — 200 行历史全部出现在 xterm scrollback
- `test_bottom_rows_preserved` — scrollback 行数与 tmux 一致
- `test_no_duplicate_visible_screen` — 无重复可见屏幕内容
- `test_empty_scrollback` — 空历史时干净加载
- `test_replay_with_active_output` — 历史和实时输出不交错

**CI 修复：**
- 修复 mypy 类型错误（conftest.py 缺类型注解、read_xterm_buffer 返回 Any）
- 添加缺失的 `client` AsyncClient fixture（test_health/test_tabs 需要）
- 添加 `types-requests` dev 依赖
- 移除 CI yaml 中未安装的 `--timeout=120` 标志

**迭代过程（本分支历次提交）：**
- `1cc28ed` 移除 `-J` flag，添加 write buffer 防止历史/实时数据交错
- `9015dac` 移动端键盘弹起 3 层防抖：CSS `100lvh`、xterm `onResize` debounce、`visualViewport` 键盘状态检测
- `1c153f0` 恢复 scrollback-only 回放，修复全量回放导致的可见屏幕重复
- `53a8780` 完整重写为 Phase A/B 模型 + 5 个 Playwright E2E 测试
- `6670033` CI 修复：mypy、client fixture、pytest-timeout

**Files**: terminal.py, ttyd_manager.py, test_terminal_replay.py, conftest.py, ci.yml, pyproject.toml

## 2026-04-13

### cd1e247 fix: preserve terminal scrollback across tab switches
- Tab switching no longer loses scrollback history
- **Files**: terminal.py, ttyd_manager.py, TerminalView.vue

## 2026-04-11

### 07300a6 feat: improve tab bar scrolling experience on mobile
- **Files**: TabBar.vue

### ffaddb2 chore: standardize backend port to 8173
- Consolidate all config, docs, scripts to use port 8173
- **Files**: README.md, config.py, docker/*, docs/DEPLOYMENT.md, scripts/*

### 5ccd61b fix: make backend CI checks pass
- Fix type annotations and import issues for mypy/black/isort
- **Files**: filesystem.py, tabs.py, terminal.py, main.py, ttyd_manager.py, tests/*

### 108108c fix: stabilize frontend lint step in CI
- Fix ESLint config and dependencies for CI
- **Files**: eslint.config.js, package.json

### 5394fea fix: align backend tooling and typing with CI checks
- Add missing type annotations across auth, api, models, services
- **Files**: api/*.py, auth/*.py, models/*.py, services/*.py, pyproject.toml

### 6e2172a fix: keep tmux CI session alive for validation
- **Files**: ci.yml

## 2026-04-10

### cc7682d fix: resolve terminal text selection by disabling tmux mouse mode
- Set `tmux mouse off` — tmux mouse mode intercepted all mouse events, preventing xterm.js native text selection
- Allow browser context menu when text is selected
- **Files**: terminal.py, ttyd_manager.py, TerminalView.vue

### e3f8ab2 fix: enable text selection and copy in terminal, prevent browser context menu
- Remove interfering CSS, add context menu guard for selected text
- **Files**: terminal.py, TerminalView.vue

## 2026-04-09

### 3679463 feat: add cursor agent terminal support
- New `AgentType.CURSOR` — launches user's shell instead of `claude` CLI
- Tab creation supports `agent_type` field (claude/cursor)
- **Files**: tabs.py, schemas.py, ttyd_manager.py, TabBar.vue, terminalStore.ts, types/index.ts, vite.config.ts, start.sh

## 2026-04-02

### 7e33500 fix: support updating commented env vars in start-temp-tunnel.sh
- **Files**: scripts/start-temp-tunnel.sh

## 2026-04-01

### ec55c80 fix: improve mobile terminal scrolling by removing aggressive CSS constraints
- **Files**: TerminalView.vue

### b44ba93 feat: skip auth for local network requests
- Private IPs (10.x, 172.16-31.x, 192.168.x, loopback) bypass Feishu auth
- **Files**: auth.py, dependencies.py, config.py

### c2fd589 feat: add tab rename and fix duplicate tab
- **Files**: TabBar.vue

### df930a0 feat: add layout memory and duplicate tab features
- Persist layout choice in localStorage, add duplicate tab button
- **Files**: TabBar.vue, terminalStore.ts

### 011f481 feat: add open_id whitelist support and improve WebSocket cookie parsing
- Add `AUTH_ALLOWED_OPEN_IDS` config, manually parse WS cookie header (FastAPI Cookie decorator unreliable on WS)
- **Files**: auth.py, dependencies.py, config.py

### 900bdf7 feat: add one-click temp tunnel scripts and update vite config
- `scripts/start-temp-tunnel.sh` — start backend + frontend + Cloudflare Tunnel
- `allowedHosts: true` in Vite config for tunnel support
- **Files**: vite.config.ts, scripts/*

### fbbbd47 feat: add Cloudflare Tunnel support for public hosting
- Cloudflared setup/run scripts, config example
- **Files**: scripts/*, docs/DEPLOYMENT.md

### efc9a70 feat: merge Feishu OAuth authentication and public deployment support
- Full Feishu OAuth 2.0 integration (login/callback/logout/session)
- Email whitelist, Nginx and frp config for public deployment
- DEPLOYMENT.md documentation
- **Files**: api/auth.py, auth/*.py, config.py, models/schemas.py, api/tabs.py, api/terminal.py, docker/*, docs/DEPLOYMENT.md

## 2026-03-27

### Initial: solo mode fix
- Fix solo mode to launch `IS_SANDBOX=1 claude --dangerously-skip-permissions` correctly
- Use `bash -c` wrapper instead of `tmux send-keys`
- Add file logging to `~/.claude_hub/logs/backend.log`
