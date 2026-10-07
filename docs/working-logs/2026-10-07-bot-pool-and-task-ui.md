# Bot pool and unified Task UI

## Scope and release boundary

This continues the Task/CLI and native-subagent work described in
[the v2 implementation log](2026-10-07-agent-workflow-v2.md). Development and
verification use `~/claude_hub_worktree/agent-workflow-v2` on
`feat/agent-workflow-v2`, after `f4dea66` and `5d8a2ed`.

The candidate does not replace the production checkout. No main merge, push,
service restart, real provider invocation, public OAuth login, or live Feishu
callback was performed for this change. The last successfully verified remote
baseline remains the one recorded in the earlier log; this work does not claim
a fresh remote-main synchronization.

## Task presentation and execution responsibility

There is still one WorkspaceTask collection. The Workspace board and details
show its creation source, execution responsibility, explicit progress, and
runtime observations. Source is descriptive; it is not an authorization rule.

- The existing manual form defaults to Workspace-managed execution. Creating a
  record does not press Start or create a reviewer.
- Initiator-managed records offer explicit progress and handoff controls, not
  managed Start, review, abort, terminal-send, or cleanup controls. Backend
  ownership checks remain authoritative.
- The Task details component is keyed by Workspace and Task identity. Its own
  asynchronous handlers also verify the captured target before changing local
  state, so correctness does not depend only on remounting.
- A failed or unconfirmed progress/handoff request retains its original call ID
  and payload. Reopening the component offers explicit confirmation; it never
  automatically replays a write. Conditional deletion prevents an old response
  from removing a newer pending request.
- Reporter credentials are associated with Task source and execution epoch.
  A later-epoch replay receipt does not make an old key valid again. A successful
  write and a failed board refresh are separate outcomes.

### Task creation and capabilities

A modern create request is persisted before POST, with its request key and,
when applicable, reporter key. Pending requests are immutable until explicitly
discarded. A confirmed create whose local credential save fails offers local
storage retry/copy rather than another create POST.

Capabilities have explicit `idle`, `loading`, `supported`, `unsupported`, and
`error` states. Only an actual 404 enables the old Workspace-only create shape.
Loading, network failures, malformed responses, and 5xx responses do not disable
idempotency by silently choosing that old shape. Both the button and submit
handler enforce this rule. Saved modern requests cannot be sent as old requests.

Capability reads check request generation, abort state, and Workspace identity
both after fetch and after JSON parsing. A stale Workspace caller cannot abort
the active Workspace's request. Modern create receipts must contain valid
execution-control, epoch, and progress-revision fields; old-server receipts keep
their older compatibility contract.

### Legacy Chat work

Legacy Chat work remains readable and can be explicitly stopped. Its UI and
runtime guard reject pause, resume, interval edits, extra mutation fields, and
writes to already-terminal records. Each history card opens its own Workspace;
it does not guess from the first card or the last selected Workspace. Existing
history fields and result notifications are retained. New Tasks belong in the
unified Workspace view.

## Shared Feishu Bot pool

The instance has one shared Bot pool under existing Hub access control, without
a separate Bot-administrator workflow. Stored Bots have immutable App IDs and
write-only credentials. Environment credentials remain read-only, with an
explicit invalid-environment state rather than mixing partial sources.

One Bot can bind one Chat, and one Chat can bind one Bot. Occupied resources are
not silently taken over. Pairing is two-step: an authorized Hub operator obtains
a code, sends it in a Feishu direct conversation, and enters the confirmation
word returned to that conversation. The browser cannot retrieve or prefill that
word. Web OAuth identity and Bot-app sender identity are not assumed to share
an `open_id` namespace.

Current routes:

| Method | Path | Purpose |
| --- | --- | --- |
| GET / POST | `/api/feishu/bot/bots` | Read the safe pool / add a stored Bot |
| PATCH / DELETE | `/api/feishu/bot/bots/{id}` | Edit metadata / delete |
| PUT | `/api/feishu/bot/bots/{id}/secrets` | Replace write-only credentials |
| POST | `/api/feishu/bot/bots/{id}/pair/start` | Issue a pairing code |
| POST | `/api/feishu/bot/bots/{id}/pair/activate` | Confirm a claimed pairing |
| DELETE | `/api/feishu/bot/bots/{id}/pairing` | Release binding and pending pairing state |
| POST | `/api/feishu/bot/events/{id}` | Receive that Bot's callbacks |

The old single-Bot configuration and binding routes return 410. The old
`/events` callback route is environment-only compatibility, not an alias for an
arbitrary pool entry. The frontend and backend must be deployed together.

### Persistence and callback admission

- Pool files, revisions, binding generations, and tombstones are authoritative.
  Stored migration preserves credentials and revocation, not old bindings or
  pairing codes. Old deduplication records are migrated separately so replayed
  delivery does not acquire a new target after migration.
- Invalid UTF-8, unsupported versions, malformed/non-finite/out-of-range numeric
  state, and damaged state fail closed without overwriting the original file.
- Environment App ID collisions do not steal a stored Bot's identity. Removing
  a conflict or restoring an older environment value does not resurrect a
  revoked binding.
- Bot-specific publication gates serialize current-configuration/binding checks
  with bounded outbound replies. Tokens are obtained outside the publication
  gate. Uncertain delivery is not retried through a second reply path.
- Callback admission is rechecked after subscription/Goal-lock waits, directly
  around native submission. The internal guard is not an HTTP request field and
  is not held for the entire model-output wait.
