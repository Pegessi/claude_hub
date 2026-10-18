"""Standalone Markdown reader acceptance with synthetic content and mocked APIs.

Use an owned loopback Vite server, never the shared Hub:
  python tests/browser_markdown_reader.py OWNED_URL NEW_OUTPUT_DIR EXISTING_BROWSER

The known Google Fonts stylesheet is replaced with empty CSS so screenshots use
system fonts. All other external requests and every unknown API request fail.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.async_api import Page, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "reader-review-tab"
REPORT_PATH = "/fixture/reports/阅读体验验证.md"
TITLE = "阅读体验验证报告 / Reader review"
SAMPLE = f"""# {TITLE}

**合成示例 / Synthetic sample** · 本文仅用于界面验证。所有名称、状态和数值均为虚构测试数据，不代表线上指标。

## 摘要 / Summary

一份报告应当先说明结论，再提供足够的证据。清晰的标题、适当的行距和稳定的阅读宽度，帮助读者快速找到需要的信息。

This synthetic report checks the reading experience across desktop and narrow screens. The document remains readable without loading the Chat or Workspace interface.

> 本次检查只验证页面行为与排版，不涉及真实任务或线上服务。
>
> UI-only acceptance; no production data or provider connection is used.

## 观察结果 / Findings

| 检查项 / Check | 桌面 / Desktop | 窄屏 / Narrow | 操作方式 / Input | 观察内容 / Observation | 示例说明 / Sample note |
| --- | --- | --- | --- | --- | --- |
| 标题层级 / Headings | Sample A | Sample B | 浏览 / Read | 层级 / Hierarchy | synthetic-heading-review |
| 表格阅读 / Tables | Sample C | Sample D | 键盘 / Keyboard | 滚动 / Scrolling | synthetic-scroll-check |
| 源码切换 / Source | Sample E | Sample F | 点击 / Click | 保留节点 / DOM reuse | synthetic-dom-retention |

## 验证方法 / Method

以下代码仅用于检查代码块的显示，不会执行。

```python
report = {{"sample": True, "status": "ready"}}
for section in ["Summary", "Findings", "Method"]:
    print(f"{{section}}: {{report['status']}}")
```

### 后续检查 / Follow-up

- 核对正文与标题之间的留白，确保段落容易区分。
- 在窄屏上通过键盘滚动表格，不让整个页面横向移动。
- 切换到源码再返回，保留文档内容和已有的标题节点。

<svg viewBox="0 0 240 32" width="240" height="32" aria-label="Synthetic appendix link"><a href="details.md"><text x="0" y="22" fill="currentColor">Synthetic appendix</text></a></svg>

SVG 后的正文 / Text after the SVG link remains visible.

---

