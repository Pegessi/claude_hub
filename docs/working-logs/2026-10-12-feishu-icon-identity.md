# Feishu icon identity semantics

## Scope

This follow-up separates three UI concepts that had been rendered through one
image URL: the pane's connection control, the Feishu product brand and the
identity of a bound Bot. It does not change pairing, credentials, message
delivery or the accepted Feishu message types.

## Behavior

- The pane header keeps its original link glyph and status dot. It remains a
  connection control rather than changing appearance with whichever Bot is
  bound.
- The connection panel heading always uses a local outline Feishu SVG. The mark
  uses `currentColor`, contains no embedded bitmap and remains sharp in dark
  and light themes.
- A bound connection card uses the Bot's trusted HTTPS avatar. Missing or
  failed images fall back to the same outline Feishu mark.
- A Feishu message first looks up the current Bot pool by the message's exact
  App ID, then its durable event avatar. A current Chat binding is considered
  only for legacy provenance without an App ID, so old messages cannot acquire
  an unrelated Bot's face. The transcript's memo key includes the pool revision
  so a Bot avatar that loads after history is painted still appears.
- Avatar failure state is keyed by the complete Bot or message identity. A
  failed request for one Bot therefore does not suppress a retry for another
  Bot that happens to have the same avatar URL.

## Verification

- `node --test frontend/tests/feishuBindingWiring.test.mjs`
- `./scripts/verify.sh frontend-types`
- `./scripts/verify.sh frontend-lint`
- `./scripts/verify.sh frontend-tests`
- `./scripts/verify.sh frontend-build`
- `CLAUDE_HUB_BROWSER_EXECUTABLE=... CLAUDE_HUB_BROWSER_PORT=5297 ./scripts/verify.sh browser-mocks`

The browser gate ran all nine mocked Chrome scenarios. It verified the desktop
and mobile layouts, original header glyph, outline panel mark, failed-avatar
fallback, cross-Chat same-URL retry and real Bot avatars on bound cards and
message provenance. All APIs were mocked; this is not a live Feishu connectivity
test. The task handoff records the exact evidence directory and final review.
