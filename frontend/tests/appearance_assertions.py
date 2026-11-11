"""Shared assertions for appearance controls and real rendered consumers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page, expect

PALETTES = ("Classic", "Contrast", "Mono", "Studio", "Carbon", "Zenless", "Frontier", "Los Santos")


def palette_id(name: str) -> str:
    return name.lower().replace(" ", "-")


def luminance(color: str) -> float:
    values = [float(value) for value in re.findall(r"[\d.]+", color)[:3]]
    assert len(values) == 3, color
    if color.startswith("rgb"):
        values = [value / 255 for value in values]
    else:
        assert color.startswith("color(srgb "), color
    channels = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values
    ]
    return sum(value * weight for value, weight in zip(channels, (0.2126, 0.7152, 0.0722)))


def contrast(foreground: str, background: str) -> float:
    bright, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (bright + 0.05) / (dark + 0.05)


async def choose_mode(page: Page, name: str) -> None:
    control = page.get_by_role("radio", name=name, exact=True)
    await control.locator("..").click()
    await expect(control).to_be_checked()


async def choose_palette(page: Page, name: str) -> None:
    label = name.replace("-", " ").title()
    control = page.get_by_role("radio", name=re.compile(rf"^{label}\b"))
    await control.locator("..").click()
    await expect(control).to_be_checked()


async def select_appearance(page: Page, mode: str, palette: str) -> None:
    appearance = page.get_by_role("button", name="Appearance", exact=True)
    if not await appearance.is_visible():
        await page.get_by_label("App menu", exact=True).click()
    await appearance.click()
    await choose_mode(page, mode.title())
    await choose_palette(page, palette.title())
    await page.get_by_role("dialog").get_by_role("button", name="Done", exact=True).click()
    if mode != "system":
        await expect(page.locator("html")).to_have_attribute("data-theme", mode)
    await expect(page.locator("html")).to_have_attribute("data-palette", palette_id(palette))


async def rendered_button_state(
    button: Locator, interaction: str, release_target: Locator | None = None
) -> dict[str, Any]:
    page = button.page
    await expect(button).to_be_enabled()
    if interaction == "rest":
        await page.mouse.move(0, 0)
    else:
        await button.hover()
    pressed = interaction == "pressed"
    if pressed:
        await page.mouse.down()
    try:
        await button.evaluate(
            "el => Promise.all(el.getAnimations().map(animation => animation.finished))"
        )
        rendered = await button.evaluate("""el => {
          const style = getComputedStyle(el);
          return {foreground: style.color, background: style.backgroundColor,
            hovered: el.matches(':hover'), pressed: el.matches(':active'), label: el.textContent.trim()};
        }""")
        assert rendered["hovered"] == (interaction != "rest"), rendered
        assert rendered["pressed"] == pressed, rendered
        return {"interaction": interaction, **rendered}
    finally:
        if pressed:
            # A pointer-up over the CTA would be a real submission. Move onto
            # inert content within its container before releasing instead.
            if release_target is not None:
                await release_target.hover()
            else:
                await page.mouse.move(0, 0)
            await page.mouse.up()


async def split_pane_themes(page: Page) -> list[dict[str, Any]]:
    previous = await page.evaluate("""() => {
      const root = document.documentElement;
      const preference = JSON.parse(localStorage.getItem('claude_hub_appearance') || 'null');
      return {mode: preference?.mode ?? root.dataset.theme, palette: root.dataset.palette};
    }""")
    panes = page.locator(".terminal-pane")
    await expect(panes).to_have_count(2)
    await expect(panes.locator(".pane-session-name")).to_have_count(2)
    report: list[dict[str, Any]] = []
    try:
        for mode in ("dark", "light"):
            for palette in PALETTES:
                await select_appearance(page, mode, palette)
                for selected in (0, 1):
                    await panes.nth(selected).locator(".pane-session-name").click()
                    await expect(panes.nth(selected)).to_have_class(re.compile(r"\bactive\b"))
                    await expect(page.locator(".terminal-pane.active")).to_have_count(1)
                    await panes.evaluate_all(
                        "els => Promise.all(els.flatMap(el => el.getAnimations().map(animation => animation.finished)))"
                    )
                    rendered = await panes.evaluate_all("""els => els.map(el => {
                      const style = getComputedStyle(el);
                      return {active: el.classList.contains('active'), border: style.borderTopColor,
                        width: parseFloat(style.borderTopWidth), background: style.backgroundColor,
                        label: el.querySelector('.pane-session-name').textContent};
                    })""")
                    active, inactive = rendered[selected], rendered[1 - selected]
                    assert active["active"] and not inactive["active"], rendered
                    assert active["border"] != inactive["border"], (palette, mode, rendered)
                    ratio = contrast(active["border"], active["background"])
                    assert active["width"] >= 1 and ratio >= 3, (palette, mode, rendered, ratio)
                    report.append(
                        {
                            "palette": palette,
                            "mode": mode,
                            "selected": selected,
                            "borderContrast": round(ratio, 3),
                            "panes": rendered,
                        }
                    )
    finally:
        await select_appearance(page, previous["mode"], previous["palette"])
    return report


async def filled_button_themes(
    page: Page,
    button: Locator,
    secondary: Locator | None = None,
    screenshot_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Measure the enabled button at rest/hover/press through every picker choice.

    Using the button's computed fill, rather than a palette's token in isolation,
    catches consumers that accidentally use a text/link color as a filled hover.
    """
    previous = await page.evaluate("""() => {
      const root = document.documentElement;
      const preference = JSON.parse(localStorage.getItem('claude_hub_appearance') || 'null');
      return {mode: preference?.mode ?? root.dataset.theme, palette: root.dataset.palette};
    }""")
    report: list[dict[str, Any]] = []
    previous_viewport = page.viewport_size
    game_palettes = ("Zenless", "Frontier", "Los Santos")

    try:
        for mode in ("dark", "light"):
            for palette in PALETTES:
                await select_appearance(page, mode, palette)
                await expect(button).to_be_enabled()
                for interaction in ("rest", "hover", "pressed"):
                    rendered = await rendered_button_state(button, interaction)
                    if secondary is not None:
                        secondary_background = await secondary.evaluate(
                            "el => getComputedStyle(el).backgroundColor"
                        )
                        assert rendered["background"] != secondary_background, (
                            f"{rendered['label']} must retain a distinct primary fill, "
                            f"not the secondary button's neutral background: {rendered}"
                        )
                    ratio = contrast(rendered["foreground"], rendered["background"])
                    assert ratio >= 4.5, (
                        f"{rendered['label']} {palette}/{mode} interaction={interaction}: "
                        f"{ratio:.2f}:1 < 4.5:1 ({rendered})"
                    )
                    report.append(
                        {"palette": palette, "mode": mode, **rendered, "contrast": round(ratio, 3)}
                    )
                if screenshot_dir is not None and palette in game_palettes:
                    screenshot = f"chat-game-{palette_id(palette)}-{mode}.png"
                    await page.screenshot(path=str(screenshot_dir / screenshot), full_page=True)
                    report[-1]["screenshot"] = screenshot
        if screenshot_dir is not None:
            await page.set_viewport_size({"width": 390, "height": 844})
            for palette in game_palettes:
                await select_appearance(page, "dark", palette)
                await button.scroll_into_view_if_needed()
                await page.screenshot(
                    path=str(screenshot_dir / f"phone-game-{palette_id(palette)}-dark.png"),
                    full_page=True,
                )
    finally:
        if previous_viewport is not None:
            await page.set_viewport_size(previous_viewport)
        await select_appearance(page, previous["mode"], previous["palette"])
    return report
