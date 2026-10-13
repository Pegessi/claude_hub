# MASO HTTP integration cross-validation

## Candidate, ownership and isolation

User requested independent cross-validation of the delivered MASO integration.
Parent Task: `a4e26637-f749-4ec7-90fc-997793489da1` (already completed).
Cross-validation Task: `dfd6ed5e-520f-4e1e-89ec-a7d66490c67c`, explicitly
initiator-controlled, with configured reviewed policy retained. No Workspace
dispatch or execution handoff. Task credentials stayed in the protected default
file and were not delegated.

Candidate base: `55f25318e1b3baef2ee4e797ad418e3427223c00`.
Branch: `codex/maso-cross-validation`; worktree:
`~/claude_hub_worktree/maso-cross-validation`. Root was the only writer. Two
independent native reviewers checked installed MASO lifecycle sources and Hub
API/persistence boundaries, reproduced findings, then re-reviewed the fixes.
Neither reviewer mutated services or shared state.

Initial checkout setup accidentally fast-forwarded primary main to the candidate
because the command retained the primary cwd after creating the linked worktree.
This was reported immediately and restored with `git reset --keep` to
`86f75f72bd1c0a5d01855bb3214cd76cc8455e97`. The unrelated dirty `start.sh` remained
unchanged (SHA256 `c48d836cb583388ef10c18f6e9b10fce80b31145420365ccb4b45e24cba7f35e`).
Work then continued exclusively in the feature checkout. Final primary state
was verified at that original SHA with only the same `start.sh` modification.
There was no push, primary service restart or intended main integration.

## Reproduced findings and fixes

1. MASO sets its registry IDLE before a deferred cancellation finalizer emits
   root `agent_end`. Status/indicator polling accepted a new run too early;
   the old end could finish the new Hub turn. Stop now removes the owned queued
   UUID, aborts, awaits session `exit`, and confirms live resume before reuse.
   Installed `Runtime.despawn` waits for finalizers before unregistering the
   root; a fresh registration of the same ID cannot succeed before that point.
   Failed exit/resume retains the cleanup guard and delivery sidecar.
2. Cancelled SSE readers prevented adapter terminal cleanup. A new epoch now
   prunes old text/reasoning states rather than retaining every cancelled turn.
3. PUT could change the displayed cwd while MASO continued in the original
   directory. Changing a MASO cwd is rejected before any tab mutation.
4. Initial Plan was accepted despite unavailable capabilities. Creation and
   transport now reject it before remote execution. Validation is on the create
   request, so an older persisted Plan record remains readable and accurately
   serialized while its unsupported execution stays refused.
5. An omitted service address followed a changed backend default after restart.
   Creation freezes the resolved address in tab env. Model/env changes preserve
   it for omitted or empty values. Verified IDs without a frozen endpoint are
   refused rather than rebound to a guessed service.
6. A stale tool confirmation after Stop fell through as a new JSON prompt.
   MASO rejects answers without a matching pending confirmation. Terminal edges
   durably retire cards; orphan recovery reconstructs only its turn/epoch's
   unresolved cards from paginated durable events before closing them.

Re-review also reproduced and fixed three boundaries in the initial fixes:
legacy Plan response validation, empty endpoint persistence, and cold orphan
card recovery. Both reviewers approved the final source with no remaining
actionable finding. Independent pagination reproduction exceeded 500 events.

Source-exact/inert evidence remains under `/tmp/maso-source-repro.bl7SMr`,
`/tmp/maso-exit-fence-review.0IBy1C`,
`/tmp/maso-final-boundary-review-lywwk7bq`, and
`/tmp/maso-final-lifecycle-review-sg_qmxmc`. These are bounded local reproduction
artifacts, not assertions that live network failures were induced.

## Real browser, Hub backend and MASO acceptance

