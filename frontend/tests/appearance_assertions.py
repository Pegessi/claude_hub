"""Shared assertions for appearance controls and real rendered consumers."""

from __future__ import annotations

import base64
import colorsys
import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from playwright.async_api import ElementHandle, Locator, Page, Request, expect

PALETTES = ("Classic", "Contrast", "Mono", "Studio", "Carbon", "Zenless", "Frontier", "Los Santos")
GAME_PALETTES = ("Zenless", "Frontier", "Los Santos")
ART_PIXEL_DELTA = 12
ART_VISIBLE_FRACTION = 0.05
ART_MAX_CHANGED_P95 = 76
ICON_MIN_PIXELS = 8
ICON_MASK_SIZE = 24
ICON_FAMILY_DISAGREEMENT = 0.03


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


@asynccontextmanager
async def _inline_styles(
    element: Locator, values: dict[str, str], ancestor_index: int = 0
) -> AsyncIterator[None]:
    previous = await element.evaluate(
        """(el, {values, index}) => {
      for (let i = 0; i < index; i++) el = el.parentElement;
      const previous = {};
      for (const [name, value] of Object.entries(values)) {
        previous[name] = [el.style.getPropertyValue(name), el.style.getPropertyPriority(name)];
        el.style.setProperty(name, value, 'important');
      }
      return previous;
    }""",
        {"values": values, "index": ancestor_index},
    )
    try:
        await _settle_styles(element.page)
        yield
    finally:
        await element.evaluate(
            """(el, {previous, index}) => {
          for (let i = 0; i < index; i++) el = el.parentElement;
          for (const [name, [value, priority]] of Object.entries(previous)) {
            if (value) el.style.setProperty(name, value, priority);
            else el.style.removeProperty(name);
          }
        }""",
            {"previous": previous, "index": ancestor_index},
        )
        await _settle_styles(element.page)


async def _region_png(page: Page, rect: dict[str, Any]) -> bytes:
    return await page.screenshot(
        clip={"x": rect["x"], "y": rect["y"], "width": rect["width"], "height": rect["height"]},
        animations="disabled",
        caret="hide",
    )


async def _paint_difference(
    page: Page, painted: bytes, hidden: bytes, ink: str | None = None
) -> dict[str, Any]:
    # Decode screenshots in the browser; no image-processing dependency or
    # SVG-source fingerprint can substitute for the pixels actually painted.
    result: dict[str, Any] = await page.evaluate(
        """async ({painted, hidden, ink, delta, size}) => {
      const decode = async value => {
        const bytes = Uint8Array.from(atob(value), c => c.charCodeAt(0));
        const image = await createImageBitmap(new Blob([bytes], {type:'image/png'}));
        const canvas = new OffscreenCanvas(image.width, image.height);
        const ctx = canvas.getContext('2d', {willReadFrequently:true});
        ctx.drawImage(image, 0, 0); image.close();
        return {width:canvas.width, height:canvas.height,
          data:ctx.getImageData(0, 0, canvas.width, canvas.height).data};
      };
      const on = await decode(painted), off = await decode(hidden);
      if (on.width !== off.width || on.height !== off.height) throw new Error('Paint ROI resized');
      let changed = 0;
      const histogram = Array(256).fill(0);
      const nativeMask = [];
      for (let p = 0; p < on.data.length; p += 4) {
        const difference = Math.max(...[0,1,2].map(c => Math.abs(on.data[p+c] - off.data[p+c])));
        if (difference >= delta) { changed++; histogram[difference]++; }
        if (ink) {
          const fullInk = Math.max(...[0,1,2].map(c => Math.abs(ink[c]*255 - off.data[p+c]))) * ink[3];
          // Preserve fractional coverage: rounding an RGB edge from 0.500
          // to 0.497 must not flip an entire row into a different silhouette.
          nativeMask.push(fullInk >= delta ? Math.min(1, difference / fullInk) : 0);
        }
      }
      const mask = [];
      if (ink) for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
        const sx = Math.min(on.width-1, Math.floor((x+0.5)*on.width/size));
        const sy = Math.min(on.height-1, Math.floor((y+0.5)*on.height/size));
        mask.push(nativeMask[sy*on.width+sx]);
      }
      let cumulative = 0, p95 = 0;
      if (changed) for (let value = 0; value < histogram.length; value++) {
        cumulative += histogram[value];
        if (cumulative >= Math.ceil(changed * .95)) { p95 = value; break; }
      }
      return {width:on.width, height:on.height, changed, pixels:on.width*on.height,
        fraction:changed/(on.width*on.height), changed_delta_p95:p95,
        mask, maskEncoding:ink ? 'fractional-coverage-v1' : null};
    }""",
        {
            "painted": base64.b64encode(painted).decode("ascii"),
            "hidden": base64.b64encode(hidden).decode("ascii"),
            "ink": _rgba(ink) if ink is not None else None,
            "delta": ART_PIXEL_DELTA,
            "size": ICON_MASK_SIZE,
        },
    )
    return result


async def chat_artwork(page: Page, output: Path, name: str) -> dict[str, Any]:
    return await rendered_scene_artwork(
        page.locator(".structured-timeline:visible").first, output, name
    )


