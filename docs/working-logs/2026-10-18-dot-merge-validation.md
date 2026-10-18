# Dot integration into current main

## Fixed candidate

The user authorized integration into local main. Remote push and shared-service
restart remain outside this operation.

- Dot feature parent: `7ea051712db0f55eb1c0b58719447ec4a5ac8c10`.
- Current main parent: `3ae9132df8c1d004395616537eb21a2e578545b0`.
- Merge base: `5e3da89b1e7f76aa859021b1c74fb8d9da4408c9`.
- Validated merge: `8036b54f6c3275e54c4ffe723db30c56191dc0e6`.

Main added lazy Chat history loading while the Dot branch was under review. The
only textual conflict was `CHANGELOG.md`; both complete release-note blocks were
retained and the common history left unchanged. The
[fixed-merge decision record](2026-10-18-dot-merge-decisions.json) covers that
single path. This check establishes decision coverage, not semantic correctness.

## Verification and independent review

Strict `verify.sh all` exited 0 on the clean fixed merge:

- Backend: 4369 passed, 9 skipped in 220.52s.
- Frontend: 1037 passed, no failures or skips.
- Mypy: 537 source files, no issues.
- Backend format, frontend lint/types/build, docs and checkout safety passed.
- Merge-decision record: one decision, complete coverage.

An independent integration review passed, with 51 targeted tests passing. It
verified that Dot journal sync still reads raw events with `read_since` and does
not consume the new summary/detail projections. Raw completion, user summary,
quiet reply and cursor contracts remain available. Provider pack injection and
inbox acknowledgment retain their original paths.

Git tree identities were checked independently by the owner and reviewer: the Dot
service tree matches the feature parent; agent-stream and frontend trees match
the main parent. No runtime conflict resolution or unreviewed transplant was
introduced. Dependencies and declared test tool versions did not change.

Evidence:

- `/tmp/ch-dot-main-integration/all.log` and `all.exit`.
- `/tmp/dot-merge-integration-review-2b62/pytest.log`.
- The committed fixed-merge decision record linked above.

## Delivery boundary

This evidence commit changes documentation only after the validated merge. Before
fast-forwarding main, both checkouts and the target main SHA are rechecked.

No provider turn, Feishu notification, Dot rotation, schedule update, cleanup or
shared-service restart was performed. The browser/terminal explicit suites were
not rerun for this merge; the main-side Chat feature's prior browser evidence is
recorded in its own integration log. This merge makes no new live latency or
notification-delivery claim. Existing user-owned Dot role files and active
provider-session snapshots keep the activation boundaries documented in
[Dot guidance](../DOT.md#guidance-updates).
