# Dot execution-backend editing and switching

## Scope and baseline

- User confirmed execution-backend switching (Claude/Codex/Cursor/TraeX/MASO),
  rather than only changing a Claude API endpoint or model.
- Base: `44e68edf89ce953fcc85b9dbd745dc2e129177aa`.
- Working branch: `feat/dot-provider-edit`.
- Hub Task: `96169744-bb0b-4d98-97bc-4ad343bcf044` (initiator controlled).
- Implementation lives in this session's private code copy under
  `/home/tiger/claude_hub_workspace/dot-provider-edit/repo`. The session write
  boundary takes precedence over the repository's usual linked-worktree path.
  The primary checkout and live Dot configuration have not been modified.

## Contract

Execution settings have their own editor and authenticated `/api/dot/execution`
GET/PUT endpoints. A PUT includes the observed revision and a complete replacement
environment. The response containing launch configuration is not cacheable.
Changing backend resets the browser draft to the new backend's defaults; no old
provider credentials/model/endpoint are implicitly copied. MASO uses the existing
readiness probe, and tab creation rechecks service readiness.

A changed configuration creates a fresh Dot Chat, archives the previous Chat and
keeps its display history. Dot's file-based memory, focus and tracked tasks remain
in the same home. No provider-native history or conversation id is transferred.
The two Dot-owned schedules retain their ids, enabled state, timing and custom
text, with their target changed to the new tab.

The current implementation rejects replacement during active/unfinished turns,
uncertain MASO delivery, nonterminal Goals or queued scheduled work. Feishu
bindings must be explicitly removed first and rebound afterwards; other enabled
schedules must be disabled, removed or retargeted. This feature does not rewrite
pairing generations or silently transfer unrelated automation ownership.

Setup, identity edits and execution changes share one Dot lock. Replacement and
recovery take the scheduler's per-tab lock before Goal/input admission. A
secret-free pending marker precedes retirement; final identity and marker removal
share an atomic Dot state write. Recovery restores old schedule targets, archives
the unused candidate, restores the prior tab and discards only undispatched
candidate-target schedule runs. A Git commit failure after the identity is durable
is logged as a history failure, not returned as a false failed switch.

## Provider integration

Shared native `SessionCore` prepares Dot context once under the send lock. The
provider owns conversation creation/rotation; Dot associates only the prepared
generation and owning tab. Delivery acknowledgement follows the provider's
accepted-send boundary. Failed/cancelled turns retain existing unread-event and
rotation-retry semantics.

- Claude: constructive UUID and native `--append-system-prompt`.
- Codex/TraeX: provider-assigned thread id and developer instructions.
- Cursor: constructive id and hidden prompt prefix.
- MASO: provider-assigned session id and hidden prompt prefix.

Cursor/MASO prefixes are user-input context, not a claimed native system-role
channel. No undocumented provider API fields are used.

## Review and validation record

Independent lifecycle review identified two issues: candidate-target queued
schedule runs surviving rollback, and a post-commit Git error being reported as a
failed PUT. Both have fixes and focused regression tests. Identity rename/switch
serialization was also checked and corrected.

Parent UI inspection found post-commit tab refresh errors incorrectly presented
as mutation failures, plus unsupported-current-provider draft hydration copying
env into a fallback provider. The editor now separates refresh-only retries from
PUT retries and initializes fallback providers from their own defaults. Browser
coverage includes these cases.

Intermediate verified checks (not the final combined gate):

- Dot execution/API focused tests: 33 passed before the final recovery additions.
- Frontend unit suite: 1045 passed; focused Dot/preflight suite: 43 passed.
- Frontend types, scoped lint and production build passed; existing large-chunk
  warning remains.
- Mocked real-SPA browser acceptance passed at
  `/tmp/ch-ui-dot-provider-refresh-KLHGvf/evidence`; the task-owned port 5413 was
  closed afterwards. This uses mocked APIs, not live provider inference.

Final combined validation on the stable implementation:

- `scripts/verify.sh all`: PASS. Backend 4,532 passed / 10 skipped; backend types
  covered 545 files; frontend 1,045 passed. Docs, checkout safety, formatting,
  frontend lint/types and production build passed.
- `scripts/verify.sh browser-mocks`: 14/14 scenarios passed, including the Dot
  execution editor and the existing Markdown reader. Evidence:
  `/tmp/claude-hub-browser-mocks.POYeyG/run`. Runner cleanup completed and port
  5483 was no longer accepting connections; the helper does not certify complete
  browser process-tree exit.
- `scripts/verify.sh terminal-hmr`: 12/12 passed. Evidence:
  `/tmp/claude-hub-verify.driDs8`.
- Independent lifecycle re-review: prior findings closed. Independent native
  re-review: staged association/adoption, single cold thread creation, accepted-only
  acknowledgement and repeated-cancellation cleanup passed review. The final
  allocation/status-query failure path also retains known provider identities and
  cleans or retries candidates; its independent recheck passed 4 cases. Earlier
  cleanup rechecks passed 7 cases; the final MASO test file passed 44 cases.
- The first browser gate exposed an existing immediate PageDown/scroll assertion.
  It now waits for the same `scrollTop > 0` condition on the browser frame, without
  sleeps or a weaker assertion. Independent review and the complete rerun passed.
- `terminal-replay` and real provider inference were not run. Transport tests use
  isolated mocks; they do not prove external credentials, quota or every installed
  provider version. Cursor/MASO use hidden user-input context as described above.
  Codex/TraeX offer no evidenced thread-delete RPC: a rejected provider-created
  candidate is not adopted/persisted but can remain as an unused upstream thread.

The private code branch is the deliverable. No merge, push, deployment,
primary-service restart or live Dot provider change was performed.
