"""Diff review comments smoke: line gutter, inline comment, tray, one message.

Mounts the real `ChatToolGroup` diff and `ChatReviewTray` on the
`diff_review_harness.html` page with a fixture Codex patch, so the surface is
exercised without a Hub backend. Nothing is sent: the composed message is read
out of the harness's own draft queue, which is where the real composer queue
would receive it.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_diff_review.py \
    http://127.0.0.1:5394/tests/diff_review_harness.html /tmp/diff-review-ui [browser]
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}
HARNESS_PATH = "/tests/diff_review_harness.html"


async def main(browser_run: BrowserRun) -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    url, output_arg = sys.argv[1:3]
    parsed = urlparse(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "::1")
        or parsed.port in (None, 5173, 8173, 10025, 18183)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != HARNESS_PATH
    ):
        raise SystemExit(
            "Use an owned numeric-loopback Vite server and the diff review harness path"
        )

    output = Path(output_arg)
    output.mkdir(parents=True, exist_ok=True)
    page_errors: list[str] = []
    api_requests: list[str] = []
    notes: list[str] = []
    # The mock owner reads this file back and requires "result": "PASS" before it
    # records a passing scenario; the lifecycle.json next to it only reports the
    # runner's own process outcome.
    report = {
        "result": "FAIL",
        "scenario": "diff review comments",
        "notes": notes,
        "api_requests": api_requests,
        "page_errors": page_errors,
        "screenshots": [],
        "boundary": (
            "Real diff card and review tray components over a fixture patch; the page calls "
            "no Hub API and no backend or provider traffic is involved."
        ),
    }
    failure: BaseException | None = None
    page = None

    def note(text: str) -> None:
        notes.append(text)
        print(f"- {text}", flush=True)

    try:
        async with browser_run.context(viewport=DESKTOP) as context:
            page = await context.new_page()
            page.on("pageerror", lambda error: page_errors.append(f"pageerror: {error}"))
            page.on(
                "console",
                lambda message: (
                    page_errors.append(f"console.{message.type}: {message.text}")
                    if message.type == "error"
                    else None
                ),
            )
            page.on(
                "request",
                lambda request: (
                    api_requests.append(request.url) if "/api/" in request.url else None
                ),
            )
            await page.goto(url, wait_until="load")

            diff = page.locator(".activity-diff")
            await expect(diff).to_be_visible(timeout=15_000)

            # 1. Every file in the patch gets a header row, and numbered hunks put a
            #    line number in the gutter next to the code.
            files = page.locator(".activity-diff-file-name")
            await expect(files).to_have_count(2)
            await expect(files.first).to_have_text(
                "backend/claude_hub/services/terminal_runtime/manager/status.py",
            )
            gutter = await page.eval_on_selector_all(
                ".activity-diff-num",
                "nodes => nodes.map(node => node.textContent.trim())",
            )
            assert gutter[1:5] == ["236", "237", "238", "238"], f"gutter numbers: {gutter[:6]}"
            assert gutter.count("") == 2, f"file headers carry no line number: {gutter}"
            note(f"gutter shows {len([value for value in gutter if value])} numbered rows")

            async def comment(row_text: str, text: str, *, shift: bool = False) -> None:
                row = page.locator(".activity-diff-line", has_text=row_text).first
                await row.scroll_into_view_if_needed()
                await row.hover()
                add = row.locator(".activity-diff-add")
                if shift:
                    await add.click(modifiers=["Shift"])
                else:
                    await add.click()
                await expect(page.locator(".activity-diff-editor")).to_be_visible()
                textarea = page.locator(".activity-diff-editor textarea")
                await expect(textarea).to_be_focused()
                await textarea.fill(text)
                await page.locator(".activity-diff-editor-save").click()
                await expect(page.locator(".activity-diff-editor")).to_have_count(0)

            # 2. A comment pins under the row it was written on and joins the tray.
            await comment(
                'snapshot["pending_question"]',
                "Expose this only when a question is really pending.",
            )
            await expect(page.locator(".activity-diff-note")).to_have_count(1)
            await expect(page.locator(".activity-diff-note-text")).to_have_text(
                "Expose this only when a question is really pending.",
            )
            await expect(page.locator(".review-tray-count")).to_contain_text("1 comment")

            # 3. Shift-click selects a range: one comment that names the span.
            first = page.locator(".activity-diff-line", has_text="const line = lines[index]").first
            await first.scroll_into_view_if_needed()
            await first.hover()
            await first.locator(".activity-diff-add").click()
            await expect(page.locator(".activity-diff-editor")).to_be_visible()
            await page.keyboard.press("Escape")
            await expect(page.locator(".activity-diff-editor")).to_have_count(0)
            await comment(
                'out.push({ sign: "@", text: line, hunk })',
                "Comment the whole hunk, not just one row.",
                shift=True,
            )
            await expect(page.locator(".activity-diff-editor-target")).to_have_count(0)
            await expect(page.locator(".review-tray-count")).to_contain_text("2 comments")
            await expect(page.locator(".review-tray-jump").last).to_contain_text("269-271")
            note("shift-click range composed as utils/toolActivity.ts:269-271")

            # 4. Clicking the tray entry reveals its row.
            await page.locator(".activity-diff").evaluate("node => { node.scrollTop = 0 }")
            await page.locator(".review-tray-jump").last.click()
            marked = page.locator(".activity-diff-line--target")
            await expect(marked).to_have_count(1)
            await expect(marked).to_have_attribute("data-diff-row", "15")
            note("tray jump highlighted the anchored row")

            await page.screenshot(path=str(output / "diff-review-desktop.png"), full_page=False)
            report["screenshots"].append("diff-review-desktop.png")

            # 5. Notes can be edited and removed from the tray.
            await page.locator(".review-tray-item-actions button", has_text="Edit").first.click()
            editor = page.locator(".review-tray-editor textarea")
            await expect(editor).to_be_visible()
            await editor.fill("Expose the pending-question flag only when it is set.")
            await page.locator(".review-tray-item-actions button", has_text="Save").click()
            await expect(page.locator(".review-tray-note").first).to_have_text(
                "Expose the pending-question flag only when it is set.",
            )
            await page.locator(".review-tray-item-actions button", has_text="Remove").last.click()
            await expect(page.locator(".review-tray-count")).to_contain_text("1 comment")

            # 6. The narrow layout keeps every tray control inside the viewport.
            await page.set_viewport_size(MOBILE)
            await expect(page.locator(".review-tray")).to_be_visible()
            overflow = await page.evaluate(
                """() => {
                  const tray = document.querySelector('.review-tray');
                  const box = tray.getBoundingClientRect();
                  const outside = [...tray.querySelectorAll('button')]
                    .map(node => node.getBoundingClientRect())
                    .filter(rect => rect.right > innerWidth + 1 || rect.left < -1);
                  return { trayRight: box.right, width: innerWidth, outside: outside.length };
                }""",
            )
            assert overflow["trayRight"] <= overflow["width"] + 1, f"tray overflows: {overflow}"
            assert overflow["outside"] == 0, f"tray buttons outside the viewport: {overflow}"
            await page.screenshot(path=str(output / "diff-review-mobile.png"), full_page=False)
            report["screenshots"].append("diff-review-mobile.png")
            note("mobile tray stays inside 390px with reachable buttons")
            await page.set_viewport_size(DESKTOP)

            # 7. A warm-cache history reload renumbers the turn's parts, so a
            #    saved row can outlive the row it was written on: the tray says
            #    so instead of silently doing nothing.
            await page.evaluate("""() => {
                  const harness = window.__DIFF_REVIEW__;
                  window.__PRENUMBERED__ = harness.part.key;
                  harness.part.key = `${harness.part.key}-reloaded`;
                  harness.expandedToolGroups.set(harness.turn.key, new Set([harness.part.key]));
                }""")
            await expect(page.locator(".activity-diff-note")).to_have_count(0)
            await page.locator(".review-tray-jump").first.click()
            stale = page.locator(".review-tray-stale")
            await expect(stale).to_be_visible()
            await expect(stale).to_have_text(
                "Line no longer in this turn. The note still names its file and line."
            )
            await expect(page.locator(".activity-diff-line--target")).to_have_count(0)
            await page.screenshot(path=str(output / "diff-review-stale.png"), full_page=False)
            report["screenshots"].append("diff-review-stale.png")
            note("a renumbered part marks the entry as no longer in this turn")

            # 8. When the row resolves again the mark clears and the jump works.
            await page.evaluate("""() => {
                  const harness = window.__DIFF_REVIEW__;
                  harness.part.key = window.__PRENUMBERED__;
                  harness.expandedToolGroups.set(harness.turn.key, new Set([harness.part.key]));
                }""")
            await page.locator(".review-tray-jump").first.click()
            await expect(page.locator(".review-tray-stale")).to_have_count(0)
            await expect(page.locator(".activity-diff-line--target")).to_have_count(1)
            note("the mark clears once the row resolves again")

            # 9. Send composes exactly one message through the composer queue.
            await page.locator(".review-tray-send").click()
            sent = await page.evaluate("""() => {
                  const harness = window.__DIFF_REVIEW__;
                  return {
                    drafts: harness.draftQueue.value.map(draft => draft.message),
                    comments: harness.review.comments.value.length,
                    tray: Boolean(document.querySelector('.review-tray')),
                  };
                }""")
            assert len(sent["drafts"]) == 1, f"exactly one queued message: {sent}"
            message = sent["drafts"][0]
            lines = message.split("\n")
            assert lines[0] == "Review comments (1):", message
            assert lines[1] == (
                "1. backend/claude_hub/services/terminal_runtime/manager/status.py:239 — "
                '`snapshot["pending_question"] = runtime.has_pending_question()` — '
                "Expose the pending-question flag only when it is set."
            ), message
            assert (
                sent["comments"] == 0 and sent["tray"] is False
            ), f"tray cleared after send: {sent}"
            note("send queued one message: " + json.dumps(message))

            assert api_requests == [], f"the harness must not call the Hub API: {api_requests}"
            assert page_errors == [], f"page errors: {page_errors}"
            report["result"] = "PASS"

    except BaseException as exc:
        failure = exc
        report["error_type"] = type(exc).__name__
        if page is not None:
            try:
                await page.screenshot(path=str(output / "failure.png"), full_page=True)
            except Exception:
                pass

    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    run_browser_main(main)
