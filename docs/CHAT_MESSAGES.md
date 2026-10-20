# Ordinary Chat tab message inbox

`claude-hub tab message TAB_ID --message "Please review the patch" --call-id review-42`
queues one ordinary user turn in an existing native Chat conversation. No schedule,
Task, managed session or new provider conversation is created. Repeat the same
call ID and exact payload to recover the same receipt; conflicting content is 409.

Use `--message-file FILE` or `--message-file -` (stdin) instead of `--message` for
multiline text. IDs generated when omitted are printed to stderr before network
I/O. `--source-tab-id SOURCE` is an **unverified caller hint**, never proof of an
agent's identity. `--json` prints receipts. Enqueue success means intent is stored,
not that the model has completed it.

```bash
claude-hub --json tab message TARGET --message-file request.txt --call-id request-42
claude-hub --json tab message-status TARGET MESSAGE_ID
claude-hub --json tab message-cancel TARGET MESSAGE_ID
```

Authenticated REST endpoints:

- POST `/api/workspaces/tabs/{tab_id}/messages`: strict body
  `{message,call_id,source_tab_id?}`, durable 202 receipt.
- GET at the collection: this actor's receipts; GET `/{message_id}`: one receipt.
- DELETE `/{message_id}`: cancel queued/waiting only; dispatching, accepted and
  uncertain require explicit reconciliation or normal Chat controls, not deletion.

Native top-level Chat targets only. Missing/non-native/managed/archived targets
are rejected; archive after acceptance holds queued messages. Busy and active or
blocked Goals wait; the inbox never steers, answers a provider approval or resumes
Goal work. Separate UI/browser drafts and other ingress keep their own ordering;
FIFO is guaranteed within this inbox, not across ingress channels.

Message size: 64 KiB UTF-8; call ID: 256 characters; backlog: 100 messages or
1 MiB pending text per target. Overload is 429. One JSON store
`CLAUDE_HUB_HOME/chat_messages.json` is atomically replaced and fsynced under the
application's single-instance runtime ownership. Full receipts expire after 30
days (410), but idempotency tombstones are retained indefinitely to prevent old
arbitrary caller IDs becoming executable again. Body text and optional source
hints are removed from expired terminal records.

State is queued/waiting/dispatching/accepted/completed/failed/cancelled/uncertain.
Possible provider submission is durably marked before native I/O. Provider
acknowledgement and persisted model completion are distinct. Ambiguous delivery
blocks later inbox messages and **never automatically replays**. After a crash,
only provably not-submitted intent can retry. Started/accepted turns without
terminal proof become uncertain; an existing live turn can remain owned. Lost
accepted runtime ownership becomes uncertain after a 60-second evidence grace.
A genuine persisted terminal event resolves uncertainty. There is intentionally
no force-retry endpoint: review the target conversation before creating a new
intent; uncertain old intent continues to hold the inbox until terminal evidence
is recovered. Exactly-once provider execution is not promised.

The native transcript labels turns “Hub message” with authenticated display actor.
Caller source hints stay explicitly unverified. The inbox panel displays receipt
states without fabricating user transcript lines. No assistant text is relayed to
a source Chat or Feishu. Current local-network auth remains instance-wide shared
trust; it is not per-agent or cross-user isolation. External whitelist membership
is rechecked at submission; no new ACL or credential is introduced.

Enqueue/completion wake delivery immediately. A three-second lifecycle-owned
redrain also notices Goal release, unarchive and cold-runtime readiness, without
the scheduler. Each tab has one independently tracked submission task: a stalled
provider acknowledgement in one tab cannot block another tab's delivery or
periodic recovery. Shutdown quiesces new submissions and waits owned submission;
a stuck provider may delay shutdown. Corrupt inbox state disables inbox delivery
without taking down unrelated Chat service. No manual state-file editing is a
supported recovery operation.


## Scheduled entry mode

Scheduled `chat_turn` occurrences use this same inbox coordinator, per-tab FIFO,
native before-delivery fence and recovery core. The timer owns occurrence UUID5
identity and explicit supersede/disable/delete policy; distinct direct intents
are never coalesced. Scheduled metadata remains `origin=scheduled_task` and Dot
system turns retain `yields_to_user`. Public message bodies cannot request these
internal capabilities. Existing scheduler run APIs remain a history projection.

Scheduled uncertainty remains terminal history after the existing 10-minute
stale grace / 60-second liveness recheck, while direct uncertainty still blocks
its FIFO until reliable evidence resolves it. Operator cancellation of a running
scheduled occurrence releases its scheduler slot without killing the provider;
public message cancellation remains unsent-only. One-shot timer exhaustion does
not cancel its pending occurrence; an explicit disable does.

On first load, existing scheduled records are imported idempotently using their
original turn IDs. Existing persisted occurrences are retained even when their
combined backlog with direct messages exceeds the shared new-intent cap; new
admissions wait for space. Scheduled message sizes retain their existing contract;
public direct-message byte limits remain unchanged. No deployment is performed
by this implementation.
