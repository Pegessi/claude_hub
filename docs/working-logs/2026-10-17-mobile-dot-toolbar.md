# Mobile Dot toolbar alignment

## Scope

- Base: `5c9c69f256a50464425d317a8cbfb42ced05894d`.
- The user's screenshot shows the compact Dot launcher higher than the other
  mobile toolbar buttons and asks to omit the Status signal summary.
- Keep the compact launcher at the shared 30px control height and center the
  mobile toolbar's children consistently. Preserve the separate Chat and memory
  targets, including their existing widths and 24px-or-larger hit areas.
- Omit aggregate Status/Agents markup on mobile while keeping their component
  instances and shared polling ownership alive. Per-tab status remains live;
  desktop summaries and polling reference counts are unchanged.
- No changes to Dot setup, provider requests, panel data, dependencies or timers.

## Workspace boundary

This session permits writes only inside its own working directory. The candidate
therefore uses a private feature repository snapshot under that directory rather
than the repository guide's external linked-worktree location. The shared primary
checkout, its branch and the running Hub are untouched. Integration is not part
of this request.

## Regression evidence

The existing Dot browser scenario now compares rendered toolbar boxes at
360/390/768px, checks both Dot buttons by ordinary clicks, retains the per-tab
status marker and checks desktop behavior at 769/1280px.

Before the fix, the 360px run failed its new geometry assertion with no page
errors or unexpected API requests:

- Dot: y=3, height=38.
- Add: y=9, height=30.
- App menu: y=11, height=30.

The failure demonstrates both the oversized compact control and the mixed
center/end alignment. The owned browser/context/driver closed, and the dedicated
Vite port no longer accepted connections after cleanup.

## Independent review correction

The first candidate removed the summary component instances on phones. Review
identified that these instances also own the shared agent-status poller, which
updates per-tab status and terminal-history consumers. Geometry-only acceptance
had not covered this dependency.

The strengthened browser test first reproduced that regression: with summaries
unmounted, the mobile tab stayed `offline` instead of receiving the fixture's
`working` status. The corrected design keeps the instances mounted and passes
`hideSummary` only to their root template guard. It does not relocate or duplicate
the poller. The test now waits for a real scheduled poll to change the mobile
indicator from `working` to `attention` while the summary DOM remains absent.

## Final validation

- `scripts/verify.sh all` with strict declared tool versions: PASS. Backend:
  4,027 passed, 10 skipped (optional UI/zsh and the clean-checkout provenance
  case); frontend: 940 passed. Formatting, types, docs and bundle build passed.
- Full `browser-mocks`: 12/12 PASS, including the strengthened Dot scenario.
  Its three mobile samples all measured Dot/Add/App-menu at y=8 and height=30;
  status advanced through a subsequent scheduled poll without summary DOM.
- The focused Dot run also passed. Both its new geometry assertion and its
  status-polling assertion were demonstrated to fail against the relevant
  defective candidate before the corresponding correction.
- Independent re-review confirmed the polling issue resolved and found no
  remaining actionable correctness issues.
- Browser/context/driver cleanup completed and the owned Vite ports closed.
  The browser owner deliberately does not claim exhaustive descendant-process
  exit verification. No live Hub/provider traffic was used.
- The build retains a >500kB chunk advisory. Terminal HMR/replay, real-device
  and live-provider checks were not run for this focused toolbar change.
- The clean-checkout provenance case is rerun after committing; its result is
  retained with the delivery evidence, not inferred from the skipped suite case.
