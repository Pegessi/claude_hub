# MASO HTTP Chat

Create a **Chat** and choose **MASO** as its provider. Hub connects directly to
the existing MASO service; it does not launch a MASO CLI or a second agent runtime.
Endpoint selection uses the Chat's `MASO_BASE_URL`, then the Hub process's
`MASO_BASE_URL`, then the service setting in `backend/.env`, then the Hub
process's `MASO_PORT` (mapped to IPv4 loopback).
Port shorthand is a startup setting; Chat environment presets select a service
with `MASO_BASE_URL` so UI readiness and creation use the same target.
An explicit address or port is checked on its own; an unreachable configured
service does not fall back to another instance. Without these settings, Hub
checks local ports from `~/.maso/instances.json` and the legacy
`http://127.0.0.1:9796` endpoint. It selects the sole usable service; multiple
ready services require `MASO_BASE_URL`. Stale entries are ignored after probing,
and public export URLs in the registry are never used for local discovery.
Set `MASO_BASE_URL` in the new Chat's environment to select a particular HTTP(S)
endpoint. Keep the endpoint fixed for
that Chat; Hub freezes its resolved address at creation, including when the
backend's default later changes. Create a new Chat to change services,
providers or working directories.

Choosing MASO checks the service from the Hub host before enabling Create.
The check reads the model catalog and default model within a bounded timeout;
it does not launch MASO, create a session or invoke a model. If the service is
missing, unreachable or misconfigured, the dialog explains that MASO cannot be
used and keeps the user's draft. Start/fix the service and use **Retry check**.
Changing a service preset performs a fresh check. Creation rechecks availability
before allocating a tab, and an existing unavailable Chat shows the service
error with **Retry**. This checks advertised configuration, not upstream model
credentials, quota or inference success.

## Startup check

MASO's service-wide URL is part of the existing backend `Settings` configuration.
To configure it once, add the intended URL to `backend/.env` (preserve the file's
other settings), then use the normal authorized Hub startup path:

```dotenv
MASO_BASE_URL=http://127.0.0.1:11004
```

This is an example local address. `start.sh` runs the backend from `backend/`,
where Settings reads `.env`; the repository root `.env.example` is the template.
The process environment overrides the file, and a Chat URL overrides both.
An absent/empty setting permits local discovery. Startup probe, preflight and
Chat creation share this resolver, so a file-configured service is not replaced
by whichever local instance discovery finds. Changes to this startup setting
take effect when the backend starts again; existing Chats retain their saved URL.

Production `start.sh` / `service_launcher` and development `start.sh --dev`
report the selected MASO URL and model count at startup. MASO being absent or
ambiguous warns without preventing other Hub providers from starting. Selecting
MASO and creating a new Chat repeat discovery/readiness checks; startup success
is not an admission token. Existing Chats keep their saved endpoint even when
the local registry changes.

To inspect the address independently, from `backend/` run:

```bash
uv run python -m claude_hub.maso_probe
```

It emits JSON and exits nonzero if there is no unique usable target. `--startup`
prints a diagnostic and exits zero for expected configuration/service failures.
The probe reads only `/api/models` and `/api/models/default`, bypasses proxy
environment variables and creates no session or model request. Automatic local
discovery reads at most 64 KiB and probes at most 16 candidates concurrently
within 3.5 seconds; each service check has its existing 3-second deadline.
Unavailable discovery does not start MASO or scan processes or arbitrary ports.
Remote services, multiple instances and an unreadable/unsupported registry can
always be handled with an explicit `MASO_BASE_URL`.

For example, when an intended local service listens on port `11004`, launch
Hub with `MASO_PORT=11004` or `MASO_BASE_URL=http://127.0.0.1:11004`. The port is
an example, not a universal default. To fix an existing Chat pinned to a different
address, create a new MASO Chat with the intended URL; changing its provider
endpoint in place is refused.

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
An existing verified session without a persisted service address is refused;
create a new Chat rather than guessing which service owns its opaque ID.

## Image input

MASO Chats accept pasted or selected PNG, JPEG, GIF and WebP images, up to 8 MiB
of original bytes per image. Text is optional. Images remain attached when a
draft queues behind a running turn, and Hub retains bounded display previews for
history and reload. Original image bytes are sent to MASO, not the smaller
display preview. The selected MASO model and upstream endpoint must also support
visual input; the service's model catalog has no per-model vision capability
field.

Hub validates every image before creating or uploading to a session. It uploads
512 KiB binary chunks over HTTP, then sends text and session-scoped image refs
in one message. This works with a remote MASO_BASE_URL and avoids MASO's proxy
limit on large JSON bodies. It does not pass Hub filesystem paths or authenticated
preview URLs to the service.

Upload failure happens before message delivery and permits a fresh send.
Transport cancellation fences a pending upload so it cannot start a later model
turn. The composer keeps Stop disabled until the send request finishes, including
its upload; Stop then cancels the delivered turn. Unused uploaded refs expire
under MASO's own policy; Hub does not delete service files. Once the message POST
starts, the existing uncertain-delivery rules still apply.

MASO's user-message echoes are not added to Hub history, so provider data URLs
are not duplicated in the display event log. Opening history does not upload or
resend earlier images. Forked history transfers text; it does not automatically
attach past images to the new context.

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
message UUID, requests abort and exits the owned MASO session tree. Exit waits
for cancellation finalizers and context persistence; an idle status alone cannot
prove completion because MASO can emit an old `agent_end` afterwards. Hub confirms
a live resume of the same saved session before accepting another prompt. This
clears that session tree's remaining queued work and pauses its MASO automatic
objective; it leaves other sessions and the shared working directory intact.
Pending confirmation cards are retired with their turn, including on crash
recovery; stale card answers are rejected before message delivery.
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

This integration supports local Chat, text, images, reasoning, tool output and
tool confirmations. Goals, Plan mode, editing earlier turns and
Workspace-managed worker execution are not advertised. Automatic long-context
condensation remains MASO's responsibility; Hub adds no separate condenser.
