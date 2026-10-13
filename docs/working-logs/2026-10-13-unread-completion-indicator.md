# Unread completion indicator

## Outcome

An idle Chat with a completed turn that has not been opened now uses a solid
8px success badge with a compact checkmark. The badge keeps the same footprint
as an ordinary status light, while its larger translucent halo pulses beyond
that footprint so the unread result remains noticeable. The state tooltip and
accessible label read `Completed, unread`.

## Boundaries

- The backend remains the source of truth for `is_unread`; opening a Chat still
  clears the flag through the existing viewed-tab path.
- Working, needs-attention and offline indicators are unchanged. The completion
  badge applies only to `idle` plus `is_unread`.
- Reduced-motion users keep the checkmark and a static outer outline. Motion is
  reinforcement rather than the only carrier of meaning.

## Verification

- The sidebar structural regression pins the explicit label, 8px solid badge,
  compact checkmark, expanded halo pulse and reduced-motion fallback.
- Dedicated feature-server browser acceptance checks the rendered badge at
  desktop in both themes, in the narrow mobile drawer, and with reduced motion.
- The focused sidebar regression passes all 15 cases; frontend lint and type
  checks pass, as do all 775 frontend unit tests and the production build.
- The exact-toolchain mocked browser gate passes all nine scenarios.
- `./scripts/verify.sh all` passes every target except the unrelated real 7-tab
  cold-recovery timing test: three attempts took 11.41s, 10.06s and 10.43s
  against a 10s budget. Its isolated rerun passed in 9.93s.
- The separate terminal-HMR gate repeatedly stops on the existing
  `test_harness_working_claude_remount_waits_bootstrap_not_early_reveal`
  assertion because the harness does not observe its `content-pending`
  intermediate state. This change does not touch TerminalView or the harness.
- Candidate review found no remaining actionable defect after keeping the badge
  at the ordinary 8px status-light size and moving emphasis into its outer halo.
