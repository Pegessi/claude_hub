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

MASO's `post_llm_response` supplies the authoritative text and reasoning snapshot,
including responses with no streamed tokens. Hub appends only the missing suffix,
so partial streams and repeated snapshots do not duplicate the answer. Dropped
response frames are ignored; content-bearing `turn_end` frames remain supported.
This suffix repair does not replace conflicting prefixes or retract already
persisted token deltas when a later response is dropped.
`turn_end` ends one model call and `agent_end` ends one agent run, **not** the
whole delegated workflow. Hub keeps the same session SSE subscription while
native children run and SendMessage wakes the root. Root wake-ups before tree
quiescence retain the current Hub turn and its immutable display timeline.
Tree activity includes nested running/waiting/paused/aborting agents and
foreground condensation, not just the root's status.

MASO 0.9.0 exposes no atomic tree-and-pending-RPC completion fence. Hub checks
status twice without intervening activity before completing a quiescent display
turn, but **does not close the subscription**: a later native root or child start opens a
new attributed Hub turn, persisted and broadcast without user input or reload.
Idle viewer eviction does not retire this subscription; explicit session Stop,
rotation/deletion or service shutdown owns teardown. Durable tool calls/results and tool
confirmation cards use the same Hub timeline as other providers.

MASO owns condensation. It can archive and rewrite its live trajectory, and its
`context_condensed` event can arrive before the actual splice. Hub shows the
condensation status and retains its own display events. It neither replaces the
visible conversation with a rewritten snapshot nor resends the Hub transcript
to reconstruct compressed context. Forks remain the existing explicit exception:
a new conversation receives its selected history once.

The first send subscribes to SSE before posting; subsequent sends reuse that
subscription. Native turn adoption shares the manager's send/Stop lock. A pending
submission fences quiescence until its exact `message_injected` UUID echo proves
MASO consumed the input; an unrelated native report cannot finish that user's
turn while upload or POST is pending. Initial snapshots/replay establish a baseline and are not appended
as new conversation output. A resumed session's active status does not authorize
aborting its children or queued native reports. Explicit Stop still aborts and
awaits exit/resume finalizers before allowing a fresh subscription.

MASO's bounded event IDs can reset, so an interrupted connection or failed status
monitor fails the display turn rather than claiming lossless replay. It does not
automatically abort remote work. A possibly accepted POST or interrupted delivery
is quarantined in `.maso-delivery.json` (recording `uncertain`, `post_acknowledged`,
and `turn_id`) and never automatically resent. While quarantined, runtime status
snapshots report `attention` rather than `idle`/`Ready`. An acknowledged POST whose
SSE stream disconnected can auto-reconcile on the next send only after MASO confirms
the session tree is idle both before and after `GET /messages/pending/{message_id}`
returns `404` with no intervening activity. An unacknowledged POST (`post_acknowledged=false`,
such as a request timeout that may still be paused before broker enqueue) never
auto-reconciles from `idle + pending 404`; explicit Stop (`cancel_active_turn`,
including cold targeted Stop matching the persisted `turn_id`) deletes any pending
message, aborts and exits the old session, and rotates to a fresh MASO session so a
delayed enqueue stays fenced on the retired session ID. Reopening the UI
after a backend crash is not a lossless backfill of provider output produced
while disconnected.

## Hub-managed Goals

Direct top-level MASO Chats advertise Hub-managed Goal mode. Hub persists the
objective, checkpoint, dispatch identity and lifecycle, then sends each bounded
continuation through the same ordinary MASO message endpoint used by Chat. It
does not enable or call MASO Auto; only Hub schedules these Goal turns. Managed
Workspace Chats retain their Task-owned controls and do not expose direct-Chat
Goals.

A trailing Goal control envelope is model output, not proof that the full MASO
run ended. Hub keeps the control text out of the visible transcript and waits
for the root `agent_end` before observing the Goal step as complete; `turn_end`
and child `agent_end` events cannot release the next continuation. This keeps
MASO tools, child agents and finalizers inside the turn that started them.

Goal pause, clear and steering use the existing exact-turn cancellation path.
Unknown HTTP acceptance or an interrupted SSE stream retains the same fail-closed
behavior as ordinary MASO Chat: Hub does not automatically repeat the Goal step.
Plan mode remains unavailable for MASO, so Goals always run in default Agent
mode.

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

A failed HTTP send or mid-turn stream disconnect can leave acceptance or
completion unknown. MASO has no cancellation tombstone for a POST that has not
reached the broker yet. Hub therefore never automatically retries that POST and
keeps the uncertain delivery record across restarts. Before refusing a subsequent
send, Hub reconciles against MASO: if `GET /api/sessions/{id}/status` confirms
the session tree is idle (`is_running=false`, `status=idle`, and no active
children or condensation) and `GET /api/sessions/{id}/messages/pending/{message_id}`
returns `404` (not queued), Hub clears the quarantine sidecar and admits the new
prompt without resending the old one. If the remote session is still active, the
message UUID remains pending, or the service returns an error/is unreachable,
further sends stay refused (fail-closed). Explicit Stop (`cancel_active_turn`)
also runs when `turn_in_flight` is false but an uncertain delivery sidecar is
present: it deletes the pending message UUID, requests abort, exits the session,
confirms an idle resumed session, and clears the quarantine sidecar.

This integration supports local Chat, text, images, reasoning, tool output, tool
confirmations and Hub-managed Goals. Plan mode and editing earlier turns are not
advertised. Automatic long-context condensation remains MASO's responsibility;
Hub adds no separate condenser.

## Workspace agents and reviewers

Workspace agent creation also accepts `session_kind=chat` with `agent_type=maso`.
This uses the existing Task assignment, report, review and human-acceptance flow;
a model turn ending does not complete a Task. The associated tab is a navigation
record only and does not start ttyd, tmux or a shell. Open it from Workspace to
read the managed-session Chat history.

Managed MASO execution is limited to a loopback MASO endpoint on the Hub machine.
The MASO tools must see the selected working directory and be able to call the
Hub report URL supplied in the assignment. A service catalog probe does not
prove those tool/callback requirements; remote MASO service callbacks and SSH
native Chat execution are not supported by this managed path. Ordinary standalone
MASO Chat keeps its existing endpoint support.

Workspace Stop pauses native execution and queued mailbox delivery. Resume with
an explicit message or Task continuation; unknown message acceptance remains
blocked rather than automatically repeating the assignment. Unrelated reviews
receive a fresh native provider context while prior display history remains.
The long first-turn Hub runtime guidance's injection frequency is unchanged.
