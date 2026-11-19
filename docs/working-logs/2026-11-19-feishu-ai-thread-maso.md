# Feishu /ai thread and MASO endpoint fixes

The P2P web adapter forced `in_thread` off for top-level messages despite the caller's request. The gateway schema and request builder encode root ID, parent ID and the thread flag; regression tests cover P2P and group replies and chaining. Mocked gateway behavior does not establish live Feishu folding.

The 12:41 failure in backend.log occurred during MASO session creation. Tab admission discovered 127.0.0.1:11004, but managed provisioning persisted the original environment, so the transport resolved the fallback 127.0.0.1:9796, which refused connections. The subsequent model switch sent the tab's pinned endpoint to that transport, which correctly refused changing its identity. Both requested model keys appeared in the live catalog. Managed provisioning now persists the tab endpoint and /ai replaces legacy mismatched sessions without rebinding their transport. HTTP errors retain status and transport errors report their exception class without request data.

Live Feishu folding and end-to-end model execution require separate acceptance against a safe chat and a restarted deployment; the running services were not restarted for this change.

Codex /ai provider switching also used a static or legacy-first selection despite the local catalog advertising `[redacted]` first and not advertising `[redacted]`. Provider-change defaults now prefer visible cache-backed entries; explicit model selections remain supported. The cache is a discovery hint, not a guarantee of current entitlement.
