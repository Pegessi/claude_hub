# Feishu Bot avatars under COEP

## Symptom and root cause

The Bot pool contained Ted's Feishu-managed HTTPS avatar and the CDN returned a
valid 240 by 240 PNG with `Access-Control-Allow-Origin: *`. The production Chat
still rendered the outline brand fallback because its top-level response sets
`Cross-Origin-Embedder-Policy: require-corp`, while a plain `<img>` defaults to
`no-cors`. Chrome rejected that request with
`ERR_BLOCKED_BY_RESPONSE.NotSameOriginAfterDefaultedToSameOriginByCoep` and the
identity component correctly handled the resulting image error as a fallback.

## Fix

`FeishuIdentityMark` now sets `crossorigin="anonymous"` on the trusted HTTPS
avatar image. This selects a CORS request that the Feishu CDN explicitly allows
and preserves the page's cross-origin isolation. It avoids weakening COEP and
does not add a backend fetch proxy or a new server-side request surface.

## Verification

- The focused wiring test fails without the CORS attribute and passes with it.
- Both Feishu browser scenarios assert the rendered image's CORS mode.
- The complete mocked Chrome gate passes all nine scenarios under the Vite
  COOP/COEP headers.
- A task-owned Vite preview loads the current Ted avatar from the real Feishu
  CDN and checks its decoded dimensions without changing the shared service.
