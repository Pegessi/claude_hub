# Unified attachment preview: local main integration

## Fixed inputs

- Local main parent: `c1f5ddb6ea6ddf8bc5132e2e701567be13678fbe`.
- Attachment candidate: `831e2af808a328bc386a515197dcdaf175a5a88b`, including
  feature `7732df0` and the reviewed navigation/layout/lifecycle fixes.
- Merge base: `44e68edf89ce953fcc85b9dbd745dc2e129177aa`.
- Fixed merge: `e9aa1a118439749e27d758792c6710d2523361ea`.
- Merge tree: `7e02c2e1c47c8e0128ed6df393d0f91b83951d49`.

Main had gained Dot execution-backend switching and delivery/selection ownership
fixes while the attachment work was under review. The integration retains both
sides. `CHANGELOG.md` was the sole native conflict: both attachment entries and
the full Dot entry were retained without editing their contents. All other
parent-owned paths retain the corresponding parent's exact Git object, including
the deleted old image-lightbox component.

The fixed merge decision record binds the exact merge and Git version. It
covers the Changelog resolution, not later commits or semantic correctness.
Independent frontend and backend reviewers checked both parent deltas and
merged consumers. Their verdicts are PASS: Dot replacement respects newer
selection, preview cleanup remains singly owned, and fresh Dot session identities
do not inherit old attachment working-directory or temporary-reference grants.

## Browser-test correction after the fixed merge

The first complete browser run passed 13/14 scenarios. The mobile scroll check
failed because its reference was sampled before Playwright's locator-click
preparation: passive instrumentation recorded scrollTop **440** across stable
frames, then **78 before pointerdown**, still **78 while the dialog was open**,
and **78 after Back/focus restoration**. There was no application scrollTop
setter in that interval. A follow-up hit test showed the closing mobile sidebar
still covering the link center at 440; locator click preparation moved the link
to a hittable position. The original <=2px assertion was comparing against a
position already superseded before the user's click reached the application.

The following test-only change waits for a checked visible, unobstructed hit
point and uses real mouse input after sampling the reference. It avoids locator auto-scrolling after the
reference is recorded. It neither forces clicks nor relaxes the <=2px limit.
The production code remains the reviewed fixed-merge tree; this later test/doc
delta is separately reviewed and validated.

Evidence root: `/home/tiger/claude_hub_workspace/unified-attachments/integration/`.
The passive trace is `browser-diagnosis/run2/mobile-scroll-390.json`; earlier
failed runs remain available and are not counted as passing checks.

## Validation and delivery

The separately reviewed test correction is commit
`1c27fd03233432efd02a80e8e65b6df563a8cbdc`. Its product sources are identical to
the fixed merge; only the browser scenario changed. Both complete targeted
scenario reruns passed, and passive measurement confirmed identical reference
and pointerdown positions (desktop 463/463, mobile 440/440).

Final checks on that frozen delivery commit:

- `scripts/verify.sh all`: PASS. Backend **4649 passed, 9 skipped**; frontend
  **1060 passed**; documentation, committed checkout safety, backend formatting
  and types, frontend lint/types and bundle build passed.
- `scripts/verify.sh browser-mocks`: **14/14 passed**, every runner exited zero,
  owned Vite port stopped accepting connections. Summary:
  `claude-hub-browser-mocks.ou6emt/run/summary.json`.
- Independent fixed-merge backend review: PASS, including **181** focused tests
  and route/OpenAPI checks. Frontend review: PASS, including **85** targeted
  lifecycle/history/store/Dot tests. The subsequent test-only correction was
  independently approved without weakening the original assertion.
- `merge-decisions` for `e9aa1a1`: PASS, one decision with complete coverage.
  Its adjacent JSON record was generated from the immutable merge identities.
- Final logs: `verify-delivery.log`, `browser-delivery.log`,
  `merge-decisions-check.log`, `frontend-review/`, `backend-review/`.

The initial runtime-only virtual environment was missing test dependencies;
those provenance failures collected no tests and are not success evidence. The
integration-only environment was repaired using the locked offline dev extra.
The initial 13/14 browser failure and all diagnosis artifacts remain retained.

These final documentation/decision-record additions do not change production
sources or test logic and are checked separately. Browser checks use real Vue
and Chromium with mocked APIs; they do not prove live provider, OAuth, physical
Safari/iOS or shared-service deployment. Terminal HMR/replay and performance
suites were not run. The existing App bundle-size warning remains.

This operation authorizes local main integration only. It does not push to the
remote, change production build artifacts, install the main environment, or
restart the shared Hub.
