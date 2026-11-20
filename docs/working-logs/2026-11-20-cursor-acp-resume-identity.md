# Cursor ACP resume identity

The Hub kept displaying the same Chat history across six turns, even though
Cursor ACP replaced the underlying conversation during turn six. The Hub
stream had contiguous sequences 0–837 and used a stable, tab-derived session
identifier. The Cursor store and backend warning exposed the provider change.

| UTC on November 20 | Evidence |
| --- | --- |
| 06:21:01.591–06:38:14.363 | Turn one completes at sequence 556. |
| 07:03:04.091–07:03:42.206 | Turn two completes at sequence 595. |
| 07:05:12.678–07:08:42.038 | Turn three completes at sequence 680. |
| 07:08:42.377–07:19:22.444 | Turn four ends as cancelled at sequence 714. |
| 07:19:22.815–07:23:27.525 | Turn five completes at sequence 829. The prior conversation's last WAL write was 30 ms before completion. |
| 07:24:45.662 | The backend logs a failed ACP load of the prior conversation and announces a fresh session. |
| 07:24:53.095 | The Hub records turn six starting at sequence 830. |
| 07:25:15.587 | The replacement conversation's metadata file is created. The Hub later stores its ID as verified. |
| 07:25:44.429 | Turn six completes at sequence 837. |

The resume attempt preceded the persisted turn-start event. Prompt summaries
from turns one through five matched records in the prior Cursor store. The
replacement store contained none of those matches. Both stores recorded the
configured model in their available `modelName` fields. The warning omitted
the provider's reason for rejecting the load. The Hub stream does not record
browser navigation history.

The old `CursorSession._initialize_session` caught a provider RPC error from
`session/load` and proceeded to `session/new` unless explicit recovery had
set a required conversation. It persisted the new ACP ID to the same Hub
tab once model and mode setup succeeded. The tab-derived stream ID kept the
existing Hub transcript visible.

Verified conversations now fail initialization when ACP rejects the load or
explicitly returns a different ID. Retries load the same verified ID. Legacy
stream-init records can only establish an unverified binding. The explicit
rotation workflow still prepares a replacement and defers adoption to its
caller. Regression tests cover RPC rejection, a mismatched load response,
late legacy init records, and recovery on the original ID.

## Follow-up on cold starts

The next launch selected a proxy at 07:24:39.788 UTC. The prior logged
launch had selected the same route. The launch fingerprint hashes network
configuration inputs, so changes in probe outcomes alone leave the
fingerprint stable. The historical records omit the prior process's exit
status and the trigger for the next launch.

The native idle check used the last subscriber timestamp and could reap a
healthy process immediately after a long turn finished with no viewers.
The timeout now starts from the later of the last subscriber activity and
the last turn completion. The reaper checks both again under the send lock.
Diagnostics now distinguish idle reap, a configuration restart, and
unexpected Cursor stdout closure.

An isolated copy of the prior Cursor store, including its WAL, passed
SQLite quick check. Its schema version, conversation ID, and canonical
working directory matched. With network syscalls blocked, initialize
succeeded but session/load returned `-32603` with
`Failed to initialize session services` and empty stderr. A fresh-session
control produced the same error. In the installed Cursor bundle both
methods emit this detail when shared-service initialization throws,
before the load handler looks up the conversation ID. The historical RPC
detail was unavailable, so the reproduction cannot attribute that failure
to a specific network or provider error.

ACP now retries a startup handshake or verified session load once after
short backoff if the request times out, its connection closes, or the server
reports a recognized startup, network, or SQLite contention error. Only the
original verified ID is sent on a retry. The process becomes ready after
session setup completes. Diagnostics emit the phase, error category and
RPC code without copying the RPC payload.

The independent review also reproduced a nonzero subprocess exit carrying
a connection reset during session/load. The exit now enters the same
bounded startup retry, including when a subprocess exits without a
diagnostic. Other nonzero exits with a diagnostic still surface the failure.
If an ACP write fails before the reader observes EOF, startup gives that
process a bounded window to publish its exit diagnostic. This preserves a
permanent rejection even when the broken pipe is observed first.
