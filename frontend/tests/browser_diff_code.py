"""Diff code highlighting gate: token colours, the language chip, inert rows.

Mounts the real `ChatToolGroup` diff card on `diff_code_harness.html` with a
fixture patch, waits for the highlighting chunk, and reads what the browser
actually painted — computed colours, not class names. It also pins the parts of
the card that syntax highlighting must NOT change: the line-number and sign
columns, the +/- tint, and the fact that a row whose text is a script tag stays
inert text.

Run only with a dedicated Vite server, never 5173/8173:
  python tests/browser_diff_code.py \
    http://127.0.0.1:5397/tests/diff_code_harness.html /tmp/diff-code-ui [browser]
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from browser_lifecycle import BrowserRun, run_browser_main
from playwright.async_api import expect

HARNESS_PATH = "/tests/diff_code_harness.html"
# The fixture patch: a Python file, then a file whose extension the bundled
# highlighter has no grammar for. Rows, in reading order, and what the card must
# still show in the gutter next to each of them.
GUTTER = ["", "236", "237", "238", "238", "239", "240", "241", "242", "243", "244", "245", "246"]
# The second file restarts its numbering; its hunk header carries no number either.
GUTTER += ["", "1", "2", "1", "2"]
SIGNS = ["", "", "", "-", "+", "+", "+", "", "", "", "", "+", "+", "", "-", "-", "+", "+"]
# 400 rows is the card's ceiling (the diff builder caps a diff there and appends
# one truncation row, so the fixture lands one row past it).
BIG_DIFF_ROWS = 401


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
        raise SystemExit("Use an owned numeric-loopback Vite server and the diff code harness path")

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
        "scenario": "diff code highlighting",
        "notes": notes,
        "api_requests": api_requests,
        "page_errors": page_errors,
        "screenshots": [],
        "boundary": (
            "Real diff card component over a fixture patch; the page calls no Hub API and "
            "no backend or provider traffic is involved."
        ),
    }
    failure: BaseException | None = None
    page = None

    def note(text: str) -> None:
        notes.append(text)
        print(f"- {text}", flush=True)

    try:
        async with browser_run.context(viewport={"width": 1440, "height": 900}) as context:
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

            python_card = page.locator('[data-case="python"]')
            await expect(python_card.locator(".activity-diff")).to_be_visible(timeout=15_000)

            # 1. The card asks for the highlighter itself, and the rows are
            #    repainted once the chunk lands. A cold server has to compile and
            #    pre-bundle that chunk on first use, so this wait is generous.
            await page.wait_for_function(
                "() => window.__DIFF_CODE__?.ready?.value === true", timeout=30_000
            )
            # `to_be_attached` retries, so a first-run dependency optimization
            # that reloads the page cannot make this read a half-painted card.
            tokens = python_card.locator('.activity-diff-text span[class*="hljs-"]')
            await expect(tokens.first).to_be_attached(timeout=30_000)
            count = await tokens.count()
            assert count > 0, "the Python rows were never tokenised"
            note(f"{count} token spans painted inside .activity-diff-text")

            # 2. Real colours, read off the rendered spans: a Python line reads
            #    with at least two distinct token colours, not one wash.
            colours = await python_card.evaluate("""card => {
                  const seen = {};
                  for (const span of card.querySelectorAll('.activity-diff-text span')) {
                    const colour = getComputedStyle(span).color;
                    for (const name of span.classList) {
                      if (name.startsWith('hljs-')) seen[name] ??= colour;
                    }
                  }
                  return seen;
                }""")
            keyword = colours.get("hljs-keyword")
            string = colours.get("hljs-string")
            literal = colours.get("hljs-literal")
            assert keyword and string and literal, f"token kinds painted: {colours}"
            painted = {keyword, string, literal}
            assert len(painted) == 3, f"distinct token colours: {colours}"
            note(f"keyword {keyword} / string {string} / literal {literal} are three colours")

            # 3. The +/- tint and the sign column still tell the two sides apart.
            tints = await python_card.evaluate("""card => ({
                  add: getComputedStyle(card.querySelector('.activity-diff-line--add')).backgroundColor,
                  del: getComputedStyle(card.querySelector('.activity-diff-line--del')).backgroundColor,
                  ctx: getComputedStyle(card.querySelector('.activity-diff-line--ctx')).backgroundColor,
                })""")
            assert tints["add"] != tints["ctx"], f"added rows keep their tint: {tints}"
            assert tints["del"] != tints["ctx"], f"removed rows keep their tint: {tints}"
            note(f"add {tints['add']} / del {tints['del']} / ctx {tints['ctx']}")

            # 4. A row whose text is a script tag is text: escaped in the markup,
            #    and no element was created from it.
            script_row = python_card.locator(".activity-diff-line", has_text="<script>")
            await expect(script_row).to_have_count(1)
            inert = await script_row.evaluate("""row => ({
                  html: row.querySelector('.activity-diff-text').innerHTML,
                  text: row.querySelector('.activity-diff-text').textContent,
                  scripts: row.querySelectorAll('script').length,
                  cardScripts: row.closest('.activity-diff').querySelectorAll('script').length,
                })""")
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in inert["html"], inert["html"]
            assert inert["text"] == '    label = f"<script>alert(1)</script>"', inert["text"]
            assert inert["scripts"] == 0 and inert["cardScripts"] == 0, f"inert row: {inert}"
            note("a <script> row stayed inert text")

            # 5. The language chip names the grammar, and only where there is one.
            chips = python_card.locator(".activity-diff-file-lang")
            await expect(chips).to_have_count(1)
            await expect(chips.first).to_have_text("Python")
            await expect(python_card.locator(".activity-diff-file")).to_have_count(2)
            note("one chip: Python; the .log file header carries none")

            # 6. The line-number and sign columns are exactly as they were: no
            #    spans leaked into them, and nothing shifted.
            gutter = await python_card.evaluate("""card => ({
                  nums: [...card.querySelectorAll('.activity-diff-num')]
                    .map(node => node.textContent.trim()),
                  signs: [...card.querySelectorAll('.activity-diff-sign')]
                    .map(node => node.textContent.trim()),
                  nested: [...card.querySelectorAll('.activity-diff-num, .activity-diff-sign')]
                    .filter(node => node.childElementCount > 0).length,
                })""")
            assert gutter["nums"] == GUTTER, f"gutter numbers changed: {gutter['nums']}"
            assert gutter["signs"] == SIGNS, f"sign column changed: {gutter['signs']}"
            assert gutter["nested"] == 0, f"highlighting must not reach the gutter: {gutter}"
            note(f"gutter and sign columns unchanged across {len(GUTTER)} rows")

            # 7. A click on a row's gutter button still opens the comment editor,
            #    so nothing about the row's interactions depends on the markup
            #    being plain text.
            add_row = python_card.locator(".activity-diff-line", has_text="pending_question").first
            await add_row.scroll_into_view_if_needed()
            await add_row.hover()
            await add_row.locator(".activity-diff-add").click()
            await expect(python_card.locator(".activity-diff-editor")).to_be_visible()
            await page.keyboard.press("Escape")
            await expect(python_card.locator(".activity-diff-editor")).to_have_count(0)
            note("a highlighted row still anchors an inline review comment")

            # 8. A theme flip keeps every token span and repaints it from the
            #    other palette, so a card is never left in the old theme.
            before_theme = keyword
            await page.evaluate("() => { document.documentElement.dataset.theme = 'light' }")
            await page.wait_for_timeout(200)
            light = await python_card.evaluate("""card => {
                  const span = card.querySelector('.activity-diff-text span.hljs-keyword');
                  return span ? getComputedStyle(span).color : null;
                }""")
            assert light is not None and light != before_theme, f"theme flip repaint: {light}"
            await page.evaluate("() => { document.documentElement.dataset.theme = 'dark' }")
            await page.wait_for_timeout(200)
            note(f"the light palette painted keywords {light} instead of {before_theme}")

            await page.screenshot(path=str(output / "diff-code-desktop.png"), full_page=False)
            report["screenshots"].append("diff-code-desktop.png")

            # 9. A diff past the row ceiling keeps plain text — but the same
            #    gutter, tint and chip, and the text is untouched.
            big_card = page.locator('[data-case="big"]')
            await expect(big_card.locator(".activity-diff")).to_be_visible()
            big = await big_card.evaluate("""card => ({
                  files: card.querySelectorAll('.activity-diff-file').length,
                  lines: card.querySelectorAll('.activity-diff-line').length,
                  tokens: card.querySelectorAll('.activity-diff-text span[class*="hljs-"]').length,
                  plain: card.querySelectorAll('.activity-diff-text').length,
                  chip: card.querySelector('.activity-diff-file-lang')?.textContent ?? '',
                })""")
            assert big["files"] + big["lines"] == BIG_DIFF_ROWS, f"big diff rows: {big}"
            assert big["tokens"] == 0, f"a diff past the ceiling must stay plain: {big}"
            assert big["plain"] == big["lines"], f"every row still rendered: {big}"
            assert big["chip"] == "Python", f"the chip does not depend on the ceiling: {big}"
            note(f"a {BIG_DIFF_ROWS}-row diff rendered {big['plain']} plain rows and no spans")
            await page.screenshot(path=str(output / "diff-code-large.png"), full_page=False)
            report["screenshots"].append("diff-code-large.png")

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
