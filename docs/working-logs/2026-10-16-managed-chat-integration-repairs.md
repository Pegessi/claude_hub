# Managed Chat integration repairs

## Scope and fixed inputs

The user requested repairs after the read-only pre-merge cross-validation found
image cache lifetime defects, test cleanup/type failures, and a latest-main
shutdown-admission integration risk.

- Feature parent: `f77a637f33fd81f935e17f32af83a6e752a917b2`.
- Main parent: `407acd8a6ab5447b04ec780460ba4d32d898f16c`.
- Merge base: `a89dd7870e256fb871ff9edb8bd94f9887479922`.
- Active checkout: `managed-workspace-chat`, branch `feat/managed-workspace-chat`.
- The older `managed-chat-main-integration` checkout remains unchanged as review
  evidence for its `15894e5`-based candidate.

This phase repairs and validates the feature checkout only. It does not update
primary `main`, push, or deploy/restart the shared Hub. The existing initiator
Task remains in execution epoch 3; delegated agents have no Task-management or
reporter-key authority.

## Required behavior

1. An image read admitted for a deleted session must not save retained bytes after
   namespace cleanup or expose them to a subsequently reused managed session ID.
   Cache publication and cleanup need a common serialization boundary, including
   cancellation and any background I/O that outlives its waiter.
2. Managed image responses must not be reused from browser cache across friendly
   session ID reuse. Prefer `no-store` over adding a new public URL identity scheme.
3. A detached direct-tab image request must not recreate a deleted cache namespace.
   Preserve the main branch's image containment, retention, source-version, MIME,
   shared quota and eviction behavior.
4. A narrowly typed pre-provider rejection may restore a managed message to a
   retryable state only when no provider input was submitted. Ordinary provider
   failures, cancellation and unknown acknowledgements remain uncertain.
5. A rejected attempt after durable `turn_started` must still retry with the same
   call ID and actually submit once; a start event is not a delivery receipt.
6. Planned restart retains conversation/startup/Stop checks. Managed sessions are
   not replayed through the direct Chat continuation path, and paused managed
   input remains paused.
7. Tests that construct a local TTYDManager must delete every test-owned tab even
   when an assertion fails. Production direct-tab versus managed-tab ownership
   must not be weakened to make cleanup easier.

## Ownership

The image/lifecycle owner resolves shared API/tailer conflicts and owns capture
admission and image tests. The restart owner owns the narrowly typed rejection
contract, bridge/mailbox changes and restart tests; shared API/tailer changes are
coordinated through the first owner. The parent owns the local resource fixture,
changelog/log integration, aggregate validation and delivery. Independent reviewers
recheck the frozen result rather than relying on the earlier feature approval.

## Implemented repairs

- Image reads capture the exact workspace/tab/creation-time owner. Cache reads,
  saves and discard recheck that owner under the shared mutation lock. Tailer
  publication also checks ownership after awaited persistence and capture.
  Cancelled readers and their late thread completions cannot restore a retired
  namespace. Successful managed image responses use `Cache-Control: no-store`.
- `NativeInputNotSubmittedError` identifies only an admission rejection before
  provider input. Both lazy API manager getters, initialized managers and
  per-tailer admission boundaries use it. The bridge removes only the rejected
  attempt; the existing mailbox path restores the same envelope and call ID to
  pending. Generic exceptions and cancellation retain uncertain-delivery evidence.
- Late rejection after persisted `turn_started` is terminalized locally and remains
  retryable. Start events do not become acceptance receipts. Independent restart
  restoration with a fresh WorkspaceManager proves the same call ID reaches the
  provider once after the rejected attempt submitted zero inputs.
- Type corrections preserve the actual module singleton and model contracts.
  Restart fixtures match the revised quiesce order. The local TTYDManager fixture
  finalizes every owned tab even when an assertion fails.

## Frozen validation

Production and test files were frozen before final validation. The 726-file
manifest is `/tmp/managed-chat-integration-repair-candidate.json`; its initial
staged tree was `bd632aeec66c3c37ede39b04f0a127fb511480fb`. Subsequent edits are
limited to documentation: this evidence log, the changelog and the immutable
merge-decision record. All four
native conflicts have been resolved in the index, not just in working files.

| Check | Result | Retained evidence |
| --- | --- | --- |
| Canonical `verify.sh all` | PASS: backend 3669 passed, 8 skipped; frontend 858 passed; formatting, type checks, lint and build passed | `/tmp/managed-chat-integration-repair-all.log` |
| Backend type provenance | PASS: 274 source files | `/tmp/ch-verify-tools.sZHPGP` |
| Canonical browser scenarios | PASS: 11 runners, including managed questions and restart/draft handling | `/tmp/managed-chat-f77-407-browser-mocks/summary.json` |
| Terminal HMR with isolated backend/browser | PASS: 12 tests | `/tmp/managed-chat-integration-repair-hmr.log`, `/tmp/claude-hub-verify.61NVoS` |
| Lifecycle/image independent regression | PASS: 177 image/attachment/managed tests and 421 session/stream/native/CLI tests; no actionable finding | Independent lifecycle review |
| Final lazy API admission and restart composition | PASS: 139 tests, including the real bridge/API path without replacing `_manager` | `/tmp/claude-hub-verify.hHK5GB` |
| Independent final retry invariants and mailbox | PASS: 18 and 23 tests | `/tmp/claude-hub-verify.Vfu0mT`, `/tmp/claude-hub-verify.NDQ6Xq` |
| Fresh-manager persisted late-rejection retry | PASS: 1 independent test, zero first-attempt provider inputs and exactly one retry input | `/tmp/claude-hub-verify.F7wrMT` |
| Real fixture resource cleanup | PASS, including intentional assertion failure; no matching owned ttyd remained | `/tmp/claude-hub-verify.fYRZoa`, `/tmp/managed-chat-resource-failure-finalizer.log` |
| Index checks | PASS: no unmerged paths, `git diff --cached --check`, `verify.sh check-staged` | Final staged candidate |

Independent behavioral probes also passed: allocator-reused managed IDs returned
404 for both the stale request and replacement lookup without old bytes or residual
cache; direct-tab deletion returned stale 404 with zero cache rows; cancelled
blocked reads left zero rows/residual after their background thread completed.
The parent separately reproduced managed allocator reuse and no-store behavior.

The independent lifecycle and restart reviewers found no remaining actionable
correctness issue in their reviewed scope. The lifecycle reviewer also pinned the
fully staged candidate and checked its reviewed hashes. The fixed merge SHA and
manual-resolution explanations belong in the accompanying decision record,
committed after that merge so it can bind an immutable object.

## Limits and delivery boundary

Browser scenario APIs are mocked. Retry probes use an in-memory provider while
exercising actual persistence, mailbox, bridge, API and tailer paths. No live
Claude/Codex/Cursor/MASO provider or shared-service restart was exercised, and no
resource-savings claim is made. Existing test deprecation warnings and the frontend
bundle-size warning remain non-failing. The separate terminal-replay/performance
and manual real-provider acceptance targets were not run in this repair phase.

This is validated feature-branch delivery. Primary `main` stays at `407acd8`; no
push, deployment or shared-service change is authorized by this record. Updating
`main` remains on hold for the user's confirmation after this repaired candidate
is delivered.
