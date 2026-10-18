# Markdown reader fixed-merge validation

> Historical first candidate: publication was blocked by a concurrent main
> update. Use the [latest-main validation](2026-10-18-markdown-reader-latest-main-validation.md)
> for the final publication guard; the checks below remain first-round evidence.

## Authorized scope and fixed objects

The user requested cross-validation and integration when checks pass. Publication
is limited to local `main`: no remote push, shared-service restart or deployment.
The integration candidate was assembled in a session-local independent Git
checkout to respect the session write boundary. The primary checkout was not
used for conflict editing.

- Merge: `00c029f22004d0caa49a3b0296505fe1c6fcc548`.
- Parent 1, refreshed local main and origin/main:
  `3ae9132df8c1d004395616537eb21a2e578545b0`.
- Parent 2, reviewed Markdown delivery:
  `c1408cff3798d8721ba6adb86ef1145d23770488`.
- Unique base: `5e3da89b1e7f76aa859021b1c74fb8d9da4408c9`.
- Merge tree: `0dcc8dbefb7b0a2937df32b732ad46175729d7d9`.
- Decision record: `2026-10-18-markdown-reader-merge-decisions.json` beside
  this log. It binds the immutable merge, not its documentation descendants.

## Both-parent preservation and independent review

The independent reviewer approved the fixed merge. The owner also recomputed
all 52 paths changed by only one parent: each merge blob matches that parent
exactly, with zero mismatches.

Four paths changed in both parents:

- `CHANGELOG.md`: the only native conflict/manual decision. Preserve both reader
  entries first, then main's history invalidation/lazy-loading entries, without
  duplication; the pre-existing tail is unchanged.
- `backend/claude_hub/api/agent_stream.py`: retain main's lazy-history parameters
  and forwarding; add both Markdown endpoint exports.
- `docs/UI_ACCEPTANCE.md`: retain lazy-history acceptance and add reader coverage.
- `frontend/tests/browser_chat_image_quality.py`: retain all Markdown/image
  assertions and main's initial folded-history expansion before accessing
  execution-derived cards.

No product code was manually changed during resolution. The temp-file grant
scanner still reads `AgentStreamStore.path`, the canonical durable event log;
it does not consume or mutate main's derived conversation-history index.
Delivered assistant text, and therefore Markdown links, remains visible while
execution details use the lazy/folded path.

## Validation on the clean fixed merge

- `./scripts/verify.sh all`: PASS.
  - Backend: **4414 passed, 9 skipped**.
  - Frontend: **1042 passed**.
  - Mypy: **538 files**, no errors.
  - Formatting, lint, Vue types, docs, checkout safety and Vite build passed.
- `./scripts/verify.sh browser-mocks`: **14 scenarios passed**. All scenario
  processes exited zero, context/browser/driver cleanup completed, and owned
  Vite port 5384 was closed. The merged Chat history and standalone reader
  flows were exercised with mocked APIs, not a shared production backend.
- Independent combined focused checks: backend Markdown/history **112 passed**;
  frontend Markdown/history/folding **121 passed**.
- `./scripts/verify.sh merge-decisions --merge <merge> --record <record>`:
  one decision, complete coverage. This is paired with independent semantic
  review; the structural checker alone does not establish correctness.
- Both-parent diff checks and AGENTS/CLAUDE byte identity passed.

Evidence root:
`/home/tiger/claude_hub_workspace/markdown-preview/merge-validation/`.
The final logs are `verify-all.log`, `browser-mocks.log`, and
`merge-decisions-check.log`. Browser lifecycle/report evidence is under
`evidence/claude-hub-browser-mocks.qqx4zl/run/`.

## Publication guard and remaining boundaries

Before local integration, recheck that main is clean and still names parent 1;
refuse if it advanced. The final local update must be fast-forward-only to the
verified merge plus its reviewed documentation-only record commit. New files
must not overwrite pre-existing untracked or ignored user content.

No shared merge hook was present when checked. No dependency installation,
frontend build copy, or service restart was performed in the primary checkout.
The running UI therefore still needs a separately authorized deployment.
Dedicated terminal HMR/replay, live provider and Feishu checks were not run;
those flows were not changed. Browser descendant exit beyond the owned handles
is not claimed by the lifecycle runner. The documented live-path and linear
transcript-scan limitations for temporary Markdown files remain unchanged.
