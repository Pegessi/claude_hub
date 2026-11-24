# Personal AI image awareness

A quoted rich-text command was recognized, but its interim web-gateway reply failed before AI dispatch. No original mutation request was replayed. The gateway status alone does not identify a payload defect or expired login.

Normalized user messages now retain ordered image resource references bound to their original message IDs. Web direct-image and reachable rich-text image nodes, OpenAPI message bodies and WebSocket intake preserve those references through durable queues. Referenced messages are read under the same authorized personal account; no Bot token, invitation or cross-account fallback is used for image acquisition.

Images are downloaded with bounded streaming, explicit authentication/permission errors, no credential-bearing redirects and shared attachment count/byte/MIME validation. Header dimensions and SHA256 accompany source-message/resource provenance. The existing Chat attachment contract carries original bytes atomically with text to both managed and standalone native sessions, including capability checks and bounded previews. Completed-turn recovery never re-downloads or redispatches the images. The former universal “image unavailable” disclosure is removed; acquisition failures abort before model submission rather than silently omitting content.

Validation includes actual personal-account read-only image acquisition and an isolated native-provider visual acceptance with a randomized marker present only in image pixels. The shared attachment decoder and actual provider consume original image bytes; no real-chat writes are needed. Deterministic regressions cover direct/rich/quoted images, WS and durable ownership, managed/tab input, expired-token refresh, denied reads, redirects, download errors and attachment limits. A baseline comparison fails on the prior boolean-only implementation. Runtime configuration and credentials remain untouched; no push or deployment is included.

## Quoted reply topic identity

A further read-only comparison found a main-chat quoted command with a positive chat position and a citation root that did not resolve to a topic. A later actual topic reply had a negative chat position and a resolvable topic root. The old adapter interpreted every non-empty root as a topic, so a requested new topic inherited the earlier citation root.

Distinguish positive main-chat positions, negative topic positions and restored records with explicit thread metadata. Refresh incomplete cached targets by numeric ID; a citation root alone is not fallback topic evidence. For a requested new topic, use the command as the request root; existing topics retain their root. Interpret the send receipt using the requested effective thread mode and preserve its returned thread identity in cache. Do not change ordinary non-topic quote replies.

Offline protobuf tests cover main quotes, actual topics, fresh transports, read refresh failures, OpenAPI aliases and root-only receipts followed by another reply. Existing synthetic topic fixtures now use the observed topic position shape. This proves the client construction defect and its correction, not the remote gateway's unique reason for a historical HTTP 502; no real write is replayed.

Topic pagination previously overwrote chat position with thread position. Keep a separate optional `topic_position`; its presence records the topic endpoint provenance, preserves ordering, and prevents real paginated topic messages from being classified as main-chat quotes. A direct pagination-to-wire regression covers both positive positions without guessing their meaning.

## Protocol thread coordinates

A separate read-only failure investigation found replies in a THREAD group with positive chat positions and explicit `threadPosition` values 0, 1 and 2; the root had -1. The ordinary P2P quote used for the earlier fix omitted this field, while a true P2P topic carried 0. The protocol schema already declared the read field, but `message_of` discarded it. Preserve field presence in the existing `topic_position` member, including zero and -1, and retain the same field in Frontier decoding. The shared classifier then uses explicit thread provenance ahead of chat position without adding another mode cache or listener.

Offline protobuf-to-reply tests cover absent, root, zero and positive coordinates. Service-level tests return HTTP 400 to both the interim and error fallback replies and assert that neither tab creation nor Agent dispatch starts. This preserves the existing execution gate. These tests prove request construction and failure control flow; they do not prove which field caused a historical remote HTTP 400. No historical command is replayed, and successful image acquisition/vision does not establish that a THREAD reply is remotely accepted.
