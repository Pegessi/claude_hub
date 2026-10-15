# Color remaining quota by available share

The provider quota menu previously gave every `within` row the same green bar,
so a model with 39% remaining looked as healthy as one with 97% remaining.
The requested change makes the available share visible at a glance.

Branch: `codex/quota-colors`; base: local `main` at `a89dd78`.
The primary checkout was clean and origin was fetched. Local and remote main
already diverged; this change preserves both and does not integrate unrelated
changes or restart the primary Hub.

## Display rules

- Above 50% remaining: green; above 20% through 50%: amber; above 10%
  through 20%: orange; 10% or less: red.
- The number, remaining-share bar and row accent use the same severity.
  The accent remains visible when an exhausted bar has zero width.
- Provider `near` and `over` states retain at least low and critical severity,
  respectively. These presentation colors do not rewrite provider status or
  enforcement behavior. Missing readings and uncapped limits remain neutral.
- Numeric text uses tabular figures. Prepaid credit drops insignificant decimal
  zeroes as a string, preserving precision and the unknown-unit notice.
- Critical text uses the theme's danger-text color: the ordinary light-theme
  danger color measured only 4.42:1 against the row background.

## Validation and review

- Independent read-only review found no actionable issues; quota unit tests:
  52 passed. The reviewer also checked the final danger-text correction.
- Frontend lint, types, 838 unit tests and bundle build passed with Node
  24.21.0 and pnpm 9.15.9. The existing bundle-size warning remains.
- Standard verification backend formatting/types passed; backend default scope:
  3396 passed, 7 skipped. Python 3.11.16, uv 0.12.15.
- `browser-mocks`: all registered scenarios passed, with lifecycle cleanup
  and the owned Vite port closed. Summary:
  `/tmp/claude-hub-browser-mocks.dQOFWo/run/summary.json`.
- `terminal-hmr`: 12 passed, no skips; JUnit evidence:
  `/tmp/claude-hub-verify.J1Jq4L/junit.xml`.
- Dedicated Vite on port 5297 rendered the real QuotaMenu and App theme styles
  against a mocked quota API. Dark/light at 420px and 320px showed all four
  colors, neutral missing readings, no text overlap or horizontal overflow,
  and numeric text contrast of at least 4.5:1. Screenshots were inspected.
  The owned preview server was stopped; ports 5297 and 5298 were closed.

Screenshots, the visual check, fixture page and verification logs are retained
locally in `/home/tiger/claude_hub_workspace/quota-colors/`. Browser checks do
not establish live provider connectivity. The browser owner records full
process-tree exit as unverified; its context/browser/driver cleanup and owned
server exit passed. No production deployment is part of this branch delivery.
