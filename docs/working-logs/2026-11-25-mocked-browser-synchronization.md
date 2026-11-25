# Mocked browser synchronization

The Markdown reader samples opacity while the code overflow hint is fading.
Rounding a value such as `0.99858` to 1 released the wait before the next
assertion compared the raw value with 1. The wait now compares raw values
with the expected endpoints and allows 40 polls at 50 ms intervals. Unit
coverage exercises both transition directions, a missing hint, and the
timeout budget.

Restoring the sandboxed HTML preview after copying its source takes two
synchronization steps. The test observes the asynchronous `Copied` status
before restoring the preview. After scrolling the external link into view,
it waits two animation frames in each document and polls the hit targets.
The parent must hit the iframe at the link's page coordinates and the child
must hit the link or one of its descendants. The popup assertion still
uses a normal Playwright click.

The browser checks run Chromium against the real frontend with mocked API
responses. The two affected scenarios can be repeated independently. The
complete suite runs through `verify.sh browser-mocks`, which is an explicit
gate alongside `verify.sh all`.
