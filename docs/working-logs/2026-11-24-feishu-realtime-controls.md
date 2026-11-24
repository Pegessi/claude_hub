# Feishu WebSocket event controls

Use **Feishu realtime events (WebSocket)** in Dot → Edit → Proactive radar
or Feishu settings → Personal /ai. Dot's Work radar bar shows the same health
summary. These controls work with the scheduled radar master off.

`claude-hub dot patrol --realtime` enables dispatch and `--no-realtime` disables
it. `claude-hub dot patrol` shows effective dispatch, WebSocket connection,
and replay health. JSON mode exposes the status fields and counters.

Authenticated `GET /api/dot/patrol` includes a `realtime` object.
`GET /api/dot/patrol/realtime` returns only that object. Send
`{"realtime_enabled": false}` to `PATCH /api/dot/patrol` to update the
dispatch switch. A valid `CLAUDE_HUB_FEISHU_REALTIME_EVENTS_ENABLED` value
takes precedence and appears as `env_override` in the response. Writes
preserve other keys in the per-runtime JSON. Decoding failures leave the
original document intact so its contents can be recovered.

The personal Web listener remains available for `/ai`, and Web replay
continues advancing through messages observed while dispatch is off. A
configured Web session continues to suppress CLI patrol during both a
disconnect and an intentional dispatch pause. A delivery already handed to
the event sink can finish. The switch gates new deliveries at dispatch.