async def rendered_scene_artwork(host: Locator, output: Path, name: str) -> dict[str, Any]:
    """Measure the actual owner's fixed bottom-128px viewport scene region."""
    page = host.page
    await expect(host).to_be_visible()
    source: dict[str, Any] = await host.evaluate(r"""async el => {
      const style = getComputedStyle(el), scene = style.getPropertyValue('--palette-scene').trim();
      const match = /^url\((.*)\)$/.exec(scene);
      if (!match) return {scene, local:false, decoded:false};
      const url = new URL(match[1].trim().replace(/^['"]|['"]$/g, ''), location.href);
      const local = url.origin === location.origin || url.protocol === 'data:';
      if (!local) return {scene, url:url.href, local, decoded:false};
      const image = new Image(); image.src = url.href;
      try {
        await Promise.race([image.decode(), new Promise((_, reject) =>
          setTimeout(() => reject(new Error('Artwork decode timeout')), 5000))]);
      } catch (error) { return {scene, url:url.href, local, decoded:false, error:String(error)}; }
      return {scene, url:url.href, local, decoded:image.naturalWidth > 0 && image.naturalHeight > 0,
        width:image.naturalWidth, height:image.naturalHeight};
    }""")
    assert source["local"] and source["decoded"], ("Local artwork decode", source)
    await _settle_styles(page)
    geometry: dict[str, Any] = await host.evaluate("""el => {
      const rect = el.getBoundingClientRect(), timeline = rect;
      const left = Math.ceil(Math.max(0, timeline.left) + 4);
      const right = Math.floor(Math.min(innerWidth, timeline.right) - 4);
      const bottom = Math.floor(Math.min(innerHeight, timeline.bottom));
      const top = Math.ceil(Math.max(0, timeline.top, bottom - 128));
      return {host:rect.toJSON(), timeline:timeline.toJSON(),
        glow:getComputedStyle(el).getPropertyValue('--palette-scene-glow'),
        clip:{x:left,y:top,width:right-left,height:bottom-top}};
    }""")
    assert geometry["clip"]["width"] > 0 and geometry["clip"]["height"] > 0, geometry
    painted = await _region_png(page, geometry["clip"])
    async with _inline_styles(host, {"--palette-scene": "none"}):
        off_geometry = await host.evaluate("""el => ({host:el.getBoundingClientRect().toJSON(),
          timeline:el.getBoundingClientRect().toJSON(),
          glow:getComputedStyle(el).getPropertyValue('--palette-scene-glow')})""")
        assert off_geometry == {key: geometry[key] for key in ("host", "timeline", "glow")}, (
            "Artwork toggle changed glow or layout",
            geometry,
            off_geometry,
        )
        hidden = await _region_png(page, geometry["clip"])
    painted_name, hidden_name = f"{name}-art-on.png", f"{name}-art-off.png"
    (output / painted_name).write_bytes(painted)
    (output / hidden_name).write_bytes(hidden)
    difference = await _paint_difference(page, painted, hidden)
    assert difference["fraction"] >= ART_VISIBLE_FRACTION, (
        "Artwork visible area",
        name,
        difference,
        geometry,
    )
    assert difference["changed_delta_p95"] <= ART_MAX_CHANGED_P95, (
        "Artwork salience",
        name,
        difference,
        geometry,
    )
    return {
        "source": source,
        "geometry": geometry,
        "paint": difference,
        "screenshots": [painted_name, hidden_name],
    }


async def empty_pane_phone_themes(
    page: Page, pane: Locator, output: Path, return_focus: Locator
) -> list[dict[str, Any]]:
    """Exercise the existing empty pane, restoring viewport, theme and active pane."""
    viewport = page.viewport_size
    assert viewport is not None
    previous: dict[str, Any] = await page.evaluate("""() => {
      const preference = JSON.parse(localStorage.getItem('claude_hub_appearance') || 'null');
      return {mode:preference?.mode ?? document.documentElement.dataset.theme,
        palette:document.documentElement.dataset.palette,
        active:[...document.querySelectorAll('.terminal-pane')].findIndex(el => el.classList.contains('active'))};
    }""")
    assert previous["active"] >= 0
    results = []
    style = """el => ({padding:getComputedStyle(el).padding,size:getComputedStyle(el).backgroundSize,
      image:getComputedStyle(el).backgroundImage,rect:el.getBoundingClientRect().toJSON(),
      overflow:el.scrollWidth-el.clientWidth})"""
    try:
        await pane.click()
        await page.set_viewport_size({"width": 390, "height": 844})
        await select_appearance(page, "dark", "Classic")
        baseline_padding = await pane.evaluate("el => getComputedStyle(el).padding")
        for mode in ("dark", "light"):
            for palette in GAME_PALETTES:
                await select_appearance(page, mode, palette)
                await expect(pane).to_be_in_viewport()
                observed = await pane.evaluate(style)
                assert (
                    observed["padding"] == baseline_padding and observed["overflow"] <= 1
                ), observed
                assert observed["size"].split(",")[0].strip().split()[0] == "520px", observed
                art = await rendered_scene_artwork(
                    pane, output, f"empty-pane-{palette_id(palette)}-{mode}"
                )
                hint = await artwork_text_contrast(
                    pane.locator(".empty-hint"), ".pane-empty", output
                )
                results.append(
                    {"palette": palette, "mode": mode, "style": observed, "art": art, "hint": hint}
                )
    finally:
        await page.set_viewport_size(viewport)
        await select_appearance(page, previous["mode"], previous["palette"])
        prior = page.locator(".terminal-pane").nth(previous["active"])
        await prior.locator(".pane-session-name, .pane-empty").first.click()
        await _native_tab_to(return_focus)
        await expect(prior).to_have_class(re.compile(r"(^|\s)active(\s|$)"))
    return results


