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

## Independent cross-validation follow-up

A fresh review of commit `b38e3ac` found a later delivery window: the tailer
validated intent before calling the transport, but Claude subsequently awaited
provider environment preparation. A Stop, configuration change or conversation
switch during that await still allowed the continuation to reach the provider.
An independent probe reproduced all three cases without invoking a provider.

The strict resume requirement now carries its synchronous admission callback
through provider preparation. Claude/Cursor recheck after environment selection
and old-process teardown, then again after subprocess creation before writing
stdin. If that final check rejects, the empty process is terminated. Codex and
TraeX check before their turn/start write; MASO checks after stream readiness and
before marking uncertain delivery or initiating its message POST. The callback
is cleared when the automatic attempt ends, preserving later manual input.
An explicit Retry can replace an idle runtime while startup is awaiting; the
same validator also fences tailer/transport ownership, so the pending automatic
message cannot move to that unguarded replacement.
Once input has been written or an HTTP message attempt initiated, existing
cancellation and uncertain-delivery controls apply; automatic replay is still
disabled.

Regression coverage exercises actual Claude/Cursor input paths across environment,
teardown and spawn awaits, with positive controls and Stop/configuration/
conversation/shutdown changes. Only external process I/O is replaced; Cursor's
separate store proof is stubbed in this lifecycle test. Codex RPC and MASO mocked
HTTP tests cover revocation during startup/stream readiness and manual follow-up.

The earlier HMR failure is retained alongside the unchanged-code full rerun.
Independent source review identifies a possible observer sampling window, but
without a timing trace this is neither a proven root cause nor proof that the
failure predates this change. Live provider execution and shared-service restart
remain outside this validation.
