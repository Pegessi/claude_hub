# Feishu /ai reply-created thread identity

Main-chat commands previously provisioned under the main channel even when their
interim reply created a thread. A follow-up inside that thread created a second
managed session, while unrelated main-chat commands could share the first one.

Provisioning now uses a copied execution message with the incoming thread or,
when replying in-thread, the interim receipt thread. The original command stays
unchanged for context and delivery. The execution identity determines the channel
lock, channel record, session title and dispatched turn metadata.

If only the final receipt supplies a thread, migrate the provisional main record
to that thread without leaving a main alias. The same managed tab/session is
retained. The channel lock stays held through final delivery and binding.
Thread commands wait for a provisional main command before resolving their
channel mapping, so an early follow-up cannot provision a second session while
the first final receipt is pending. No thread returned, or an unthreaded reply, preserves main reuse.
A completed turn interrupted during final delivery keeps its provisional record
under a command-private pending key. Recovery acquires the channel lock and
publishes the final thread mapping without another command reusing that session.
The final receipt cannot retroactively change the metadata of an executed turn.

Transport inspection: OpenAPI reply decoding and incoming user-message decoding
both retain the server thread_id. Web adapter reply_message and to_user_message
both select thread_id or root_id for topic messages. Thus each transport's
returned identity matches its subsequent incoming thread identity; no cross-
transport ID conversion is introduced here.

Regression coverage exercises full handle_message provisioning and dispatch:
created-thread follow-up reuse, two separate created threads, disabled thread
reply, missing thread receipts, final-only fallback reuse and final-only thread
separation, each both sequentially and with the first final receipt paused
while the second command arrives. Assertions cover exact tab/session identities, lock ownership,
metadata, channel records and initial session titles.

Validation results are recorded in the Task report; independent review checks
the full diff and related consumers. No live messaging or service restart is
part of this change.
