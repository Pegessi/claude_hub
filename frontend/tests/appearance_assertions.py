"""Shared assertions for appearance controls and real rendered consumers."""

from __future__ import annotations

import re
from typing import Any

from playwright.async_api import Locator, Page, expect

PALETTES = ("Classic", "Slate", "Sand", "Forest", "Dusk", "Contrast")


def contrast(foreground: str, background: str) -> float:
    def luminance(color: str) -> float:
        values = [float(value) for value in re.findall(r"[\d.]+", color)[:3]]
        assert len(values) == 3, color
        if color.startswith("rgb"):
            values = [value / 255 for value in values]
        else:
            assert color.startswith("color(srgb "), color
        channels = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in values
        ]
        return sum(value * weight for value, weight in zip(channels, (0.2126, 0.7152, 0.0722)))

    bright, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (bright + 0.05) / (dark + 0.05)


async def choose_mode(page: Page, name: str) -> None:
    control = page.get_by_role("radio", name=name, exact=True)
    await control.locator("..").click()
    await expect(control).to_be_checked()


async def choose_palette(page: Page, name: str) -> None:
    control = page.get_by_role("radio", name=re.compile(rf"^{name}\b"))
    await control.locator("..").click()
    await expect(control).to_be_checked()


async def filled_button_themes(
    page: Page, button: Locator, secondary: Locator | None = None
) -> list[dict[str, Any]]:
    """Measure the real enabled button at rest/hover through every picker choice.

    Using the button's computed fill, rather than a palette's token in isolation,
    catches consumers that accidentally use a text/link color as a filled hover.
    """
    previous = await page.evaluate("""() => {
      const root = document.documentElement;
      const preference = JSON.parse(localStorage.getItem('claude_hub_appearance') || 'null');
      return {mode: preference?.mode ?? root.dataset.theme, palette: root.dataset.palette};
    }""")
    report: list[dict[str, Any]] = []

    async def select(mode: str, palette: str) -> None:
        await page.get_by_role("button", name="Appearance", exact=True).click()
        await choose_mode(page, mode.title())
        await choose_palette(page, palette.title())
        await page.get_by_role("dialog").get_by_role("button", name="Done", exact=True).click()
        if mode != "system":
            await expect(page.locator("html")).to_have_attribute("data-theme", mode)
        await expect(page.locator("html")).to_have_attribute("data-palette", palette.lower())

    try:
        for mode in ("dark", "light"):
            for palette in PALETTES:
                await select(mode, palette)
                await expect(button).to_be_enabled()
                for hovered in (False, True):
                    if hovered:
                        await button.hover()
                    else:
                        await page.mouse.move(0, 0)
                    await button.evaluate(
                        "el => Promise.all(el.getAnimations().map(animation => animation.finished))"
                    )
                    rendered = await button.evaluate("""el => {
                      const style = getComputedStyle(el);
                      return {foreground: style.color, background: style.backgroundColor,
                        hovered: el.matches(':hover'), label: el.textContent.trim()};
                    }""")
                    assert rendered["hovered"] == hovered, rendered
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
                        f"{rendered['label']} {palette}/{mode} hovered={hovered}: "
                        f"{ratio:.2f}:1 < 4.5:1 ({rendered})"
                    )
                    report.append(
                        {"palette": palette, "mode": mode, **rendered, "contrast": round(ratio, 3)}
                    )
    finally:
        await select(previous["mode"], previous["palette"])
    return report
