# Feishu Bot quoted message context

The inbound SDK event already carries parent and root IDs, but Chat turn metadata and provider text omitted them. Before dispatch, retrieve the parent with the owning Bot identity and attach a bounded summary to that message. The existing interrupted-turn carryover retains each prepared message text. Failed lookups are explicit. Historical v3 turns reconstruct from their stored optional IDs.

The parent text is supplied as contextual content and images are indicated, not forwarded as attachments. Validate with the focused Feishu dispatch and merged-forward suites, backend type and format checks, and the repository verification entry point.
