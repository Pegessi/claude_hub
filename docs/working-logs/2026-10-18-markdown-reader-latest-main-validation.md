# Markdown reader latest-main integration validation

This supersedes the publication guard in the earlier
[main-integration log](2026-10-18-markdown-reader-main-integration.md).
The first merge and its documentation (`00c029f` / `729d2ee`) passed validation,
but were not published: the final guard detected that main had advanced from
`3ae9132` to `3f5db8b` with independently integrated Dot changes.

## Fixed second candidate

- Merge: `a3375ac1b442f671395a11662c148f1f4883157a`.
- Parent 1, validated reader/history integration and its records:
  `729d2eec7bfbc236702851611d0d4f2eb874cecf`.
- Parent 2, latest main/origin main:
  `3f5db8b7823136cac4f339805ddbbd10ecd07827`.
- Unique merge base: `3ae9132df8c1d004395616537eb21a2e578545b0`.
- Merge tree: `d9019fe2efd4f35fb6661f393fccb785ad016c90`.
- Bound record: `2026-10-18-markdown-reader-latest-main-merge-decisions.json`.

Only `CHANGELOG.md` overlapped and conflicted. Its resolution retains reader
entries first, concurrent Dot guidance/final-review entries next, and the
unchanged history entries and older tail after them. No product code required
manual resolution. Independent review approved this exact merge and record.

The owner independently recomputed preservation of all 37 single-parent paths:
zero blob mismatches. Frontend tree `51237b6370d56375d3036d67e04ce0edd07e13ee`
is byte-identical to parent 1. The Dot service tree is byte-identical to parent
2, while reader/agent-stream code remains parent 1's. Neither change rewrites
the other's cache, lock, history or task ownership.

## Validation on the clean second merge

- `./scripts/verify.sh all`: PASS.
  - Backend: **4454 passed, 9 skipped**.
  - Frontend: **1042 passed**.
  - Mypy: **540 files**, no errors.
  - Formatting, lint, Vue types, docs, checkout safety and Vite build passed.
- `./scripts/verify.sh browser-mocks`: **14 scenarios passed** again. All runner
  exits were zero, context/browser/driver cleanup completed, and owned port
  5384 was closed. This remains mocked-API UI evidence, not a live provider or
  shared production test.
- Independent combined Markdown API / Dot final-review / guidance / service
  regression: **156 passed**.
- Fixed-merge decision verification: one decision, complete coverage.
- Both-parent diff checks and AGENTS/CLAUDE identity: PASS.

Evidence is retained under
`/home/tiger/claude_hub_workspace/markdown-preview/merge-validation/`:
`verify-all-latest.log`, `browser-mocks-latest.log`, and
`merge-decisions-latest-check.log`. Final browser reports/lifecycle files are in
`evidence/claude-hub-browser-mocks.N8OrEI/run/`.

## Publication and boundaries

Publish only through a guarded fast-forward of local main while it is clean
and still names parent 2 (`3f5db8b`). Recheck incoming added paths to avoid
replacing pre-existing untracked or ignored user files. Publication includes
this documentation-only record commit, reviewed separately from the merge.
The earlier candidate guard failure made no change to the primary checkout.

Remote push and service restart/deployment are not authorized by this task.
Dependencies, builds and tests used only the session-local integration checkout;
no shared service was started/stopped or reconfigured. Dedicated terminal
HMR/replay and live provider/Feishu checks were not run. The existing temporary
Markdown live-path and linear history-scan limitations remain unchanged.