*End of synthetic sample · 合成示例结束。*
"""


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def preview_url(base: str, path: str) -> str:
    return f"{base}/?{urlencode({'markdown': path, 'scope': TAB})}"


def contrast_ratio(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        values = [float(value) / 255 for value in re.findall(r"[\d.]+", color)[:3]]
        if len(values) != 3:
            raise AssertionError(f"Expected an RGB color, got {color!r}")
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in values
        ]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    bright, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (bright + 0.05) / (dark + 0.05)


async def geometry(page: Page) -> dict[str, Any]:
    return await page.evaluate("""() => {
      const box = el => {
        if (!el) return null;
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        return {x: rect.x, y: rect.y, width: rect.width, height: rect.height,
          top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
          clientWidth: el.clientWidth, scrollWidth: el.scrollWidth,
          fontSize: parseFloat(style.fontSize), lineHeight: parseFloat(style.lineHeight),
          paddingTop: parseFloat(style.paddingTop), paddingLeft: parseFloat(style.paddingLeft),
          paddingBottom: parseFloat(style.paddingBottom),
          marginTop: parseFloat(style.marginTop), marginBottom: parseFloat(style.marginBottom),
          color: style.color, background: style.backgroundColor};
      };
      const main = document.querySelector('.markdown-reader');
      const article = document.querySelector('article');
      const controls = [...document.querySelectorAll('nav[aria-label="Document actions"] a, nav[aria-label="Document actions"] button')]
        .map(el => {
          const range = document.createRange(); range.selectNodeContents(el);
          const text = range.getBoundingClientRect();
          return {label: el.textContent.trim(), ...box(el),
            text: {left: text.left, right: text.right, top: text.top, bottom: text.bottom}};
        });
      return {viewport: {width: innerWidth, height: innerHeight},
        theme: document.documentElement.dataset.theme,
        page: {clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth},
        main: box(main), header: box(document.querySelector('.markdown-reader-header')),
        title: box(document.querySelector('.markdown-reader-title h1')),
        article: box(article), content: box(article?.querySelector('.markdown-content')),
        paragraph: box(article?.querySelector('p')), h1: box(article?.querySelector('h1')),
        h2: box(article?.querySelector('h2')), h3: box(article?.querySelector('h3')),
        table: box(article?.querySelector('.markdown-table-scroll')), controls,
        loading: box(document.querySelector('.markdown-reader-loading')),
        notice: box(document.querySelector('.markdown-reader-notice')),
        error: box(document.querySelector('.markdown-reader-error')),
        errorHeading: box(document.querySelector('.markdown-reader-error h2')),
        errorCopy: box(document.querySelector('.markdown-reader-error p')),
        errorActions: box(document.querySelector('.markdown-reader-error-actions')),
        retry: box(document.querySelector('.markdown-reader-error button'))};
    }""")


def assert_no_page_overflow(measured: dict[str, Any]) -> None:
    for name in ("main", "page"):
        assert measured[name]["scrollWidth"] <= measured[name]["clientWidth"] + 1, measured


def assert_action_geometry(
    measured: dict[str, Any], *, mobile: bool, source_available: bool = True
) -> None:
    controls = measured["controls"]
    assert len(controls) == (3 if source_available else 2), controls
    for control in controls:
        assert control["width"] >= 44, control
        assert control["height"] >= (44 if mobile else 36), control
        assert (
            control["left"] >= 0 and control["right"] <= measured["viewport"]["width"] + 1
        ), control
        assert control["scrollWidth"] <= control["clientWidth"] + 1, control
        assert control["text"]["left"] >= control["left"] - 1, control
        assert control["text"]["right"] <= control["right"] + 1, control
        assert control["text"]["top"] >= control["top"] - 1, control
        assert control["text"]["bottom"] <= control["bottom"] + 1, control
    for left, right in zip(controls, controls[1:]):
        if abs(left["top"] - right["top"]) < 2:
            assert right["left"] - left["right"] >= 4, controls
    if mobile and not source_available:
        back, refresh = controls
        assert "Back to Hub" in back["label"] and refresh["label"] == "Refresh", controls
        assert refresh["width"] <= 100, refresh
        assert abs(refresh["right"] - measured["header"]["right"]) <= 1, measured
        assert abs(refresh["top"] - back["top"]) <= 1, controls
        assert refresh["left"] - back["right"] >= 8, controls


async def screenshot(page: Page, output: Path, report: dict[str, Any], name: str) -> None:
    await page.screenshot(path=str(output / name), full_page=True)
    report["screenshots"].append(name)


async def check_source_retention(page: Page, reads: list[str]) -> dict[str, Any]:
    article = page.get_by_role("article")
    heading = article.get_by_role("heading", name=TITLE, exact=True)
    await expect(article.locator(".hljs-string").first).to_be_attached()
    original = await heading.element_handle()
    assert original is not None
    baseline_reads = len(reads)
    back = page.get_by_role("link", name="Back to Hub")
    await back.focus()
    await page.keyboard.press("Tab")
    toggle = page.get_by_role("button", name="Show source", exact=True)
    await expect(toggle).to_be_focused()
    focus = await toggle.evaluate("""el => {
      const style = getComputedStyle(el);
      return {visible: el.matches(':focus-visible'), outline: style.outlineStyle,
        outlineWidth: parseFloat(style.outlineWidth)};
    }""")
    assert focus["visible"] and focus["outline"] != "none" and focus["outlineWidth"] >= 2, focus
    for iteration in range(3):
        if iteration == 0:
            await page.keyboard.press("Enter")
        else:
            await page.get_by_role("button", name="Show source", exact=True).click()
        source = page.locator(".markdown-reader-source")
        await expect(source).to_be_visible()
        assert await source.text_content() == SAMPLE
        assert await original.evaluate("node => node.isConnected")
        await expect(page.locator("article")).to_be_hidden()
        await page.get_by_role("button", name="Show preview", exact=True).click()
        await expect(heading).to_be_visible()
        assert await heading.evaluate("(node, previous) => node === previous", original)
        assert len(reads) == baseline_reads, reads
    await original.dispose()
    return {
        "cycles": 3,
        "same_heading_dom_node": True,
        "extra_reads": len(reads) - baseline_reads,
        "exact_source": True,
        "keyboard_focus": focus,
    }


async def main(browser_run: BrowserRun) -> None:
    base = sys.argv[1].rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port in {None, 5173, 8173, 10025, 18183}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("Use an owned numeric-loopback Vite server")
    output = Path(sys.argv[2]).resolve()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    report: dict[str, Any] = {
        "result": "FAIL",
        "screenshots": [],
        "geometry": {},
        "api_requests": [],
        "source_modules": [],
        "forbidden_modules": [],
        "unknown_requests": [],
        "external_requests": [],
        "stubbed_font_requests": [],
        "blocked_websockets": [],
        "page_errors": [],
        "boundary": "Real standalone Vue reader; synthetic report and mocked Markdown API. Known Google Fonts CSS is empty; screenshots use system fonts. No provider or live backend.",
    }
    reads: list[str] = []
    loading_release = asyncio.Event()
    loading_requested = asyncio.Event()
    recover_error = False
    page: Page | None = None
    failure: BaseException | None = None
    try:
        async with browser_run.context(
            viewport={"width": 1280, "height": 1440},
            launch_args=["--disable-background-networking", "--disable-component-update"],
        ) as context:
            context.set_default_timeout(5000)
            context.set_default_navigation_timeout(10000)
            await context.add_init_script(
                """if (location.protocol === 'http:' && !localStorage.getItem('claude_hub_color_scheme')) {
              localStorage.setItem('claude_hub_color_scheme', 'dark');
            }"""
            )

            async def block_websocket(socket) -> None:
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="standalone reader mock")

            async def route_all(route) -> None:
                request = route.request
                parsed_request = urlparse(request.url)
                if (
                    parsed_request.hostname == "fonts.googleapis.com"
                    and parsed_request.path == "/css2"
                ):
                    report["stubbed_font_requests"].append(request.url)
                    await route.fulfill(status=200, content_type="text/css", body="")
                    return
                if origin(request.url) != origin(base):
                    report["external_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                path = parsed_request.path
                if path.startswith("/src/"):
                    report["source_modules"].append(path)
                    if (
                        path == "/src/App.vue"
                        or "/features/terminal/" in path
                        or "/features/workspace/" in path
                        or path
                        in {
                            "/src/stores/terminalStore.ts",
                            "/src/components/TerminalGridView.vue",
                            "/src/components/AgentWorkspaceView.vue",
                        }
                    ):
                        report["forbidden_modules"].append(path)
                if not path.startswith("/api/") and request.method in {"GET", "HEAD"}:
                    await route.continue_()
                    return
                report["api_requests"].append([request.method, path, parsed_request.query])
                expected_endpoint = f"/api/workspaces/tabs/{TAB}/stream/markdown"
                query = parse_qs(parsed_request.query)
                document_path = query.get("path", [""])[0]
                allowed_paths = {
                    REPORT_PATH,
                    "/fixture/loading.md",
                    "/fixture/empty.md",
                    "/fixture/truncated.md",
                    "/fixture/error.md",
                }
                if (
                    request.method != "GET"
                    or path != expected_endpoint
                    or set(query) != {"path"}
                    or document_path not in allowed_paths
                ):
                    report["unknown_requests"].append([request.method, path, parsed_request.query])
                    await route.fulfill(
                        status=501,
                        content_type="application/json",
                        body=json.dumps({"detail": "unmocked reader request"}),
                    )
                    return
                reads.append(document_path)
                if document_path == "/fixture/loading.md":
                    loading_requested.set()
                    await asyncio.wait_for(loading_release.wait(), timeout=15)
                if document_path == "/fixture/error.md" and not recover_error:
                    await route.fulfill(
                        status=404,
                        content_type="application/json",
                        body=json.dumps({"detail": "document not available"}),
                    )
                    return
                content = SAMPLE
                if document_path == "/fixture/empty.md":
                    content = ""
                elif document_path == "/fixture/truncated.md":
                    content = "# 部分文档 / Partial document\n\n合成示例：这里只展示文档的开头。"
                elif document_path == "/fixture/error.md":
                    content = "# Retry succeeded\n\nSynthetic recovery sample."
                body = {
                    "path": document_path,
                    "filename": document_path.rsplit("/", 1)[-1],
                    "content": content,
                    "size_bytes": (
                        1024 * 1024
                        if document_path == "/fixture/truncated.md"
                        else len(content.encode("utf-8"))
                    ),
                    "truncated": document_path == "/fixture/truncated.md",
                }
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(body)
                )

            await context.route_web_socket("**/*", block_websocket)
            await context.route("**/*", route_all)
            page = await context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            try:
                await page.goto(preview_url(base, REPORT_PATH), wait_until="domcontentloaded")
                article = page.get_by_role("article")
                await expect(article.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
                await expect(article).to_contain_text("合成示例 / Synthetic sample")
                await expect(article.locator("blockquote")).to_have_count(1)
                await expect(article.locator("table")).to_have_count(1)
                await expect(article.locator("table th")).to_have_count(6)
                await expect(article.locator("pre code")).to_have_count(1)
                await expect(article.locator("li")).to_have_count(3)
                svg_link = article.locator("svg a")
                await expect(svg_link).to_have_count(1)
                await expect(svg_link).to_have_attribute("target", "_self")
                assert parse_qs(urlparse(await svg_link.get_attribute("href")).query) == {
                    "markdown": ["/fixture/reports/details.md"],
                    "scope": [TAB],
                }
                await expect(article).to_contain_text("Text after the SVG link remains visible.")
                report["svg_anchor"] = {
                    "sanitized_and_rendered": True,
                    "current_scope": True,
                    "target": "_self",
                    "following_text_preserved": True,
                }
                await expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                assert reads == [REPORT_PATH], reads
                report["source_retention"] = await check_source_retention(page, reads)

                for theme in ("dark", "light"):
                    if theme == "light":
                        await page.evaluate(
                            "localStorage.setItem('claude_hub_color_scheme', 'light')"
                        )
                        await page.reload(wait_until="domcontentloaded")
                        await expect(
                            article.get_by_role("heading", name=TITLE, exact=True)
                        ).to_be_visible()
                    await expect(page.locator("html")).to_have_attribute("data-theme", theme)
                    await page.evaluate("document.fonts.ready")
                    await page.locator(".markdown-reader").evaluate("el => { el.scrollTop = 0 }")
                    measured = await geometry(page)
                    assert_no_page_overflow(measured)
                    assert_action_geometry(measured, mobile=False)
                    assert 880 <= measured["article"]["width"] <= 980, measured
                    assert measured["content"]["fontSize"] >= 16, measured
                    assert (
                        measured["content"]["lineHeight"] / measured["content"]["fontSize"] >= 1.6
                    ), measured
                    assert (
                        measured["h1"]["fontSize"]
                        > measured["h2"]["fontSize"]
                        > measured["h3"]["fontSize"]
                        >= 18
                    ), measured
                    assert measured["article"]["top"] - measured["header"]["bottom"] >= 28, measured
                    ratio = contrast_ratio(
                        measured["content"]["color"], measured["main"]["background"]
                    )
                    assert ratio >= 4.5, (theme, ratio)
                    measured["body_contrast_ratio"] = ratio
                    report["geometry"][f"desktop-{theme}"] = measured
                    await screenshot(page, output, report, f"reader-desktop-{theme}.png")
                assert (
                    report["geometry"]["desktop-dark"]["main"]["background"]
                    != report["geometry"]["desktop-light"]["main"]["background"]
                )

                for width in (390, 320):
                    await page.set_viewport_size({"width": width, "height": 844})
                    await page.locator(".markdown-reader").evaluate("el => { el.scrollTop = 0 }")
                    measured = await geometry(page)
                    assert_no_page_overflow(measured)
                    assert_action_geometry(measured, mobile=True)
                    assert measured["main"]["paddingLeft"] >= 16, measured
                    table = article.get_by_role("region", name="Scrollable table")
                    await table.scroll_into_view_if_needed()
                    await table.evaluate("el => { el.scrollLeft = 0 }")
                    assert await table.evaluate("el => el.scrollWidth > el.clientWidth")
                    await table.focus()
                    await page.keyboard.press("ArrowRight")
                    await page.wait_for_function(
                        "document.querySelector('.markdown-table-scroll').scrollLeft > 0"
                    )
                    measured["table_keyboard_scroll_left"] = await table.evaluate(
                        "el => el.scrollLeft"
                    )
                    assert_no_page_overflow(await geometry(page))
                    await page.locator(".markdown-reader").evaluate("el => { el.scrollTop = 0 }")
                    report["geometry"][f"mobile-{width}"] = measured
                    await screenshot(page, output, report, f"reader-mobile-{width}.png")

                # 640 CSS px models 200% desktop zoom reflow. A separate 24px body
                # override exercises 150% text enlargement; it is a test-only stress,
                # not a claim that browser zoom or OS text preferences were changed.
                await page.set_viewport_size({"width": 640, "height": 1000})
                enlarged = await page.add_style_tag(
                    content=".markdown-reader .markdown-content { font-size: 24px !important; }"
                )
                await page.locator(".markdown-reader").evaluate("el => { el.scrollTop = 0 }")
                measured = await geometry(page)
                assert measured["content"]["fontSize"] == 24, measured
                assert_no_page_overflow(measured)
                assert_action_geometry(measured, mobile=True)
                report["geometry"]["large-text-reflow"] = measured
                report["large_text_boundary"] = (
                    "640 CSS px desktop 200%-zoom-equivalent reflow plus test-only 24px body text (150%); no actual browser zoom or OS preference change."
                )
                await screenshot(page, output, report, "reader-large-text.png")
                await enlarged.evaluate("el => el.remove()")

                await page.set_viewport_size({"width": 390, "height": 844})
                await page.goto(
                    preview_url(base, "/fixture/loading.md"), wait_until="domcontentloaded"
                )
                await asyncio.wait_for(loading_requested.wait(), timeout=5)
                await expect(page.get_by_role("status")).to_have_text("Loading Markdown…")
                await expect(
                    page.get_by_role("button", name="Refresh", exact=True)
                ).to_be_disabled()
                measured = await geometry(page)
                assert measured["loading"]["fontSize"] >= 14, measured
                assert measured["loading"]["paddingTop"] >= 20, measured
                assert measured["loading"]["top"] - measured["header"]["bottom"] >= 20, measured
                assert_no_page_overflow(measured)
                assert_action_geometry(measured, mobile=True, source_available=False)
                report["geometry"]["loading"] = measured
                loading_release.set()
                await expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()

                await page.goto(
                    preview_url(base, "/fixture/empty.md"), wait_until="domcontentloaded"
                )
                await expect(
                    page.get_by_text("This document is empty.", exact=True)
                ).to_be_visible()
                measured = await geometry(page)
                assert measured["notice"]["fontSize"] >= 14, measured
                assert measured["notice"]["top"] - measured["header"]["bottom"] >= 20, measured
                assert_no_page_overflow(measured)
                report["geometry"]["empty"] = measured

                await page.goto(
                    preview_url(base, "/fixture/truncated.md"), wait_until="domcontentloaded"
                )
                await expect(page.get_by_role("status")).to_contain_text(
                    "Only the first part is displayed"
                )
                await expect(
                    page.get_by_role("heading", name="部分文档 / Partial document", exact=True)
                ).to_be_visible()
                measured = await geometry(page)
                assert measured["article"]["top"] - measured["notice"]["bottom"] >= 12, measured
                assert (
                    measured["notice"]["lineHeight"] / measured["notice"]["fontSize"] >= 1.5
                ), measured
                assert_no_page_overflow(measured)
                report["geometry"]["truncated"] = measured

                await page.goto(
                    preview_url(base, "/fixture/error.md"), wait_until="domcontentloaded"
                )
                alert = page.get_by_role("alert")
                await expect(alert).to_contain_text("Unable to preview this document")
                await expect(alert).to_contain_text("moved or deleted")
                measured = await geometry(page)
                assert measured["error"]["paddingLeft"] >= 20, measured
                assert measured["error"]["top"] - measured["header"]["bottom"] >= 20, measured
                assert (
                    measured["errorCopy"]["top"] - measured["errorHeading"]["bottom"] >= 10
                ), measured
                assert (
                    measured["errorActions"]["top"] - measured["errorCopy"]["bottom"] >= 10
                ), measured
                assert (
                    measured["retry"]["height"] >= 44 and measured["retry"]["width"] >= 44
                ), measured
                assert_no_page_overflow(measured)
                assert_action_geometry(measured, mobile=True, source_available=False)
                report["geometry"]["error"] = measured
                await screenshot(page, output, report, "reader-error-mobile.png")
                error_url = page.url
                before_retry = len(reads)
                recover_error = True
                await alert.get_by_role("button", name="Retry", exact=True).click()
                await expect(
                    page.get_by_role("heading", name="Retry succeeded", exact=True)
                ).to_be_visible()
                assert page.url == error_url and len(reads) == before_retry + 1, reads
                report["document_reads"] = reads
                assert not report["unknown_requests"], report["unknown_requests"]
                assert not report["external_requests"], report["external_requests"]
                assert not report["forbidden_modules"], report["forbidden_modules"]
                assert not report["page_errors"], report["page_errors"]
                report["result"] = "PASS"
            except BaseException:
                if not page.is_closed():
                    try:
                        await screenshot(page, output, report, "reader-failure.png")
                    except Exception:
                        pass
                raise
            finally:
                loading_release.set()
    except BaseException as exc:
        failure = exc
        report["error_type"] = type(exc).__name__
        if page is not None and not page.is_closed():
            try:
                await screenshot(page, output, report, "reader-failure.png")
            except Exception:
                pass
    finally:
        loading_release.set()
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if failure is not None:
        raise failure


if __name__ == "__main__":
    run_browser_main(main)
