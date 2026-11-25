# Feishu realtime background triage

Realtime WebSocket intake already owns a bounded 256-item queue, a delivery lock,
a durable replay journal, and cancellation at shutdown. Keep those ownership
boundaries and replace the urgent Dot event sink with an ephemeral Workspace Chat
turn. The router also caps concurrent diagnoses at two for other callers. Normal
production intake remains serial within its background worker; Dot's user Chat
and `/ai` command execution do not wait for that worker.

The existing patrol Workspace lifecycle supplies completion subscription,
approval cancellation, timeouts, stale-session recovery, archive retry and
quarantine. Sessions are caller-owned ephemeral Chat workers with bootstrap off.
CLI providers enter Plan mode before receiving the prompt. MASO has no Hub Plan
mode: its read-only behavior uses explicit prompt instructions and stops when
approval is required. This is an instruction contract, not an OS sandbox.

The agent returns `skip` or `notify`, a summary and an optional reply draft.
Suppressed results have durable ingress receipts. Notifications use Dot's bound
Bot, then store normal-severity external events with summary/draft payloads.
These events cannot trigger a wake and are included in a later user-turn context.
The next-context projection is bounded; full drafts remain in the inbox record.
Owner replies are checked again after diagnosis. Pending decisions and confirmed
notification state share Dot's existing ingress SQLite file so passive-write
retries reuse the decision and do not resend confirmed cards. A process crash
between external send acknowledgement and recording it can still repeat a card;
the external transport has no durable idempotency contract here.

Execution selectors reuse the `/ai` live model catalog and logical-model/effort
mapping. Empty model selects the installation's advertised preferred model,
then a flash/fast high-reasoning option, then an available CLI provider. No
installation model identifiers are embedded. Explicit empty reasoning effort
survives round trips for providers whose intensity is encoded by model choice.

The legacy twenty-minute poll, polling CLI/API controls and InfoWall write path
are removed. Existing poll fields and Feishu poll history are ignored on disk
load and omitted on save. Daily 11:00 and 17:30 digest controls remain.

Validation covers all four triggers, skip/notify behavior, passive context,
provider fallback and logical IDs, success/malformed output/cancellation cleanup,
notification retry, owner-reply rechecks and settings persistence. Mocked browser
coverage exercises selector saves/readback, narrow layouts and surviving Dot
controls. Full verification results and candidate SHA are recorded in the Task
report. Live provider execution and external Feishu delivery are not acceptance
evidence from these isolated tests; no shared services or runtime files changed.
