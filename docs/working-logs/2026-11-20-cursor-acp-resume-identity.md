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