async def _native_tab_to(control: Locator) -> None:
    for _ in range(100):
        await control.page.keyboard.press("Tab")
        if await control.evaluate("el => document.activeElement === el"):
            await expect(control).to_be_focused()
            return
    raise AssertionError(f"Native Tab did not reach {control}")


async def rendered_theme_icon(control: Locator, output: Path, name: str) -> dict[str, Any]:
    icon = control.locator("svg[data-theme-icon][data-icon-family]")
    await expect(icon).to_be_visible()
    await expect(icon).to_have_attribute("aria-hidden", "true")
    await _settle_styles(control.page)
    state: dict[str, Any] = await icon.evaluate("""el => ({
      rect:el.getBoundingClientRect().toJSON(), ink:getComputedStyle(el).color,
      family:el.dataset.iconFamily, slot:el.dataset.themeIcon})""")
    painted = await _region_png(control.page, state["rect"])
    async with _inline_styles(icon, {"opacity": "0"}):
        hidden = await _region_png(control.page, state["rect"])
        assert await icon.evaluate("el => el.getBoundingClientRect().toJSON()") == state["rect"]
    difference = await _paint_difference(control.page, painted, hidden, state["ink"])
    (output / f"{name}-icon-on.png").write_bytes(painted)
    (output / f"{name}-icon-off.png").write_bytes(hidden)
    result = {**state, **difference}
    (output / f"{name}-icon.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    assert difference["changed"] >= ICON_MIN_PIXELS and any(difference["mask"]), (
        "Icon painted contribution",
        name,
        state,
        difference,
    )
    return result


def assert_icon_families(first: dict[str, Any], second: dict[str, Any]) -> float:
    assert first["slot"] == second["slot"] and first["family"] != second["family"]
    a: list[float] = first["mask"]
    b: list[float] = second["mask"]
    assert len(a) == len(b) == ICON_MASK_SIZE**2
    assert all(0 <= coverage <= 1 for coverage in [*a, *b])
    disagreement = sum(abs(left - right) for left, right in zip(a, b)) / len(a)
    assert disagreement >= ICON_FAMILY_DISAGREEMENT, (
        "Theme icon family shape",
        first["slot"],
        first["family"],
        second["family"],
        disagreement,
    )
    return disagreement


def assert_zenless_orange(rendered: dict[str, Any]) -> dict[str, float]:
    fill = _rgba(rendered["background"])
    hue, saturation, _ = colorsys.rgb_to_hsv(*fill[:3])
    assert fill[3] == 1 and 12 <= hue * 360 <= 50 and saturation >= 0.65, (
        "Zenless orange primary",
        rendered,
        {"hue": hue * 360, "saturation": saturation},
    )
    assert luminance(rendered["foreground"]) < luminance(rendered["background"]), (
        "Zenless dark paired ink",
        rendered,
    )
    return {"hue": hue * 360, "saturation": saturation}


def _over_rgba(ink: list[float], backing: list[float]) -> list[float]:
    alpha = ink[3] + backing[3] * (1 - ink[3])
    if not alpha:
        return [0, 0, 0, 0]
    return [
        (ink[c] * ink[3] + backing[c] * backing[3] * (1 - ink[3])) / alpha for c in range(3)
    ] + [alpha]


def _gradient_ink_bounds(colors: list[list[float]]) -> list[list[float]]:
    """Bound sRGB/default-Oklab interpolation, including interior channel extrema."""
    assert colors and all(
        c[3] == colors[0][3] for c in colors
    ), "Unsupported varying gradient alpha"
    to_lms = (
        (0.4122214708, 0.5363325363, 0.0514459929),
        (0.2119034982, 0.6806995451, 0.1073969566),
        (0.0883024619, 0.2817188376, 0.6299787005),
    )
    to_lab = (
        (0.2104542553, 0.7936177850, -0.0040720468),
        (1.9779984951, -2.4285922050, 0.4505937099),
        (0.0259040371, 0.7827717662, -0.8086757660),
    )
    from_lab = (
        (1.0, 0.3963377774, 0.2158037573),
        (1.0, -0.1055613458, -0.0638541728),
        (1.0, -0.0894841775, -1.2914855480),
    )
    from_lms = (
        (4.0767416621, -3.3077115913, 0.2309699292),
        (-1.2684380046, 2.6097574011, -0.3413193965),
        (-0.0041960863, -0.7034186147, 1.7076147010),
    )

    def multiply(matrix: tuple[tuple[float, ...], ...], vector: list[float]) -> list[float]:
        return [sum(a * b for a, b in zip(row, vector)) for row in matrix]

    labs = []
    for color in colors:
        linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in color[:3]]
        labs.append(multiply(to_lab, [x ** (1 / 3) for x in multiply(to_lms, linear)]))
    lows = [min(c[k] for c in colors) for k in range(3)]
    highs = [max(c[k] for c in colors) for k in range(3)]
    for start, end in zip(labs, labs[1:]):
        u = multiply(from_lab, start)
        v = multiply(from_lab, [b - a for a, b in zip(start, end)])
        cubics = [[a**3, 3 * a * a * b, 3 * a * b * b, b**3] for a, b in zip(u, v)]
        for channel, row in enumerate(from_lms):
            c = [sum(row[k] * cubics[k][power] for k in range(3)) for power in range(4)]
            points = [0.0, 1.0]
            a, b, d = 3 * c[3], 2 * c[2], c[1]
            if abs(a) < 1e-12:
                if abs(b) >= 1e-12:
                    points.append(-d / b)
            elif (discriminant := b * b - 4 * a * d) >= 0:
                points.extend(
                    ((-b - discriminant**0.5) / (2 * a), (-b + discriminant**0.5) / (2 * a))
                )
            for t in points:
                if 0 <= t <= 1:
                    value = max(
                        0.0,
                        min(
                            1.0, sum(coefficient * t**power for power, coefficient in enumerate(c))
                        ),
                    )
                    value = (
                        12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
                    )
                    lows[channel] = min(lows[channel], max(0.0, value - 1e-6))
                    highs[channel] = max(highs[channel], min(1.0, value + 1e-6))
    return [lows + [colors[0][3]], highs + [colors[0][3]]]


