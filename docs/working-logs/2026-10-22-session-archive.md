# Managed Chat session archive and restore

## Contract

Managed Chat archive retains the ManagedSession, event JSONL, attachments,
delivery receipts and verified provider conversation id. It stops the provider
and archives the headless navigation tab. Archived records are unavailable for
new messages, dispatch, reuse, resident execution and reviewer selection.
Unarchive restores availability and attempts runtime startup; provider failures
are logged and a subsequent open/input can retry normal cold startup. It does
not restore cleared resident/dispatcher pointers or re-enable resident automation.

Workspace API (authenticated, following existing workspace controls):

- `POST /api/workspaces/{workspace_id}/sessions/{session_id}/archive`
- `POST /api/workspaces/{workspace_id}/sessions/{session_id}/unarchive`
- `GET /api/workspaces/{workspace_id}/board?include_archived=true`

Both mutation endpoints return the redacted session. Unknown or cross-workspace
ids return 404; unsupported terminal kind, active task bindings and lifecycle
conflicts return 409. Repeating the requested archive state is a no-op. Terminal
sessions must use tab archive/unarchive; direct managed Chat tab mutation still
returns 409. `DELETE /api/workspaces/sessions/{session_id}` remains permanent,
including for archived records. Archived stream events are readable without
starting a native transport.

## Ownership and failure handling

Session lifecycle owns archive state (`archived`, `archived_at`, `updated_at`).
Old persisted records default to active. The workspace mutation lock precedes
the session pump lock, matching deletion and mailbox admission. Archive/restore
runs in shielded lifecycle cleanup so request cancellation cannot release those
locks halfway through runtime retirement or state commit.

Archive first fences native owners and validates delivery evidence with the same
rules used by deletion. It archives the backing tab and stops managed stream
owners without discarding evidence. Only then does it persist the archived
session and clear resident/dispatcher pointers. Provider shutdown can capture a
conversation id; the final session update uses the current record after stopping.
Failed archive retains the owner paused and needing attention for inspection or
retry. A failed persistence operation can leave the tab archived while the
session remains quarantined; retry archive completes the transition.

The stream fence survives archive until durable restore. A successful archive
also persists `archive_provider_fenced` when a runtime fence was verified.
Session and workspace deletion can use this evidence after a cold backend
restart, only while the session remains archived and unable to start a provider.
Restore clears the evidence before admitting a new runtime. Delivery validation
still rejects uncertain MASO receipts, even with a verified fence; a missing
proof never becomes a guessed fence. Legacy records default to no proof.

The process-local fence survives archive until durable restore. Runtime admission also
checks the current persisted archive flag, including after a backend restart.
Restore commits the available session before releasing the fence and attempting
provider startup. Stream history reads use the persisted store for archived
records, avoiding provider launch.

## Manual verification after a future deploy

Use a disposable, idle managed Chat with at least one completed turn; record its
session and workspace ids and verified provider conversation id.

1. Run `claude-hub session archive SESSION --workspace-id WORKSPACE` twice.
2. Verify ordinary `session list WORKSPACE` omits it and
   `session list WORKSPACE --include-archived` shows it. `session status SESSION`
   still finds the retained record. Chat history is checked through the stream
   endpoint in the next step; `session logs` reads terminal output.
3. Read `/api/workspaces/sessions/SESSION/stream/events`; verify saved messages
   remain, and confirm the provider process/tailer has stopped.
4. Run `session unarchive SESSION --workspace-id WORKSPACE` twice. Confirm it is
   listed normally, the provider id is unchanged, and a new message continues
   the conversation. Provider startup may fail independently; inspect logs if so.
5. Attempt archive while a disposable task is queued/working/in review and
   confirm 409 without losing ownership. Verify direct managed tab archive is 409.
6. If permanent removal is desired for this disposable session only, archive and
   then restart a disposable isolated backend and run `session delete SESSION`;
   confirm its record and structured history vanish. Never restart the shared Hub
   merely for this verification.

No deployment, merge or shared runtime mutation is performed by this change.

## Validation

Focused API/session/CLI tests: 116 passed. Native transport archive/restore test:
1 passed. Full repository checks and independent review results are recorded in
the final Task report. Canonical formatting uses Black/isort. Supplemental
Ruff check passes with the repository wildcard-import/re-export diagnostics
(F405/F401 and the existing E402 import-order exception) excluded; unfiltered Ruff already reports these on the base. Ruff
format also differs from the base Black formatting in three touched files.
These existing convention differences are retained rather than reformatting
unrelated code. Tests use isolated runtime homes/state and fake providers;
no live provider continuity or future deployment is claimed. No lessons needed.


## Review corrections

Managed review reproduced a cold-restart gap: successful archive retained MASO
delivery receipts while its verified fence existed only in process memory. The
new persisted fence evidence fixes archive/reload/delete without bypassing
uncertain-delivery validation. Regression coverage includes session and workspace
deletion after serialized reload, missing/stale proof, uncertain receipts and
proof clearing on restore.

The two history tests now use pytest-managed async execution instead of
`asyncio.run`, which left the main thread without an event loop and broke a
later synchronous transport test. Both session/stream/CLI file orders and the
reviewer's minimal order reproducer are rerun; final counts are in the Task report.
