# Feishu Web rejection diagnostics and auto reply fallback

The Web reply path discarded non-200 response details. In `auto` mode without an OAuth token, its synthetic record has `source=web_session`; the exception guard consequently rejected every Bot fallback, including explicit HTTP 400 request rejections. Incoming Web messages also use numeric IDs that cannot safely be passed to OpenAPI.

## Changes

- Gateway non-200 failures log and carry HTTP status, validated trace ID, Packet status, best-effort protobuf Status/JSON code and a bounded protocol-vocabulary summary. Unknown prose, numbers and opaque strings are redacted; raw response bodies, request contents and credentials are never logged. Plain-text errors receive the same vocabulary projection. Authentication failures retain session invalidation. Existing exception-to-run-reason propagation persists the same diagnostics.
- Only `auto` plus `allow_bot_fallback` permits fallback for explicit HTTP request rejection (400/404/405/413/415/422). Explicit `web_session`, uncertain deliveries, timeouts and 5xx retain their previous route boundary.
- An existing `om_` alias permits a Bot thread reply. Without a message alias, `allow_bound_or_dm_fallback_on_not_in_chat` must be enabled: a read-only explicit `oc_` mapping permits the configured/bound Bot send path; otherwise a matching configured app and `ou_` owner permit a DM. No ID guessing or implicit membership invitation occurs. Bot timeout, unclassified HTTP failure and missing receipt stay uncertain, with no additional fallback attempt. `/ai` suppresses error replies after either an uncertain interim receipt or final answer.

## Evidence and boundaries

Focused offline HTTP tests cover Packet/Status/JSON/malformed responses, secret and body exclusion, durable `recent_runs.reason`, mapped thread replies, mapped group sends, bound Bot and owner DM fallback, absent configuration, disabled fallback, explicit Web routing and uncertainty on either transport. Validation and frozen SHA are recorded in the task handoff.

Read-only live metadata confirmed `send_mode=auto`. No real message/reaction, deployment, shared-service restart, push or main-checkout edit was performed. The historical THREAD HTTP 400 field-level cause remains unproven: incoming Pull messages cannot establish outbound Put validation rules, and historical request/response bytes were not retained. No speculative Put encoding change is included.

Lesson applied: `a-closed-file-list-can-omit-behavior-required-by-acceptance-criteria-compare-the`; review covers the full incremental diff and the existing run-reason consumer, plus the inherited merge boundary.

## Independent review correction

The first native read-only review found that a code-less/non-JSON Bot HTTP error could escape as `HTTPStatusError` after submit. Normalize that path to delivery uncertainty, and suppress the service error reply for uncertain interim delivery as well as final delivery. A real offline HTTP chain (Web 400 → Bot non-JSON 502) asserts one Bot send, no error reply and no Agent dispatch.