def _text_paints(proof: dict[str, Any]) -> list[dict[str, list[float]]]:
    path = (
        proof["path"][: proof["plateIndex"] + 1] if proof["plateIndex"] >= 0 else proof["path"][:-1]
    )
    leaf = path[0]
    if "text" in leaf["clip"] and leaf["image"] != "none":
        image = leaf["image"]
        assert image.startswith("linear-gradient(") and not re.search(
            r"\bin\s+(?!srgb\b|oklab\b)", image
        ), ("Unsupported gradient interpolation", leaf)
        assert _rgba(proof["foreground"])[3] == 0, (
            "Unsupported gradient and solid ink overlap",
            leaf,
        )
        assert not re.search(r"\b(?:oklab|oklch|lab|lch|hsla?)\(|color\((?!srgb\b)", image), (
            "Unsupported gradient color space",
            leaf,
        )
        colors = [
            _rgba(stop) for stop in re.findall(r"(?:rgba?\([^)]*\)|color\(srgb[^)]*\))", image)
        ]
        assert len(colors) >= 2, ("Unsupported clipped text paint", leaf)
        inks = _gradient_ink_bounds(colors)
    else:
        inks = [_rgba(proof["foreground"])]
    states = []
    for ink in inks:
        background = [0.0, 0.0, 0.0, 0.0]
        for index, node in enumerate(path):
            assert node["blend"] == "normal" and node["filter"] == node["mask"] == "none", (
                "Unsupported text composition",
                node,
            )
            assert node["textShadow"] == "none" and node["stroke"] == 0, (
                "Unsupported text outline",
                node,
            )
            clipped = "text" in node["clip"]
            paint = [0.0, 0.0, 0.0, 0.0] if clipped else _rgba(node["background"])
            if node["image"] != "none" and not (index == 0 and clipped):
                # Only the alert's two identical stops are a provably uniform tint.
                color = r"(?:rgba?\([^)]*\)|color\(srgb[^)]*\))"
                uniform = re.fullmatch(
                    rf"linear-gradient\(\s*({color})\s*,\s*({color})\s*\)", node["image"]
                )
                assert uniform and _rgba(uniform[1]) == _rgba(uniform[2]), (
                    "Unmodelled ink backdrop",
                    node,
                )
                paint = _over_rgba(_rgba(uniform[1]), paint)
            ink, background = _over_rgba(ink, paint), _over_rgba(background, paint)
            ink[3] *= node["opacity"]
            background[3] *= node["opacity"]
        states.append({"ink": ink, "off": background})
    return states


def _backed_text_contrast(proof: dict[str, Any]) -> float:
    states = _text_paints(proof)
    assert all(s["ink"][3] == s["off"][3] == 1 for s in states), proof
    inks = [luminance(f"color(srgb {' '.join(map(str, s['ink'][:3]))})") for s in states]
    background = luminance(f"color(srgb {' '.join(map(str, states[0]['off'][:3]))})")
    if min(inks) <= background <= max(inks):
        return 1.0
    return min((max(ink, background) + 0.05) / (min(ink, background) + 0.05) for ink in inks)


