# Feishu Web events and position replay

A configured personal Web session routes incoming mentions, direct messages, participated-topic replies and configured watch-group URLs to Dot. Pushes are journaled before their protocol ACK; recovery reuses numeric Web message IDs. The first snapshot establishes current chat positions without delivering history. Reconnect recovery covers recent inbox chats, locally recorded chats and topics, and configured numeric watch-group IDs. Recent roots are examined to discover topics; arbitrary older topics created during an outage are outside this bounded recovery scope.

Position responses may contain invisible holes. Once the requested high-water position is visible, intermediate absent positions are logged at debug level and skipped; RPC failure or an unavailable trailing high-water leaves the checkpoint for retry. This is the explicitly chosen engineering policy, not a server guarantee that a successful sparse response was complete. Topic and chat checkpoints advance after processing, and queued pushes survive restart through the local journal.

When a personal Web session is configured, scheduled and manual Feishu CLI patrol are skipped. A CLI-only installation retains its patrol. The independent EVO & CFS 30-minute schedule is unrelated.

After deployment and validation, confirm the Web session and frontier/replay health, test an incoming event and reconnect recovery, then stop the external mentions poller with `tmux -L dot-mentions kill-server`. This operator command was not run during development. The older external poller uses OpenAPI IDs and must not run alongside Web delivery.

The bounded recovery and sparse-hole policy require operational acceptance. Simulated tests cannot prove server-side exactly-once delivery. Replay errors keep the Web stream unhealthy and require inspection.
