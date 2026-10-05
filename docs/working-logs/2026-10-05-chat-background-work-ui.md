# Chat background work UI

## Scope and design

Builds on `codex/agent-os-integration` (`87df43d`) and the accompanying Chat work
bridge API. `StructuredPane` now includes a compact `ChatWorkPanel` outside the
conversation log and composer. A durable monitor occupies one card across its
checks. Task and monitor results, reported validation/source IDs, and bounded
execution history are disclosed on demand. Review remains visibly pending;
worker output does not imply task acceptance.

`useChatWork` consumes `GET /api/tabs/{tab_id}/work` and
`PATCH /api/tabs/{tab_id}/work/{work_id}`. Agent-side creation uses the bridge
CLI; the frontend does not independently construct tasks or sessions.

Monitor controls pause future checks, resume/retry, stop future work/request
worker interruption, and change the interval (minimum 60 seconds). Stop is
labeled **Stop requested**: the card explicitly notes that an external operation
may still finish. Task work also supports stop/retry, without inventing a new
review acceptance flow. Reported validation is identified as source evidence,
not independently verified truth.

## Reading and notification lifecycle

- Visible active Chat: one reader, 15-second polling while work is active,
  60-second polling otherwise, plus immediate refresh at Chat turn boundaries.
- Hidden document or deactivated cached pane: no background work polling.
  Reopening resumes an authoritative read. Backend execution is independent.
- Polls are aborted on deactivation/tab switch; response versions and captured
  source tabs prevent stale results overwriting a mutation or a different Chat.
- Only one mutation is offered at a time. A lost response causes reconciliation;
  if reconciliation also fails, controls stay disabled until status is refreshed.
- Meaningful observed results (anomaly/completed/decision/failed) reuse the
  existing toast queue. New results in one read are grouped into one notification.
  Progress never notifies. Dedupe uses bounded opaque report/task identities in
  browser storage, with an in-memory fallback and cross-pane merging.
- **Limit:** inactive/hidden Chats receive their result notification on reopening;
  this change adds no app-wide polling bus or OS push notifications.
- Checks/results never append messages or provider turns to the Chat transcript.

## Validation

- Focused executable tests cover stale reads, overlapping activation/mutations,
  tab switching, deactivation, lost-response reconciliation, failure disabling,
  progress suppression, bounded dedupe, reload and cross-pane dedupe.
- `pnpm lint:check`, `pnpm build` (including TypeScript), and frontend unit tests.
  Vite retains the existing advisory about chunks larger than 500 kB.
- Full-app Playwright contract smoke runs against the dedicated worktree Vite
  server at `127.0.0.1:5287`. Both API proxy targets point to `127.0.0.1:9`; every
  API request is intercepted before navigation, preventing contact with live Hub.
  Desktop and mobile check pause/resume/stop/interval controls, source report
  disclosure, pending review, Chat switches, preserved composer draft, reload,
  no transcript injection, and no horizontal overflow/composer obstruction.
- Reproducible harness: `frontend/tests/browser_chat_work.py`. Artifacts from
  development: `/tmp/claude-hub-chat-work-ui/{desktop.png,mobile.png,report.json}`.
- This browser evidence uses mocked API responses; real backend/provider
  execution and backend disk persistence require integration verification.

The task-owned Vite server is stopped before branch delivery. Main services,
tmux, and live Hub state are untouched. The integration owner maintains the
combined changelog and performs independent cross-layer review.
