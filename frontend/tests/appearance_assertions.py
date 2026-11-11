"""Shared assertions for appearance controls and real rendered consumers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import ElementHandle, Locator, Page, Request, expect

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


async def _pane_content_geometry(panes: Locator) -> list[dict[str, Any]]:
    geometry: list[dict[str, Any]] = await panes.evaluate_all("""els => els.map(el => {
      const rect = selector => {
        const content = el.querySelector(selector);
        if (!content || !content.getClientRects().length) {
          throw new Error(`Missing visible pane content: ${selector}`);
        }
        const box = content.getBoundingClientRect();
        return {x: box.x, y: box.y, width: box.width, height: box.height};
      };
      return {label: el.querySelector('.pane-session-name').textContent.trim(),
        wrapper: rect('.pane-structured'), chat: rect('.structured-pane')};
    })""")
    return geometry


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
                # Compare focus only, after the palette's layout has settled.
                await _settle_styles(page)
                for selected in (0, 1):
                    before_geometry = await _pane_content_geometry(panes)
                    await panes.nth(selected).locator(".pane-session-name").click()
                    await expect(panes.nth(selected)).to_have_class(re.compile(r"\bactive\b"))
                    await expect(page.locator(".terminal-pane.active")).to_have_count(1)
                    await panes.evaluate_all(
                        "els => Promise.all(els.flatMap(el => el.getAnimations().map(animation => animation.finished)))"
                    )
                    after_geometry = await _pane_content_geometry(panes)
                    assert after_geometry == before_geometry, (
                        "Focus changed inner pane geometry",
                        palette,
                        mode,
                        selected,
                        {"before": before_geometry, "after": after_geometry},
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
                            "contentGeometry": after_geometry,
                        }
                    )
    finally:
        await select_appearance(page, previous["mode"], previous["palette"])
    return report


async def _settle_styles(page: Page) -> None:
    await page.evaluate("""async () => {
      for (let pass = 0; pass < 5; pass++) {
        await new Promise(resolve => requestAnimationFrame(resolve));
        const finite = document.getAnimations().filter(animation =>
          animation.playState === 'running' &&
          Number.isFinite(animation.effect?.getComputedTiming().endTime));
        if (!finite.length) return;
        await Promise.all(finite.map(animation => animation.finished.catch(() => {})));
      }
      throw new Error('Finite styles did not settle');
    }""")


def _rgba(color: str) -> list[float]:
    values = [float(value) for value in re.findall(r"-?[\d.]+(?:e[-+]?\d+)?", color)]
    assert len(values) in (3, 4), color
    if color.startswith("rgb"):
        values[:3] = [value / 255 for value in values[:3]]
    else:
        assert color.startswith("color(srgb "), color
    return values if len(values) == 4 else [*values, 1]


def _over_backgrounds(color: list[float], backgrounds: list[list[float]]) -> list[list[float]]:
    if color[3] == 1:
        return [color[:3]]
    if color[3] == 0:
        return backgrounds
    assert backgrounds, f"Translucent paint has no opaque backing: {color}"
    return [
        [color[i] * color[3] + base[i] * (1 - color[3]) for i in range(3)] for base in backgrounds
    ]


def _paint_contrast(rendered: dict[str, Any]) -> float:
    # Include alpha overlays and gradient stops, not the unrelated flat ancestor
    # behind them. Channel bounds conservatively cover the sampled materials.
    backgrounds: list[list[float]] = []
    for layer in reversed(rendered["layers"]):
        assert layer["opacity"] == 1, rendered
        backgrounds = _over_backgrounds(_rgba(layer["color"]), backgrounds)
        image = layer["image"]
        if image != "none":
            assert image.startswith(("linear-gradient(", "radial-gradient(")), rendered
            assert image.count("gradient(") == 1, rendered
            stops = re.findall(r"(?:rgba?\([^)]*\)|color\(srgb[^)]*\))", image)
            assert len(stops) >= 2, rendered
            gradient: list[list[float]] = []
            for stop in stops:
                gradient.extend(_over_backgrounds(_rgba(stop), backgrounds))
            backgrounds = gradient
    assert backgrounds, f"Selected consumer has no opaque paint: {rendered}"
    foreground = _rgba(rendered["foreground"])
    bounds = [
        [bound(color[channel] for color in backgrounds) for channel in range(3)]
        for bound in (min, max)
    ]
    rendered["background_bounds"] = [f"color(srgb {' '.join(map(str, color))})" for color in bounds]
    low, high = (luminance(color) for color in rendered["background_bounds"])
    if low <= luminance(rendered["foreground"]) <= high:
        return 1  # Endpoints alone cannot certify a gradient crossing the ink.
    ratios = []
    for background, serialized in zip(bounds, rendered["background_bounds"]):
        ink = _over_backgrounds(foreground, [background])[0]
        ratios.append(contrast(f"color(srgb {' '.join(map(str, ink))})", serialized))
    return min(ratios)


async def _focus_state(control: Locator) -> dict[str, Any]:
    state: dict[str, Any] = await control.evaluate("""el => {
      const rgba = color => color.match(/-?[\\d.]+(?:e[-+]?\\d+)?/g).map(Number);
      const alpha = color => { const values = rgba(color); return values.length === 4 ? values[3] : 1; };
      const layers = start => {
        const result = [];
        for (let node = start; node; node = node.parentElement) {
          const s = getComputedStyle(node);
          result.push({color:s.backgroundColor, image:s.backgroundImage, opacity:Number(s.opacity)});
          const stops = s.backgroundImage.match(/(?:rgba?\\([^)]*\\)|color\\(srgb[^)]*\\))/g) || [];
          if (alpha(s.backgroundColor) === 1 || (stops.length >= 2 && stops.every(c => alpha(c) === 1))) break;
        }
        return result;
      };
      const clip = {left:0, top:0, right:innerWidth, bottom:innerHeight};
      for (let node = el.parentElement; node; node = node.parentElement) {
        const s = getComputedStyle(node), r = node.getBoundingClientRect();
        if (s.overflowX !== 'visible') {
          clip.left = Math.max(clip.left, r.x + node.clientLeft);
          clip.right = Math.min(clip.right, r.x + node.clientLeft + node.clientWidth);
        }
        if (s.overflowY !== 'visible') {
          clip.top = Math.max(clip.top, r.y + node.clientTop);
          clip.bottom = Math.min(clip.bottom, r.y + node.clientTop + node.clientHeight);
        }
      }
      const s = getComputedStyle(el), r = el.getBoundingClientRect();
      return {focused:document.activeElement === el, focusVisible:el.matches(':focus-visible'),
        palette:document.documentElement.dataset.palette, mode:document.documentElement.dataset.theme,
        rect:{x:r.x,y:r.y,width:r.width,height:r.height}, clip,
        foreground:s.color, inside:layers(el), outside:layers(el.parentElement),
        outline:{color:s.outlineColor, style:s.outlineStyle, width:parseFloat(s.outlineWidth), offset:parseFloat(s.outlineOffset)},
        shadow:s.boxShadow, borders:['Top','Right','Bottom','Left'].map(side => ({
          color:s[`border${side}Color`], style:s[`border${side}Style`], width:parseFloat(s[`border${side}Width`])}))};
    }""")
    return state


def _shadow_parts(value: str) -> list[tuple[str, tuple[float, ...], bool]]:
    colors: list[str] = []

    def color_token(match: re.Match[str]) -> str:
        colors.append(match.group(0))
        return f"@{len(colors) - 1}@"

    normalized = re.sub(r"(?:rgba?\([^)]*\)|color\(srgb[^)]*\))", color_token, value)
    parts: list[tuple[str, tuple[float, ...], bool]] = []
    for shadow in normalized.split(","):
        token = re.search(r"@(\d+)@", shadow)
        dimensions = tuple(float(value) for value in re.findall(r"(-?[\d.]+)px", shadow))
        if token and len(dimensions) == 4:
            parts.append((colors[int(token[1])], dimensions, "inset" in shadow))
    return parts


def _assert_focus_indicator(
    name: str, rest: dict[str, Any], focused: dict[str, Any]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    rect, clip = focused["rect"], focused["clip"]

    def add_cue(kind: str, color: str, gap: float, width: float, inside: bool) -> None:
        points = (
            (rect["x"] - gap, rect["y"] + rect["height"] / 2),
            (rect["x"] + rect["width"] + gap, rect["y"] + rect["height"] / 2),
            (rect["x"] + rect["width"] / 2, rect["y"] - gap),
            (rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] + gap),
        )
        visible_sides = sum(
            clip["left"] <= x <= clip["right"] and clip["top"] <= y <= clip["bottom"]
            for x, y in points
        )
        paint = {"foreground": color, "layers": focused["inside" if inside else "outside"]}
        ratio = _paint_contrast(paint)
        candidates.append(
            {
                "kind": kind,
                "width": width,
                "visibleSides": visible_sides,
                "contrast": round(ratio, 3),
                "color": color,
                "backgroundBounds": paint["background_bounds"],
                "pass": width >= 1 and visible_sides == 4 and ratio >= 3,
            }
        )

    outline = focused["outline"]
    if (
        outline != rest["outline"]
        and outline["style"] not in ("none", "hidden")
        and outline["width"] >= 1
    ):
        gap = outline["offset"] + outline["width"] / 2
        add_cue("outline", outline["color"], gap, outline["width"], gap < 0)
    if focused["shadow"] != rest["shadow"]:
        unchanged = {
            (tuple(_rgba(color)), dimensions, inset)
            for color, dimensions, inset in _shadow_parts(rest["shadow"])
        }
        for color, dimensions, inset in _shadow_parts(focused["shadow"]):
            # A newly added weak ring cannot borrow an unchanged decorative
            # inset's contrast merely because the whole shadow string changed.
            if (tuple(_rgba(color)), dimensions, inset) in unchanged:
                continue
            if dimensions[:3] == (0, 0, 0) and dimensions[3] >= 1:
                gap = dimensions[3] / 2 * (-1 if inset else 1)
                add_cue("inset-shadow" if inset else "shadow", color, gap, dimensions[3], inset)
    borders = focused["borders"]
    if all(
        new != old and new["width"] >= 1 and new["style"] not in ("none", "hidden")
        for new, old in zip(borders, rest["borders"])
    ):
        for new in borders:
            add_cue("border", new["color"], -new["width"] / 2, new["width"], True)
        if not all(candidate["pass"] for candidate in candidates[-4:]):
            for candidate in candidates[-4:]:
                candidate["pass"] = False
    assert any(candidate["pass"] for candidate in candidates), (
        "Keyboard focus indicator",
        name,
        focused["palette"],
        focused["mode"],
        {
            "candidates": candidates,
            "restOutline": rest["outline"],
            "focusOutline": outline,
            "restShadow": rest["shadow"],
            "focusShadow": focused["shadow"],
        },
    )
    return candidates


async def keyboard_focus_controls(page: Page, controls: dict[str, Locator]) -> list[dict[str, Any]]:
    """Reach real controls by Tab and require a visible, contrast-safe focus cue."""
    handles: dict[str, ElementHandle] = {}
    samples: list[dict[str, Any]] = []
    try:
        for name, control in controls.items():
            handle = await control.element_handle()
            assert handle is not None, name
            handles[name] = handle
        remaining = dict(handles)
        await page.mouse.move(0, 0)
        for _ in range(100):
            await page.keyboard.press("Tab")
            focused_name: str | None = await page.evaluate(
                "targets => Object.keys(targets).find(key => targets[key] === document.activeElement) || null",
                remaining,
            )
            if focused_name is None:
                continue
            control = controls[focused_name]
            await expect(control).to_be_focused()
            await _settle_styles(page)
            focused = await _focus_state(control)
            assert focused["focused"] and focused["focusVisible"], (focused_name, focused)
            await page.keyboard.press("Shift+Tab")
            await _settle_styles(page)
            rest = await _focus_state(control)
            assert not rest["focused"] and not rest["focusVisible"], (focused_name, rest)
            candidates = _assert_focus_indicator(focused_name, rest, focused)
            samples.append(
                {
                    "control": focused_name,
                    "palette": focused["palette"],
                    "mode": focused["mode"],
                    "cues": candidates,
                }
            )
            remaining.pop(focused_name)
            # Browser chrome can occupy a stop: continue Tab traversal rather
            # than synthesizing focus or assuming one Tab returns to the target.
            if not remaining:
                return samples
        raise AssertionError(f"Keyboard controls not reached: {list(remaining)}")
    finally:
        for handle in handles.values():
            await handle.dispose()


async def inline_tab_rename(page: Page) -> dict[str, Any]:
    """Check real auto-focused rename ink/boundary and cancel without a PATCH."""
    tab = page.locator(".tab.active")
    label = tab.locator(".tab-name")
    original = (await label.text_content() or "").strip()
    editor = tab.locator(".tab-name-input")
    patches: list[str] = []
    rendered: dict[str, Any] = {}

    def observe_patch(request: Request) -> None:
        if request.method == "PATCH" and "/api/tabs/" in request.url:
            patches.append(request.url)

    page.on("request", observe_patch)
    try:
        await label.dblclick()
        # No editor click/Locator.press/focus may repair failed automatic focus.
        await expect(editor).to_be_focused()
        await expect(editor).to_have_value(original)
        await page.keyboard.press("Control+A")
        await page.keyboard.type("Unsubmitted keyboard probe")
        await expect(editor).to_have_value("Unsubmitted keyboard probe")
        await _settle_styles(page)
        rendered = await _focus_state(editor)
    finally:
        if await editor.count() and await editor.evaluate("el => document.activeElement === el"):
            await page.keyboard.press("Escape")
        await _settle_styles(page)
        page.remove_listener("request", observe_patch)
    await expect(editor).to_have_count(0)
    await expect(label).to_have_text(original)
    assert not patches, patches
    text = _paint_contrast({"foreground": rendered["foreground"], "layers": rendered["inside"]})
    outline = rendered["outline"]
    boundary = _paint_contrast({"foreground": outline["color"], "layers": rendered["inside"]})
    assert text >= 4.5, ("Inline rename text", rendered["palette"], rendered["mode"], text)
    assert outline["style"] not in ("none", "hidden") and outline["width"] >= 1 and boundary >= 3, (
        "Inline rename boundary",
        rendered["palette"],
        rendered["mode"],
        outline,
        boundary,
    )
    return {
        "original": original,
        "typed": "Unsubmitted keyboard probe",
        "cancelled": original,
        "automaticFocus": True,
        "patchRequests": len(patches),
        "textContrast": round(text, 3),
        "boundaryContrast": round(boundary, 3),
    }


async def _selected_chrome_contrast(page: Page) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    # Foregrounds belong to the real nested label/glyph, not the parent's color.
    targets = (
        (".mode-button.active", None, "color", 4.5),
        (".tab.active", ".tab-name", "color", 4.5),
        (".tab.active", ".pane-indicator", "color", 4.5),
        (".tab.active", ".tab-close", "color", 3),
        (".tab.active", ".tam-trigger", "color", 3),
        (".chat-sidebar__item.active", ".chat-sidebar__item-name", "color", 4.5),
        (".chat-sidebar__item.active", ".chat-sidebar__item-time", "color", 4.5),
        (".chat-sidebar__item.active", ".tam-trigger", "color", 3),
        (".layout-btn.active", ".layout-cell", "backgroundColor", 3),
    )
    for surface, child, foreground, minimum in targets:
        parent = page.locator(surface).first
        await expect(parent).to_be_visible()
        element = parent.locator(child).first if child else parent
        icon = child in (".tab-close", ".tam-trigger")
        interactions: tuple[str, ...] = ("hover", "pressed") if icon else ("rest", "hover")
        if child == ".chat-sidebar__item-time":
            # The timestamp intentionally gives its slot to the hover menu.
            interactions = ("rest",)
        for interaction in interactions:
            if interaction == "rest":
                await page.mouse.move(0, 0)
            else:
                # Sidebar menus are hover-revealed; then hover the glyph itself.
                await parent.hover()
                await element.hover()
            await expect(element).to_be_visible()
            if interaction == "pressed":
                await page.mouse.down()
            try:
                await _settle_styles(page)
                rendered = await element.evaluate(
                    """(el, {surface, foreground}) => {
                  const parent = el.closest(surface);
                  const style = getComputedStyle(el);
                  const layers = [];
                  for (let node = foreground === 'backgroundColor' ? el.parentElement : el;
                    node; node = node.parentElement) {
                    const s = getComputedStyle(node);
                    layers.push({color: s.backgroundColor, image: s.backgroundImage,
                      opacity: Number(s.opacity)});
                    if (node === parent) break;
                  }
                  return {foreground: style[foreground], layers, pressed: el.matches(':active'),
                    hovered: el.matches(':hover'), opacity: Number(style.opacity)};
                }""",
                    {"surface": surface, "foreground": foreground},
                )
                assert rendered["opacity"] == 1, rendered
                assert rendered["pressed"] == (interaction == "pressed"), rendered
                assert rendered["hovered"] == (interaction != "rest"), rendered
                ratio = _paint_contrast(rendered)
                samples.append(
                    {
                        "surface": surface,
                        "child": child,
                        "interaction": interaction,
                        "minimum": minimum,
                        "contrast": round(ratio, 3),
                        "pass": ratio >= minimum,
                        **rendered,
                    }
                )
            finally:
                if interaction == "pressed":
                    await page.mouse.move(0, 0)
                    await page.mouse.up()
    await page.get_by_role("button", name="Appearance", exact=True).click()
    dialog = page.get_by_role("dialog")
    checked = dialog.locator(".appearance-mode input:checked + span")
    for interaction in ("rest", "hover", "pressed"):
        rendered = await rendered_button_state(checked, interaction, dialog.locator("h2"))
        assert _rgba(rendered["background"])[3] == 1, rendered
        ratio = contrast(rendered["foreground"], rendered["background"])
        samples.append(
            {
                "surface": "checked appearance mode",
                "interaction": interaction,
                "minimum": 4.5,
                "contrast": round(ratio, 3),
                "pass": ratio >= 4.5,
                **rendered,
            }
        )
    await dialog.get_by_role("button", name="Done", exact=True).click()
    await _settle_styles(page)
    return samples


async def _game_chat_cues(page: Page, palette: str) -> dict[str, Any]:
    await _settle_styles(page)
    rendered = await page.evaluate("""() => {
      const selectors = {nav: '.mode-button.active', tab: '.tab.active',
        sidebar: '.chat-sidebar__item.active', composer: '.composer-row',
        plan: '.goal-plan-card', title: '.goal-plan-eyebrow'};
      return Object.fromEntries(Object.entries(selectors).map(([name, selector]) => {
        const el = document.querySelector(selector);
        if (!el || !el.getClientRects().length) throw new Error(`Missing cue: ${selector}`);
        const s = getComputedStyle(el);
        const shadows = s.boxShadow.replace(/(?:rgba?\\([^)]*\\)|color\\([^)]*\\))/g, '').split(',')
          .map(shadow => [...shadow.matchAll(/(-?[\\d.]+)px/g)].map(match => Number(match[1])));
        return [name, {radii: ['TopLeft', 'TopRight', 'BottomRight', 'BottomLeft']
            .map(corner => parseFloat(s[`border${corner}Radius`])),
          topStyle: s.borderTopStyle, topWidth: parseFloat(s.borderTopWidth),
          leftWidth: parseFloat(s.borderLeftWidth), rightWidth: parseFloat(s.borderRightWidth),
          font: s.fontFamily, fontStyle: s.fontStyle, weight: Number(s.fontWeight),
          transform: s.textTransform, stretch: s.fontStretch,
          height: el.getBoundingClientRect().height,
          hardOffset: shadows.some(values => values[0] > 0 && values[1] > 0 && values[2] === 0)}];
      }));
    }""")
    nav, composer, plan = rendered["nav"], rendered["composer"], rendered["plan"]
    if palette == "Zenless":
        checks = {
            "capsule_navigation": min(nav["radii"]) >= nav["height"] / 2,
            "oblique_hierarchy": nav["fontStyle"] == "italic" and nav["weight"] >= 700,
            "asymmetric_cards": all(
                max(item["radii"]) > min(item["radii"]) for item in (composer, plan)
            ),
            "hard_offsets": composer["hardOffset"] and plan["hardOffset"],
        }
    elif palette == "Frontier":
        checks = {
            "square_bookplates": all(max(item["radii"]) <= 3 for item in (nav, composer, plan)),
            "double_rules": all(
                item["topStyle"] == "double" and item["topWidth"] >= 3 for item in (composer, plan)
            ),
            "serif_hierarchy": bool(re.search(r"(?:^|,)\s*serif$", nav["font"], re.I)),
        }
    else:
        checks = {
            "square_hud": all(max(item["radii"]) <= 2 for item in (nav, composer, plan)),
            "leading_bars": all(
                item["leftWidth"] > item["rightWidth"] and item["leftWidth"] >= 3
                for item in (nav, composer, plan)
            ),
            "condensed_hierarchy": nav["transform"] == "uppercase"
            and nav["weight"] >= 700
            and ("condensed" in nav["stretch"] or float(nav["stretch"].removesuffix("%")) < 100),
        }
    return {"checks": checks, "rendered": rendered}


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
    game_report: list[dict[str, Any]] = []

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
                    game_report.append(
                        {
                            "palette": palette,
                            "mode": mode,
                            "cues": await _game_chat_cues(page, palette),
                            "selected_chrome": await _selected_chrome_contrast(page),
                            "keyboard_focus": await keyboard_focus_controls(
                                page,
                                {
                                    "nav": page.locator(".mode-button.active"),
                                    "layout": page.locator(".layout-btn.active").first,
                                    "tab-menu": page.locator(".tab.active .tam-trigger"),
                                    "tab-close": page.locator(".tab.active .tab-close"),
                                    "sidebar-main": page.locator(
                                        ".chat-sidebar__item.active .chat-sidebar__item-main"
                                    ),
                                    "sidebar-menu": page.locator(
                                        ".chat-sidebar__item.active .tam-trigger"
                                    ),
                                    "add-tab": page.locator(".add-tab"),
                                    "goal-primary": button,
                                },
                            ),
                            "rename": await inline_tab_rename(page),
                        }
                    )
                    print(f"Game chrome captured: {palette}/{mode}", flush=True)
                    screenshot = f"chat-game-{palette_id(palette)}-{mode}.png"
                    await page.screenshot(path=str(screenshot_dir / screenshot), full_page=True)
                    report[-1]["screenshot"] = screenshot
        if screenshot_dir is not None:
            await page.set_viewport_size({"width": 390, "height": 844})
            # The actual phone shell is selected at boot, not just by CSS width.
            await page.reload(wait_until="domcontentloaded")
            await expect(button).to_be_enabled()
            for variant in game_report:
                await select_appearance(page, variant["mode"], variant["palette"])
                await button.scroll_into_view_if_needed()
                await _settle_styles(page)
                await expect(button).to_be_in_viewport()
                if variant["palette"] == "Zenless":
                    variant["phone_keyboard_focus"] = await keyboard_focus_controls(
                        page,
                        {
                            "mobile-trigger": page.get_by_label("App menu", exact=True),
                        },
                    )
                box = await button.bounding_box()
                assert box is not None and box["height"] >= 44, box
                overflow = await page.evaluate("document.documentElement.scrollWidth - innerWidth")
                assert overflow <= 1, (variant["palette"], variant["mode"], overflow)
                screenshot = f"phone-game-{palette_id(variant['palette'])}-{variant['mode']}.png"
                await page.screenshot(path=str(screenshot_dir / screenshot), full_page=True)
                variant["phone"] = {
                    "screenshot": screenshot,
                    "primary_height": box["height"],
                    "overflow": overflow,
                }
    finally:
        if screenshot_dir is not None:
            (screenshot_dir / "chat-game-chrome.json").write_text(
                json.dumps({"variants": game_report, "primary_buttons": report}, indent=2) + "\n",
                encoding="utf-8",
            )
        if previous_viewport is not None:
            await page.set_viewport_size(previous_viewport)
        if screenshot_dir is not None:
            await page.reload(wait_until="domcontentloaded")
            await expect(button).to_be_enabled()
        await select_appearance(page, previous["mode"], previous["palette"])
    failures = [
        {"palette": variant["palette"], "mode": variant["mode"], "sample": sample}
        for variant in game_report
        for sample in variant["selected_chrome"]
        if not sample["pass"]
    ]
    assert not failures, f"Selected chrome contrast: {failures}"
    assert all(all(variant["cues"]["checks"].values()) for variant in game_report), game_report
    return report
