"""Exercise Chat image fidelity and the Cursor sub-agent card in the real SPA.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_chat_image_quality.py <owned-loopback-url> <new-output-dir> <browser> [--html-report FILE]

All APIs are mocked. No Hub backend or provider is contacted.
Use --html-ids-only instead of --html-report FILE for the standalone ID regression;
that focused mode works with the built SPA as well as the task-owned Vite server.
"""

from __future__ import annotations

import asyncio
import base64
import json
import posixpath
import re
import struct
import sys
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "image-quality-tab"
OTHER_TAB = "other-image-tab"
THIRD_TAB = "third-image-tab"
LONG_FILENAME = "generated_output_" * 13 + ".py"
LONG_CODE_SOURCE = "print('This source must remain readable')\n" * 50
REGISTERED_HTML_PATH = "/fixture/task-assets/report.html"
REGISTERED_ID_HTML_PATH = "/fixture/task-assets/id-report.html"
WINDOW_NAMED_IDS = ("parent", "postMessage", "scrollTo", "Element", "location", "crypto")
UNREGISTERED_HTML_PATH = "/fixture/unregistered.html"
UNAUTHORIZED_HTML_PATH = "/fixture/unauthorized.html"
HTML_EXTERNAL_LINK = "https://example.com/html-preview-user-link"
TRUNCATED_HTML_SOURCE = "<!doctype html><html><body><h1>Incomplete HTML</h1><script>"
TURN = "cursor-turn"
NOW = "2026-10-12T08:00:00Z"
REPORT = "Run B regressed on 3 cases."
OUTPUT_PATH = "/tmp/dotbm/dot-mobile-work.png"
OUTPUT_TEXT = (
    "[View screenshot](/tmp/result.png)\n\n![Missing screenshot](/tmp/missing.png)"
    "\n\n[排查记录](<reports/排查 记录.md>)"
    "\n\n[code.py](code.py) · [data.json](data.json) · [large.log](large.log)"
    " · [empty.txt](empty.txt) · [slow.json](slow.json) · [archive.zip](archive.zip)"
    f"\n\n[Long generated code]({LONG_FILENAME})"
    "\n\n[HTML report](report.html) · [HTM report](report.htm) · [Truncated HTML](truncated.html)"
    " · [HTML ID report](id-report.html)"
    f"\n\n[Registered HTML](HTML_PREVIEW_ORIGIN{REGISTERED_HTML_PATH})"
    f" · [Unregistered HTML](HTML_PREVIEW_ORIGIN{UNREGISTERED_HTML_PATH})"
)
PYTHON_SOURCE = (
    'payload = "<script>window.__attachmentUnsafe = true</script>"\n'
    "print(payload)\n"
    "# Long source line: " + "preserve_spacing_and_text " * 40 + "\n"
)
JSON_SOURCE = '{\n  "markup": "<img src=x onerror=alert(1)>",\n  "value": 42\n}\n'
LOG_SOURCE = "First available log line\n" * 30
LONG_MARKDOWN_SOURCE = (
    "# Long document\n\n"
    + "\n\n".join(
        f"Record {index}: this paragraph keeps the reader scrollable." for index in range(40)
    )
    + "\n\n[Read Python](../code.py)\n"
)
MARKDOWN_PATH = "/fixture/project/reports/排查 记录.md"
MARKDOWN_SOURCE = (
    "# Investigation record\n\nThe **decoded output** is stable.\n\n"
    "| Case | A | B | C | D | E | F | G |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
    "| Sample | alpha | beta | gamma | delta | epsilon | zeta | eta |\n\n"
    '```python\nprint("preview")\n```\n\n'
    "[Details](details.md#evidence)\n\n[Missing document](missing.md)\n\n"
    "[Python source](../code.py)\n\n[JSON data](../data.json)\n\n[Long document](long.md)\n\n"
    "[Scoped preview](/?markdown=%2Ffixture%2Fproject%2Freports%2Fdetails.md&scope=other-tab#evidence)\n\n"
    "![Parent plot](../plot.png)\n\n"
    "[Restricted document](denied.md)\n\n"
    "[External documentation](https://example.com/manual.md)\n\n"
    "<script>window.__markdownUnsafe = true</script>\n\n"
    '<a href="javascript:alert(1)" onclick="window.__markdownUnsafe = true">Unsafe link</a>\n\n'
    '<img src="javascript:alert(1)" onerror="window.__markdownUnsafe = true">'
)
RICH_PREFIX = "Rich content preview\n\n\\[\\frac{1}{"
CODE_TEXT = 'formula = r"\\[x\\]"\nprint("<tag> & $HOME")\n'
RICH_SUFFIX = (
    "2}\\]\n\n"
    r"Inline $x_1^2$ and \(p=\frac{1}{2}\). Prices $5 and $10. Literal `$x$`."
    '\n\nShell $HOME.${USER}; echo "$HOME" "$USER"; echo "$?".'
    "\n\n\\[\\Delta=\\mathrm{KL}(A|EVO)-\\mathrm{KL}(A|B)\\]\n\n"
    "$$\\begin{pmatrix}1 & 2 \\\\ 3 & 4\\end{pmatrix}$$\n\n"
    "\\[" + "+".join([r"\frac{a_i^2+b_i^2}{1+\sqrt{c_i}}"] * 20) + "\\]\n\n"
    "| Formula | A | B | C | D | E | F | G |\n"
    "| --- | ---: | :---: | --- | --- | --- | --- | --- |\n"
    "| $z^3$ | alpha | beta | gamma | delta | epsilon | zeta | eta |\n\n"
    "```python\n" + CODE_TEXT + "```\n\n"
    r"\[\notARealCommand{<img src=x onerror=alert(1)>}\]"
    "\n\n[Formula $x$](/tmp/result.png)"
    '\n\n<span onclick="window.__unsafe=true">Sanitized text</span>'
)


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(
        b"\x00" + bytes((x * 7 + y) % 256 for x in range(width * 3)) for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def html_fixture() -> str:
    image = base64.b64encode(png(16, 10)).decode("ascii")
    rows = "".join(
        f"<tr><td>Case {index}</td><td>Static report content</td></tr>" for index in range(45)
    )
    return f"""<!doctype html>
<html><head><title>Authored HTML report</title>
<base id="untrusted-base" href="https://html-preview.invalid/base/">
<meta http-equiv="refresh" content="0;url=https://html-preview.invalid/refresh">
<link rel="stylesheet" href="https://html-preview.invalid/stylesheet.css">
<style>
@import url('https://html-preview.invalid/import.css');
body {{ margin: 0; padding: 16px; background: #f3f6fa; color: #18222f; font: 14px sans-serif; }}
.report-title {{ color: rgb(18, 52, 86); border-bottom: 3px solid #123456; }}
.table-scroll {{ overflow: auto; max-width: 100%; }}
table {{ width: 1100px; border-collapse: collapse; }}
td {{ padding: 10px; border: 1px solid #456789; }}
.remote-background {{ background-image: url('https://html-preview.invalid/background.png'); }}
</style></head><body>
<h1 class="report-title">Authored HTML report</h1>
<a href="#details">Jump to details</a>
<a href="{HTML_EXTERNAL_LINK}" target="_top" ping="https://html-preview.invalid/ping">External reference</a>
<img alt="Embedded report one" src="data:image/png;base64,{image}" width="16" height="10">
<img alt="Embedded report two" src="data:image/png;base64,{image}" width="16" height="10">
<div class="remote-background">No automatic external resources</div>
<img id="untrusted-remote-image" src="https://html-preview.invalid/image.png" srcset="https://html-preview.invalid/image2.png 2x" onerror="window.__authoredHtmlRan = true">
<img id="untrusted-local-image" src="/api/html-preview-probe">
<script id="untrusted-script">window.__authoredHtmlRan = true; parent.__authoredHtmlRan = true; fetch('/api/html-preview-probe'); top.location = 'https://html-preview.invalid/top';</script>
<iframe id="untrusted-frame" src="https://html-preview.invalid/frame"></iframe>
<object id="untrusted-object" data="https://html-preview.invalid/object"></object>
<embed id="untrusted-embed" src="https://html-preview.invalid/embed">
<form id="untrusted-form" action="https://html-preview.invalid/form"><input autofocus><button>Submit unsafe form</button></form>
<a id="unsafe-link" href="javascript:window.__authoredHtmlRan=true" onclick="window.__authoredHtmlRan=true">Unsafe link</a>
<div class="table-scroll" role="region" aria-label="Report table" tabindex="0"><table><tbody>{rows}</tbody></table></div>
<h2 id="details" tabindex="0">Report details</h2><p>End of the static report.</p>
</body></html>"""


def html_id_fixture() -> str:
    image = base64.b64encode(png(16, 10)).decode("ascii")
    links = " ".join(f'<a href="#{name}">Jump {name}</a>' for name in WINDOW_NAMED_IDS)
    headings = "".join(
        f'<section style="margin-top:240px"><h3 id="{name}">Window {name} section</h3></section>'
        for name in WINDOW_NAMED_IDS
    )
    return f"""<!doctype html><html><head><style>
body {{ font: 14px sans-serif; }} h2 {{ color: rgb(2, 3, 4); }}
#target {{ color: rgb(210, 0, 0); border-left: 5px solid rgb(210, 0, 0); }}
</style></head><body>
<h1>ID preservation report</h1>
<a href="#target">Jump target</a> <a href="#details-safe">Jump safe control</a> {links}
<img id="getElementById" name="safe-getter-image" alt="Named getter image" src="data:image/png;base64,{image}">
<img id="addEventListener" name="safe-listener-image" alt="Named listener image" src="data:image/png;base64,{image}">
<div style="height:1800px"></div><h2 id="target" tabindex="0">Target section</h2>
<div style="height:260px"></div><h2 id="details-safe">Safe control section</h2>{headings}
<p style="margin-top:240px"><a href="#">Back to top</a></p>
<form id="removed-form"><input name="discarded-input"></form>
<iframe id="removed-frame" src="https://html-preview.invalid/removed-frame"></iframe>
<object id="removed-object" data="https://html-preview.invalid/removed-object"></object>
<meta id="removed-meta" http-equiv="refresh" content="0;url=https://html-preview.invalid/removed-meta">
<script id="untrusted-id-script">window.__idScriptRan = true; fetch('https://html-preview.invalid/id-probe');</script>
</body></html>"""


def png_size(data_url: str) -> tuple[int, int]:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    assert raw[12:16] == b"IHDR", raw[:16]
    return struct.unpack(">II", raw[16:24])


def event(
    sequence: int,
    event_type: str,
    payload: dict,
    message_id: str,
    turn: str = TURN,
    tab_id: str = TAB,
) -> dict:
    return {
        "stream_sequence": sequence,
        "session_id": f"terminal-{tab_id}",
        "tab_id": tab_id,
        "agent_type": "cursor",
        "type": event_type,
        "turn_id": turn,
        "message_id": message_id,
        "call_id": (payload.get("tool_call_id") if event_type.startswith("tool_call") else None),
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


EVENTS = [
    event(
        0,
        "turn_started",
        {"summary": "Compare runs", "mode": "default"},
        f"{TURN}:user",
    ),
    event(
        1,
        "tool_call_started",
        {
            "tool_call_id": "task-1",
            "name": "Task",
            "args": {
                "description": "比对评测",
                "prompt": "Compare run A and run B.",
                "subagentType": {"explore": {}},
                "agentId": "0104ad90-f345-4192-bd28-5545f1df1663",
            },
        },
        "task-1",
    ),
    event(
        2,
        "tool_call_completed",
        {
            "tool_call_id": "task-1",
            "status": "completed",
            "result": {
                "success": {
                    "conversationSteps": [{"assistantMessage": {"text": REPORT}}],
                    "agentId": "0104ad90-f345-4192-bd28-5545f1df1663",
                    "durationMs": 1200,
                }
            },
        },
        "task-1",
    ),
    event(
        3,
        "tool_call_started",
        {
            "tool_call_id": "image-read",
            "name": "Read",
            "args": {"file_path": OUTPUT_PATH},
        },
        "image-read",
    ),
    event(
        4,
        "tool_call_completed",
        {"tool_call_id": "image-read", "status": "completed"},
        "image-read",
    ),
    event(5, "text_delta", {"text": OUTPUT_TEXT}, f"{TURN}:assistant"),
    event(
        6,
        "turn_completed",
        {"status": "completed", "assistant_text": OUTPUT_TEXT},
        TURN,
    ),
]


async def click_timeline_link(page, link) -> float:
    """Measure scroll after actionability, without locator click's later auto-scroll."""
    await link.scroll_into_view_if_needed()
    await expect(link).to_be_visible()
    await expect(link).to_be_in_viewport()
    # A closing mobile sidebar can still cover the link after resize. Wait for
    # its real click point before recording the scroll position to preserve.
    ready = await page.wait_for_function(
        """el => {
            const timeline = el.closest('.structured-timeline')
            if (!el.isConnected || !timeline) return false
            const rect = el.getBoundingClientRect()
            const x = rect.left + rect.width / 2
            const y = rect.top + rect.height / 2
            const hit = document.elementFromPoint(x, y)
            if (rect.width <= 0 || rect.height <= 0 || x < 0 || x >= innerWidth
                || y < 0 || y >= innerHeight || !(hit === el || el.contains(hit))) return false
            return {x, y, scrollTop: timeline.scrollTop}
        }""",
        arg=await link.element_handle(),
        timeout=5000,
    )
    point = await ready.json_value()
    await ready.dispose()
    await page.mouse.click(point["x"], point["y"])
    return point["scrollTop"]


async def check_attachment_preview(page, context, output: Path, report: dict) -> None:
    """Use ordinary file links without leaving the Chat or losing its draft."""
    composer = page.locator(".composer-textarea:visible").first
    timeline = page.locator(".structured-timeline:visible").first
    draft = "Keep this unsent draft while reading attachments."
    await composer.fill(draft)
    original_url = page.url
    page_count = len(context.pages)
    link = page.get_by_role("link", name="排查记录", exact=True)
    scroll_top = await click_timeline_link(page, link)
    dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
    close = dialog.get_by_role("button", name="Close preview", exact=True)
    article = dialog.locator(".attachment-article")
    source = dialog.locator(".attachment-source")
    await expect(dialog).to_be_visible()
    assert await dialog.evaluate("el => el instanceof HTMLDialogElement && el.matches(':modal')")
    await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
    await expect(close).to_be_focused()
    assert page.url == original_url
    assert len(context.pages) == page_count
    await expect(composer).to_have_value(draft)
    for _ in range(12):
        await page.keyboard.press("Tab")
        assert await dialog.evaluate("el => el.contains(document.activeElement)")
    await dialog.get_by_role("button", name="Show source", exact=True).click()
    await expect(source).to_be_visible()
    assert await source.text_content() == MARKDOWN_SOURCE
    await expect(source.locator("script, a, img")).to_have_count(0)
    await dialog.get_by_role("button", name="Show preview", exact=True).click()
    await article.get_by_role("link", name="Details", exact=True).click()
    await expect(article.get_by_role("heading", name="Detail record", exact=True)).to_be_visible()
    await expect(article.locator("blockquote #evidence")).to_be_in_viewport()
    assert page.url == original_url
    await dialog.get_by_role("button", name="Back", exact=True).click()
    await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
    await expect(dialog.get_by_role("button", name="Back", exact=True)).to_have_count(0)
    await expect(dialog.locator(".attachment-title h1")).to_be_focused()
    assert await dialog.evaluate("el => el.contains(document.activeElement)")
    await article.get_by_role("button", name="Open image Parent plot", exact=True).click()
    await expect(dialog.get_by_role("link", name="Open original")).to_be_visible()
    await expect(dialog.locator(".structured-image-lightbox-img")).to_have_js_property(
        "naturalWidth", 1920
    )
    assert len(context.pages) == page_count
    await dialog.get_by_role("button", name="Back", exact=True).click()
    await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
    await article.get_by_role("link", name="Python source", exact=True).click()
    await expect(source).to_be_visible()
    assert await source.text_content() == PYTHON_SOURCE
    await expect(source.locator("script, a, img")).to_have_count(0)
    await dialog.get_by_role("button", name="Back", exact=True).click()
    await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
    await article.get_by_role("link", name="Long document", exact=True).click()
    nested_code = article.get_by_role("link", name="Read Python", exact=True)
    await nested_code.scroll_into_view_if_needed()
    body = dialog.locator(".attachment-body")
    reader_scroll = await body.evaluate("el => el.scrollTop")
    assert reader_scroll > 300
    await nested_code.click()
    await expect(source).to_be_visible()
    assert await source.text_content() == PYTHON_SOURCE
    await dialog.get_by_role("button", name="Back", exact=True).click()
    await expect(nested_code).to_be_in_viewport()
    assert abs(await body.evaluate("el => el.scrollTop") - reader_scroll) <= 2
    await dialog.get_by_role("button", name="Back", exact=True).click()
    await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
    await article.get_by_role("link", name="Missing document", exact=True).click()
    error = dialog.get_by_role("alert")
    await expect(error).to_be_visible()
    await expect(article).to_have_count(0)
    await error.get_by_role("button", name="Retry", exact=True).click()
    await expect(article.get_by_role("heading", name="Recovered document")).to_be_visible()
    await close.click()
    await expect(dialog).to_be_hidden()
    await expect(link).to_be_focused()
    assert abs(await timeline.evaluate("el => el.scrollTop") - scroll_top) <= 2
    await expect(composer).to_have_value(draft)

    for width in (390, 320):
        await page.set_viewport_size({"width": width, "height": 844})
        code_link = page.get_by_role("link", name="code.py", exact=True)
        scroll_top = await click_timeline_link(page, code_link)
        await expect(dialog).to_be_visible()
        await expect(source).to_be_visible()
        assert await source.text_content() == PYTHON_SOURCE
        await expect(source.locator(".hljs-string").first).to_be_attached()
        await expect(source.locator("script, a, img")).to_have_count(0)
        assert not await page.evaluate("Boolean(window.__attachmentUnsafe)")
        for name in ("Close preview", "Wrap lines", "Copy"):
            control = dialog.get_by_role("button", name=name, exact=True)
            await expect(control).to_be_in_viewport()
            bounds = await control.bounding_box()
            assert bounds and bounds["height"] >= 44, (width, name, bounds)
        wrap = dialog.get_by_role("button", name="Wrap lines", exact=True)
        pressed = await wrap.get_attribute("aria-pressed")
        await wrap.click()
        await expect(wrap).to_have_attribute(
            "aria-pressed", "false" if pressed == "true" else "true"
        )
        if await wrap.get_attribute("aria-pressed") != "true":
            await wrap.click()
        assert await dialog.evaluate("el => el.scrollWidth <= el.clientWidth")
        await dialog.get_by_role("button", name="Copy", exact=True).click()
        assert await page.evaluate("window.__copiedCode.at(-1)") == PYTHON_SOURCE
        await page.screenshot(path=str(output / f"attachment-code-{width}.png"), full_page=True)
        report["screenshots"].append(f"attachment-code-{width}.png")
        await page.go_back()
        await expect(dialog).to_be_hidden()
        assert page.url == original_url
        assert len(context.pages) == page_count
        await expect(code_link).to_be_focused()
        await expect(composer).to_have_value(draft)
        assert abs(await timeline.evaluate("el => el.scrollTop") - scroll_top) <= 2

        await page.get_by_role("link", name="data.json", exact=True).click()
        await expect(source).to_be_visible()
        assert await source.text_content() == JSON_SOURCE
        await expect(source.locator("script, a, img")).to_have_count(0)
        await close.click()
        await expect(dialog).to_be_hidden()
        await expect(composer).to_have_value(draft)

    await page.get_by_role("link", name="large.log", exact=True).click()
    await expect(source).to_be_visible()
    assert await source.text_content() == LOG_SOURCE
    await expect(dialog.get_by_role("status")).to_contain_text(
        re.compile("first|truncat|large", re.I)
    )
    await close.click()
    await expect(dialog).to_be_hidden()
    await page.get_by_role("link", name="empty.txt", exact=True).click()
    await expect(dialog).to_contain_text(re.compile("empty", re.I))
    await close.click()
    await expect(dialog).to_be_hidden()
    await page.get_by_role("link", name="archive.zip", exact=True).click()
    await expect(dialog.get_by_role("alert")).to_contain_text("no built-in preview")
    await expect(dialog.get_by_role("button", name="Retry", exact=True)).to_have_count(0)
    await close.click()
    await expect(dialog).to_be_hidden()

    await page.get_by_role("link", name="slow.json", exact=True).click()
    await expect(dialog.get_by_role("status")).to_contain_text(re.compile("loading", re.I))
    await close.click()
    await expect(dialog).to_be_hidden()
    await page.get_by_role("link", name="code.py", exact=True).click()
    await expect(source).to_be_visible()
    await asyncio.sleep(0.5)
    assert await source.text_content() == PYTHON_SOURCE
    await page.keyboard.press("Escape")
    await expect(dialog).to_be_hidden()
    await expect(composer).to_have_value(draft)

    await page.get_by_role("link", name="code.py", exact=True).click()
    await expect(source).to_be_visible()
    await close.click()
    await expect(dialog).to_be_hidden()
    await page.go_forward()
    await expect(dialog).to_be_hidden()
    await page.get_by_role("link", name="code.py", exact=True).click()
    await expect(source).to_be_visible()
    await close.click()
    await expect(dialog).to_be_hidden()
    await expect(composer).to_have_value(draft)
    assert page.url == original_url
    await composer.fill("")
    await page.set_viewport_size({"width": 1280, "height": 900})
    report["attachment_preview"] = {
        "ordinary_click_stays_in_chat": True,
        "native_modal_focus_containment": True,
        "markdown_source_is_inert": True,
        "nested_markdown_code_and_image_links": True,
        "nested_fragment_visible": True,
        "nested_reader_back_restores_scroll": True,
        "last_back_restores_dialog_title_focus": True,
        "missing_retry_preserves_target": True,
        "mobile_widths": [390, 320],
        "mobile_back_and_close_preserve_draft_scroll": True,
        "code_highlight_and_exact_copy": True,
        "json_source_is_inert": True,
        "truncated_and_empty_states": True,
        "unsupported_binary_explained_without_fetch": True,
        "late_response_does_not_replace_next_file": True,
        "forward_then_reopen_can_close": True,
    }


async def active_chat(page) -> str | None:
    return await page.evaluate("""async () => {
        const { useTerminalStore } = await import('/src/stores/terminalStore.ts')
        return useTerminalStore().activeTabId
    }""")


async def check_preview_routing(page, context, output: Path, report: dict) -> None:
    """Overlay history must not replay an older Chat deep link."""
    base = page.url.split("?", 1)[0]
    await page.set_viewport_size({"width": 1280, "height": 900})
    evidence: dict = {"dismissals": [], "same_document_navigation": []}
    report["attachment_routing"] = evidence
    b_row = page.locator(f'.chat-sidebar__item[data-tab-id="{OTHER_TAB}"] .chat-sidebar__item-main')
    dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
    for action in ("close", "escape", "browser_back"):
        await page.goto(base + f"?tab={TAB}", wait_until="domcontentloaded")
        await expect(
            page.locator(f'.chat-sidebar__item[data-tab-id="{TAB}"] .chat-sidebar__item-main')
        ).to_have_attribute("aria-current", "page")
        await b_row.click()
        await expect(b_row).to_have_attribute("aria-current", "page")
        composer = page.locator(".composer-textarea:visible").first
        draft = f"Keep Chat B draft after {action}."
        await composer.fill(draft)
        url_before = page.url
        await page.get_by_role("link", name="Other code", exact=True).click()
        await expect(dialog.locator(".attachment-source")).to_have_text(PYTHON_SOURCE)
        if action == "close":
            await dialog.get_by_role("button", name="Close preview", exact=True).click()
        elif action == "escape":
            await page.keyboard.press("Escape")
        else:
            await page.go_back()
        await expect(dialog).to_be_hidden()
        # The App's async deep-link handler runs after the modal's popstate listener.
        await asyncio.sleep(0.15)
        settled = await active_chat(page)
        evidence["dismissals"].append(
            {
                "action": action,
                "expected_tab": OTHER_TAB,
                "active_tab": settled,
                "url_before": url_before,
                "url_after": page.url,
            }
        )
        assert settled == OTHER_TAB, evidence["dismissals"][-1]
        await expect(b_row).to_have_attribute("aria-current", "page")
        await expect(composer).to_have_value(draft)
        assert page.url == url_before

    await page.get_by_role("link", name="Other code", exact=True).click()
    await expect(dialog).to_be_visible()
    await page.evaluate(
        """async tabId => {
        const { useTerminalStore } = await import('/src/stores/terminalStore.ts')
        useTerminalStore().setActiveTab(tabId)
    }""",
        THIRD_TAB,
    )
    await expect(dialog).to_be_hidden()
    await asyncio.sleep(0.15)
    evidence["programmatic_activation"] = {
        "expected_tab": THIRD_TAB,
        "active_tab": await active_chat(page),
    }
    assert evidence["programmatic_activation"]["active_tab"] == THIRD_TAB
    await expect(
        page.locator(f'.chat-sidebar__item[data-tab-id="{THIRD_TAB}"] .chat-sidebar__item-main')
    ).to_have_attribute("aria-current", "page")

    await page.goto(base + f"?tab={TAB}", wait_until="domcontentloaded")
    await b_row.click()
    await expect(b_row).to_have_attribute("aria-current", "page")
    # Seed two different same-document URLs; actual Back/Forward must still
    # deliver genuine route changes, not get swallowed with overlay cleanup.
    await page.evaluate(
        """([a, b]) => {
        const url = new URL(location.href)
        url.searchParams.set('tab', a)
        history.replaceState(null, '', url)
        url.searchParams.set('tab', b)
        history.pushState(null, '', url)
    }""",
        [TAB, OTHER_TAB],
    )
    for action, target in (("back", TAB), ("forward", OTHER_TAB)):
        if action == "back":
            await page.go_back()
        else:
            await page.go_forward()
        row = page.locator(f'.chat-sidebar__item[data-tab-id="{target}"] .chat-sidebar__item-main')
        await expect(row).to_have_attribute("aria-current", "page")
        await asyncio.sleep(0.15)
        actual = await active_chat(page)
        evidence["same_document_navigation"].append(
            {"action": action, "expected_tab": target, "active_tab": actual, "url": page.url}
        )
        assert actual == target
        assert parse_qs(urlparse(page.url).query)["tab"] == [target]
    await page.screenshot(path=str(output / "attachment-routing.png"), full_page=True)
    report["screenshots"].append("attachment-routing.png")
    await page.goto(base + f"?tab={TAB}", wait_until="domcontentloaded")


async def check_source_viewport(page, container) -> dict:
    source = container.locator(".attachment-source")
    await expect(source).to_have_text(LONG_CODE_SOURCE)
    metrics = await container.evaluate("""root => {
        const body = root.querySelector('.attachment-body')
        const source = root.querySelector('.attachment-source')
        const rect = body.getBoundingClientRect()
        const style = getComputedStyle(body)
        const top = Math.max(rect.top + parseFloat(style.paddingTop), 0)
        const bottom = Math.min(rect.bottom - parseFloat(style.paddingBottom), innerHeight)
        return {viewportHeight: innerHeight, bodyTop: rect.top, bodyBottom: rect.bottom,
          contentViewport: bottom - top, sourceLineHeight: parseFloat(getComputedStyle(source).lineHeight),
          headerBottom: root.querySelector('.attachment-header').getBoundingClientRect().bottom}
    }""")
    assert metrics["contentViewport"] >= metrics["sourceLineHeight"] * 2, metrics
    await source.focus()
    await page.keyboard.press("PageDown")
    await asyncio.sleep(0.15)
    assert await container.locator(".attachment-body").evaluate("el => el.scrollTop > 0")
    return metrics


async def check_mobile_actions(container, names: tuple[str, ...]) -> list[dict]:
    controls = []
    for name in names:
        control = container.get_by_role(
            "link" if name in {"Open separately", "Back to Hub"} else "button",
            name=name,
            exact=True,
        )
        await control.scroll_into_view_if_needed()
        await expect(control).to_be_in_viewport()
        geometry = await control.evaluate("""el => {
            const rect = el.getBoundingClientRect()
            const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
            return {name: el.textContent.trim(), height: rect.height, top: rect.top, bottom: rect.bottom,
              reachable: hit === el || el.contains(hit)}
        }""")
        controls.append(geometry)
        assert geometry["height"] >= 44 and geometry["reachable"], geometry
    return controls


async def check_long_attachment_mobile(page, context, output: Path, report: dict) -> None:
    await page.set_viewport_size({"width": 320, "height": 390})
    assert len(LONG_FILENAME) == 224
    await page.get_by_role("link", name="Long generated code", exact=True).click()
    dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
    await expect(dialog).to_be_visible()
    evidence = {"name_length": len(LONG_FILENAME), "viewport": {"width": 320, "height": 390}}
    report["long_filename_dialog"] = evidence
    evidence["source_geometry"] = await check_source_viewport(page, dialog)
    evidence["controls"] = await check_mobile_actions(
        dialog, ("Close preview", "Wrap lines", "Copy", "Refresh", "Open separately")
    )
    await dialog.get_by_role("button", name="Copy", exact=True).click()
    assert await page.evaluate("window.__copiedCode.at(-1)") == LONG_CODE_SOURCE
    await page.screenshot(path=str(output / "long-filename-dialog-320x390.png"), full_page=True)
    report["screenshots"].append("long-filename-dialog-320x390.png")
    await dialog.get_by_role("button", name="Close preview", exact=True).click()
    await expect(dialog).to_be_hidden()
    await page.set_viewport_size({"width": 1280, "height": 900})


async def close_secondary_page(secondary, parent) -> None:
    # Per-page emulation shares a native headless window. Restore its bounds so
    # returning to a desktop parent does not leave iframe hit testing mobile-sized.
    if parent.viewport_size and secondary.viewport_size != parent.viewport_size:
        await secondary.set_viewport_size(parent.viewport_size)
    await secondary.close()


async def check_standalone_attachment_mobile(page, context, output: Path, report: dict) -> None:
    await page.get_by_role("link", name="code.py", exact=True).click()
    dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
    async with context.expect_page() as opened:
        await dialog.get_by_role("link", name="Open separately", exact=True).click()
    reader = await opened.value
    reader.on("pageerror", lambda error: report["page_errors"].append(str(error)))
    evidence: dict = {}
    report["standalone_attachment_mobile"] = evidence
    try:
        await reader.wait_for_load_state("domcontentloaded")
        await reader.set_viewport_size({"width": 320, "height": 568})
        source = reader.locator(".attachment-source")
        await expect(source).to_have_text(PYTHON_SOURCE)
        assert parse_qs(urlparse(reader.url).query)["attachment"] == ["code.py"]
        await expect(reader.locator(".app, .structured-pane")).to_have_count(0)
        evidence["controls"] = await check_mobile_actions(
            reader.locator(".attachment-viewer"), ("Wrap lines", "Copy", "Refresh", "Back to Hub")
        )
        await reader.reload(wait_until="domcontentloaded")
        await expect(source).to_have_text(PYTHON_SOURCE)
        await reader.get_by_role("button", name="Refresh", exact=True).click()
        await expect(source).to_have_text(PYTHON_SOURCE)
        await expect(source.locator("script, a, img")).to_have_count(0)

        query = urlencode({"attachment": LONG_FILENAME, "scope": TAB})
        await reader.set_viewport_size({"width": 320, "height": 390})
        await reader.goto(page.url.split("?", 1)[0] + "?" + query, wait_until="domcontentloaded")
        viewer = reader.locator(".attachment-viewer")
        evidence["long_source_geometry"] = await check_source_viewport(reader, viewer)
        evidence["long_controls"] = await check_mobile_actions(
            viewer, ("Wrap lines", "Copy", "Refresh", "Back to Hub")
        )
        await reader.screenshot(
            path=str(output / "long-filename-standalone-320x390.png"), full_page=True
        )
        report["screenshots"].append("long-filename-standalone-320x390.png")
        await reader.get_by_role("link", name="Back to Hub", exact=True).click()
        await expect(reader.locator(".structured-pane:visible")).to_be_visible()
        assert await active_chat(reader) == TAB
        evidence["back_to_hub_routes_to_owner"] = True
    finally:
        await close_secondary_page(reader, page)
        await dialog.get_by_role("button", name="Close preview", exact=True).click()
        await expect(dialog).to_be_hidden()


async def html_frame(container):
    iframe = container.locator('iframe.attachment-html-preview[title="HTML preview"]')
    await expect(iframe).to_be_visible()
    handle = await iframe.element_handle()
    frame = await handle.content_frame()
    assert frame is not None
    return iframe, frame


async def wait_for_frame_scroll(frame, selector: str = "") -> None:
    # wait_for_function evaluates its predicate inside the document and is
    # correctly blocked by the preview's no-unsafe-eval CSP. Poll read-only CDP
    # evaluations instead; the scroll itself must still come from keyboard input.
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        position = await frame.evaluate(
            "selector => selector ? document.querySelector(selector).scrollLeft : scrollY", selector
        )
        if position > 0:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"HTML frame did not scroll: {selector or 'document'}")


async def check_html_preview(page, context, output: Path, report: dict) -> None:
    """Render authored HTML while its scripts, origin and automatic fetches stay blocked."""
    source_text = html_fixture()
    composer = page.locator(".composer-textarea:visible").first
    draft = "Keep this draft while reading HTML."
    await composer.fill(draft)
    original_url = page.url
    pages_before = len(context.pages)
    link = page.get_by_role("link", name="HTML report", exact=True)
    await click_timeline_link(page, link)
    dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
    iframe, frame = await html_frame(dialog)
    await expect(frame.get_by_role("heading", name="Authored HTML report")).to_be_visible()
    await expect(dialog).to_contain_text("Static HTML preview.")
    sandbox = (await iframe.get_attribute("sandbox") or "").split()
    assert "allow-same-origin" not in sandbox
    assert "allow-top-navigation" not in sandbox
    assert "allow-forms" not in sandbox
    await expect(
        frame.locator(
            "#untrusted-script, #untrusted-base, meta[http-equiv=refresh], iframe, object, embed, form"
        )
    ).to_have_count(0)
    await expect(
        frame.locator("[onclick], [onerror], [srcset], [ping], [href^='javascript:']")
    ).to_have_count(0)
    assert not await frame.evaluate("Boolean(window.__authoredHtmlRan)")
    assert not await page.evaluate("Boolean(window.__authoredHtmlRan)")
    assert (
        await frame.locator(".report-title").evaluate("el => getComputedStyle(el).color")
        == "rgb(18, 52, 86)"
    )
    for name in ("Embedded report one", "Embedded report two"):
        await expect(frame.get_by_role("img", name=name)).to_have_js_property("naturalWidth", 16)
    # DevTools evaluation bypasses script CSP; these checks test origin isolation,
    # separately from the authored script's removal and non-execution above.
    origin_checks = await frame.evaluate("""() => {
        const blocked = operation => { try { operation(); return false } catch { return true } }
        return {parentDom: blocked(() => parent.document.body),
          cookies: blocked(() => document.cookie),
          localStorage: blocked(() => localStorage.getItem('html-preview-test'))}
    }""")
    assert all(origin_checks.values()), origin_checks
    bridge_scripts = await frame.evaluate(
        "[...document.scripts].map(script => ({nonce: script.nonce || script.getAttribute('nonce') || '', hasNonce: script.hasAttribute('nonce')}))"
    )
    serialized_frame = await iframe.get_attribute("srcdoc") or ""
    report["html_bridge_diagnostics"] = {
        "child_scripts": [
            {"nonce_length": len(item["nonce"]), "has_nonce_attribute": item["hasNonce"]}
            for item in bridge_scripts
        ],
        "serialized_script_count": len(re.findall(r"<script\b", serialized_frame)),
        "serialized_nonce_count": len(re.findall(r'nonce="[0-9a-f]{32}"', serialized_frame)),
    }
    channel_match = re.search(r'nonce="([0-9a-f]{32})"', serialized_frame)
    assert channel_match, report["html_bridge_diagnostics"]
    channel = channel_match.group(1)
    assert re.fullmatch(r"[0-9a-f]{32}", channel)
    # These are deliberate test-only forgeries, not evidence that authored
    # scripts can read the nonce or synthesize a cross-origin event.
    await page.evaluate(
        """channel => {
        dispatchEvent(new MessageEvent('message', {origin: 'null', source: window,
          data: {type: 'attachment-html-close', channel}}))
        const iframe = document.querySelector('iframe.attachment-html-preview')
        dispatchEvent(new MessageEvent('message', {origin: location.origin, source: iframe.contentWindow,
          data: {type: 'attachment-html-close', channel}}))
    }""",
        channel,
    )
    await frame.evaluate(
        "parent.postMessage({type: 'attachment-html-close', channel: 'wrong-channel'}, '*')"
    )
    await page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    await expect(dialog).to_be_visible()
    parent_origin = await page.evaluate("location.origin")
    await frame.evaluate(
        """({channel, parentOrigin}) => {
        scrollTo(0, 0)
        dispatchEvent(new MessageEvent('message', {origin: 'https://wrong-origin.invalid', source: parent,
          data: {type: 'attachment-html-init', channel, hash: '#details'}}))
        dispatchEvent(new MessageEvent('message', {origin: parentOrigin, source: window,
          data: {type: 'attachment-html-init', channel, hash: '#details'}}))
    }""",
        {"channel": channel, "parentOrigin": parent_origin},
    )
    await page.evaluate(
        """() => document.querySelector('iframe.attachment-html-preview').contentWindow.postMessage(
        {type: 'attachment-html-init', channel: 'wrong-channel', hash: '#details'}, '*')"""
    )
    await frame.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert await frame.evaluate("scrollY") == 0
    assert not report["html_probe_requests"], report["html_probe_requests"]
    requests_before_anchor = len(report["html_probe_requests"])
    history_before_anchor = await page.evaluate("history.length")
    await frame.evaluate("""() => {
        window.__htmlClickTrace = []
        document.addEventListener('click', event => queueMicrotask(() => window.__htmlClickTrace.push({
          target: event.target?.getAttribute?.('href'), prevented: event.defaultPrevented})))
    }""")
    report["html_fragment_diagnostics"] = {"before": await frame.evaluate("""() => ({
        readyState: document.readyState, url: location.href, scrollY, height: innerHeight,
        target: document.getElementById('details').getBoundingClientRect().toJSON()})""")}
    await frame.get_by_role("link", name="Jump to details", exact=True).click()
    report["html_fragment_diagnostics"]["after"] = await frame.evaluate(
        """() => ({
        readyState: document.readyState, url: location.href, scrollY, height: innerHeight,
        target: document.getElementById('details').getBoundingClientRect().toJSON(), clicks: window.__htmlClickTrace})"""
    )
    (output / "html-fragment.json").write_text(
        json.dumps(report["html_fragment_diagnostics"], indent=2)
    )
    await expect(frame.get_by_role("heading", name="Report details")).to_be_in_viewport()
    assert await frame.evaluate("scrollY > 0")
    assert page.url == original_url and len(context.pages) == pages_before
    assert len(report["html_probe_requests"]) == requests_before_anchor
    assert await page.evaluate("history.length") == history_before_anchor
    await frame.evaluate("document.documentElement.dataset.retainedHtml = 'yes'")
    saved_scroll = await frame.evaluate("scrollY")
    reads_before_toggle = sum(
        request[1].endswith("/attachment") for request in report["api_requests"]
    )
    await dialog.get_by_role("button", name="Show source", exact=True).click()
    source = dialog.locator(".attachment-source")
    await expect(source).to_be_visible()
    assert await source.text_content() == source_text
    await expect(source.locator("script, img, style, iframe")).to_have_count(0)
    await expect(iframe).to_be_hidden()
    await dialog.get_by_role("button", name="Copy", exact=True).click()
    assert await page.evaluate("window.__copiedCode.at(-1)") == source_text
    await dialog.get_by_role("button", name="Show preview", exact=True).click()
    await expect(iframe).to_be_visible()
    assert await frame.evaluate("document.documentElement.dataset.retainedHtml") == "yes"
    assert abs(await frame.evaluate("scrollY") - saved_scroll) <= 2
    assert (
        sum(request[1].endswith("/attachment") for request in report["api_requests"])
        == reads_before_toggle
    )

    external = frame.get_by_role("link", name="External reference", exact=True)
    await expect(external).to_have_attribute("target", "_blank")
    assert "noopener" in (await external.get_attribute("rel") or "").split()
    assert not report["html_explicit_external_requests"]
    async with context.expect_page() as opened:
        await external.click()
    external_page = await opened.value
    try:
        await external_page.wait_for_load_state("domcontentloaded")
        assert await external_page.evaluate("window.opener === null")
        await expect(
            external_page.get_by_text("Explicit external reference opened")
        ).to_be_visible()
    finally:
        await close_secondary_page(external_page, page)
    assert page.url == original_url
    await frame.get_by_role("link", name="Jump to details", exact=True).focus()
    await page.keyboard.press("Escape")
    await expect(dialog).to_be_hidden()
    await expect(link).to_be_focused()
    await expect(composer).to_have_value(draft)

    mobile_geometry = []
    for width, height in ((390, 844), (320, 844), (320, 390)):
        await page.set_viewport_size({"width": width, "height": height})
        await click_timeline_link(page, link)
        iframe, frame = await html_frame(dialog)
        await expect(frame.get_by_role("heading", name="Authored HTML report")).to_be_visible()
        await check_mobile_actions(
            dialog, ("Close preview", "Show source", "Copy", "Open separately")
        )
        geometry = await iframe.evaluate("""el => {
            const r = el.getBoundingClientRect()
            const body = el.closest('.attachment-body').getBoundingClientRect()
            return {width: innerWidth, height: innerHeight, frameHeight: r.height,
              visibleHeight: Math.min(r.bottom, body.bottom, innerHeight) - Math.max(r.top, body.top, 0)}
        }""")
        assert geometry["visibleHeight"] >= 80, geometry
        mobile_geometry.append(geometry)
        await frame.get_by_role("link", name="Jump to details", exact=True).focus()
        await page.keyboard.press("PageDown")
        await wait_for_frame_scroll(frame)
        table = frame.get_by_role("region", name="Report table", exact=True)
        await table.focus()
        await page.keyboard.press("ArrowRight")
        await wait_for_frame_scroll(frame, ".table-scroll")
        await page.screenshot(
            path=str(output / f"html-preview-{width}x{height}.png"), full_page=True
        )
        report["screenshots"].append(f"html-preview-{width}x{height}.png")
        if width == 390:
            await page.keyboard.press("Escape")
        else:
            await page.go_back()
        await expect(dialog).to_be_hidden()
        await expect(composer).to_have_value(draft)
        assert page.url == original_url

    await page.set_viewport_size({"width": 1280, "height": 900})
    await page.get_by_role("link", name="HTM report", exact=True).click()
    _, frame = await html_frame(dialog)
    await expect(frame.get_by_role("heading", name="Authored HTML report")).to_be_visible()
    await dialog.get_by_role("button", name="Close preview", exact=True).click()
    await expect(dialog).to_be_hidden()
    await page.get_by_role("link", name="Truncated HTML", exact=True).click()
    await expect(dialog.locator(".attachment-source")).to_be_visible()
    assert await dialog.locator(".attachment-source").text_content() == TRUNCATED_HTML_SOURCE
    await expect(dialog).to_contain_text("This HTML file is truncated. Only source is available.")
    await expect(dialog.locator("iframe")).to_have_count(0)
    await expect(
        dialog.get_by_role("button", name=re.compile("Show (source|preview)"))
    ).to_have_count(0)
    await dialog.get_by_role("button", name="Close preview", exact=True).click()
    await expect(dialog).to_be_hidden()
    await composer.fill("")
    report["html_preview"] = {
        "authored_css_and_embedded_pngs": True,
        "source_and_copy_exact": True,
        "source_toggle_preserves_frame_dom_and_scroll": True,
        "origin_isolation": origin_checks,
        "authored_script_removed_and_unexecuted": True,
        "test_only_forged_source_origin_channel_rejected": True,
        "test_only_child_init_forgery_rejected": True,
        "automatic_resources_blocked": True,
        "internal_anchor_local": True,
        "explicit_external_noopener": True,
        "iframe_focused_escape_and_browser_back": True,
        "mobile_geometry": mobile_geometry,
        "truncated_source_only": True,
        "htm_extension": True,
    }


async def check_html_id_document(page, frame, evidence: dict, *, initial_fragment: bool) -> None:
    await expect(frame.get_by_role("heading", name="ID preservation report")).to_be_visible()
    target = frame.get_by_role("heading", name="Target section", exact=True)
    evidence.update(
        {
            "target_count": await frame.locator('[id="target"]').count(),
            "target_color": await target.evaluate("el => getComputedStyle(el).color"),
            "ids": await frame.evaluate("[...document.querySelectorAll('[id]')].map(el => el.id)"),
            "initial_scroll": await frame.evaluate("scrollY"),
            "target_rect": await target.bounding_box(),
        }
    )
    assert evidence["target_count"] == 1, evidence
    await expect(target).to_have_attribute("id", "target")
    assert evidence["target_color"] == "rgb(210, 0, 0)", evidence
    for image_name, expected_id, expected_name in (
        ("Named getter image", "getElementById", "safe-getter-image"),
        ("Named listener image", "addEventListener", "safe-listener-image"),
    ):
        image = frame.get_by_role("img", name=image_name, exact=True)
        await expect(image).to_have_attribute("id", expected_id)
        await expect(image).to_have_attribute("name", expected_name)
        await expect(image).to_have_js_property("naturalWidth", 16)
    evidence["document_named_properties"] = await frame.evaluate("""() => ({
        getter: document.getElementById?.tagName,
        listener: document.addEventListener?.tagName,
        windowPostMessage: typeof window.postMessage,
        windowElement: typeof window.Element,
        windowScrollTo: typeof window.scrollTo})""")
    assert evidence["document_named_properties"] == {
        "getter": "IMG",
        "listener": "IMG",
        "windowPostMessage": "function",
        "windowElement": "function",
        "windowScrollTo": "function",
    }, evidence["document_named_properties"]
    await expect(
        frame.locator(
            "#untrusted-id-script, #removed-form, #removed-frame, #removed-object, #removed-meta, [onclick], [onerror], form, iframe, object, embed"
        )
    ).to_have_count(0)
    assert not await frame.evaluate("Boolean(window.__idScriptRan)")
    assert await frame.evaluate("() => {try {parent.document.body;return false}catch{return true}}")
    if initial_fragment:
        await expect(target).to_be_in_viewport()
        assert await frame.evaluate("scrollY > 0")
    history_before = await page.evaluate("history.length")
    root_url = page.url
    destinations = [
        ("Jump safe control", "Safe control section", "details-safe"),
        ("Jump target", "Target section", "target"),
        *((f"Jump {name}", f"Window {name} section", name) for name in WINDOW_NAMED_IDS),
    ]
    for link_name, heading_name, target_id in destinations:
        heading = frame.get_by_role("heading", name=heading_name, exact=True)
        await expect(heading).to_have_attribute("id", target_id)
        await frame.get_by_role("link", name=link_name, exact=True).click()
        await expect(heading).to_be_in_viewport()
        assert await frame.evaluate("scrollY > 0")
        assert await page.evaluate("history.length") == history_before
        assert page.url == root_url
    await frame.get_by_role("link", name="Back to top", exact=True).click()
    assert await frame.evaluate("scrollY") == 0
    assert await page.evaluate("history.length") == history_before
    assert page.url == root_url
    evidence["empty_fragment_returns_top"] = True
    evidence["removed_tag_ids_absent"] = True
    evidence["click_targets"] = [target_id for _, _, target_id in destinations]
    evidence["history_unchanged"] = True


async def check_html_id_preview(page, context, output: Path, report: dict) -> None:
    evidence: dict = {"dialog": {}, "initial": {}, "reload": {}}
    report["html_id_preservation"] = evidence
    await page.get_by_role("link", name="HTML ID report", exact=True).click()
    dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
    _, frame = await html_frame(dialog)
    await check_html_id_document(page, frame, evidence["dialog"], initial_fragment=False)
    await frame.get_by_role("link", name="Jump target", exact=True).focus()
    await page.keyboard.press("Escape")
    await expect(dialog).to_be_hidden()
    evidence["iframe_escape"] = True
    reader = await context.new_page()
    reader.on("pageerror", lambda error: report["page_errors"].append(str(error)))
    try:
        await reader.set_viewport_size({"width": 390, "height": 844})
        url = (
            page.url.split("?", 1)[0]
            + "?"
            + urlencode({"attachment": REGISTERED_ID_HTML_PATH, "registered": "1"})
            + "#target"
        )
        await reader.goto(url, wait_until="domcontentloaded")
        _, frame = await html_frame(reader)
        await check_html_id_document(reader, frame, evidence["initial"], initial_fragment=True)
        await reader.reload(wait_until="domcontentloaded")
        _, frame = await html_frame(reader)
        await check_html_id_document(reader, frame, evidence["reload"], initial_fragment=True)
        await reader.screenshot(path=str(output / "html-id-preservation.png"), full_page=True)
        report["screenshots"].append("html-id-preservation.png")
    finally:
        await close_secondary_page(reader, page)


async def check_registered_html(
    page, context, output: Path, report: dict, real_report: Path | None = None
) -> None:
    dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
    original_url = page.url
    pages_before = len(context.pages)
    await page.get_by_role("link", name="Registered HTML", exact=True).click()
    _, frame = await html_frame(dialog)
    await expect(frame.get_by_role("heading", name="Authored HTML report")).to_be_visible()
    assert len(context.pages) == pages_before and page.url == original_url
    separate = dialog.get_by_role("link", name="Open separately", exact=True)
    href = await separate.get_attribute("href")
    anchor_query = parse_qs(urlparse(href).query)
    assert anchor_query.get("attachment") == [REGISTERED_HTML_PATH]
    assert anchor_query.get("registered") == ["1"]
    async with context.expect_page() as opened:
        await separate.click()
    reader = await opened.value
    reader.on("pageerror", lambda error: report["page_errors"].append(str(error)))
    report["registered_popup_navigation"] = {"href": href, "initial_url": reader.url}
    try:
        try:
            await reader.wait_for_url(
                lambda url: urlparse(url).path == "/"
                and parse_qs(urlparse(url).query).get("attachment") == [REGISTERED_HTML_PATH]
                and parse_qs(urlparse(url).query).get("registered") == ["1"],
                wait_until="domcontentloaded",
                timeout=10000,
            )
        finally:
            report["registered_popup_navigation"]["settled_url"] = reader.url
        query = parse_qs(urlparse(reader.url).query)
        assert query["attachment"] == [REGISTERED_HTML_PATH] and query["registered"] == ["1"]
        _, frame = await html_frame(reader)
        await expect(frame.get_by_role("heading", name="Authored HTML report")).to_be_visible()
        await expect(reader.locator(".app, .structured-pane")).to_have_count(0)
        await reader.reload(wait_until="domcontentloaded")
        _, frame = await html_frame(reader)
        await expect(frame.get_by_role("heading", name="Authored HTML report")).to_be_visible()
        base = original_url.split("?", 1)[0]
        # A hash-only goto on the existing reader is same-document navigation.
        # Load a fresh document to exercise the initial parent-to-frame message.
        await reader.goto("about:blank")
        await reader.goto(
            base
            + "?"
            + urlencode({"attachment": REGISTERED_HTML_PATH, "registered": "1"})
            + "#details",
            wait_until="domcontentloaded",
        )
        _, frame = await html_frame(reader)
        await expect(frame.get_by_role("heading", name="Report details")).to_be_in_viewport()
        assert await frame.evaluate("scrollY > 0")
        await reader.reload(wait_until="domcontentloaded")
        _, frame = await html_frame(reader)
        await expect(frame.get_by_role("heading", name="Report details")).to_be_in_viewport()
        assert await frame.evaluate("scrollY > 0")
        for path, status_text in (
            (UNREGISTERED_HTML_PATH, "not registered"),
            (UNAUTHORIZED_HTML_PATH, "Sign in to Hub"),
        ):
            await reader.goto(
                base + "?" + urlencode({"attachment": path, "registered": "1"}),
                wait_until="domcontentloaded",
            )
            await expect(reader.get_by_role("alert")).to_contain_text(status_text)
            await expect(reader.locator("iframe")).to_have_count(0)
            assert "private backend detail" not in await reader.get_by_role("alert").inner_text()
        if real_report is not None:
            await reader.goto(
                base + "?" + urlencode({"attachment": str(real_report), "registered": "1"}),
                wait_until="domcontentloaded",
            )
            iframe, frame = await html_frame(reader)
            await expect(frame.locator("body")).to_be_visible()
            images = await frame.locator('img[src^="data:image/png"]').count()
            assert images >= 2
            for index in range(images):
                image = frame.locator('img[src^="data:image/png"]').nth(index)
                await expect(image).not_to_have_js_property("naturalWidth", 0)
            await reader.set_viewport_size({"width": 390, "height": 844})
            await expect(iframe).to_be_visible()
            report["real_html_report"] = {
                "size_bytes": real_report.stat().st_size,
                "embedded_pngs": images,
                "rendered_with_mocked_registered_endpoint": True,
            }
    finally:
        await close_secondary_page(reader, page)
        await dialog.get_by_role("button", name="Close preview", exact=True).click()
        await expect(dialog).to_be_hidden()
    await page.get_by_role("link", name="Unregistered HTML", exact=True).click()
    await expect(dialog.get_by_role("alert")).to_contain_text("not registered")
    await expect(dialog.locator("iframe")).to_have_count(0)
    await dialog.get_by_role("button", name="Close preview", exact=True).click()
    await expect(dialog).to_be_hidden()
    report["registered_html"] = {
        "same_origin_path_opens_inline": True,
        "standalone_registered_query_and_reload": True,
        "initial_fragment_load_and_reload": True,
        "unregistered_and_401_response_ui": True,
        "boundary": "Mocked response handling only; backend authorization is tested separately.",
    }


async def check_markdown_preview(page, context, output: Path, report: dict) -> None:
    """Keep explicit standalone reading and reload compatibility covered."""
    link = page.get_by_role("link", name="排查记录", exact=True)
    await expect(link).to_be_visible()
    link_query = parse_qs(urlparse(await link.get_attribute("href")).query)
    assert link_query == {
        "markdown": ["reports/排查 记录.md"],
        "scope": [TAB],
    }, link_query
    await link.click()
    dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
    await expect(dialog).to_be_visible()
    async with context.expect_page() as opened:
        await dialog.get_by_role("link", name="Open separately", exact=True).click()
    reader = await opened.value
    await dialog.get_by_role("button", name="Close preview", exact=True).click()
    await expect(dialog).to_be_hidden()
    reader.on("pageerror", lambda error: report["page_errors"].append(str(error)))
    try:
        await reader.wait_for_load_state("domcontentloaded")
        article = reader.get_by_role("article")
        await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
        await expect(reader.locator(".markdown-reader-path")).to_have_text(MARKDOWN_PATH)
        await expect(article.locator("strong")).to_have_text("decoded output")
        await expect(article.locator("table")).to_have_count(1)
        await expect(article.locator("pre code")).to_have_text('print("preview")')
        await expect(reader.locator(".structured-pane")).to_have_count(0)
        assert reader.url != page.url
        assert await page.get_by_role("link", name="排查记录", exact=True).count() == 1
        assert await reader.evaluate("window.opener === null")
        external = article.get_by_role("link", name="External documentation", exact=True)
        await expect(external).to_have_attribute("href", "https://example.com/manual.md")
        scoped = article.get_by_role("link", name="Scoped preview", exact=True)
        assert parse_qs(urlparse(await scoped.get_attribute("href")).query) == {
            "markdown": ["/fixture/project/reports/details.md"],
            "scope": [TAB],
        }
        parent_plot = article.get_by_role("img", name="Parent plot", exact=True)
        image_url = urlparse(await parent_plot.get_attribute("src"))
        assert image_url.path == f"/api/workspaces/tabs/{TAB}/stream/agent-image"
        assert parse_qs(image_url.query) == {"path": ["/fixture/project/plot.png"]}
        assert "/../" not in parse_qs(image_url.query)["path"][0]
        await expect(parent_plot).to_have_js_property("naturalWidth", 1920)
        assert (
            await article.locator(
                'script,[onclick],[onerror],[href^="javascript:"],[src^="javascript:"]'
            ).count()
            == 0
        )
        assert not await reader.evaluate("Boolean(window.__markdownUnsafe)")

        await reader.get_by_role("button", name="Show source", exact=True).click()
        source = reader.locator(".markdown-reader-source")
        await expect(source).to_be_visible()
        assert await source.text_content() == MARKDOWN_SOURCE
        assert await source.locator("script, a, img").count() == 0
        await reader.get_by_role("button", name="Show preview", exact=True).click()
        await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
        preview_url = reader.url
        await reader.reload(wait_until="domcontentloaded")
        await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
        assert reader.url == preview_url
        await reader.screenshot(path=str(output / "markdown-reader-desktop.png"), full_page=True)
        report["screenshots"].append("markdown-reader-desktop.png")

        await reader.set_viewport_size({"width": 390, "height": 844})
        actions = reader.get_by_role("navigation", name="Document actions")
        await expect(actions.get_by_role("link", name="Back to Hub")).to_be_in_viewport()
        await expect(actions.get_by_role("button", name="Refresh", exact=True)).to_be_in_viewport()
        await expect(actions.get_by_role("button", name="Show source")).to_be_in_viewport()
        assert await reader.locator(".markdown-reader").evaluate(
            "el => el.scrollWidth <= el.clientWidth"
        )
        table = article.get_by_role("region", name="Scrollable table")
        assert await table.evaluate("el => el.scrollWidth > el.clientWidth")
        await table.focus()
        await reader.keyboard.press("ArrowRight")
        await asyncio.sleep(0.1)
        assert await table.evaluate("el => el.scrollLeft > 0")
        await actions.get_by_role("button", name="Show source").click()
        await expect(source).to_be_visible()
        await reader.get_by_role("button", name="Show preview", exact=True).click()
        await reader.screenshot(path=str(output / "markdown-reader-mobile.png"), full_page=True)
        report["screenshots"].append("markdown-reader-mobile.png")

        page_count = len(context.pages)
        renderer_requested = asyncio.Event()
        release_renderer = asyncio.Event()

        async def delay_highlighter(route) -> None:
            path = urlparse(route.request.url).path
            if "highlight__js" in path or "highlight.js" in path:
                renderer_requested.set()
                await release_renderer.wait()
            await route.fallback()

        await reader.route("**/*", delay_highlighter)
        try:
            await article.get_by_role("link", name="Details", exact=True).click()
            heading = article.locator("blockquote").get_by_role(
                "heading", name="Evidence", exact=True
            )
            duplicate_heading = article.locator("li").get_by_role(
                "heading", name="Evidence", exact=True
            )
            await expect(article.get_by_role("heading", name="Evidence", exact=True)).to_have_count(
                2
            )
            await expect(heading).to_be_visible()
            await asyncio.wait_for(renderer_requested.wait(), timeout=5)
            await expect(heading).to_have_attribute("id", "evidence")
            await expect(duplicate_heading).to_have_attribute("id", "evidence-1")
            await expect(article.locator(".hljs-string")).to_have_count(0)
            release_renderer.set()
            await expect(article.locator(".hljs-string")).to_have_count(1)
            await expect(heading).to_have_attribute("id", "evidence")
            await expect(duplicate_heading).to_have_attribute("id", "evidence-1")
        finally:
            release_renderer.set()
            await reader.unroute("**/*", delay_highlighter)
        nested_query = parse_qs(urlparse(reader.url).query)
        assert nested_query == {
            "markdown": ["/fixture/project/reports/details.md"],
            "scope": [TAB],
        }, nested_query
        assert urlparse(reader.url).fragment == "evidence"
        await expect(
            article.locator("blockquote").get_by_role("heading", name="Evidence", exact=True)
        ).to_have_attribute("id", "evidence")
        await expect(
            article.locator("li").get_by_role("heading", name="Evidence", exact=True)
        ).to_have_attribute("id", "evidence-1")
        assert len(context.pages) == page_count
        await reader.go_back(wait_until="domcontentloaded")
        await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
        await article.get_by_role("link", name="Scoped preview", exact=True).click()
        await expect(
            article.locator("blockquote").get_by_role("heading", name="Evidence", exact=True)
        ).to_be_visible()
        assert parse_qs(urlparse(reader.url).query) == nested_query
        assert len(context.pages) == page_count
        await reader.go_back(wait_until="domcontentloaded")
        await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
        await article.get_by_role("link", name="Missing document", exact=True).click()
        error = reader.get_by_role("alert")
        await expect(error).to_contain_text("Unable to preview this document")
        await expect(error).to_contain_text("moved or deleted")
        await expect(article).to_have_count(0)
        failed_url = reader.url
        await error.get_by_role("button", name="Retry", exact=True).click()
        await expect(article.get_by_role("heading", name="Recovered document")).to_be_visible()
        assert reader.url == failed_url
        await expect(error).to_have_count(0)
        await reader.go_back(wait_until="domcontentloaded")
        await expect(article.get_by_role("heading", name="Investigation record")).to_be_visible()
        await article.get_by_role("link", name="Restricted document", exact=True).click()
        await expect(error).to_contain_text("moved or deleted")
        await expect(article).to_have_count(0)
        assert "secret" not in await error.inner_text()
        await error.get_by_role("button", name="Retry", exact=True).click()
        await expect(error).to_contain_text("moved or deleted")
        await reader.screenshot(path=str(output / "markdown-reader-error.png"), full_page=True)
        report["screenshots"].append("markdown-reader-error.png")
        report["markdown_preview"] = {
            "explicit_action_opens_independent_page": True,
            "unicode_space_path": True,
            "rendered_heading_table_code": True,
            "source_preserved_as_text": True,
            "reload_preserves_document": True,
            "nested_relative_link_preserves_scope": True,
            "source_preview_url_rebound_to_current_scope": True,
            "parent_image_path_normalized": True,
            "heading_anchor_survives_lazy_highlighter": True,
            "nested_duplicate_heading_ids": ["evidence", "evidence-1"],
            "fragment_preserved": True,
            "external_link_unchanged": True,
            "sanitized": True,
            "mobile_width": 390,
            "missing_retry_recovers_same_document": True,
            "denied_error_does_not_leak_detail": True,
        }
    except BaseException:
        await reader.screenshot(path=str(output / "markdown-reader-failure.png"), full_page=True)
        raise
    finally:
        await close_secondary_page(reader, page)


async def block_worker_registration(context) -> None:
    # Playwright's built-in block script reads navigator.serviceWorker without
    # guarding opaque sandbox origins. Keep registration blocked without that error.
    await context.add_init_script("""(() => {
        let workers;
        try { workers = navigator.serviceWorker; }
        catch (error) { if (error.name === 'SecurityError') return; throw error; }
        if (!workers) return;
        Object.defineProperty(Object.getPrototypeOf(workers), 'register', {
            configurable: false, writable: false,
            value() {
                window.__workerRegistrationAttempts = (window.__workerRegistrationAttempts || 0) + 1;
                return Promise.reject(new DOMException('Registration blocked by browser acceptance', 'NotAllowedError'));
            }
        });
    })();""")


async def html_ids_browser_main(browser_run: BrowserRun) -> None:
    """Focused built-SPA regression for HTML IDs, also called by canonical coverage."""
    base = sys.argv[1].rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port in {None, 5173, 8173}
    ):
        raise ValueError("Use an owned numeric-loopback review server")
    output = Path(sys.argv[2]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    content = html_id_fixture()
    report: dict = {
        "result": "FAIL",
        "api_requests": [],
        "page_errors": [],
        "html_probe_requests": [],
        "initial": {},
        "reload": {},
        "boundary": "Built or development SPA with mocked registered HTML response; no backend authorization claim.",
    }
    failure = None
    async with browser_run.context(
        viewport={"width": 390, "height": 844}, service_workers="allow"
    ) as context:
        await block_worker_registration(context)

        async def route_request(route) -> None:
            request = route.request
            address = urlparse(request.url)
            if address.hostname == "html-preview.invalid":
                report["html_probe_requests"].append(request.url)
                await route.abort("blockedbyclient")
            elif origin(request.url) != origin(base):
                await route.abort("blockedbyclient")
            elif address.path == "/api/workspaces/artifacts/html-preview":
                report["api_requests"].append(request.url)
                assert parse_qs(address.query).get("path") == [REGISTERED_ID_HTML_PATH]
                await route.fulfill(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "path": REGISTERED_ID_HTML_PATH,
                            "filename": "id-report.html",
                            "kind": "html",
                            "language": "xml",
                            "content": content,
                            "size_bytes": len(content.encode("utf-8")),
                            "truncated": False,
                        }
                    ),
                )
            elif address.path.startswith("/api/"):
                raise AssertionError(f"Unexpected API: {address.path}")
            else:
                await route.continue_()

        await context.route("**/*", route_request)
        page = await context.new_page()
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        try:
            url = (
                base
                + "/?"
                + urlencode({"attachment": REGISTERED_ID_HTML_PATH, "registered": "1"})
                + "#target"
            )
            await page.goto(url, wait_until="domcontentloaded")
            _, frame = await html_frame(page)
            await check_html_id_document(page, frame, report["initial"], initial_fragment=True)
            await page.reload(wait_until="domcontentloaded")
            _, frame = await html_frame(page)
            await check_html_id_document(page, frame, report["reload"], initial_fragment=True)
            assert page.url == url
            assert not report["page_errors"] and not report["html_probe_requests"]
            report["result"] = "PASS"
        except BaseException as error:
            failure = error
        finally:
            await page.screenshot(path=str(output / "html-ids.png"), full_page=True)
    (output / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


async def main(browser_run: BrowserRun, real_report: Path | None = None) -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    base = sys.argv[1].rstrip("/")
    parsed_base = urlparse(base)
    if (
        parsed_base.scheme != "http"
        or parsed_base.hostname not in {"127.0.0.1", "::1"}
        or parsed_base.port in {None, 5173, 8173, 10025, 18183}
        or parsed_base.username is not None
        or parsed_base.password is not None
        or parsed_base.path not in {"", "/"}
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise SystemExit("Use an owned numeric-loopback review server")
    output = Path(sys.argv[2]).resolve()
    # run_browser_main validates the browser and creates the output dir.
    html_text = html_fixture()
    html_id_text = html_id_fixture()
    real_html_text = None
    if real_report is not None:
        real_report = real_report.resolve(strict=True)
        if real_report.stat().st_size > 512 * 1024:
            raise ValueError("The optional report must fit the complete HTML preview limit")
        real_html_text = real_report.read_text(encoding="utf-8")
    for row in EVENTS:
        for field in ("text", "assistant_text"):
            value = row["payload"].get(field)
            if isinstance(value, str):
                row["payload"][field] = value.replace("HTML_PREVIEW_ORIGIN", base)

    tabs = [
        {
            "id": TAB,
            "name": "Image Chat",
            "cwd": "/fixture/project",
            "agent_type": "cursor",
            "session_kind": "chat",
            "port": 0,
            "is_active": True,
            "created_at": NOW,
        }
    ]
    tabs.append({**tabs[0], "id": OTHER_TAB, "name": "Other Image Chat"})
    tabs.append({**tabs[0], "id": THIRD_TAB, "name": "Third Image Chat"})
    other_events = [
        event(
            0,
            "turn_started",
            {"summary": "Chat B file"},
            "other-turn:user",
            "other-turn",
            OTHER_TAB,
        ),
        event(
            1,
            "text_delta",
            {"text": "[Other code](code.py)"},
            "other-turn:assistant",
            "other-turn",
            OTHER_TAB,
        ),
        event(
            2,
            "turn_completed",
            {"status": "completed", "assistant_text": "[Other code](code.py)"},
            "other-turn",
            "other-turn",
            OTHER_TAB,
        ),
    ]
    send_payloads: list[dict] = []
    markdown_attempts: dict[str, int] = {}
    attachment_attempts: dict[str, int] = {}
    report: dict = {
        "result": "FAIL",
        "api_requests": [],
        "blocked_external_requests": [],
        "blocked_websockets": [],
        "unknown_requests": [],
        "page_errors": [],
        "sends": [],
        "screenshots": [],
        "renderer_requests": [],
        "html_probe_requests": [],
        "html_explicit_external_requests": [],
        "service_worker_registration_requests": [],
        "boundary": "Real SPA rendering with mocked APIs; no backend or provider traffic.",
    }
    failure: BaseException | None = None
    page = None
    output_image = png(1920, 1080)

    async with browser_run.context(
        viewport={"width": 1280, "height": 900},
        launch_args=["--disable-background-networking", "--disable-component-update"],
        service_workers="allow",
    ) as context:
        await block_worker_registration(context)
        try:

            async def block_websocket(socket) -> None:
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="mocked image smoke")

            async def route_all(route) -> None:
                request = route.request
                parsed = urlparse(request.url)
                if parsed.path == "/html-preview-worker.js":
                    report["service_worker_registration_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                if request.url == HTML_EXTERNAL_LINK:
                    report["html_explicit_external_requests"].append(request.url)
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<p>Explicit external reference opened</p>",
                    )
                    return
                if (
                    parsed.hostname == "html-preview.invalid"
                    or parsed.path == "/api/html-preview-probe"
                ):
                    report["html_probe_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                if origin(request.url) != origin(base):
                    report["blocked_external_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                path = parsed.path
                if not path.startswith("/api/"):
                    if "/katex" in path or "highlight__js" in path or "highlight.js" in path:
                        report["renderer_requests"].append(path)
                    await route.continue_()
                    return
                report["api_requests"].append([request.method, path, parsed.query])

                body: object
                if path == "/api/workspaces/artifacts/html-preview":
                    selected = parse_qs(parsed.query).get("path", [""])[0]
                    if selected == REGISTERED_HTML_PATH:
                        content = html_text
                    elif selected == REGISTERED_ID_HTML_PATH:
                        content = html_id_text
                    elif real_report is not None and selected == str(real_report):
                        content = real_html_text
                    else:
                        await route.fulfill(
                            status=401 if selected == UNAUTHORIZED_HTML_PATH else 404,
                            content_type="application/json",
                            body=json.dumps({"detail": "private backend detail"}),
                        )
                        return
                    body = {
                        "path": selected,
                        "filename": Path(selected).name,
                        "content": content,
                        "size_bytes": len(content.encode("utf-8")),
                        "truncated": False,
                        "kind": "html",
                        "language": "xml",
                    }
                elif path == "/api/auth/check":
                    body = {"auth_required": False, "user": None}
                elif (
                    request.method == "GET"
                    and path.startswith("/api/workspaces/tabs/")
                    and path.endswith("/messages")
                ):
                    body = {"messages": []}
                elif path == "/api/tabs":
                    body = tabs
                elif path in {"/api/tabs/status", "/api/tabs/archived"}:
                    body = []
                elif path.startswith("/api/tabs/sidebar-preferences"):
                    body = {"pinned_tab_ids": []}
                elif path == "/api/env-presets":
                    body = {"custom_presets": [], "hidden_builtin_ids": []}
                elif path == "/api/feishu/bot/bots":
                    body = {
                        "pool_revision": 0,
                        "bots": [],
                        "pool_editable": True,
                        "deprecated_env": [],
                        "focus_bot_id": None,
                    }
                elif path.endswith("/capabilities"):
                    body = {
                        "structured": True,
                        "send_message": True,
                        "supports_goals": False,
                        "supports_images": True,
                        "agent_type": "cursor",
                        "supports_mode_switch": False,
                    }
                elif path.endswith("/events"):
                    since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    rows = [item for item in EVENTS if item["stream_sequence"] > since]
                    if f"/tabs/{OTHER_TAB}/" in path:
                        rows = [item for item in other_events if item["stream_sequence"] > since]
                    elif f"/tabs/{THIRD_TAB}/" in path:
                        rows = []
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {
                        "events": rows,
                        "next_sequence": next_sequence,
                        "has_more": False,
                    }
                elif path in {
                    f"/api/workspaces/tabs/{TAB}/stream/markdown",
                    f"/api/workspaces/tabs/{TAB}/stream/attachment",
                    f"/api/workspaces/tabs/{OTHER_TAB}/stream/attachment",
                }:
                    document_path = posixpath.normpath(parse_qs(parsed.query).get("path", [""])[0])
                    attempts = (
                        attachment_attempts if path.endswith("/attachment") else markdown_attempts
                    )
                    attempts[document_path] = attempts.get(document_path, 0) + 1
                    missing_path = "/fixture/project/reports/missing.md"
                    if document_path == "/fixture/project/reports/denied.md":
                        await route.fulfill(
                            status=404,
                            content_type="application/json",
                            body=json.dumps({"detail": "secret filesystem detail"}),
                        )
                        return
                    if document_path == missing_path and attempts[document_path] == 1:
                        await route.fulfill(
                            status=404,
                            content_type="application/json",
                            body=json.dumps({"detail": "document not available"}),
                        )
                        return
                    if path.endswith("/attachment") and document_path in {
                        "report.html",
                        "report.htm",
                        "id-report.html",
                        "truncated.html",
                    }:
                        content = (
                            TRUNCATED_HTML_SOURCE
                            if document_path == "truncated.html"
                            else html_id_text if document_path == "id-report.html" else html_text
                        )
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "path": f"/fixture/project/{document_path}",
                                    "filename": document_path,
                                    "content": content,
                                    "size_bytes": (
                                        700000
                                        if document_path == "truncated.html"
                                        else len(content.encode("utf-8"))
                                    ),
                                    "truncated": document_path == "truncated.html",
                                    "kind": "html",
                                    "language": "xml",
                                }
                            ),
                        )
                        return
                    if path.endswith("/attachment") and document_path in {
                        "code.py",
                        "/fixture/project/code.py",
                        "data.json",
                        "/fixture/project/data.json",
                        "large.log",
                        "empty.txt",
                        "slow.json",
                        LONG_FILENAME,
                    }:
                        filename = Path(document_path).name
                        if filename == "slow.json":
                            await asyncio.sleep(0.35)
                        content = {
                            "code.py": PYTHON_SOURCE,
                            "data.json": JSON_SOURCE,
                            "large.log": LOG_SOURCE,
                            "empty.txt": "",
                            "slow.json": '{"late": true}',
                            LONG_FILENAME: LONG_CODE_SOURCE,
                        }[filename]
                        body = {
                            "path": f"/fixture/project/{filename}",
                            "filename": filename,
                            "content": content,
                            "size_bytes": (
                                900000 if filename == "large.log" else len(content.encode("utf-8"))
                            ),
                            "truncated": filename == "large.log",
                            "kind": (
                                "code"
                                if filename.endswith(".py")
                                else "data" if filename.endswith(".json") else "text"
                            ),
                            "language": (
                                "python"
                                if filename.endswith(".py")
                                else ("json" if filename.endswith(".json") else "plaintext")
                            ),
                        }
                        await route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(body),
                        )
                        return
                    if document_path in {"reports/排查 记录.md", MARKDOWN_PATH}:
                        resolved_path = MARKDOWN_PATH
                        content = MARKDOWN_SOURCE
                    elif document_path == "/fixture/project/reports/long.md":
                        resolved_path = document_path
                        content = LONG_MARKDOWN_SOURCE
                    elif document_path == "/fixture/project/reports/details.md":
                        resolved_path = document_path
                        content = (
                            "# Detail record\n\n> ## Evidence\n>\n> The nested report loaded.\n\n"
                            "- ## Evidence\n  Duplicate heading inside a list.\n\n"
                            '```python\nprint("evidence")\n```'
                        )
                    elif document_path == missing_path:
                        resolved_path = document_path
                        content = "# Recovered document\n\nThe retry kept the requested path."
                    else:
                        report["unknown_requests"].append([request.method, path, document_path])
                        await route.fulfill(
                            status=501,
                            content_type="application/json",
                            body=json.dumps({"detail": "unmocked Markdown path"}),
                        )
                        return
                    body = {
                        "path": resolved_path,
                        "content": content,
                        "size_bytes": len(content.encode("utf-8")),
                        "truncated": False,
                    }
                    if path.endswith("/attachment"):
                        body.update(
                            {
                                "filename": Path(resolved_path).name,
                                "kind": "markdown",
                                "language": "markdown",
                            }
                        )
                elif path.endswith("/stream/agent-image"):
                    image_path = parse_qs(parsed.query).get("path", [""])[0]
                    await route.fulfill(
                        status=404 if image_path == "/tmp/missing.png" else 200,
                        content_type="image/png",
                        body=b"" if image_path == "/tmp/missing.png" else output_image,
                    )
                    return
                elif path.endswith("/wait"):
                    since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    await asyncio.sleep(0.2)
                    rows = [item for item in EVENTS if item["stream_sequence"] > since]
                    if f"/tabs/{OTHER_TAB}/" in path:
                        rows = [item for item in other_events if item["stream_sequence"] > since]
                    elif f"/tabs/{THIRD_TAB}/" in path:
                        rows = []
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {
                        "events": rows,
                        "next_sequence": next_sequence,
                        "has_more": False,
                    }
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=": mock\n\n"
                    )
                    return
                elif path.endswith("/stream/send") and request.method == "POST":
                    payload = json.loads(request.post_data or "{}")
                    send_payloads.append(payload)
                    turn = payload["client_turn_id"]
                    EVENTS.append(
                        event(
                            len(EVENTS),
                            "turn_started",
                            {"mode": "default"},
                            f"{turn}:user",
                            turn,
                        )
                    )
                    EVENTS.append(
                        event(
                            len(EVENTS),
                            "turn_completed",
                            {"status": "completed"},
                            turn,
                            turn,
                        )
                    )
                    body = {"ok": True}
                elif path.endswith("/goal/current"):
                    body = None
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                elif path == "/api/dot/identity" and request.method == "GET":
                    # The Dot launcher in the tab bar reads Dot's name and avatar.
                    body = {"name": "Dot", "avatar": "", "avatar_image": None}
                else:
                    report["unknown_requests"].append([request.method, path])
                    await route.fulfill(
                        status=501,
                        content_type="application/json",
                        body=json.dumps({"detail": f"unmocked API: {request.method} {path}"}),
                    )
                    return
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )

            await context.route_web_socket("**/*", block_websocket)
            await context.route("**/*", route_all)
            page = await context.new_page()
            await page.add_init_script("""window.__copiedCode = [];
                Object.defineProperty(navigator, 'clipboard', { configurable: true,
                  value: { writeText: async text => window.__copiedCode.push(text) } });""")
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(base + f"/?tab={TAB}", wait_until="domcontentloaded")

            # Execution-derived cards and screenshots now start folded even
            # when a legacy backend response already contains their payloads.
            process = page.locator(".structured-turn").first.get_by_test_id(
                "history-details-toggle"
            )
            await expect(process).to_have_attribute("aria-expanded", "false")
            await process.click()
            await expect(process).to_have_attribute("aria-expanded", "true")
            card = page.locator(".subagent-card").first
            await expect(card).to_be_visible()
            await expect(card.locator(".subagent-name")).to_contain_text("explore")
            await card.locator("summary").click()
            await expect(card.locator(".subagent-body")).to_contain_text(REPORT)
            assert "conversationSteps" not in await card.inner_text()
            await page.screenshot(path=str(output / "cursor-subagent-card.png"), full_page=True)
            report["screenshots"].append("cursor-subagent-card.png")

            # Ordinary local image links and Read screenshots both render as
            # real images. The encoded bytes stay original; only CSS fits the
            # thumbnail. Missing images leave an explicit quiet placeholder.
            preview = page.get_by_role("button", name="Open image View screenshot", exact=True)
            await expect(preview).to_be_visible()
            await expect(page.locator(".agent-image-card img")).to_have_count(1)
            await expect(page.locator(".quoted-img-missing")).to_contain_text("image unavailable")
            assert await preview.locator("img").evaluate("img => img.naturalWidth") == 1920
            await preview.click()
            dialog = page.get_by_role("dialog", name="Attachment preview", exact=True)
            await expect(dialog).to_be_visible()
            original = dialog.get_by_role("link", name="Open original")
            assert await original.get_attribute("href") == await dialog.locator(
                "img"
            ).get_attribute("src")
            await dialog.get_by_role("button", name="Actual size").click()
            assert (
                await dialog.locator("img").evaluate("img => img.getBoundingClientRect().width")
                == 1920
            )
            await dialog.get_by_role("button", name="Fit image").click()
            assert await dialog.locator("img").evaluate(
                "img => img.getBoundingClientRect().width < innerWidth"
            )
            await page.keyboard.press("Escape")
            await expect(preview).to_be_focused()
            await page.set_viewport_size({"width": 390, "height": 844})
            await expect(preview).to_be_visible()
            await preview.focus()
            await page.keyboard.press("Enter")
            await expect(dialog).to_be_visible()
            await expect(dialog.get_by_role("button", name="Close preview")).to_be_focused()
            await page.keyboard.press("Tab")
            assert await dialog.evaluate("el => el.contains(document.activeElement)")
            await expect(dialog.get_by_role("button", name="Actual size")).to_be_in_viewport()
            await expect(original).to_be_in_viewport()
            await page.screenshot(path=str(output / "assistant-image-mobile.png"), full_page=True)
            report["screenshots"].append("assistant-image-mobile.png")
            await dialog.get_by_role("button", name="Close preview").click()
            await expect(preview).to_be_focused()
            assert await page.locator(".structured-pane").evaluate(
                "el => el.scrollWidth <= el.clientWidth"
            )
            report["assistant_images"] = {
                "natural_width": 1920,
                "actual_size_width": 1920,
                "mobile_width": 390,
                "plain_link_preview": True,
                "read_preview": True,
                "missing_placeholder": True,
            }
            await page.set_viewport_size({"width": 1280, "height": 900})

            # A queued Chat can activate while a KeepAlive instance owns an
            # open Teleport dialog. The old pane must release its window and
            # keyboard trap without restoring focus into the hidden Chat.
            await preview.click()
            await expect(dialog).to_be_visible()
            await page.evaluate(
                """async tabId => {
                const { useTerminalStore } = await import('/src/stores/terminalStore.ts')
                useTerminalStore().setActiveTab(tabId)
            }""",
                OTHER_TAB,
            )
            await expect(dialog).to_be_hidden()
            active_composer = page.locator(".composer-textarea:visible").first
            await active_composer.focus()
            await page.keyboard.press("Tab")
            assert await page.evaluate(
                "!document.activeElement.closest('.attachment-preview-dialog')"
            )
            await page.evaluate(
                """async tabId => {
                const { useTerminalStore } = await import('/src/stores/terminalStore.ts')
                useTerminalStore().setActiveTab(tabId)
            }""",
                TAB,
            )
            await expect(preview).to_be_visible()
            report["chat_activation_closes_image"] = True

            # Real streamed Markdown: a partial formula remains readable;
            # arriving delimiters replace it with KaTeX without another send.
            assert not report["renderer_requests"], report["renderer_requests"]
            rich_turn = "rich-markdown-turn"
            EVENTS.extend(
                [
                    event(
                        len(EVENTS),
                        "turn_started",
                        {"summary": "Render Markdown"},
                        rich_turn,
                        rich_turn,
                    ),
                    event(
                        len(EVENTS) + 1,
                        "text_delta",
                        {"text": RICH_PREFIX},
                        rich_turn + ":assistant",
                        rich_turn,
                    ),
                ]
            )
            rich = page.locator(".markdown-content:visible").filter(has_text="Rich content preview")
            await expect(rich).to_contain_text("frac")
            await expect(rich.locator(".katex")).to_have_count(0)
            EVENTS.extend(
                [
                    event(
                        len(EVENTS),
                        "text_delta",
                        {"text": RICH_SUFFIX},
                        rich_turn + ":assistant",
                        rich_turn,
                    ),
                    event(
                        len(EVENTS) + 1,
                        "turn_completed",
                        {
                            "status": "completed",
                            "assistant_text": RICH_PREFIX + RICH_SUFFIX,
                        },
                        rich_turn,
                        rich_turn,
                    ),
                ]
            )
            await expect(rich.locator(".katex")).to_have_count(7)
            await page.evaluate("document.fonts.ready")
            await expect(rich).to_contain_text("Prices $5 and $10")
            await expect(rich).to_contain_text(
                'Shell $HOME.${USER}; echo "$HOME" "$USER"; echo "$?".'
            )
            await expect(rich.locator("code").filter(has_text="$x$")).to_have_count(1)
            await expect(rich.locator(".math-source")).to_contain_text("notARealCommand")
            await expect(rich.locator('img[alt="Formula $x$"]')).to_have_count(1)
            assert await rich.locator(".katex-mathml math").count() == 7
            assert await rich.locator("[onclick], img[src=x], script").count() == 0
            assert (
                await rich.locator('th[align="right"]').evaluate(
                    "el => getComputedStyle(el).textAlign"
                )
                == "right"
            )
            assert (
                await rich.locator('td[align="center"]').evaluate(
                    "el => getComputedStyle(el).textAlign"
                )
                == "center"
            )
            assert not await page.evaluate("Boolean(window.__unsafe)")
            assert any("katex" in path for path in report["renderer_requests"])
            code = rich.locator(".markdown-code-block")
            await expect(code.locator("pre code")).to_have_text(CODE_TEXT)
            await expect(code.locator(".hljs-string").first).to_be_attached()
            copy = code.get_by_role("button", name="Copy code", exact=True)
            await copy.scroll_into_view_if_needed()
            await copy.click()
            await expect(copy).to_have_text("Copied")
            assert await page.evaluate("window.__copiedCode.at(-1)") == CODE_TEXT
            # A tall review viewport keeps the entire message above clipping
            # scroll containers. Capture the formula/table/code portion as PNG.
            await page.set_viewport_size({"width": 1280, "height": 1600})
            await rich.scroll_into_view_if_needed()
            await rich.screenshot(path=str(output / "rich-markdown-desktop.png"))
            bounds = await rich.bounding_box()
            code_bounds = await code.bounding_box()
            assert bounds and code_bounds
            await page.screenshot(
                path=str(output / "rich-markdown-preview.png"),
                clip={
                    "x": bounds["x"],
                    "y": bounds["y"],
                    "width": bounds["width"],
                    "height": code_bounds["y"] + code_bounds["height"] - bounds["y"],
                },
            )
            report["screenshots"].append("rich-markdown-desktop.png")
            report["screenshots"].append("rich-markdown-preview.png")
            report["adversarial_markdown"] = await page.evaluate(r"""async () => {
                const { MarkdownBlockCache, joinBlocks } = await import('/src/utils/markdownBlocks.ts')
                const input = '\\['.repeat(32768) + '\\\\]'
                const cache = new MarkdownBlockCache()
                const start = performance.now()
                cache.render(input, { complete: true })
                const elapsedMs = performance.now() - start
                const dollarInput = '$a '.repeat(16000) + '$'
                const dollarCache = new MarkdownBlockCache()
                const dollarStart = performance.now()
                dollarCache.render(dollarInput, { complete: true })
                const dollarElapsedMs = performance.now() - dollarStart
                const literalCache = new MarkdownBlockCache()
                const literalHtml = joinBlocks(literalCache.render(
                  'Literal <code>$x$</code> and <kbd>\\(y\\)</kbd>.', { complete: true }
                ))
                const literalTemplate = document.createElement('template')
                literalTemplate.innerHTML = literalHtml
                const hostile = '<a href="javascript:alert(1)" onclick="alert(1)">bad</a>\n\n'
                  + '\\[\\href{javascript:alert(1)}{bad}\\]\n\n'
                  + '<img src="javascript:alert(1)" onerror="alert(1)">'
                const template = document.createElement('template')
                template.innerHTML = joinBlocks(new MarkdownBlockCache().render(hostile, {
                  complete: true, linkMarkdownPaths: true, quotedTabId: 'image-quality-tab',
                }))
                return { input_bytes: input.length, elapsed_ms: elapsedMs,
                  incomplete_has_math: cache.hasMath,
                  dollar_input_bytes: dollarInput.length, dollar_elapsed_ms: dollarElapsedMs,
                  dollar_has_math: dollarCache.hasMath,
                  raw_code_has_math: literalCache.hasMath,
                  raw_code_text: literalTemplate.content.querySelector('code')?.textContent,
                  raw_kbd_text: literalTemplate.content.querySelector('kbd')?.textContent,
                  dangerous_nodes: template.content.querySelectorAll(
                    'script,[onclick],[onerror],[href^="javascript:"],[src^="javascript:"]'
                  ).length }
                }""")
            assert not report["adversarial_markdown"]["incomplete_has_math"]
            assert not report["adversarial_markdown"]["dollar_has_math"]
            assert not report["adversarial_markdown"]["raw_code_has_math"]
            assert report["adversarial_markdown"]["raw_code_text"] == "$x$"
            # Native Marked still applies ordinary punctuation escaping in
            # inline HTML; the math extension must leave that behavior intact.
            assert report["adversarial_markdown"]["raw_kbd_text"] == "(y)"
            assert report["adversarial_markdown"]["dangerous_nodes"] == 0
            # Broad freeze guard, not a device-level performance claim. The
            # original extension blocks for >10s on this 64KiB fixture.
            assert report["adversarial_markdown"]["elapsed_ms"] < 5000
            assert report["adversarial_markdown"]["dollar_elapsed_ms"] < 5000
            for width in (390, 280):
                await page.set_viewport_size({"width": width, "height": 844})
                table = rich.get_by_role("region", name="Scrollable table")
                assert await table.evaluate("el => el.scrollWidth > el.clientWidth")
                await table.focus()
                await page.keyboard.press("ArrowRight")
                await asyncio.sleep(0.1)
                assert await table.evaluate("el => el.scrollLeft > 0")
                wide_math = rich.locator(".markdown-math-display").nth(3)
                assert await wide_math.evaluate("el => el.scrollWidth > el.clientWidth")
                await wide_math.focus()
                await page.keyboard.press("ArrowRight")
                await asyncio.sleep(0.1)
                assert await wide_math.evaluate("el => el.scrollLeft > 0")
                assert await page.locator(".structured-pane:visible").evaluate(
                    "el => el.scrollWidth <= el.clientWidth"
                )
                await code.screenshot(path=str(output / f"rich-code-{width}.png"))
                await page.set_viewport_size({"width": width, "height": 1800})
                await rich.screenshot(path=str(output / f"rich-markdown-{width}.png"))
                report["screenshots"].append(f"rich-markdown-{width}.png")
            report["rich_markdown"] = {
                "rendered_formulas": 7,
                "streamed_partial_completed": True,
                "code_currency_preserved": True,
                "copy_exact_code": True,
                "clipboard": "mock writeText; real click and code extraction",
                "sanitized": True,
                "keyboard_scroll_widths": [390, 280],
                "lazy_renderers": True,
            }
            await page.set_viewport_size({"width": 1280, "height": 900})
            await check_attachment_preview(page, context, output, report)
            await check_markdown_preview(page, context, output, report)
            await check_preview_routing(page, context, output, report)
            await check_long_attachment_mobile(page, context, output, report)
            await check_standalone_attachment_mobile(page, context, output, report)
            await check_html_preview(page, context, output, report)
            await check_registered_html(page, context, output, report, real_report)
            await check_html_id_preview(page, context, output, report)
            assert not report["html_probe_requests"], report["html_probe_requests"]
            report["markdown_requests"] = markdown_attempts
            report["attachment_requests"] = attachment_attempts

            composer = page.locator(".composer-textarea").first
            file_input = page.locator(".composer-file-input").first
            for name, (width, height) in (
                ("screen.png", (1600, 1000)),
                ("wide.png", (9000, 40)),
            ):
                await file_input.set_input_files(
                    {
                        "name": name,
                        "mimeType": "image/png",
                        "buffer": png(width, height),
                    }
                )
                await expect(page.locator(".composer-attachments .attachment-thumb")).to_have_count(
                    1
                )
                send_button = page.locator(".composer-send-btn").first
                await expect(send_button).to_have_text("Send", timeout=10000)
                await expect(send_button).to_be_enabled(timeout=10000)
                await composer.fill(f"look at {name}")
                await send_button.click()
                deadline = asyncio.get_running_loop().time() + 10
                expected = len(report["sends"]) + 1
                while (
                    len(send_payloads) < expected and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.05)
                assert len(send_payloads) == expected, send_payloads
                [attachment] = send_payloads[-1]["attachments"]
                preview = attachment.get("preview_data_url") or attachment["data_url"]
                report["sends"].append(
                    {
                        "file": name,
                        "preview_is_original": attachment.get("preview_is_original", False),
                        "sends_preview_bytes": "preview_data_url" in attachment,
                        "original_size": png_size(attachment["data_url"]),
                        "preview_mime": preview.split(";", 1)[0],
                        "preview_size": png_size(preview),
                    }
                )

            screen, wide = report["sends"]
            assert screen["preview_is_original"] and not screen["sends_preview_bytes"], screen
            assert screen["original_size"] == (1600, 1000), screen
            assert not wide["preview_is_original"] and wide["sends_preview_bytes"], wide
            assert wide["preview_mime"] == "data:image/png", wide
            assert wide["preview_size"][0] == 8192, wide

            report["service_worker_harness"] = await page.evaluate("""async () => {
                let rejection = '';
                try { await navigator.serviceWorker.register('/html-preview-worker.js'); }
                catch (error) { rejection = error.name; }
                return {mode: 'allow with guarded prototype registration blocker', rejection,
                  registrations: (await navigator.serviceWorker.getRegistrations()).length,
                  attempts: window.__workerRegistrationAttempts};
            }""")
            assert report["service_worker_harness"]["rejection"] == "NotAllowedError"
            assert report["service_worker_harness"]["registrations"] == 0
            assert report["service_worker_harness"]["attempts"] == 1
            assert not report["service_worker_registration_requests"]
            assert not report["unknown_requests"], report["unknown_requests"]
            assert not report["page_errors"], report["page_errors"]
            report["result"] = "PASS"
        except BaseException as exc:
            failure = exc
            report["error_type"] = type(exc).__name__
            if page is not None:
                try:
                    await page.screenshot(path=str(output / "failure.png"), full_page=True)
                except Exception:
                    pass

    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if failure is not None:
        raise failure


def run_with_optional_report() -> None:
    real_report = None
    ids_only = sys.argv[4:] == ["--html-ids-only"]
    if ids_only:
        del sys.argv[4:]
    elif len(sys.argv) == 6 and sys.argv[4] == "--html-report":
        real_report = Path(sys.argv[5])
        del sys.argv[4:]

    async def selected_main(browser_run: BrowserRun) -> None:
        if ids_only:
            await html_ids_browser_main(browser_run)
        else:
            await main(browser_run, real_report)

    run_browser_main(selected_main)


if __name__ == "__main__":
    run_with_optional_report()
