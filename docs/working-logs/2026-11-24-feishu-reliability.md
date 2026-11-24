# Feishu reliability follow-up

The integration review found that unknown chat mode could produce the wrong reply encoding, read failures could be mistaken for uncertain sends, missing quoted context could silently omit images, and mention polling could stall on a full page. The fixes require known chat mode before encoding, classify uncertainty at message submission, disclose inaccessible quoted context, and process all mention pages before advancing the watermark. User-client data models were extracted to keep the client within the repository size gate. Focused regression tests cover these boundaries; full verification and independent review are required before integration.

If chat mode cannot be read and the error reply cannot be sent, release the unexecuted claim. SDK and Web events are not placed in the poller queue; the original event channel or user can retry. Poller commands do not gain automatic retry.

The cross-origin image browser check also failed on the integration baseline. A follow-up candidate probe observed a rewritten block reuse an already loaded image without a new network request. Its assertion now waits for the text update and observes subsequent requests, allowing reuse while still rejecting a no-CORS retry or multiple reloads.
