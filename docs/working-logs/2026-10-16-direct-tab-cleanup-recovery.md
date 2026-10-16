# Direct-tab durable cleanup recovery

## Scope and publication boundary

The user authorized repairing the remaining direct-tab artifact-cleanup retry
limitation and integrating the validated feature into local `main`. Remote push,
deployment, shared-service restart, and shared runtime-data migration are not
part of this task. The existing initiator-controlled Task remains in epoch 3.

The repair covers the `terminal-tabs` stream namespace used by direct Chat and
ordinary terminal tabs. Deleting a managed navigation tab may clean only that
navigation namespace; this does not introduce a cross-file cleanup transaction
for Workspace sessions or whole Workspaces. Historical untracked orphan files
are not inferred to be safe to delete.

## Fixed latest-main integration

- Feature parent: `8e65ebc2f9f2aac8be22ffec9b94ff23d60c597b`.
- Incoming main: `2d14506f2e004fc5f382c8dde0361c23acadc594`.
- Merge: `f8d06c44f4dff4b4d373db89cb56c43246da44cf`.
- Only `CHANGELOG.md` required a manual decision; both parents' entries were
  retained. The native implementation and tests use the automatic merge.
- The parent and an independent reviewer each ran 215 native, Managed Chat
  runtime, and stream-retirement tests. Independent evidence is in
  `/tmp/claude-hub-verify.KrDq1o/pytest.log`.
- `2026-10-16-managed-chat-cleanup-main-decisions.json` binds the one manual
  decision to the immutable merge. Its mechanical coverage is complete;
  semantic review and later cleanup changes require separate verification.

## Approved cleanup contract

1. Strictly retire the provider/resources while the exact tab owner is still
   discoverable. A stale request must not fence a replacement incarnation.
2. Persist tab removal and its cleanup obligation together in one atomic
   `tabs.json` snapshot. Do not remove artifacts before retirement is confirmed.
3. Store minimal identities and a unique cleanup token, not provider environment
   values or arbitrary deletion paths. Derive the `terminal-tabs` paths in code.
4. Retain the obligation on partial failure, cancellation, or acknowledgment
   persistence failure. Join submitted file work before releasing its lifecycle
   reservation; acknowledge only the same token after all artifacts are gone.
5. Reserve pending IDs. Refuse cleanup if a live owner occupies the resource key,
   including a different incarnation; do not treat the old DELETE as authority
   to stop or erase that new owner.
6. Repeated DELETE retries a pending obligation. HTTP 204 means cleanup and its
   durable acknowledgment completed; HTTP 503 explains that cleanup remains
   pending. An unknown ID without an obligation retains HTTP 404.
7. Schedule bounded, strongly owned cleanup tasks during startup after the
   backend instance lock and on existing monitor ticks. A submitted disk operation
   must not block unrelated cleanup or Workspace status refresh. Reserve pending
   IDs while unrelated tabs start; join cleanup tasks before releasing lifetime
   ownership, including cancellation and startup failure. No separate daemon or
   provider startup is used for cleanup.
8. Handle replacement/directory-sync uncertainty explicitly. Every ordinary
   state writer must preserve the authoritative pending snapshot. Matching bytes
   on reread alone do not confirm durability. Corrupt or unsupported envelopes
   must not be partially loaded or overwritten with empty state.

## Storage compatibility and recovery boundary

The new reader accepts legacy tab lists. The first successful state write stores
an object with a version, live tabs, and pending cleanup obligations. Importing
modules or reading a legacy list must not itself migrate the shared state file.
Old binaries cannot safely read or rewrite the new envelope. Before a future
explicitly authorized deployment, take a consistent runtime-data backup while
writers are stopped. Downgrade requires a separate consistency-aware recovery
plan; dropping cleanup records or changing only the version is unsafe.

No shared runtime data was migrated by this code task. A pending record is
recovery evidence, not an instruction to force-delete an arbitrary directory.
An invalid ledger or a live-owner collision requires investigation without
removing the evidence to bypass the safety check.

## Strict preview cleanup

The existing preview helpers swallowed unlink/scandir/rmdir failures. A parent
counterexample retained the private preview while the helper returned success
(`/tmp/claude-hub-verify.r88d3D`). The durable path now opts into strict cleanup;
legacy callers retain their default behavior. Physical removal precedes manifest
removal, and failures preserve the cleanup obligation. Strict cleanup confirms
surviving directories and the replaced index before successful return, including
missing-path retries after an earlier sync failure.

