# Shared Chat message sources for Feishu and Web

## Scope

Feishu Bot and the Web composer use the same existing local direct Chat tab and
provider session. This change records the entry point on each accepted user turn;
it does not create a second conversation or change the existing administrator
identity boundary. Bot setup still requires an independently authorized Feishu
application and a real compatible Web login.

## Data flow

- The Web send and edit-resend routes supply `metadata.origin = "web"` on the
  server. The request schema does not let a browser assert Feishu provenance.
- A validated Bot callback supplies `origin = "feishu"`, its `app_id`, `chat_id`,
  original `message_id`, and `sender_open_id`. These values belong to that turn,
  not to a mutable "last recipient" or the current binding panel.
- The native transport receives an explicit source description. `visible_text`
  preserves the original message in `turn_started.payload.summary`, while
  `turn_started.payload.metadata` retains the source for history replay.
- The timeline distinguishes `web`, `feishu`, and legacy `unknown`. Only an
  explicit Feishu source gets a message badge. A Feishu-looking turn ID does not
  establish provenance, and moving or removing a binding does not relabel history.
- Replies use the original message's Feishu reply endpoint. Binding authorization
  and identity are still rechecked before sending a completed response.

## Edit-resend and compatibility

`turn_source.py` defines the immutable `feishu-v1` provider-text format. Historical
metadata records that version so edit-resend can locate the original provider
input without consulting the current binding. Editing it from the Web produces a
new Web turn. The existing four-field `AgentStreamStore.find_turn()` result is
preserved; the new metadata-aware reader is used by edit-resend.

The separate Chat workflow integration must classify explicit human origins when
collecting correction evidence. Rejecting every turn with metadata would exclude
these Web and Feishu messages. That compatibility change is tracked and tested on
the workflow branch before the two features are combined.

## Current limits

- Only the existing supported p2p text / local direct Chat binding scope is covered.
- Busy Chat input is rejected with an accurate "not executed" response. This
  first stage does not add a shared server-side input queue.
- Bot input cannot answer a pending Goal/provider approval question in this stage.
  It must not accidentally consume an existing question and then wait for a new
  turn that was never created. The Web question path is unchanged.
- Display uses the reliable sender open ID; friendly sender/Bot names are not
  inferred or fetched with additional permissions.
- No real Feishu OAuth, live Bot callback, or real cross-entry provider turn has
  been validated by this stage. Synthetic callbacks are not OAuth evidence.

## Validation in the canonical worktree

Base: `ab31fb6a28360eb65b978b8508186b8ed9f44b7e`, containing current main `c37b7f3`
and the prior Bot implementation. Validation uses task-owned homes, state, caches,
and tmux configuration, with existing dependencies and no new downloads.

- Backend: 171 tests passed across Feishu Bot, public URL, agent stream, and
  edit-resend recovery suites; six changed production modules passed mypy.
- Frontend: 618 unit tests, ESLint, TypeScript, and production build passed.
- Built-SPA browser smoke: explicit existing Chromium, all API responses mocked,
  WebSockets blocked, external HTTP(S) blocked. Desktop/mobile rendering, reload,
  binding movement, and unbinding retained exactly one Feishu message badge and
  left Web/legacy messages unmarked. No page errors were observed.
- The dedicated loopback static review server was stopped; its PID was absent and
  its port was no longer listening afterward. The shared Hub was not restarted.

Local evidence: `/tmp/claude-hub-takeover-tests.KvXCMa/`, especially
`feishu-provenance-first`, `feishu-backend-types`, `feishu-frontend-validation`, and
`feishu-browser/run-1`. Browser smoke is UI evidence only, not backend or provider
execution acceptance. Independent review and broader integration remain required.
