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

/**
 * Build the share text for a tab: the clean deep link on the first line (so it
 * stays clickable and paste-able into a browser), followed by a short hint that
 * teaches an AI agent how to resolve the link to the conversation.
 *
 * The hint points at the existing GET /api/tabs/{id}/stream/events endpoint,
 * which returns the conversation as paged JSON.
 */
export function buildTabShareText(tabId: string): string {
  const link = buildTabLink(tabId)
  const eventsUrl = `${window.location.origin}/api/tabs/${encodeURIComponent(tabId)}/stream/events?since_sequence=-1&limit=5000`
  return [
    link,
    '',
    'Claude Hub deep link. To read this conversation, fetch:',
    `  ${eventsUrl}`,
    '(authenticated GET; returns paged JSON — follow next_sequence until has_more=false)',
  ].join('\n')
}
