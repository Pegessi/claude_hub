# MASO HTTP Chat

Create a **Chat** and choose **MASO** as its provider. Hub connects directly to
the existing MASO service; it does not launch a MASO CLI or a second agent runtime.
The default endpoint is `http://127.0.0.1:9796`. Set `MASO_BASE_URL` in the new
Chat's environment to use another HTTP(S) endpoint. Keep the endpoint fixed for
that Chat; create a new Chat to change services or providers.

The model picker discovers `/api/models` and `/api/models/default`, groups the
service's advertised thinking variants and sends their exact provider keys.
`MASO_MODEL` selects a provider key, including a thinking variant. Model changes
use Hub's existing switch-env operation and apply to the next turn. They do not
change the MASO service's global default or inject environment variables into
MASO's tools. Those tools run in the service's environment; Hub supplies this
Chat's ID in its first-turn guidance for explicit Hub CLI invocations.

Hub creates a `MASOAgent` session on the first send, with the Chat's working
directory and `isolation=shared`. This preserves the chosen checkout rather than
asking MASO to allocate its own Git worktree. Hub persists the returned session
ID and resumes that same model context after a backend restart. There is no
implicit import of existing MASO conversations or local trajectory files.

## Condensation and streaming

MASO's `turn_end` ends one model call; the root agent's `agent_end` completes the
Hub turn. Child endings do not complete the parent. Text and reasoning use
Python character offsets; the final model response supplies any missing suffix
without duplicating already streamed output. Durable tool calls/results and tool
confirmation cards use the same Hub timeline as other providers.

MASO owns condensation. It can archive and rewrite its live trajectory, and its
`context_condensed` event can arrive before the actual splice. Hub shows the
condensation status and retains its own display events. It neither replaces the
visible conversation with a rewritten snapshot nor resends the Hub transcript
to reconstruct compressed context. Forks remain the existing explicit exception:
a new conversation receives its selected history once.

Each turn subscribes to SSE before posting its message. Initial snapshots/replay
establish a baseline and are not appended as new conversation output. MASO's
bounded event IDs can reset, so an interrupted connection fails the turn and
attempts to stop it rather than claiming lossless replay. The next normal turn
can continue on the same session after cleanup succeeds.

## Stop and request uncertainty

Stop immediately retires the stream generation, removes this turn's pending
message UUID, requests abort and checks that execution has become inactive.
Abort alone does not remove MASO broker messages. A small delivery sidecar beside
the Hub event log preserves the owned UUID and session identity across crashes.
Failed cleanup must succeed before a new prompt can be submitted.

A failed HTTP send can leave acceptance unknown. MASO has no cancellation
tombstone for a POST that has not reached the broker yet. Hub therefore never
retries that POST, attempts pending-message removal and abort, and keeps the
uncertain delivery record. Further sends on that Chat are refused, including
after a Hub restart. The error explains that the service may still execute the
message: inspect and stop the session in MASO before starting a new Chat. An
idle status immediately after a timeout is not proof of cancellation.

This initial integration supports local Chat, text, reasoning, tool output and
tool confirmations. Images, Goals, Plan mode, editing earlier turns and
Workspace-managed worker execution are not advertised. Automatic long-context
condensation remains MASO's responsibility; Hub adds no separate condenser.
