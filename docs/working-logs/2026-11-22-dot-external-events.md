# Dot external events

The gateway shares Dot's durable inbox. Its SQLite receipt ledger serializes
acceptance and retains dedupe keys after acknowledged inbox entries age out.
The receipt transaction commits after the inbox write. A retry can recover an
interrupted receipt from the persisted inbox key.

The watcher retries unread urgent events through its existing user-yield and
in-flight checks. The first urgent attempt skips the work polling pass. Normal
and digest events remain visible to user turns and manual inbox review, and are
excluded from unattended wake turns. A successful accepted user turn consumes
the passive events it saw, while failed or cancelled turns leave them pending.
The gateway retires the oldest passive entries once 200 are pending. The context
builder escapes external
fields and keeps the complete boundary together when applying its size budget.

The Lark adapter starts one subprocess for each personal event key. It holds
stdin open, waits for the key's stderr ready marker and drains diagnostics through
SIGTERM cleanup. Oversized NDJSON records are discarded through the newline
before the reader resumes. The callback adapter accepts normalized transitions and leaves
provider-specific webhook verification to the calling producer. See the Dot guide
for [configuration and invocation](../DOT.md#external-events).

Local validation exercises the FastAPI route against a temporary Dot home, with
real SQLite receipts and inbox storage, plus a fake Lark executable for the stream
and signal contract. Live Lark credentials, subscription lifecycle against the
provider, and production callback enrollment require operational verification.
