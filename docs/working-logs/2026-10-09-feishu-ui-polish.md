# Feishu UI polish

## Scope and release boundary

This change aligns the existing Feishu Bot controls with Claude Hub's compact
dark interface. It is developed in
`~/claude_hub_worktree/feishu-ui-polish` on `codex/feishu-ui-polish`. No backend
contract, deployment, primary service restart, main merge, or push is part of
this work.

## Chat status and pairing

The structured Chat no longer reserves a large inline area for Feishu status. A
32 px icon button sits in the Chat's upper-right corner and uses a small state
dot for disconnected, pending, confirmation-required, connected, and error
states. The accessible label and tooltip retain the full status text.

Opening the icon shows a compact connection popover beneath it. The existing
Bot selection, one-time code, callback URL, manual confirmation word, connected
state, and disconnect flow remain unchanged. The popover uses the shared
`ch-btn`, `ch-input`, and `ch-select` primitives and expands to touch-sized
controls on narrow viewports.

## Bot management

The management dialog now uses a stable header and footer around a two-pane
desktop layout:

- The left sidebar shows compact Bot rows, availability, occupancy, and the
  selected state.
- The right pane separates callback information, metadata, credential
  replacement, and destructive operations into clear sections.
- Adding a Bot has a dedicated form and an explicit empty state.
- Status, source, occupancy, configured credentials, notifications, and danger
  actions use the existing Hub color and spacing tokens.
- The icon button announces its current connection state; selected Bot rows and
  configured/missing credential chips expose the same state without relying on
  color alone.

Below 700 px, the Bot list becomes bounded horizontal navigation above a
single-column detail/form view. Explicit `min-width: 0` constraints prevent the
horizontal list from expanding the dialog beyond the viewport. The Encrypt Key
hint is connected with `aria-describedby` while the control keeps a stable
accessible label.

## Verification

The focused component/store/composable Node suite passed 44 tests. Scoped
ESLint with zero warnings and `vue-tsc --noEmit` passed.

The real Vue components were exercised in Chromium on the owned Vite port 5296
with all API and WebSocket traffic mocked. The flow covered Bot creation, secret
replacement, close/remount cleanup, environment credentials, occupancy, pairing
code generation, manual confirmation, activation, disconnect, and desktop/mobile
geometry. It passed with ten expected API calls, no unknown or cross-origin
requests, and no page errors. Screenshots and the machine-readable report are in
`/tmp/feishu-ui-polish-browser`.

The first browser run exposed an accessible-name regression from the new Encrypt
Key hint. After that was fixed, geometry checks exposed a real 562 px mobile
sidebar inside the 374 px dialog; the bounded Grid fix is included here.

The complete frontend suite then passed 700 unit tests, ESLint with zero
warnings, `vue-tsc --noEmit`, and the production build. The build retains the
repository's non-blocking warning for a minified chunk above 500 kB. Browser
mocks are not a live Feishu authorization, callback, or provider acceptance
test.

An immutable-object review of candidate `bd7c12d1` found no business-state or
API-contract changes. Its one actionable accessibility finding was fixed by
adding non-color connection, selection, and credential-state semantics; the
post-fix candidate was revalidated before commit.