async def _reading_paint(control: Locator, host_selector: str) -> dict[str, Any]:
    result: dict[str, Any] = await control.evaluate(
        r"""(el, hostSelector) => {
      const entry = node => {
        const s = getComputedStyle(node);
        return {tag:node.tagName, classes:node.getAttribute('class') || '',
          background:s.backgroundColor, image:s.backgroundImage, opacity:Number(s.opacity),
          blend:s.mixBlendMode, mask:s.maskImage, filter:s.filter, clip:s.backgroundClip,
          textShadow:s.textShadow, stroke:parseFloat(s.webkitTextStrokeWidth),
          fill:s.webkitTextFillColor, decoration:s.textDecorationLine};
      };
      const path = []; let plate = -1, reachedHost = false;
      for (let node = el; node; node = node.parentElement) {
        const s = entry(node), values = s.background.match(/-?[\d.]+(?:e[-+]?\d+)?/g).map(Number);
        const alpha = values.length === 4 ? values[3] : 1;
        path.push(s);
        const interrupts = s.opacity !== 1 || s.blend !== 'normal' || s.mask !== 'none' || s.filter !== 'none';
        if (interrupts) plate = -1;
        if (node.matches(hostSelector)) { reachedHost = true; break; }
        if (plate < 0 && !interrupts && alpha === 1 && !s.image.includes('url(') && !s.clip.includes('text')) plate = path.length - 1;
      }
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT), fragments = [], unsupported = [];
      let text;
      while ((text = walker.nextNode())) if (text.textContent.trim()) {
        const parent = text.parentElement, hidden = parent.closest('[aria-hidden="true"]');
        if (hidden && hidden !== el && el.contains(hidden)) continue;
        const paint = entry(parent);
        if (paint.fill !== path[0].fill || paint.image !== path[0].image || paint.decoration !== 'none') unsupported.push(paint);
        for (let node = parent; node && node !== el; node = node.parentElement) {
          const s = entry(node);
          if (s.background !== 'rgba(0, 0, 0, 0)' || s.image !== 'none' || s.opacity !== 1 || s.filter !== 'none' || s.mask !== 'none' || s.blend !== 'normal') unsupported.push(s);
        }
        const range = document.createRange(); range.selectNodeContents(text);
        for (const rect of range.getClientRects()) if (rect.width && rect.height) fragments.push(rect.toJSON());
      }
      let outerSafe = true;
      for (let node = el.closest(hostSelector); node; node = node.parentElement) {
        const s = entry(node);
        outerSafe &&= s.opacity === 1 && s.filter === 'none' && s.mask === 'none' && s.blend === 'normal';
      }
      const x = Math.floor(Math.min(...fragments.map(r => r.left)));
      const y = Math.floor(Math.min(...fragments.map(r => r.top)));
      const right = Math.ceil(Math.max(...fragments.map(r => r.right)));
      const bottom = Math.ceil(Math.max(...fragments.map(r => r.bottom)));
      const unobscured = fragments.every(r => {
        const hit = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
        return hit && (el.contains(hit) || hit.contains(el));
      });
      return {path, plateIndex:plate, reachedHost, foreground:path[0].fill,
        fragments, unsupported, outerSafe, unobscured,
        clip:{x,y,width:right-x,height:bottom-y},
        inViewport:x >= 0 && y >= 0 && right <= innerWidth && bottom <= innerHeight};
    }""",
        host_selector,
    )
    return result


async def artwork_text_contrast(
    control: Locator, host_selector: str, evidence: Path | None = None
) -> dict[str, Any]:
    """Prove text against its actual backdrop, retaining the opaque fast path."""
    await expect(control).to_be_visible()
    await control.scroll_into_view_if_needed()
    await control.page.mouse.move(0, 0)
    await _settle_styles(control.page)
    proof = await _reading_paint(control, host_selector)
    assert proof["reachedHost"] and proof["fragments"] and not proof["unsupported"], (
        "Unsupported reading sample",
        proof,
    )
    assert proof["inViewport"] and proof["unobscured"], (
        "Reading sample clipped or obscured",
        proof,
    )
    if proof["plateIndex"] >= 0:
        proof["method"] = "opaque-front-backing"
        proof["textContrast"] = _backed_text_contrast(proof)
    else:
        assert proof["outerSafe"], ("Unsupported outer text composition", proof)
        states = _text_paints(proof)
        assert all(s["off"][3] < 1 - 1e-6 for s in states), (
            "Ill-conditioned backdrop recovery",
            states,
        )
        painted = await _region_png(control.page, proof["clip"])
        saved = await control.evaluate_handle(r"""el => {
          const parents = new Set(), walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
          let text;
          while ((text = walker.nextNode())) if (text.textContent.trim()) {
            const parent = text.parentElement, hidden = parent.closest('[aria-hidden="true"]');
            if (!hidden || hidden === el || !el.contains(hidden)) parents.add(parent);
          }
          return [...parents].map(node => {
            const values = {'-webkit-text-fill-color':'transparent'};
            if (getComputedStyle(node).backgroundClip.includes('text')) {
              values['background-image'] = 'none'; values['background-color'] = 'transparent';
            }
            const previous = Object.fromEntries(Object.keys(values).map(key => [key,
              [node.style.getPropertyValue(key),node.style.getPropertyPriority(key)]]));
            for (const [key,value] of Object.entries(values)) node.style.setProperty(key,value,'important');
            return {node,previous};
          });
        }""")
        try:
            await _settle_styles(control.page)
            backdrop = await _region_png(control.page, proof["clip"])
        finally:
            await control.page.evaluate(
                """saved => {
              for (const {node,previous} of saved) for (const [key,[value,priority]] of Object.entries(previous)) {
                if (value) node.style.setProperty(key,value,priority); else node.style.removeProperty(key);
              }
            }""",
                saved,
            )
            await saved.dispose()
            await _settle_styles(control.page)
        restored = await _reading_paint(control, host_selector)
        assert all(restored[key] == proof[key] for key in ("path", "fragments", "clip")), (
            "Reading paint restoration",
            proof,
            restored,
        )
        sampled: dict[str, Any] = await control.page.evaluate(
            """async ({png, clip, fragments, states}) => {
          const bytes = Uint8Array.from(atob(png), c => c.charCodeAt(0));
          const image = await createImageBitmap(new Blob([bytes], {type:'image/png'}));
          const canvas = new OffscreenCanvas(image.width,image.height), ctx = canvas.getContext('2d');
          ctx.drawImage(image,0,0); image.close();
          const data = ctx.getImageData(0,0,canvas.width,canvas.height).data;
          const lum = rgb => rgb.reduce((sum,x,k) => sum+[.2126,.7152,.0722][k]*(x<=.04045 ? x/12.92 : ((x+.055)/1.055)**2.4),0);
          const clamp = x => Math.max(0,Math.min(1,x));
          let minimum = Infinity, pixels = 0, worst = null;
          for (let p=0;p<data.length;p+=4) {
            const x = clip.x+((p/4)%canvas.width+.5)*clip.width/canvas.width;
            const y = clip.y+(Math.floor(p/4/canvas.width)+.5)*clip.height/canvas.height;
            if (!fragments.some(r => x>=r.left && x<r.right && y>=r.top && y<r.bottom)) continue;
            if (data[p+3] !== 255) throw new Error('Backdrop PNG is not opaque');
            pixels++;
            // One RGB unit conservatively includes PNG quantization/rounding.
            const low = [0,1,2].map(k => clamp((data[p+k]-1)/255));
            const high = [0,1,2].map(k => clamp((data[p+k]+1)/255));
            const bounds = states.map(({ink,off}) => [low,high].map(background => background.map((b,k) => {
              const underlay = clamp((b-off[k]*off[3])/(1-off[3]));
              return clamp(ink[k]*ink[3]+underlay*(1-ink[3]));
            })));
            const inkLow = [0,1,2].map(k => Math.min(...bounds.map(b => b[0][k])));
            const inkHigh = [0,1,2].map(k => Math.max(...bounds.map(b => b[1][k])));
            const il=lum(inkLow), ih=lum(inkHigh), bl=lum(low), bh=lum(high);
            const ratio = il>bh ? (il+.05)/(bh+.05) : bl>ih ? (bl+.05)/(ih+.05) : 1;
            if (ratio<minimum) { minimum=ratio; worst={x,y,background:[data[p],data[p+1],data[p+2]],inkLow,inkHigh}; }
          }
          return {minimum,pixels,worst};
        }""",
            {
                "png": base64.b64encode(backdrop).decode("ascii"),
                "clip": proof["clip"],
                "fragments": proof["fragments"],
                "states": states,
            },
        )
        assert sampled["pixels"] > 0, ("Empty reading backdrop", proof)
        if evidence is not None:
            for kind, pixels in (("painted", painted), ("backdrop", backdrop)):
                filename = f"reading-{kind}-{hashlib.sha256(pixels).hexdigest()[:16]}.png"
                target = evidence / filename
                if target.exists():
                    assert target.read_bytes() == pixels, "Reading artifact hash collision"
                else:
                    target.write_bytes(pixels)
                proof[f"{kind}Screenshot"] = filename
        proof.update(
            method="sampled-rendered-backdrop",
            textContrast=sampled["minimum"],
            sample=sampled,
            paintStates=states,
            restored=True,
        )
    assert proof["textContrast"] >= 4.5, ("Artwork text contrast", host_selector, proof)
    return proof


