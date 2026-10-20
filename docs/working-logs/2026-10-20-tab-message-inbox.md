# Durable ordinary Chat messages

Implemented Option C from the workspace investigation, on a local feature branch
only. Ordinary Chat tabs now accept text inbox intents through authenticated
POST `/api/workspaces/tabs/{tab_id}/messages`, with actor-scoped receipt GET/list
and unsent-only DELETE. `tab message`, `message-status` and `message-cancel` wrap
these APIs. See [contract and examples](../CHAT_MESSAGES.md).

The inbox atomically fsyncs an independent runtime JSON store and directory before
acknowledging intent, keeps immutable fingerprints per principal/incarnation/key,
and records possible provider submission before native I/O. Unknown execution is
uncertain, never a silent replay. The per-tab FIFO waits for native/Goal ownership;
question-answer interpretation is explicitly disabled for inbox text. A completion
observer cannot prevent Goal/schedule completion if inbox persistence fails.

Frontend history preserves Hub actor provenance and unverified source hints;
pending receipts are separate from transcript turns. Desktop/mobile/grid mocked
browser evidence covers the rendered badge without Feishu impersonation.

Review corrections: preserve the reset epoch when a fresh provider ID is assigned;
recheck external sender whitelist before send; fail-isolate corrupt inbox startup;
retain native synthetic rejection as ambiguous delivery rather than falsely
claiming provider completion. Full receipts expire after 30 days but immutable-key
tombstones remain indefinitely; this avoids imposing a timestamped ID format on
existing CLI callers. Goal/archive/readiness changes use a three-second independent
redrain; enqueue/native completion wake immediately. There is no scheduler state.

Validation and raw evidence paths, final commit list and exact counts are in the
workspace handoff `/home/tiger/evo_workspace/hub-chat-inject-20261020/IMPLEMENTATION.md`.
Backend tests use a locked worktree environment and private runtime homes; browser
APIs are mocked. No production provider, primary service restart, push or MR.
Live provider delivery and deployment remain separate pre-ship authorization.

## Review correction R1: cross-tab liveness

The first final reviewer reproduced global starvation: the coordinator awaited
all tab submissions, so one held acknowledgement blocked newly queued tabs and
periodic recovery. Track each owned pump independently, with one slot per tab,
and keep the coordinator's wakeup/recovery loop free of provider submission.
Done callbacks retrieve errors, release only their own slot and wake queued
successors after fast completion; unexpected errors retain periodic backoff.
Shutdown fences new work and joins owned pumps without cancelling potentially
submitted native work.

Three held-submit cases failed before this fix and passed afterward. Added
coverage checks busy pre-submission and possible-submission holds, another tab's
FIFO and periodic evidence recovery, shutdown/no-replay at both boundaries, and
non-spinning handling of an unexpected resolver error. Exact final counts and
logs are recorded in the workspace IMPLEMENTATION.md review addendum.
