# Cursor ACP empty session recovery

Cursor can create session metadata before its first prompt creates a store. A restart in that interval cannot load the session. On a rejected load, recovery now requires matching metadata, no ACP or matching legacy store, no durable turns or malformed history, and no explicit resume requirement before creating a replacement. Unknown filesystem state stays fail-closed.

The focused regression exercises a model change, replacement, prompt completion and later load, plus historical and required-resume rejection. Tests use isolated temporary roots and generated identifiers.

Review found that the tailer writes the current turn before a configuration-triggered restart. Recovery now recognizes that exact unsubmitted turn only while the send lock is held and no other event exists for it; the durable history remains authoritative otherwise. The regression now uses the real tailer and event store for new and previously verified shells.
