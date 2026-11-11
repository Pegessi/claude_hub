"""Accept multi-palette appearance through the real SPA and standalone reader.

Run against an owned loopback Vite server, never a shared Hub:
  python tests/browser_appearance.py OWNED_URL NEW_OUTPUT_DIR EXISTING_BROWSER

APIs and external fonts are mocked, and all WebSockets are blocked. These checks
exercise native browser storage/media/dialog behavior, not a backend or provider.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from playwright.async_api import Page, expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from appearance_assertions import PALETTES, choose_mode, choose_palette, contrast  # noqa: E402
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

STORAGE_KEY = "claude_hub_appearance"
DOCUMENT = "/fixture/appearance.md"
MARKDOWN = """# A space for focused work

The same palette follows this standalone reader without reloading its content.

```python
# A readable comment is part of the theme contract.
def greet(name):
    return {"message": f"Hello, {name}", "ready": True, "count": 42}
```

| Palette | Surface |
| --- | --- |
| Curated | Calm |
"""


async def theme_state(page: Page) -> dict[str, Any]:
    return await page.evaluate("""() => {
      const root = document.documentElement;
      const probe = document.createElement('span');
      root.appendChild(probe);
      const color = token => {
        probe.style.color = `var(--ch-${token})`;
        return getComputedStyle(probe).color;
      };
      const tokens = ['color-app-bg', 'color-main-bg', 'color-sidebar-bg', 'color-surface',
        'color-surface-raised', 'color-canvas', 'color-text', 'color-text-muted',
        'color-text-subtle', 'color-accent', 'color-accent-ring', 'color-accent-strong',
        'color-accent-fill-hover', 'color-text-inverse', 'color-message-user-bg',
        'color-message-user-muted', 'color-text-selected', 'color-surface-selected',
        'color-border', 'color-border-strong', 'terminal-bg', 'terminal-fg'];
      const code = ['keyword', 'string', 'function', 'number', 'comment', 'builtin',
        'attr', 'selector', 'meta', 'deletion', 'addition', 'symbol', 'variable',
        'template', 'quote', 'emphasis', 'strong'];
      const values = Object.fromEntries([...tokens, ...code.map(k => `code-${k}`)]
        .map(token => [token, color(token)]));
      probe.remove();
      return {mode: root.dataset.theme, palette: root.dataset.palette,
        nativeScheme: getComputedStyle(root).colorScheme,
        browserChrome: document.querySelector('meta[name="theme-color"]')?.content,
        appBackgroundToken: getComputedStyle(root).getPropertyValue('--ch-color-app-bg').trim(),
        values};
    }""")


def check_contrast(state: dict[str, Any]) -> dict[str, float]:
    values = state["values"]
    ratios: dict[str, float] = {}

    def require(fg: str, bg: str, minimum: float) -> None:
        ratio = contrast(values[fg], values[bg])
        ratios[f"{fg}/{bg}"] = round(ratio, 3)
        assert ratio >= minimum, (
            f"{state['palette']}/{state['mode']} {fg} on {bg}: "
            f"{ratio:.2f}:1 < {minimum}:1 ({values[fg]} / {values[bg]})"
        )

    for surface in ("color-main-bg", "color-sidebar-bg", "color-surface"):
        require("color-text", surface, 7)
        require("color-text-muted", surface, 4.5)
        require("color-text-subtle", surface, 4.5)
    require("color-accent-ring", "color-surface", 3)
    require("color-text-selected", "color-surface-selected", 4.5)
    for fill in ("color-accent-strong", "color-accent-fill-hover", "color-message-user-bg"):
        require("color-text-inverse", fill, 4.5)
    require("color-message-user-muted", "color-message-user-bg", 4.5)
    for token in values:
        if token.startswith("code-"):
            require(token, "color-canvas", 4.5)
    if state["palette"] == "contrast":
        require("color-border", "color-surface", 3)
    return ratios


async def assert_theme(page: Page, palette: str, mode: str) -> None:
    await expect(page.locator("html")).to_have_attribute("data-palette", palette.lower())
    await expect(page.locator("html")).to_have_attribute("data-theme", mode)
    state = await theme_state(page)
    assert state["nativeScheme"] == mode, state
    assert state["browserChrome"] == state["appBackgroundToken"], state


async def dialog_geometry(page: Page) -> dict[str, Any]:
    return await page.locator(".appearance-dialog").evaluate("""dialog => {
      const box = element => {
        const r = element.getBoundingClientRect();
        return {left: r.left, top: r.top, right: r.right, bottom: r.bottom,
          width: r.width, height: r.height};
      };
      const done = dialog.querySelector('button');
      const rect = done.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      const body = dialog.querySelector('.appearance-body');
      return {dialog: box(dialog), done: box(done), doneUnobscured: done.contains(hit),
        viewport: {width: innerWidth, height: innerHeight},
        pageOverflow: document.documentElement.scrollWidth - innerWidth,
        dialogOverflow: dialog.scrollWidth - dialog.clientWidth,
        bodyScrollable: body.scrollHeight > body.clientHeight,
        modes: [...dialog.querySelectorAll('.appearance-mode > span')].map(box),
        cards: [...dialog.querySelectorAll('.appearance-card')].map(box)};
    }""")


async def main(browser_run: BrowserRun) -> None:
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
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    unknown: list[str] = []
    errors: list[str] = []
    reads: list[str] = []
    report: dict[str, Any] = {
        "result": "FAIL",
        "screenshots": [],
        "palettes": [],
        "mobile": [],
        "unknown_requests": unknown,
        "page_errors": errors,
        "boundary": "Real SPA/reader/browser storage/media/dialogs; mocked APIs; no backend or provider.",
    }
    failure: BaseException | None = None
    page: Page | None = None

    async with browser_run.context(viewport={"width": 1440, "height": 900}) as context:

        async def block_websocket(socket) -> None:
            await socket.close(code=1000, reason="appearance browser fixture")

        async def route_request(route) -> None:
            request = route.request
            parsed = urlparse(request.url)
            if parsed.hostname == "fonts.googleapis.com":
                await route.fulfill(status=200, content_type="text/css", body="")
                return
            if (parsed.scheme, parsed.hostname, parsed.port) != (
                parsed_base.scheme,
                parsed_base.hostname,
                parsed_base.port,
            ):
                unknown.append(request.url)
                await route.abort("blockedbyclient")
                return
            path = parsed.path
            if not path.startswith("/api/"):
                await route.continue_()
                return
            if path == "/api/auth/check":
                body: Any = {"auth_required": False, "user": None}
            elif path in {
                "/api/tabs",
                "/api/tabs/archived",
                "/api/tabs/status",
                "/api/subagents",
                "/api/workspaces",
                "/api/remote/profiles",
            }:
                body = []
            elif path in {
                "/api/tabs/sidebar-preferences",
                "/api/tabs/sidebar-preferences/migrate-browser-pins",
            }:
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
            elif path == "/api/dot/identity":
                body = {"name": "Dot", "avatar": "", "avatar_image": None}
            elif path == "/api/workspaces/tabs/theme-reader/stream/markdown":
                reads.append(path)
                body = {
                    "path": DOCUMENT,
                    "content": MARKDOWN,
                    "size_bytes": len(MARKDOWN),
                    "truncated": False,
                }
            else:
                unknown.append(f"{request.method} {path}")
                await route.fulfill(
                    status=501,
                    content_type="application/json",
                    body='{"detail":"unmocked appearance API"}',
                )
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        await context.route("**/*", route_request)
        await context.route_web_socket("**/*", block_websocket)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.emulate_media(color_scheme="light", reduced_motion="reduce")
        reader = await context.new_page()
        reader.on("pageerror", lambda error: errors.append(str(error)))
        await reader.emulate_media(color_scheme="light", reduced_motion="reduce")

        async def shot(name: str) -> None:
            assert page is not None
            await page.screenshot(path=str(output / name), full_page=True)
            report["screenshots"].append(name)

        try:
            await page.goto(base)
            trigger = page.get_by_role("button", name="Appearance", exact=True)
            await expect(trigger).to_be_visible()
            await assert_theme(page, "Classic", "dark")
            viewport = await page.locator('meta[name="viewport"]').get_attribute("content")
            assert viewport is not None and "viewport-fit=cover" not in viewport
            await reader.goto(
                base + "/?" + urlencode({"markdown": DOCUMENT, "scope": "theme-reader"})
            )
            await expect(
                reader.get_by_role("heading", name="A space for focused work")
            ).to_be_visible()
            await expect(reader.locator(".hljs-string").first).to_be_visible()
            article = await reader.get_by_role("article").element_handle()
            assert article is not None
            await trigger.click()
            dialog = page.get_by_role("dialog")
            await expect(dialog).to_be_visible()
            await expect(dialog.get_by_role("button", name="Done")).to_be_focused()
            for mode in ("Dark", "Light"):
                await choose_mode(page, mode)
                canvases: set[str] = set()
                for palette in PALETTES:
                    await choose_palette(page, palette)
                    await assert_theme(page, palette, mode.lower())
                    await assert_theme(reader, palette, mode.lower())
                    state = await theme_state(page)
                    ratios = check_contrast(state)
                    reader_state = await theme_state(reader)
                    assert reader_state["values"] == state["values"], (palette, mode)
                    assert await article.evaluate("element => element.isConnected")
                    assert len(reads) == 1, reads
                    canvases.add(state["values"]["color-main-bg"])
                    selected_preview = page.locator(
                        f'.theme-preview[data-palette="{palette.lower()}"]'
                    )
                    assert (
                        await selected_preview.evaluate(
                            "el => getComputedStyle(el).backgroundColor"
                        )
                        == state["values"]["color-main-bg"]
                    )
                    report["palettes"].append(
                        {
                            "name": palette,
                            "mode": mode,
                            "minimumContrast": min(ratios.values()),
                            "ratios": ratios,
                        }
                    )
                    await shot(f"palette-{palette.lower()}-{mode.lower()}.png")
                assert (
                    len(canvases) >= 5
                ), f"palettes must change the canvas, not only an accent: {canvases}"
            await article.dispose()
            report["standalone"] = {
                "document_reads": len(reads),
                "retained_article": True,
                "live_cross_tab_updates": 12,
            }

            # Native radio keyboard behavior, followed by native modal Tab containment.
            light = page.get_by_role("radio", name="Light", exact=True)
            await light.focus()
            await page.keyboard.press("ArrowRight")
            await expect(page.get_by_role("radio", name="Dark", exact=True)).to_be_checked()
            classic = page.get_by_role("radio", name=re.compile(r"^Classic\b"))
            await classic.focus()
            await page.keyboard.press("ArrowRight")
            await expect(page.get_by_role("radio", name=re.compile(r"^Slate\b"))).to_be_checked()
            for _ in range(8):
                await page.keyboard.press("Tab")
                # Native dialogs allow a stop in browser chrome (activeElement
                # becomes body), but never a focus stop in the inert app below.
                assert await page.evaluate("""() => {
                  const active = document.activeElement;
                  return document.querySelector('dialog')?.matches(':modal') &&
                    (active === document.body || !!active?.closest('dialog'));
                }""")
            await page.keyboard.press("Escape")
            await expect(dialog).to_have_count(0)
            await expect(trigger).to_be_focused()
            await trigger.click()
            await page.mouse.click(4, 4)
            await expect(dialog).to_have_count(0)
            await expect(trigger).to_be_focused()
            report["keyboard_and_dismissal"] = (
                "Native radios, Tab containment, Escape/backdrop and focus return pass"
            )

            await trigger.click()
            await choose_mode(page, "System")
            await choose_palette(page, "Forest")
            await assert_theme(page, "Forest", "light")
            await page.emulate_media(color_scheme="dark", reduced_motion="reduce")
            await assert_theme(page, "Forest", "dark")
            await page.emulate_media(color_scheme="light", reduced_motion="reduce")
            await assert_theme(page, "Forest", "light")
            stored = await page.evaluate(
                "key => JSON.parse(localStorage.getItem(key))", STORAGE_KEY
            )
            assert stored == {"mode": "system", "palette": "forest"}, stored
            await choose_mode(page, "Dark")
            await page.emulate_media(color_scheme="light")
            await assert_theme(page, "Forest", "dark")
            await dialog.get_by_role("button", name="Done").click()
            await page.reload()
            await expect(trigger).to_be_visible()
            await assert_theme(page, "Forest", "dark")
            report["persistence"] = (
                "System follows OS without rewriting preference; explicit mode wins; selection survives reload"
            )

            # A blocked preference write must not stop the interaction or lie about saving.
            await page.evaluate(
                """key => {
              const original = Storage.prototype.setItem;
              window.restoreAppearanceStorage = () => { Storage.prototype.setItem = original; };
              Storage.prototype.setItem = function(k, v) {
                if (k === key) throw new DOMException('Blocked fixture', 'QuotaExceededError');
                return original.call(this, k, v);
              };
            }""",
                STORAGE_KEY,
            )
            await trigger.click()
            await choose_palette(page, "Sand")
            await assert_theme(page, "Sand", "dark")
            await expect(dialog.get_by_role("alert")).to_contain_text("could not save")
            await page.evaluate("() => window.restoreAppearanceStorage()")
            await choose_palette(page, "Dusk")
            await expect(dialog.get_by_role("alert")).to_have_count(0)
            await dialog.get_by_role("button", name="Done").click()
            report["blocked_storage"] = (
                "Live selection remains usable, warning appears and a subsequent save recovers"
            )

            # Legacy users keep their selected polarity; malformed new data is bounded.
            await page.evaluate(
                """key => {
              localStorage.setItem(key, '{invalid');
              localStorage.setItem('claude_hub_color_scheme', 'light');
            }""",
                STORAGE_KEY,
            )
            await page.reload()
            await expect(trigger).to_be_visible()
            await assert_theme(page, "Classic", "light")
            report["migration"] = "Malformed new storage falls back to the legacy light choice"

            for width, height in ((390, 844), (320, 568), (568, 320)):
                await page.set_viewport_size({"width": width, "height": height})
                menu = page.get_by_label("App menu", exact=True)
                await menu.click()
                await page.get_by_role("button", name="Appearance", exact=True).click()
                await expect(dialog).to_be_visible()
                # Measure final geometry, not the sheet's scale/slide entrance.
                await dialog.evaluate(
                    "el => Promise.all(el.getAnimations().map(animation => animation.finished))"
                )
                geometry = await dialog_geometry(page)
                rect = geometry["dialog"]
                assert rect["left"] >= -1 and rect["right"] <= width + 1, geometry
                assert rect["top"] >= -1 and rect["bottom"] <= height + 1, geometry
                assert geometry["pageOverflow"] <= 1 and geometry["dialogOverflow"] <= 1, geometry
                assert geometry["done"]["height"] >= 44 and geometry["doneUnobscured"], geometry
                assert all(box["height"] >= 44 for box in geometry["modes"]), geometry
                assert all(
                    box["height"] >= 44 and box["width"] >= 44 for box in geometry["cards"]
                ), geometry
                await choose_palette(page, "Contrast")
                await assert_theme(page, "Contrast", "light")
                await shot(f"phone-{width}x{height}.png")
                await dialog.get_by_role("button", name="Done").click()
                await expect(dialog).to_have_count(0)
                await expect(menu).to_be_focused()
                await menu.click()
                await page.get_by_role("button", name="Appearance", exact=True).click()
                await page.keyboard.press("Escape")
                await expect(dialog).to_have_count(0)
                await expect(menu).to_be_focused()
                report["mobile"].append(geometry)

            # The Workspace mobile catch-all must reach the same appearance surface.
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_label("App menu", exact=True).click()
            await page.get_by_role("button", name="Agent Workspace", exact=True).click()
            report["workspace_mobile"] = []
            for width in (390, 760, 761, 768):
                await page.set_viewport_size({"width": width, "height": 844})
                workspace_menu = page.get_by_label("Workspace menu", exact=True)
                await expect(workspace_menu).to_be_visible()
                await workspace_menu.click()
                await page.get_by_role("button", name=re.compile(r"^Appearance")).click()
                await expect(dialog).to_be_visible()
                await choose_palette(page, "Forest")
                await choose_mode(page, "Dark")
                await assert_theme(page, "Forest", "dark")
                await shot(f"workspace-boundary-{width}.png")
                await page.keyboard.press("Escape")
                await expect(workspace_menu).to_be_focused()
                report["workspace_mobile"].append(
                    {"width": width, "appearance_reachable": True, "focus_returned": True}
                )
            assert not unknown, unknown
            assert not errors, errors
            report["result"] = "PASS"
        except BaseException as exc:
            failure = exc
            report["error_type"] = type(exc).__name__
            report["error"] = str(exc)
            if page is not None:
                await page.screenshot(path=str(output / "failure.png"), full_page=True)

    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "palettes"}, indent=2))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    run_browser_main(main)
