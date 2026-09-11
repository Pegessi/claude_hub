/**
 * Build a deep link URL that points at a specific tab.
 *
 * The link is the current origin + pathname with a `tab` query parameter, so
 * opening it (or navigating to it) loads the app and routes to that session.
 * The tab id is URL-encoded.
 */
export function buildTabLink(tabId: string): string {
  return `${window.location.origin}${window.location.pathname}?tab=${encodeURIComponent(tabId)}`
}

/**
 * Extract a tab id from a URL query string (the part after "?").
 *
 * Returns null when there is no `tab` parameter. Pure function — pass
 * `location.search` (or any query string) and it does the parsing.
 */
export function parseTabDeepLink(search: string): string | null {
  return new URLSearchParams(search).get('tab')
}