async def no_stale_skin_chrome(page: Page) -> dict[str, Any]:
    await _settle_styles(page)
    state: dict[str, Any] = await page.evaluate("""() => {
      const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && Number(getComputedStyle(el).opacity) > 0;
      const icons = [...document.querySelectorAll('svg[data-theme-icon][data-icon-family]')]
        .filter(el => visible(el) && !el.closest('.theme-preview'))
        .map(el => ({slot:el.dataset.themeIcon, family:el.dataset.iconFamily}));
      const scenes = [...document.querySelectorAll('.structured-pane, .structured-timeline, .chat-sidebar__body, .workspace-view .board, .workspace-view .empty-board, .empty-state, .pane-empty')]
        .filter(visible).map(el => ({classes:el.className, image:getComputedStyle(el).backgroundImage,
          scene:getComputedStyle(el).getPropertyValue('--palette-scene').trim()}));
      return {icons, scenes};
    }""")
    assert not state["icons"] and all(
        item["scene"] in ("", "none") and "url(" not in item["image"] for item in state["scenes"]
    ), (
        "Stale game chrome",
        state,
    )
    return state


async def _expect_probe_failure(check: Awaitable[Any], oracle: str) -> dict[str, Any]:
    try:
        observed = await check
    except AssertionError as exc:
        detail = exc.args[0]
        assert isinstance(detail, tuple) and detail[0] == oracle, exc
        return {"oracle": oracle, "detail": detail}
    if isinstance(observed, dict):
        observed = {
            key: observed[key]
            for key in ("slot", "family", "changed", "fraction")
            if key in observed
        }
    raise AssertionError(f"Negative control incorrectly passed: {oracle}; observed={observed!r}")


async def readability_fault_controls(
    control: Locator, host: str, evidence: Path | None = None
) -> list[dict[str, Any]]:
    original = await artwork_text_contrast(control, host, evidence)
    results = []
    faults = [{"-webkit-text-fill-color": "rgba(0,0,0,0.01)"}, {"opacity": "0.05"}]
    if "text" in original["path"][0]["clip"]:
        faults[0].update({"background-image": "none", "background-color": "transparent"})
    for fault in faults:
        async with _inline_styles(control, fault):
            results.append(
                await _expect_probe_failure(
                    artwork_text_contrast(control, host, evidence), "Artwork text contrast"
                )
            )
        restored = await artwork_text_contrast(control, host)
        assert restored["path"] == original["path"], (
            "Readability fault restoration",
            original,
            restored,
        )
        results[-1]["restoredContrast"] = restored["textContrast"]
    return results


