# Feishu previews, Bot identity and long-running replies

## Scope

This follow-up fixes three regressions around the existing Feishu WebSocket
bridge: downloaded images that appeared as `Preview unavailable` in Web Chat,
generic Feishu marks after a Bot was paired, and a false Feishu failure reply
while the same Chat turn was still running normally in the browser. It does not
expand the accepted conversation or message-type surface.

## Root causes and design

- Feishu ingress constructed native attachments without a display preview. The
  provider received the original bytes, but the durable turn deliberately got
  an attachment with no id. Ingress now marks an original as its own preview
  only after applying the cache's MIME, header, 8 MiB and 8192px checks. Mixed
  batches retain a per-image optional preview, so one uncacheable original does
  not remove safe previews or block delivery to the provider.
- The pool API already exposed the Feishu-managed Bot name and HTTPS avatar,
  but the binding entry did not consume them. The entry, panel and connected
  card now use the official Bot avatar while bound and the bundled Feishu app
  mark otherwise. Source badges use the same brand fallback. Non-HTTPS and
  broken remote avatar URLs never replace the fallback. Avatar-load failures
  are keyed by Chat, Bot and URL so a reused pane cannot carry one Bot's failure
  into another Bot that happens to use the same profile URL; stale image errors
  are ignored after the identity changes.
- The external Chat bridge applied a 300-second deadline, cancelled the native
  turn at expiry, then let Feishu send a generic failure reply. WebSocket routes
  already have bounded ownership and shutdown retirement, so normal dispatches
  now wait for the authoritative terminal event without an elapsed-time
  deadline. Explicit finite timeouts retain the old cancel-and-fail behavior
  for callers that opt into it. A dedicated completion subscription also
  receives a process-local failed terminal signal when the durable terminal
  write itself fails; this releases the Feishu FIFO without publishing a
  non-durable event into the Web timeline. It also observes structured
  approval requests: because Feishu cannot answer those Web Chat cards, the
  bridge stops the turn and replies with an explicit instruction instead of
  permanently holding the FIFO. The volatile terminal fallback covers failure
  of either the explanatory error write or the final completion write.
- Queue-watchdog regression tests keep their production semantics separate:
  the live-queue cap case still exercises a 250ms cancellation bound, while
  ready/output paths use a 5s test cap and wait for both durable completion and
  release of the provider guard. This avoids loaded-runner scheduling races
  without weakening the cap behavior under test.
- Cross-review tightened the external-turn ownership boundary. A structured
  approval now releases the Feishu FIFO only after the exact provider turn has
  confirmed teardown; any mismatched, already-terminal, unknown or failed stop
  fails closed. Blocking approvals and terminal events displace stale errors in
  a saturated completion queue, while a failed durable terminal write wakes
  only the direct process-local owner and never a post-persist observer. A
  subscription whose tailer startup raises or is cancelled also removes its
  queue before propagating the original exception.

## Current Feishu interaction boundary

Supported now:

- one-to-one (`p2p`) text messages;
- rich `post` content containing text, Markdown, safe HTTP(S) links, visible
  mention names, code blocks, separators and up to ten embedded images;
- five-second burst coalescing, queued/processing reactions, an exact reply to
  the last inbound message, and durable Bot name/avatar provenance;
- 4,000 input characters, 20 MiB per downloaded image, and ten images / 40 MiB
  per coalesced native turn.

Partially supported:

- Replies sent by the Bot use Feishu's reply endpoint and therefore appear
  attached to the inbound message. Inbound quoted-parent content is not parsed
  into model context, so asking the Bot to act on only the quote can be
  ambiguous.
- Rich elements unknown to the normalizer retain plain fallback text when it is
  present; otherwise they become an explicit placeholder rather than a rich
  interactive object.

Not supported by this bridge:

- group chats, topic/thread routing, thread roots or separate per-topic Chat
  state; the normalizer accepts only `chat_type=p2p`;
- standalone image messages, files, folders, audio, video, stickers or other
  binary message types; only images embedded in a rich `post` are downloaded;
- interactive cards, card callbacks, message menus, inbound reaction meaning,
  or forwarding quote/parent metadata to the model. Reaction callback logs can
  therefore be harmless SDK noise even though the Bot's own best-effort busy
  reaction lifecycle works.

## Verification plan

Focused backend coverage exercises unbounded default waiting, explicit timeout
compatibility, safe image previews and oversized-image fallback. Frontend unit
and browser tests exercise the unbound brand mark, bound Bot avatar, broken
avatar fallback, cross-Chat same-URL retry isolation, and a renderable Feishu
timeline attachment. The three queue-cap/ready-state regressions also pass 40
consecutive repetitions (120 cases total). Final command results and
independent-review findings are recorded in the task handoff.

The lifecycle regressions additionally cover provider teardown wait/failure,
all non-confirming external-retirement outcomes, saturated approval delivery,
durable-observer suppression after append failure, and subscriber rollback on
both startup exceptions and cancellation.
