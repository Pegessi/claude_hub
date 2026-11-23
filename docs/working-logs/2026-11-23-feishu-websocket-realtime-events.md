# Real-time Feishu WebSocket Event Routing and Self-Reply Suppression

## Summary

- Added `backend/claude_hub/services/feishu_realtime_events.py` to evaluate incoming `PushedMessage` frames from the personal web-session `msg-frontier` WebSocket stream alongside `/ai` command intake in `FeishuAiPoller.handle_pushed_web_message`.
- Supported two non-self real-time trigger paths without hardcoding any group names, domains, or IDs:
  1. `@current_user` mentions in any group or P2P chat (via `PushMessagesRequest.messagesAtMe` or rich-text `TAG_AT`, excluding `@all`-only broadcasts).
  2. Configured fault/task URL patterns (`CLAUDE_HUB_FEISHU_WATCH_URL_REGEX` / `CLAUDE_HUB_FEISHU_URL_PATTERNS` / `CLAUDE_HUB_PATROL_ACTIONABLE_HOSTS`) inside configured priority watch groups (`CLAUDE_HUB_FEISHU_WATCH_CHAT_IDS` / `CLAUDE_HUB_FEISHU_WATCH_CHAT_NAMES` or local runtime JSON settings), even when the current user is not `@mentioned`.
- Added deterministic courtesy/acknowledgement suppression (`is_courtesy_or_ack_only`) and same-chat/same-topic self-reply suppression across both the real-time WebSocket hook and `services/demand_patrol/feishu_triage.py`.
- Added frontier health tracking (`FeishuAiPoller.realtime_stream_healthy`) so `DemandPatrolService` (`feishu_due`) automatically suppresses duplicate 20-minute slow Feishu polling while the real-time WebSocket stream is healthy and falls back when disconnected.