The root ran an actual Vue page on owned Vite port 5297, an actual FastAPI Hub
on 18197, and the installed MASO 0.7.3 HTTP service on 9796. Page APIs and streams
were not mocked. Before imports, the backend received private Hub home/state
paths, a unique `tmux -L` socket, a private ttyd port range and an instance ID.
Every Vite API target pointed to the isolated backend. Health identity was
checked before/after to prove the primary Hub instance did not change.

The successful run demonstrated:

- Ordinary UI creation of MASO Chat and discovery of eight catalog groups.
- Streamed real response, persistent opaque session ID and browser display.
- Public API cwd change 400, initial Plan 422 and stale answer 400 without a
  new turn or history mutation.
- UI High selection applying `seed-stable-reasoning-high` in real MASO status;
  the next response retained an earlier unique marker.
- Stop of an actual running MASO run, exactly one cancelled Hub completion,
  then a completed follow-up with the same SID and preserved marker.
- Manual condensation emitting both progress events, creating archive sequence
  1 and rewriting persisted context; the next model response retained the
  marker and the Hub display-event prefix was identical.
  This small case had eight blocks before and after; no token-reduction claim.
- Restart of only the isolated Hub backend, deliberately changing its global
  service default to an invalid address. The tab's frozen endpoint and SID,
  existing display history and marker in a subsequent response survived.
- Desktop/mobile screenshots, reachable model controls and zero page errors.

Successful evidence:
`.agent_office/maso-cross-validation/live-3/{summary.json,lifecycle.json,events.json,desktop.png,mobile.png}`.
The task-owned runner is `.agent_office/maso-cross-validation/live_chain.py`;
its explicit command was:

```bash
backend/.venv/bin/python .agent_office/maso-cross-validation/live_chain.py \
  http://127.0.0.1:5297 .agent_office/maso-cross-validation/live-3 \
  /home/tiger/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome
```

Two earlier harness attempts failed due to harness assumptions, not hidden
passing results: it checked Send enabled before filling empty text; then it
expected a nonexistent `history.is_condensing` field. Both failed attempts and
their evidence were retained. The final runner instead verifies actual archives
and persisted context changes. Browser context/browser/driver cleanup passed in
all attempts; owned backend/Vite ports closed and the primary instance stayed
unchanged. Created MASO sessions were exited and archived, and the unique task
tmux server was closed. No shared worktree or unrelated state was removed.

## Repository checks and limits

- 21 focused MASO regressions passed (7 additions to the original 14).
- Initial strict `./scripts/verify.sh all` passed 3274 backend tests (7 existing
  skips), 775 frontend tests and every other target except backend-types. Mypy
  found an `Any` return after the new status reassignment; an explicit validated
  status dictionary annotation fixes that error. Initial evidence is retained
  in `.agent_office/maso-cross-validation/verify-all.log`; final full rerun is
  recorded separately in `verify-all-final.log`. The final strict all exited
  zero: every standard target passed, 3274 backend tests/7 existing skips and
  775 frontend tests. Backend evidence: `/tmp/claude-hub-verify.JLZPXg`.
- `./scripts/verify.sh browser-mocks`: all nine runners passed;
  `/tmp/claude-hub-browser-mocks.xy05Wp/run/summary.json` confirms cleanup and
  owned Vite port 5298 closed.
- Staged-index, document invariants, `git diff --check` and byte equality of
  unchanged AGENTS.md/CLAUDE.md passed before commit.

Declared Python 3.11.16, Node 24.21.0, pnpm 9.15.9, uv 0.12.15 and Playwright
1.58.0 were used. A task-local ignored pnpm wrapper prevents the root checkout's
different Corepack default from changing verification's declared version.

Real model calls covered seed-stable and its High variant. Other catalog groups,
automatic threshold-triggered long-context condensation, real interactive tool
confirmation, and real network loss were not exercised; manual condensation and
the relevant mocked/inert failure contracts were exercised. Terminal HMR/replay
E2E were not run for this Chat-only change. Final delivery is the feature branch,
not deployment or integration into main.
