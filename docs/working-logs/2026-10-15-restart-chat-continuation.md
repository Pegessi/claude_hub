# Planned restart Chat continuation

The UI restart used to cancel working native Chat turns and leave every user
to send another message after reconnecting. The restart dialog now offers
automatic continuation, checked by default. Every eligible Chat receives the
same literal `Please continue.` in its verified original provider conversation,
including tasks written in Chinese. Older API clients omit the new flag and
retain the existing manual behavior.

## Ownership and delivery contract

The existing launcher restart mailbox stores the opt-in and per-Chat attempts;
there is no additional job runner. Capture happens during actual shutdown,
after new input is fenced, rather than while a potentially lengthy build is
still preparing. A captured turn must still be working, directly user-owned,
and free of a pending approval, question or capacity wait. Real terminals,
Goals, Workspace-managed sessions and scheduled/Bot work keep their existing
recovery owner. Idle, archived, plan-mode and unverified Chats are excluded.

Recovery checks launcher identity, a five-minute deadline, configuration digest,
provider conversation identity and the durable original turn's interruption
and cancellation. Stop or newer user input retires the attempt. Input admission
checks cover lazy managers, asynchronous validation and turn publication during
shutdown. Original prompts and attachments are never replayed.

A durable claim precedes provider delivery. This guarantees at most one
automatic attempt, not exactly-once provider execution: acceptance and local
storage cannot be committed atomically. An interrupted or uncertain claim is
reported for manual attention and never retried by a subsequent process.
Codex strict resume cannot fall back to a blank thread. Cursor's constructive
resume additionally requires exactly one matching provider database under the
effective workspace and environment; metadata-only or ambiguous stores fail
closed. MASO's delivery uncertainty remains under its existing controls.

## UI and evidence boundaries

Service recovery and continuation counts are reported separately. A lost POST
response is observed under its original request ID rather than sent again.
Chat observation tolerates transient failures for about five minutes and keeps
drafts, while permanent client errors fail promptly. Observation never invokes
provider replacement or replays input.

Backend tests use the actual mailbox, event store, tailer and provider base
class with in-memory provider delivery. A separate test runs the actual launcher
and app lifespan across backend processes in a private runtime and port, with
provider/tmux/Bot activity stubbed. It verifies one fixed continuation in the
same conversation, no message to waiting/idle Chats, and no replay after another
process starts. Strict resume, Stop races, configuration changes, stale claims,
malformed records and shutdown dispatch are exercised independently.

The registered service-restart browser scenario drives the real Vue/Pinia Chat
page on desktop and mobile with mocked APIs, including a lost restart response,
an observation outage, pending results, fixed request identity, preserved draft
and opt-out. A negative control replacing the observation retry budget with its
old value fails specifically because the recovered reply never appears.
These checks establish neither live provider connectivity nor deployment of
this feature. The shared Hub is never restarted for validation.
