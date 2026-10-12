# Monochrome Feishu connection mark

## Scope

The pane connection entry and the connection-panel heading now render one
shared local SVG. The drawing follows the current Feishu bird's three-part
silhouette, but uses Claude Hub's `currentColor` outline treatment instead of
the product icon's blue and green fills.

Bot identity is deliberately unchanged: a connected card and matching message
provenance still prefer the Bot's trusted HTTPS avatar and fall back to this
brand mark only when an avatar is unavailable or fails to load. The connection
status dot also remains independent of the icon.

## Verification contract

- Focused source tests require both connection locations to use
  `FeishuBrandMark` and require its three monochrome paths.
- The Bot-components browser scenario compares the rendered paths in the pane
  entry and open panel at desktop and mobile sizes.
- Repository frontend checks and the full mocked browser suite cover component
  compilation, theme-aware styling, control geometry and existing Feishu flows.