async def common_icon_negative(
    control: Locator, output: Path, name: str, reference: dict[str, Any], original: dict[str, Any]
) -> dict[str, Any]:
    """Use the same visible common-icon injection in focused and full checks."""
    icon = control.locator("svg[data-theme-icon][data-icon-family]")
    original_nodes = await icon.evaluate_handle("el => Array.from(el.childNodes)")
    try:
        await icon.evaluate("(el, markup) => { el.innerHTML = markup }", reference["markup"])

        async def compare_common_icon() -> float:
            substituted = await rendered_theme_icon(control, output, name)
            return assert_icon_families(reference["paint"], substituted)

        result = await _expect_probe_failure(compare_common_icon(), "Theme icon family shape")
    finally:
        # Restore Vue's actual nodes, not lookalike markup with stale vnode references.
        await icon.evaluate("(el, nodes) => el.replaceChildren(...nodes)", original_nodes)
        await original_nodes.dispose()
        await _settle_styles(control.page)
    restored = await rendered_theme_icon(control, output, f"{name}-restored")
    assert restored["mask"] == original["mask"] and restored["rect"] == original["rect"], (
        "Common-icon restoration",
        name,
    )
    result["restored_mask_matches"] = True
    result["restored_family_disagreement"] = assert_icon_families(reference["paint"], restored)
    return result


