# Real-time Feishu WebSocket Event Routing and Self-Reply Suppression

## Summary

- Added `backend/claude_hub/services/feishu_realtime_events.py` to evaluate incoming `PushedMessage` frames from the personal web-session `msg-frontier` WebSocket stream alongside `/ai` command intake in `FeishuAiPoller`.
- Decoupled WebSocket frame intake (`FeishuAiPoller._handle_frontier_pushed_web_message`) from bounded event metadata/history lookups and sink delivery via an owned background queue/worker (`_enqueue_realtime_event` / `_drain_realtime_event_queue`), ensuring a slow or stalled event sink never delays subsequent `/ai` commands or WebSocket frames.
- Supported two non-self real-time trigger paths without hardcoding any group names, domains, or IDs:
  1. `@current_user` mentions in any group or P2P chat (via `PushMessagesRequest.messagesAtMe` or rich-text `TAG_AT`, excluding `@all`-only broadcasts).
  2. Configured fault/task URL patterns (`CLAUDE_HUB_FEISHU_WATCH_URL_REGEX` / `CLAUDE_HUB_FEISHU_URL_PATTERNS` / `CLAUDE_HUB_PATROL_ACTIONABLE_HOSTS`, preserving regex quantifiers such as `{2,4}`) inside configured priority watch groups (`CLAUDE_HUB_FEISHU_WATCH_CHAT_IDS` / `CLAUDE_HUB_FEISHU_WATCH_CHAT_NAMES` or local runtime JSON settings), even when the current user is not `@mentioned`.
- Added deterministic courtesy/acknowledgement suppression (`is_courtesy_or_ack_only`) and same-chat/same-topic self-reply suppression (comparing `position` / `topic_position` on equal second-granularity timestamps and restricting direct-anchor equality to direct replies to `message_id`) across both the real-time WebSocket hook, `services/dot/events_gateway.py`, and `services/demand_patrol/feishu_triage.py`.
- Restricted cross-transport deduplication to canonical message IDs / verified aliases or bounded cross-namespace (`om_` vs Web numeric ID) matches within 180 seconds, ensuring recurring incidents or identical questions in other topics/days are never dropped.
- Added frontier health & fallback tracking (`FeishuAiPoller.realtime_stream_healthy` + `FeishuRealtimeEventRouter.needs_patrol_fallback`) so `DemandPatrolService` (`feishu_due`) suppresses duplicate 20-minute slow Feishu polling while the real-time stream is healthy, while automatically re-enabling slow patrol fallback whenever an uncovered message (e.g., unmentioned actionable P2P DM or participated-thread follow-up) arrives or a real-time delivery fails.

## Coexistence & Cutover with Sibling Branches (`29c6249b` & `15856077`)

- **With `29c6249b` (`feat/dot-notify-ee0ac02a`)**:
  - `29c6249b` adds `events_bridge.py:poll_mentions` (`python -m claude_hub.services.dot.events_bridge mentions`), a 90-second `lark-cli im +messages-search --is-at-me` poller that posts `ExternalEvent(source="feishu", event_type="im.mention", dedupe_key=message_id, severity="urgent")` to `/api/dot/events`.
  - **Gateway-level shared suppression**: `services/dot/events_gateway.py:submit_external_event` now enforces `_should_suppress_incoming_feishu_event` and `record_suppressed_external_event` (recording suppressed keys in `index.sqlite.events` with `accepted_at=0.0` so they do not consume the 60/hour source quota). Even if the legacy `events_bridge mentions` daemon runs concurrently during transition, courtesy `@me` messages, owner-replied threads, and messages already handled/suppressed by the real-time WebSocket router are rejected at the gateway and will not re-alert Dot.
  - **Cutover procedure**: Once the personal web-session (`FeishuAiPoller` + `msg-frontier`) is active (`realtime_stream_healthy=true` in `/api/feishu-ai-command/status`), disable or stop the external `events_bridge mentions` polling daemon so `lark-cli` 90s polling is fully replaced by the sub-second WebSocket stream.
- **With `15856077` (`codex/feishu-ai-image-awareness`)**:
  - `15856077` adds `topic_position` to `WebMessage` and `has_images` to `FeishuUserMessage`. `FeishuRealtimeEventRouter` reads `int(getattr(msg, "topic_position", None) or 0)`, working seamlessly both before and after `15856077` is merged.
