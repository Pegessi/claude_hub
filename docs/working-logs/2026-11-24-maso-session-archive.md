# MASO session archive lifecycle

## Outcome and ownership

The Hub lifecycle previously stopped its local stream/tab without archiving the
corresponding remote MASO runtime. This change keeps remote history but mirrors
local retirement through POST /api/sessions/{id}/archive with an explicit
archived boolean. It does not enumerate or bulk-clean remote sessions.

maso_session_archive.py holds only derived identity snapshots and finite
best-effort jobs. The existing terminal and Workspace managers remain the local
authorities. Successful writes notify their archive source; startup only records
the baseline. Workspace-only writes scope their notification to that workspace.
This covers removal paths that pop a temporary reviewer before deleting its tab,
without duplicating cleanup in each caller.

| Entry | Authoritative commit / remote identity |
| --- | --- |
| Tab archive, restore, delete; CLI and UI remove | Terminal manager committed snapshot |
| Subagent close, strict-review close, orphan cleanup | Same direct-Tab lifecycle |
| Scouting temporary model turn | Workspace session archive |
| Heterogeneous ephemeral reviewer cleanup | Workspace session removal, including deferred Tab deletion |
| Caller-owned ephemeral Task cleanup | Workspace session deletion |
| Workspace removal | Committed removal of its session records |
| Dot backend switch / rollback | Old/candidate Tab archive and previous Tab restore |
| MASO rotation, uncertain-delivery recovery, Dot rotate/tidy | Explicit successful adoption-journal finalization |

Managed Chat provider IDs are owned by Workspace sessions, not by their
potentially stale backing tabs. A confirmed provider ID and a pinned endpoint
are required; a Hub UUID or the current global service default is not evidence.

## Ordering and safety

Commit-derived intent authorizes archive=true. Current *and* committed live
references veto retirement, keyed by normalized endpoint plus opaque session ID.
An in-memory archive flag while stop is pending is not a committed retirement.
An owner-scoped adoption journal protects its old ID even if the new identity
already reached the manager, including after a cold restart.

Requests for an identity are serialized and coalesced. Restore can supersede a
queued archive, and a live reference appearing during an archive requests a
serialized restore afterwards. Absence of a live reference alone must never
turn an in-flight restore into an archive. Jobs have a two-second total request
deadline, ignore environment proxies, sanitize failure logs and do not delay
local Tab/Task transitions. Backend ownership cancels/joins jobs on shutdown.

The MASO adoption journal remains responsible for identity and seed recovery.
Old-session retirement runs only after identity, seed and journal removal
succeed. Unchanged/constructive IDs are not retired. Existing unadopted rotation
candidate exit/cleanup remains unchanged. First allocation now uses the same
journal: failed ownership persistence blocks prompt submission and preserves
the confirmed allocation for recovery. If even the journal cannot be written,
only that confirmed unowned allocation is best-effort archived. MASO persistence
callbacks propagate errors and must write before mutating a shared session object.
If an owner is archived/deleted without retrying failed first adoption, its
committed lifecycle edge captures the journal-confirmed allocation before
artifact cleanup. Unverified local records remain in the derived owner map for
this purpose; their constructive IDs never become remote request targets.
Restore uses the same journal-confirmed candidate, even after the archive
request has finished and native reopening is unavailable. Permanent removal
also captures the journal's old ID only when its verification flag is exactly
true. Resumable archive does not release the old-ID guard; another live owner
can still veto either retired identity after permanent removal.

## Validation and limits

The focused lifecycle sweep initially passed 583 tests. Independent adversarial
review then found two failure-path gaps. Four deterministic regressions were
observed failing before the fixes: in-flight restore versus uncommitted archive,
in-flight restore versus failed adoption, shared-owner retirement versus failed
adoption, and first-allocation persistence failure. The corrected MASO and archive
suites passed 120 tests. The final focused file contains 50 passing regressions,
including journal-only first-allocation teardown through registered stream owners.
The full backend suite passed 7,445 tests (10 skipped), and frontend tests passed
1,549 tests. The initial all-target run exposed two archive-journal parsing type
annotations; those were corrected without changing behavior. Final gate evidence
and the inherited public-sanitization limitation are recorded in the Task report.

Tests exercise the real local managers and persistence, with mocked HTTP for all
remote archive calls. They include API archive/restore/delete, remote timeout and
HTTP failure tolerance, cross-owner sharing, endpoint isolation, temporary
scouting/review cleanup, Dot switch/rotation/tidy, and adoption recovery.

No running provider service or historical remote session was accessed. The
documented MASO Runtime-unload contract is not an observed production RSS result.
Archive delivery is best effort, not a durable outbox: shutdown, request ambiguity
and remote outage may leave a session needing a later lifecycle action. Legacy
identity records or journals without proof of remote identity are not guessed.
No periodic sweeper, alerting, main-branch integration or push is included.

## Final-review corrections

The configured independent final reviewer found two missing transitions on the
first frozen candidate: fully drained journal-only archive followed by restore,
and permanent deletion after failed adoption finalization. All four reviewer
probes were independently reproduced as failures before the fixes.

The correction shares explicit archive/restore intent construction and only
widens old-ID retirement on permanent removal with positive verification.
An additional 18 repository regressions cover direct/managed owners, empty and
constructive first IDs, fully completed archive requests, old/new shared owners,
retained resumable journals, and false/missing/non-boolean old-ID verification.
The original 50 lifecycle cases, 18 added cases and all four external reviewer
probes pass together (72 cases). No native reopen, new model turn or weak
deletion fence is used to mask a missing remote request. Updated full-gate and
committed-candidate evidence is retained in the Task report.