- Verified `event.message.create_time` is preserved as Unix milliseconds. A
  message created before the current binding's activation cannot enter the new
  Chat, even when it is being delivered for the first time. Header event time is
  not a substitute for message time. Rejected old messages retain dedup records.

The time check requires normally synchronized server and Feishu UTC clocks. It
uses exact millisecond/float-ratio comparisons without rounding activation down
or adding a grace window. It can conservatively reject a boundary message and
does not detect every possible unobserved clock rollback. A request already
submitted to the old Chat is not migrated or resent.

The retired singleton configuration store and binding store were removed after
mapping their applicable tests to the new pool, protocol/client, and admission
suites. The old administrator variable is reported as deprecated, not used as a
second authorization layer.

## Bot UI behavior

The global dialog lists Bot names and occupancy; details contain configuration,
metadata, and destructive controls. Each Chat selects a Bot and completes its
own pairing. Configured credentials are not a claim of provider connectivity.

- All writes use the selected draft revision, not a newer shared-store revision
  that happened to arrive while the operator was editing. This also applies to
  secret replacement and delete confirmation.
- Successful creates clear secret drafts using explicit operation success, not
  a status-message string. Selection, close, and unmount invalidate obsolete
  local updates. Closing does not cancel a server operation already submitted.
- Clipboard status has a separate error/status channel and generation. A late
  clipboard error cannot replace the explanation for a failed Bot mutation.
- Polling cannot abort an in-flight pairing mutation or leave its busy flag set.
  Mutation completion resumes polling only for the current component target.
  Switching Bot, Chat, or claim clears the previous input as appropriate.
- Shared-pool reconciliation makes at most two serial reads when responses race.
  It does not recursively await its own Promise or retry without a bound.
- Safe fixed messages distinguish capacity exhaustion, a moved or missing Chat,
  read-only environment credentials, revision conflicts, and ordinary failures.

## Verification

Verification reused the existing locked frontend dependencies, Python 3.12
verification environment, and existing Chromium. There were no downloads.
Backend processes received private HOME/XDG/runtime/tmp paths. Browser checks
used owned numeric-loopback Vite ports, process/socket ownership checks, mocked
API responses, blocked WebSockets/cross-origin traffic, and explicit cleanup.
They did not start a Hub backend or a provider.

| Scope | Result | Evidence |
| --- | --- | --- |
| New Bot pool, persistence, migration, client/protocol, admission, security | 330 passed; all 124 backend source files passed mypy | `/tmp/claude-hub-v2-bot-retired.WAfWOI` |
| Task helpers and actual Pinia capabilities/create receipts | 40 passed; scoped lint/type checks passed | `/tmp/claude-hub-v2-create-receipts.jADDpX` |
| Real Task details component | PASS; 3 progress + 3 handoff requests, no unknown writes or page errors; epoch-specific key survived remount | `/tmp/claude-hub-v2-panel-browser.wmeea8z4` |
| Real Workspace view | PASS; 7 create requests; malformed receipt recovery, local key-save recovery, stale-Workspace replies, managed-action gates, and capability submission guards | `/tmp/claude-hub-v2-parent-browser.uhdf_0x4` |
| Bot request/store/composable/SFC behavior | 39 passed before four additional specific-error-message cases | `/tmp/claude-hub-v2-bot-ui-complete.VjwF7O` |
| Full frontend | 711 passed; ESLint with zero warnings, type check, and production build passed | `/tmp/claude-hub-v2-frontend-release.HYkrSJ` |
| Real Bot components, desktop/mobile | PASS; 10 mocked API calls, no unknown/cross-origin requests or page errors; compact rows and viewport bounds checked | `/tmp/claude-hub-v2-bot-browser.8y38n26k` |
| Shared Chat stream/native/attachment compatibility plus protocol/admission | 384 passed | `/tmp/claude-hub-v2-stream-compat.gQ5d4V` |
| Documentation and formatting | 13 docs-contract tests passed; Black/isort checks passed | `/tmp/claude-hub-v2-doc-format.GHenbT` |

The scopes overlap and must not be added together as unique tests. Browser
fixtures simulate server results; they are not OAuth, real Webhook, or provider
acceptance. Settings setup tests use a real Vue renderer with no DOM template;
they supplement rather than replace browser checks. The final owned Vite ports
36813 (Task panel), 38015 (Workspace view), and 47231 (Bot components) were
verified closed; browser/controller exit codes and cleanup records are retained.
The production build still reports a non-blocking chunk-size warning above
500 kB. No attempt was made to hide or raise that warning threshold.

Regression evidence includes three delayed-first-delivery failures before the
message-time fix and a real SFC test that reproduced clipboard failure replacing
a mutation error before the independent clipboard channel was added. Earlier
failed runs remain in their own artifact directories. An initial full-frontend
command found no `pnpm` executable; verification uses the exact package-script
commands via the existing local binaries instead of installing dependencies.

Independent reviews used immutable Git objects. The Task reviewer covered
ownership UI, pending recovery, capability loading, and modern receipts. Bot
reviews covered persistence/admission, UI/API contracts, and the applied
asynchronous-state fixes. Reviewer reports are not additional test executions.

## Remaining acceptance boundaries

Real model execution on this v2 candidate and real authorized OAuth/Bot round
trips remain unverified. They require their own approved account/model and
complete callback/authentication configuration. The existing production Bot
consumer must not be repurposed. The old ChatWork live harness cannot establish
acceptance of the new unified Task contract without adaptation. No token-cost,
latency, or free-form Agent delegation improvement is claimed from these tests.