Fifteen new attachment cases cover real filesystem failures, index replacement,
invalid manifests, symlinks, missing files, sync retries, and default behavior.
Together with 62 existing attachment tests and the strict form of the parent
counterexample, 78 checks passed (`/tmp/claude-hub-verify.cCaAxl`). The first new
fixture run exposed cross-test index sharing inside the private test runtime;
index and preview roots are now both isolated per test. Independent review and
the combined candidate gate remain separate requirements.

Independent review then reproduced an inherited fixed-temporary-file issue:
a preexisting index `.tmp` symlink caused the atomic writer to overwrite an
unrelated file before the final no-follow check failed. The writer now uses an
exclusively created unique temporary file and applies mode 0600 to its descriptor.
New symlink and hardlink regressions preserve the unrelated file. The unchanged
independent counterexample and 79 repository attachment tests passed in both the
parent run (`/tmp/claude-hub-verify.B3AdMA`) and independent run
(`/tmp/claude-hub-verify.Y25rRn`). This is not a power-loss experiment or a claim
to protect against an external same-user process racing arbitrary filesystem
changes.

## Candidate review corrections

A pending snapshot loaded by a new process may have been published before the
old process failed to sync its directory. Loading matching bytes is not proof
of that sync. Loaded obligations now require a strict rewrite/sync before replay
can remove artifacts. The failing-before regression is retained; the parent
also verified the correction (`/tmp/claude-hub-verify.kO67aK`). This contract is
about snapshot/order migration, not every legacy constructor side effect:
`TTYDProcess` construction may still materialize provider launch settings.

The initial awaited retry pass introduced a monitor-liveness regression. An
independent probe blocked a real submitted event-store unlink thread and showed
that both another cleanup record and Workspace status refresh stalled
(`/tmp/claude-hub-verify.mL1Sq8`). The correction schedules bounded per-record
tasks and retains their ownership through joined I/O and acknowledgment. Its
lifetime is inside the backend instance lock and includes startup failure and
shutdown. Final independent validation must exercise this new scheduling path;
earlier green aggregate runs do not validate it.

Storage review also reproduced a compatibility failure for explicitly configured
runtime/state roots that are symlinks. The snapshot writer, strict cache sync,
and stream cleanup must follow that trusted configuration boundary while still
rejecting or safely unlinking symlinks inside owned namespaces. The correction
captures the real configured parent/root rather than globally resolving derived
artifact paths. Strict stream cleanup also avoids the legacy best-effort seed
removal helper: an unlink error must retain the obligation, including exact known
seed temporary files. Preserved private-filesystem probes cover these boundaries.

## Final verification (revision 4)

The final source/test freeze contains eleven backend files on the fixed
`f8d06c44f4dff4b4d373db89cb56c43246da44cf` baseline. The parent verified the
implementation owner's hashes, and both independent reviewers rechecked the
same hashes after their runs. No further source changes followed those checks.

| Gate | Result | Local evidence |
| --- | --- | --- |
| Canonical `all`, strict tool versions | Pass: docs, checkout safety, format/types, backend, frontend lint/types/tests/build | `/tmp/direct-tab-cleanup-all-r4.log` |
| Default backend suite | 3,787 passed, 8 skipped | `/tmp/claude-hub-verify.WvtezL` |
| Frontend unit suite | 858 passed | Full-gate log above |
| Mock browser runners, unchanged frontend/runner files | 11 passed | `/tmp/claude-hub-browser-mocks.0CugoD/run` |
| Terminal HMR on final source | 12 passed | `/tmp/claude-hub-verify.KENPQm` |
| Independent storage, API, aliases and attachment probes/suites | 325 passed, plus 5 exact-sidecar fault probes | `/tmp/claude-hub-verify.1CkkRq`, `/tmp/claude-hub-verify.paOH5i` |
| Independent lifecycle/delta probes and suites | 107 passed | `/tmp/claude-hub-verify.WFuT8d` |

The independent lifecycle checks include real private backend-instance locks,
blocked submitted I/O, responsive monitoring, canceled DELETE waiters, and
startup-failure/shutdown with repeated cancellation. Storage checks include
real fsync/replace/unlink failures, reload recovery, ordinary-save preservation,
trusted-root aliases versus untrusted child links, and ASGI 503/204/404 behavior.
The artifact list is explicit: log, cursor, edit backup, seed and its known temp,
managed/MASO delivery sidecars and the known MASO temp, plus strict session
previews and their index entries. There is no historical orphan sweep.

The earlier 215-test baseline and intermediate full passes remain historical;
they do not replace revision-4 evidence. The fixed merge decision record is
checked separately from these later semantic changes. Only the clean committed
candidate is eligible for the authorized local-main update. Remote push,
deployment and shared-service restart remain excluded. No real-provider,
resource-savings, power-cut, or external administrator root-retarget guarantee
is claimed.
