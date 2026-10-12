# Feishu Bot identity and rich-message ingress

## Scope

Feishu-authored Chat turns now retain the official Bot name and avatar that
received them, and the long-connection ingress accepts both plain `text` and
rich `post` messages. The change keeps the existing p2p binding, coalescing,
deduplication, reaction and reply lifecycle; it does not add group messages,
standalone file/audio/video events or interactive-card callbacks.

## Root cause

The WebSocket normalizer previously rejected every inbound event whose
`message_type` was not `text`. A real mixed-format Feishu message therefore
logged `Only Feishu text messages are supported` and stopped before Chat
dispatch, so no model turn or reply could exist. Separately, provenance stored
only the app and sender ids, leaving the timeline with a generic `Feishu` label
and hard-coded mark.

## Design

- Credential validation calls Feishu's Bot profile endpoint and stores the
  returned app name plus an HTTPS avatar URL. Older pool entries refresh that
  presentation metadata best-effort on their next message; failure falls back
  to the local Bot label without blocking ingress. Each accepted turn copies
  the effective profile into its metadata, so later renames do not rewrite
  history.
- Rich `post` rows become bounded Markdown. Text/Markdown, safe HTTP(S) links,
  mentions, code blocks and separators preserve their order. Images become
  numbered placeholders and up to ten message resource keys. Unknown elements
  retain their text when possible and otherwise become explicit placeholders.
- Image resources are downloaded with the Bot tenant token, limited to 20 MiB
  each, and checked against their response MIME and magic bytes. Successful
  images enter the existing native Chat attachment path; a failed image keeps
  the surrounding text and placeholder instead of rejecting the turn.
- A coalesced batch shares the native path's ten-image and 40 MiB request
  budget. Extra images remain represented by their placeholders instead of
  making the entire batch fail validation. Original image bytes go only to the
  provider. They are not persisted as timeline previews, matching the existing
  attachment-cache boundary; history can therefore show a non-renderable
  attachment placeholder when no bounded preview exists.
- The timeline accepts only HTTPS Bot avatars and falls back to the `飞` mark
  if the URL is absent or the image fails to load. Historical turns without
  profile metadata keep the generic `Feishu` label.

## Verification

- Repository verification used the declared Python 3.11.16, uv 0.12.15, Node
  24.21.0 and pnpm 9.15.9 toolchain. Backend types passed. An earlier candidate's
  full backend suite finished with 2,820 passed and 19 skipped; after the final
  small concurrency/resource hardening, a full rerun reached 1,861 passed before
  the unrelated macOS service-restart timeout fixture failed to spawn its helper
  with `Operation not permitted`.
- The final focused Feishu, attachment and admission-guard regression scope
  finished with 519 passed after the profile-refresh concurrency and bounded
  streaming fixes. Black,
  isort and `git diff --check` passed.
- Frontend lint and type checking passed; the unit suite finished with 751
  passed and the production bundle built successfully.
- A task-owned Vite server on `127.0.0.1:5279` rendered the real SPA with mocked
  APIs and a mocked avatar response. Desktop/mobile and binding-state reloads
  showed one `Hub 助手` source badge with its avatar and no page errors. The
  server was stopped and the port was no longer listening afterward.

Independent review is recorded in the final task handoff rather than treated
as implied by these checks.

## Remaining limits

- A live Feishu mixed-message round trip was not performed during this task;
  resource download and payload shapes are covered through injected HTTP and
  SDK fixtures.
- Standalone image/file events remain unsupported. Only images embedded in a
  supported rich-text `post` enter this path.
- Persisted timeline thumbnails would require a bounded server-side image
  resampler. This change intentionally does not add a new image-processing
  dependency or persist full-resolution originals.
