# Feishu Bot quoted message context

The inbound SDK event already carries parent and root IDs, but Chat turn metadata and provider text omitted them. Before dispatch, retrieve the parent with the owning Bot identity and attach a bounded summary to that message. The existing interrupted-turn carryover retains each prepared message text. Failed lookups are explicit. Historical v3 turns reconstruct from their stored optional IDs.

The parent text is supplied as contextual content and images are indicated, not forwarded as attachments. Validate with the focused Feishu dispatch and merged-forward suites, backend type and format checks, and the repository verification entry point.

Coalesced v3 sources include an ordered per-message ID, parent ID and root ID list. Stored turns reconstruct the same source on edit; interrupted earlier messages retain their prepared quote in visible text.

Feishu may return only a client-upgrade placeholder for Schema 2.0 cards. A Bot-scoped in-memory cache retains up to 500 acknowledged outbound card originals, with the pre-normalized Markdown saved for titled push notices. The cache does not survive restarts and cannot recover older or evicted cards; the API fallback explicitly marks missing card body content.
