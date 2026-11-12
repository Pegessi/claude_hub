"""Exercise hidden queued-Chat activation in the real rendered SPA.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_chat_queue_activation.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from appearance_assertions import (  # noqa: E402
    GAME_PALETTES,
    backing_fault_controls,
    opaque_artwork_backing,
    select_appearance,
)
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB_A = "queue-tab-a"
TAB_B = "queue-tab-b"
OTHER_TABS = [TAB_B, *(f"queue-tab-{index}" for index in range(2, 11))]
TURN = "queue-turn-a"
NOW = "2026-10-11T08:00:00Z"


def origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlparse(url)
    return parsed.scheme, parsed.hostname, parsed.port


def event(sequence: int, event_type: str, payload: dict) -> dict:
    return {
        "stream_sequence": sequence,
        "session_id": f"terminal-{TAB_A}",
        "tab_id": TAB_A,
        "agent_type": "traex",
        "type": event_type,
        "turn_id": TURN,
        "message_id": f"{TURN}:user" if event_type == "turn_started" else TURN,
        "payload": payload,
        "created_at": NOW,
        "redacted": False,
    }


STARTED = event(0, "turn_started", {"summary": "Initial work", "mode": "default"})
COMPLETED = event(1, "turn_completed", {"status": "completed"})


def surface_stream_events() -> list[dict]:
    """Replay the production thinking/tool/status paths after queue acceptance."""
    specs: list[tuple[str, str, dict[str, Any], str]] = [
        ("history", "turn_started", {"summary": "Surface transcript proof"}, "surface-user"),
        (
            "history",
            "status",
            {"text": "Thinking…", "provider_status": "reasoning", "snapshot": True},
            "reasoning:first",
        ),
        (
            "history",
            "thinking_delta",
            {"text": "Inspect the natural reading surface."},
            "surface-thinking",
        ),
        (
            "history",
            "status",
            {"text": "Done thinking", "provider_status": "reasoning", "snapshot": True},
            "reasoning:first",
        ),
        (
            "history",
            "tool_call_started",
            {"name": "Read", "args": {"path": "/fixture/note.txt"}, "tool_call_id": "surface-read"},
            "surface-read",
        ),
        (
            "history",
            "tool_call_completed",
            {
                "name": "Read",
                "result": "A bounded result.",
                "status": "completed",
                "tool_call_id": "surface-read",
            },
            "surface-read",
        ),
        (
            "history",
            "status",
            {"text": "Done thinking", "provider_status": "reasoning", "snapshot": True},
            "reasoning:after-tool",
        ),
        ("history", "status", {"text": "Inspection activity note"}, "surface-status"),
        (
            "history",
            "error",
            {"message": "Fixture diagnostic", "detail": "A bounded diagnostic detail."},
            "surface-error",
        ),
        # A delivered answer is required for the natural process-fold boundary.
        ("history", "text_delta", {"text": "Surface answer."}, "surface-answer"),
        ("history", "turn_completed", {"status": "cancelled"}, "surface-history"),
        ("live", "turn_started", {"summary": "Surface live proof"}, "surface-live-user"),
        (
            "live",
            "tool_call_started",
            {
                "name": "Read",
                "args": {"path": "/fixture/live.txt"},
                "tool_call_id": "surface-live-read",
            },
            "surface-live-read",
        ),
        (
            "live",
            "status",
            {"text": "Thinking…", "provider_status": "reasoning", "snapshot": True},
            "reasoning:live",
        ),
    ]
    rows = []
    for sequence, (turn, kind, payload, message_id) in enumerate(specs):
        rows.append(
            {
                **event(sequence, kind, payload),
                "turn_id": f"surface-{turn}",
                "message_id": message_id,
            }
        )
    return rows


async def main(browser_run: BrowserRun) -> None:
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

    tabs = [
        {
            "id": tab_id,
            "name": name,
            "cwd": "/fixture/project",
            "agent_type": "traex",
            "session_kind": "chat",
            "port": 0,
            "is_active": True,
            "created_at": NOW,
        }
        for tab_id, name in (
            (TAB_A, "Queued Chat"),
            *((tab_id, f"Other Chat {index}") for index, tab_id in enumerate(OTHER_TABS, 1)),
        )
    ]
    completion_released = asyncio.Event()
    completion_visible = False
    send_payloads: list[dict] = []
    surface_rows: list[dict] | None = None
    surface_send_payloads: list[dict] = []
    pending_released = asyncio.Event()
    report: dict[str, Any] = {
        "result": "FAIL",
        "api_requests": [],
        "blocked_external_requests": [],
        "blocked_websockets": [],
        "unknown_requests": [],
        "page_errors": [],
        "send_payloads": send_payloads,
        "surface_send_payloads": surface_send_payloads,
        "surface_backing": [],
        "pending_backing": [],
        "screenshots": [],
        "boundary": "Real SPA rendering with mocked APIs; no backend or provider traffic.",
    }
    failure: BaseException | None = None
    page = None

    async with browser_run.context(
        viewport={"width": 1280, "height": 900},
        launch_args=["--disable-background-networking", "--disable-component-update"],
    ) as context:
        try:

            async def block_websocket(socket) -> None:
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="mocked queue smoke")

            async def route_all(route) -> None:
                nonlocal completion_visible
                request = route.request
                parsed = urlparse(request.url)
                if origin(request.url) != origin(base):
                    report["blocked_external_requests"].append(request.url)
                    await route.abort("blockedbyclient")
                    return
                path = parsed.path
                if not path.startswith("/api/"):
                    await route.continue_()
                    return
                report["api_requests"].append([request.method, path])

                body: object
                if path == "/api/auth/check":
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
                        "supports_images": False,
                        "agent_type": "traex",
                        "supports_mode_switch": False,
                    }
                elif path.endswith("/events"):
                    tab_id = TAB_A if f"/{TAB_A}/" in path else TAB_B
                    since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    rows = []
                    if tab_id == TAB_A:
                        candidates = (
                            surface_rows
                            if surface_rows is not None
                            else [
                                STARTED,
                                *([COMPLETED] if completion_visible else []),
                            ]
                        )
                        rows = [item for item in candidates if item["stream_sequence"] > since]
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {"events": rows, "next_sequence": next_sequence, "has_more": False}
                elif path.endswith("/wait"):
                    tab_id = TAB_A if f"/{TAB_A}/" in path else TAB_B
                    since = int(parse_qs(parsed.query).get("since_sequence", ["-1"])[0])
                    if surface_rows is not None:
                        await asyncio.sleep(0.1)
                        rows = (
                            [item for item in surface_rows if item["stream_sequence"] > since]
                            if tab_id == TAB_A
                            else []
                        )
                    else:
                        if tab_id == TAB_A and not completion_visible:
                            await completion_released.wait()
                            completion_visible = True
                        else:
                            await asyncio.sleep(0.2)
                        rows = [COMPLETED] if tab_id == TAB_A and since < 1 else []
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {"events": rows, "next_sequence": next_sequence, "has_more": False}
                elif path.endswith("/live"):
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=": mock\n\n"
                    )
                    return
                elif path.endswith("/stream/send") and request.method == "POST":
                    if surface_rows is not None:
                        surface_send_payloads.append(json.loads(request.post_data or "{}"))
                        await pending_released.wait()
                    else:
                        send_payloads.append(json.loads(request.post_data or "{}"))
                    body = {"ok": True}
                elif path.startswith("/api/tabs/sidebar-preferences"):
                    # Read, one-shot pin migration and per-tab pin updates
                    # share one shape; this scenario does not depend on pins.
                    body = {"pinned_tab_ids": []}
                elif path.endswith("/goal/current"):
                    body = None
                elif path.endswith("/view"):
                    body = {"ok": True}
                elif "network" in path:
                    body = {"hostname": "fixture", "addresses": []}
                elif path == "/api/dot/identity" and request.method == "GET":
                    # The Dot launcher in the tab bar reads Dot's name and avatar.
                    body = {"name": "Dot", "avatar": "", "avatar_image": None}
                elif request.method == "GET" and path == "/api/subagents":
                    # Every Chat polls Hub sub-agent links; this fixture has none.
                    body = []
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
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            await page.goto(base + f"/?tab={TAB_A}", wait_until="domcontentloaded")

            composer = page.locator(".composer-textarea").first
            await expect(composer).to_be_enabled()
            await composer.fill("Queued after the first turn")
            await composer.press("Enter")
            await expect(page.locator(".draft-queue .queue-heading")).to_contain_text(
                "1 message queued"
            )

            await page.locator(".chat-sidebar__show-more").click()
            for tab_id in OTHER_TABS:
                await page.locator(
                    f'.chat-sidebar__item[data-tab-id="{tab_id}"] .chat-sidebar__item-main'
                ).click()
            await expect(
                page.locator(f'.chat-sidebar__item[data-tab-id="{OTHER_TABS[-1]}"]')
            ).to_have_class(re.compile(r"(^|\s)active(\s|$)"))
            await page.screenshot(path=str(output / "queued-while-hidden.png"), full_page=True)
            report["screenshots"].append("queued-while-hidden.png")

            completion_released.set()
            active_a = page.locator(f'.chat-sidebar__item[data-tab-id="{TAB_A}"]')
            await expect(active_a).to_have_class(re.compile(r"(^|\s)active(\s|$)"), timeout=5000)
            await expect(page.locator(".draft-queue")).to_have_count(0, timeout=5000)
            deadline = asyncio.get_running_loop().time() + 5
            while not send_payloads and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
            assert len(send_payloads) == 1, send_payloads
            assert send_payloads[0]["text"] == "Queued after the first turn"
            await asyncio.sleep(0.25)
            assert len(send_payloads) == 1, send_payloads
            await page.screenshot(path=str(output / "activated-and-sent.png"), full_page=True)
            report["screenshots"].append("activated-and-sent.png")

            # Queue edit buffers survive bounded KeepAlive eviction and yield
            # activation while hidden, without sending the old saved text.
            await composer.fill("Do not send until reviewed")
            await composer.press("Enter")
            queued = page.locator(".queued-draft").first
            await queued.get_by_role("button", name="Edit", exact=True).click()
            await queued.get_by_role("textbox").fill("Persisted queue edit after eviction")
            for tab_id in OTHER_TABS:
                await page.locator(
                    f'.chat-sidebar__item[data-tab-id="{tab_id}"] .chat-sidebar__item-main'
                ).click()
            await asyncio.sleep(0.3)
            assert len(send_payloads) == 1
            await expect(
                page.locator(f'.chat-sidebar__item[data-tab-id="{OTHER_TABS[-1]}"]')
            ).to_have_class(re.compile(r"(^|\s)active(\s|$)"))
            await page.locator(
                f'.chat-sidebar__item[data-tab-id="{TAB_A}"] .chat-sidebar__item-main'
            ).click()
            await expect(queued.get_by_role("textbox")).to_have_value(
                "Persisted queue edit after eviction"
            )
            await queued.get_by_role("button", name="Cancel", exact=True).click()
            await expect(page.locator(".draft-queue")).to_have_count(0)
            assert len(send_payloads) == 1

            # Queue correctness is complete. Reuse its real long sidebar and
            # donor stream events for a bounded reading-surface matrix.
            surface_rows = surface_stream_events()
            await page.set_viewport_size({"width": 1280, "height": 560})
            await page.reload(wait_until="domcontentloaded")
            history = page.locator(".structured-turn").filter(has_text="Surface transcript proof")
            live = page.locator(".structured-turn").filter(has_text="Surface live proof")
            await expect(history).to_have_count(1)
            await expect(live).to_have_count(1)
            off_origin_before_surfaces = set(report["blocked_external_requests"])
            for mode in ("dark", "light"):
                for palette in GAME_PALETTES:
                    await select_appearance(page, mode, palette)
                    samples = {}
                    group = page.locator(
                        ".chat-sidebar__group:not(.chat-sidebar__group--pinned)"
                    ).first
                    more = group.locator(".chat-sidebar__show-more")
                    if await more.get_attribute("aria-expanded") == "true":
                        await more.click()
                    await expect(more).to_contain_text("Show more")
                    inactive = group.locator(".chat-sidebar__item:not(.active)").first
                    for label, control in (
                        ("folder", group.locator(".chat-sidebar__group-label")),
                        ("folder-count", group.locator(".chat-sidebar__group-count")),
                        ("show-more", more),
                        ("inactive-name", inactive.locator(".chat-sidebar__item-name")),
                        ("inactive-time", inactive.locator(".chat-sidebar__item-time")),
                    ):
                        samples[label] = await opaque_artwork_backing(
                            control, ".chat-sidebar__body"
                        )
                    await more.click()
                    await expect(more).to_contain_text("Show less")
                    samples["expanded-last-row"] = await opaque_artwork_backing(
                        group.locator(
                            ".chat-sidebar__item:not(.active) .chat-sidebar__item-name"
                        ).last,
                        ".chat-sidebar__body",
                    )
                    process = history.get_by_test_id("history-details-toggle")
                    if await process.get_attribute("aria-expanded") == "true":
                        await process.click()
                    samples["process-closed"] = await opaque_artwork_backing(
                        process.locator(".process-fold-meta"),
                        ".structured-pane",
                    )
                    await process.click()
                    await expect(process).to_have_attribute("aria-expanded", "true")
                    samples["process-open"] = await opaque_artwork_backing(
                        process.locator(".process-fold-meta"),
                        ".structured-pane",
                    )
                    thinking = history.locator(".thinking-card")
                    summary = thinking.locator("summary")
                    if await thinking.get_attribute("open") is not None:
                        await summary.click()
                    samples["thinking-closed"] = await opaque_artwork_backing(
                        summary.locator(".thinking-label > span:last-child"),
                        ".structured-pane",
                    )
                    await summary.click()
                    samples["thinking-open"] = await opaque_artwork_backing(
                        thinking.locator(".thinking-body"),
                        ".structured-pane",
                    )
                    await summary.click()
                    tools = history.locator(".tool-card--group")
                    tool_summary = tools.locator(":scope > summary")
                    if await tools.get_attribute("open") is not None:
                        await tool_summary.click()
                    samples["tool-closed"] = await opaque_artwork_backing(
                        tool_summary.locator(".activity-verb"),
                        ".structured-pane",
                    )
                    await tool_summary.click()
                    samples["tool-open"] = await opaque_artwork_backing(
                        tool_summary.locator(".activity-verb"),
                        ".structured-pane",
                    )
                    await tool_summary.click()
                    for label, control in (
                        ("status", history.locator(".event-status > span")),
                        (
                            "reasoning",
                            history.locator(".reasoning-status .thinking-label > span:last-child"),
                        ),
                        ("error", history.locator(".error-body > span")),
                        ("error-detail", history.locator(".error-detail")),
                        ("cancelled", history.locator(".turn-cancelled-marker")),
                        ("working", live.locator(".turn-working-label")),
                        ("working-step", live.locator(".turn-working-step")),
                        ("working-time", live.locator(".turn-working-elapsed")),
                        ("working-counts", live.locator(".turn-working-counts-text")),
                        (
                            "live-reasoning",
                            live.locator(".reasoning-status .thinking-label > span:last-child"),
                        ),
                    ):
                        samples[label] = await opaque_artwork_backing(control, ".structured-pane")
                    if palette == "Zenless" and mode == "dark":
                        report["backing_negatives"] = await backing_fault_controls(
                            inactive.locator(".chat-sidebar__item-name"),
                            ".chat-sidebar__body",
                        )
                    report["surface_backing"].append(
                        {"palette": palette, "mode": mode, "samples": samples}
                    )
                    screenshot = (
                        f"reading-backplates-{palette.lower().replace(' ', '-')}-{mode}.png"
                    )
                    await page.screenshot(path=str(output / screenshot), full_page=True)
                    report["screenshots"].append(screenshot)
                    print(f"Reading backplates captured: {palette}/{mode}", flush=True)

            surface_rows.append(
                {
                    **event(len(surface_rows), "turn_completed", {"status": "completed"}),
                    "turn_id": "surface-live",
                    "message_id": "surface-live",
                }
            )
            await expect(page.locator(".turn-working")).to_have_count(0)
            await composer.fill("Pending reading surface proof")
            await composer.press("Enter")
            pending = page.locator(".structured-turn--pending")
            try:
                await expect(pending).to_have_count(1)
                for mode in ("dark", "light"):
                    for palette in GAME_PALETTES:
                        await select_appearance(page, mode, palette)
                        report["pending_backing"].append(
                            {
                                "palette": palette,
                                "mode": mode,
                                "status": await opaque_artwork_backing(
                                    pending.locator(".event-status--pending > span"),
                                    ".structured-pane",
                                ),
                                "message": await opaque_artwork_backing(
                                    pending.locator(
                                        ".conversation-bubble--user .markdown-content p"
                                    ),
                                    ".structured-pane",
                                ),
                            }
                        )
                        screenshot = (
                            f"pending-backplate-{palette.lower().replace(' ', '-')}-{mode}.png"
                        )
                        await page.screenshot(path=str(output / screenshot), full_page=True)
                        report["screenshots"].append(screenshot)
            finally:
                pending_released.set()
            assert len(surface_send_payloads) == 1, surface_send_payloads
            assert surface_send_payloads[0]["text"] == "Pending reading surface proof"
            assert len(send_payloads) == 1, send_payloads
            assert set(report["blocked_external_requests"]) <= off_origin_before_surfaces
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
    print(
        json.dumps(
            {
                **{
                    key: value
                    for key, value in report.items()
                    if key not in ("surface_backing", "pending_backing")
                },
                "surface_backing_variants": len(report["surface_backing"]),
                "pending_backing_variants": len(report["pending_backing"]),
            },
            indent=2,
        )
    )
    if failure is not None:
        raise failure


if __name__ == "__main__":
    run_browser_main(main)