async def _rich_desktop_chrome(
    page: Page,
    button: Locator,
    output: Path,
    palette: str,
    mode: str,
    icon_samples: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    name = f"rich-{palette_id(palette)}-{mode}"
    result: dict[str, Any] = {
        "art": await chat_artwork(page, output, name),
        "icons": [],
        "negatives": [],
    }
    for slot, label in (("terminal", "Terminal"), ("workspace", "Agent Workspace")):
        control = page.get_by_role("tab", name=label, exact=True)
        icon = control.locator("svg[data-theme-icon][data-icon-family]")
        sample = await rendered_theme_icon(control, output, f"{name}-{slot}")
        assert sample["slot"] == slot and sample["family"] == palette_id(palette), sample
        prior = icon_samples.setdefault((mode, slot), [])
        sample["family_disagreements"] = [
            assert_icon_families(item["paint"], sample) for item in prior
        ]
        markup = await icon.inner_html()
        if palette == "Zenless" and mode == "dark":
            async with _inline_styles(icon, {"opacity": "0"}):
                result["negatives"].append(
                    await _expect_probe_failure(
                        rendered_theme_icon(control, output, f"{name}-{slot}-hidden"),
                        "Icon painted contribution",
                    )
                )
            restored = await rendered_theme_icon(control, output, f"{name}-{slot}-unhidden")
            assert restored["mask"] == sample["mask"], ("Hidden-icon restoration", slot)
            result["negatives"][-1]["restored_mask_matches"] = True
        if palette == "Frontier" and mode == "dark":
            result["negatives"].append(
                await common_icon_negative(
                    control,
                    output,
                    f"{name}-{slot}-common",
                    prior[0],
                    sample,
                )
            )
        prior.append({"paint": sample, "markup": markup})
        result["icons"].append(sample)
    if palette == "Zenless" and mode == "dark":
        host = page.locator(".structured-timeline:visible").first
        async with _inline_styles(host, {"background-image": "var(--palette-scene-glow)"}):
            result["negatives"].append(
                await _expect_probe_failure(
                    chat_artwork(page, output, f"{name}-hidden"),
                    "Artwork visible area",
                )
            )
        async with _inline_styles(host, {"--palette-scene": "none"}):
            result["negatives"].append(
                await _expect_probe_failure(
                    chat_artwork(page, output, f"{name}-missing"),
                    "Local artwork decode",
                )
            )
        result["art_restored"] = await chat_artwork(page, output, f"{name}-restored")
        async with _inline_styles(button, {"background-color": "#d6f300"}):
            try:
                assert_zenless_orange(await rendered_button_state(button, "rest"))
            except AssertionError as exc:
                assert exc.args[0][0] == "Zenless orange primary", exc
                result["negatives"].append(
                    {"oracle": "Zenless orange primary", "detail": exc.args[0]}
                )
            else:
                raise AssertionError("Old lime primary was accepted")
        assert_zenless_orange(await rendered_button_state(button, "rest"))
        result["negatives"].extend(await immersive_fault_controls(page, output, name))
    # Activate the real native tab controls, not their decorative SVG nodes.
    workspace = page.get_by_role("tab", name="Agent Workspace", exact=True)
    await _native_tab_to(workspace)
    await page.keyboard.press("Enter")
    await expect(workspace).to_have_attribute("aria-selected", "true")
    await expect(page.locator(".workspace-view")).to_be_visible()
    terminal = page.get_by_role("tab", name="Terminal", exact=True)
    await _native_tab_to(terminal)
    await page.keyboard.press("Space")
    await expect(terminal).to_have_attribute("aria-selected", "true")
    await expect(button).to_be_visible()
    await _settle_styles(page)
    result["resting_navigation"] = await terminal.evaluate("""el => ({
      background:getComputedStyle(el).backgroundColor,
      accent:getComputedStyle(el).getPropertyValue('--ch-color-accent-strong').trim(),
      rect:el.getBoundingClientRect().toJSON()})""")
    result["native_activation"] = ["Agent Workspace:Enter", "Terminal:Space"]
    return result


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


async def quiet_chat_surfaces(page: Page) -> dict[str, Any]:
    """Reject resting decorative frames without banning functional focus cues."""
    await _settle_styles(page)
    rendered: list[dict[str, Any]] = await page.evaluate("""() => {
      const selectors = ['.structured-composer','.composer-row','.structured-turn',
        '.conversation-bubble--assistant','.turn-actions'];
      return selectors.flatMap(selector => [...document.querySelectorAll(selector)]
        .filter(el => el.getClientRects().length).map(el => {
          const s = getComputedStyle(el);
          return {selector,rect:el.getBoundingClientRect().toJSON(),background:s.backgroundColor,
            image:s.backgroundImage,shadow:s.boxShadow,
            borders:['Top','Right','Bottom','Left'].map(side => ({width:parseFloat(s[`border${side}Width`]),style:s[`border${side}Style`]})),
            pseudo:['::before','::after'].map(pseudo => {
              const p = getComputedStyle(el,pseudo);
              return {content:p.content,shadow:p.boxShadow,widths:['Top','Right','Bottom','Left'].map(side => parseFloat(p[`border${side}Width`]))};
            })};
        }));
    }""")
    assert {".structured-composer", ".composer-row", ".structured-turn"} <= {
        item["selector"] for item in rendered
    }, ("Missing quiet surfaces", rendered)
    checks = {"no_heavy_frames": True, "no_turn_slabs": True}
    for item in rendered:
        checks["no_heavy_frames"] &= all(
            b["width"] <= 1 and b["style"] != "double" for b in item["borders"]
        )
        for _, dimensions, _ in _shadow_parts(item["shadow"]):
            checks["no_heavy_frames"] &= dimensions[3] <= 0 and (
                dimensions[2] > 0 or dimensions[0] == dimensions[1] == 0
            )
        for pseudo in item["pseudo"]:
            if pseudo["content"] not in ("none", "normal"):
                checks["no_heavy_frames"] &= (
                    max(pseudo["widths"]) <= 1 and pseudo["shadow"] == "none"
                )
        if item["selector"] in (
            ".structured-turn",
            ".conversation-bubble--assistant",
            ".turn-actions",
        ):
            checks["no_turn_slabs"] &= (
                _rgba(item["background"])[3] == 0
                and item["image"] == "none"
                and item["shadow"] == "none"
            )
    assert all(checks.values()), ("Quiet chat surfaces", checks, rendered)
    return {"checks": checks, "rendered": rendered}


async def immersive_fault_controls(page: Page, output: Path, name: str) -> list[dict[str, Any]]:
    original = await quiet_chat_surfaces(page)
    async with _inline_styles(
        page.locator(".composer-row"),
        {"border": "4px double #ff6f0f", "box-shadow": "6px 6px 0 #777"},
    ):
        frame = await _expect_probe_failure(quiet_chat_surfaces(page), "Quiet chat surfaces")
    assert await quiet_chat_surfaces(page) == original, "Old frame restoration"
    frame["restored"] = True
    owner = page.locator(".structured-timeline:visible").first
    snapshot = """el => ({rect:el.getBoundingClientRect().toJSON(),image:getComputedStyle(el).backgroundImage,
      position:getComputedStyle(el).backgroundPosition,size:getComputedStyle(el).backgroundSize,
      opacity:getComputedStyle(el).opacity,filter:getComputedStyle(el).filter,mask:getComputedStyle(el).maskImage,
      glow:getComputedStyle(el).getPropertyValue('--palette-scene-glow')})"""
    state = await owner.evaluate(snapshot)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 320"><path fill="white" d="M0 0h1200v320H0z"/><path fill="black" d="M0 0h200v320H0zm400 0h200v320H400zm400 0h200v320H800z"/></svg>'
    scene = (
        'url("data:image/svg+xml;base64,' + base64.b64encode(svg.encode()).decode("ascii") + '")'
    )
    async with _inline_styles(owner, {"--palette-scene": scene}):
        changed = await owner.evaluate(snapshot)
        assert all(changed[k] == state[k] for k in state if k != "image"), (
            "Strong art changed layout/glow",
            state,
            changed,
        )
        strong = await _expect_probe_failure(
            chat_artwork(page, output, f"{name}-strong"), "Artwork salience"
        )
    assert await owner.evaluate(snapshot) == state, "Strong art computed-style restoration"
    restored = await chat_artwork(page, output, f"{name}-strong-restored")
    strong["restored"] = True
    strong["restoredPaint"] = restored["paint"]
    return [frame, strong]


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
    game_palettes = GAME_PALETTES
    game_report: list[dict[str, Any]] = []
    icon_samples: dict[tuple[str, str], list[dict[str, Any]]] = {}

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
                    if palette == "Zenless":
                        rendered["orange"] = assert_zenless_orange(rendered)
                    report.append(
                        {"palette": palette, "mode": mode, **rendered, "contrast": round(ratio, 3)}
                    )
                if screenshot_dir is not None and palette in game_palettes:
                    game_report.append(
                        {
                            "palette": palette,
                            "mode": mode,
                            "cues": await quiet_chat_surfaces(page),
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
                            "rich_chrome": await _rich_desktop_chrome(
                                page,
                                button,
                                screenshot_dir,
                                palette,
                                mode,
                                icon_samples,
                            ),
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
                variant["phone_quiet"] = await quiet_chat_surfaces(page)
                variant["phone_art"] = await chat_artwork(
                    page,
                    screenshot_dir,
                    f"rich-phone-{palette_id(variant['palette'])}-{variant['mode']}",
                )
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
