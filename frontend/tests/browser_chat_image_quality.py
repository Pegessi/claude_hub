"""Exercise Chat image fidelity and the Cursor sub-agent card in the real SPA.

Run only with a task-owned Vite server, never 5173/8173:
  python tests/browser_chat_image_quality.py <owned-loopback-url> <new-output-dir> <browser>

All APIs are mocked. No Hub backend or provider is contacted.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import sys
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_lifecycle import BrowserRun, run_browser_main  # noqa: E402

TAB = "image-quality-tab"
OTHER_TAB = "other-image-tab"
TURN = "cursor-turn"
NOW = "2026-10-12T08:00:00Z"
REPORT = "Run B regressed on 3 cases."
OUTPUT_PATH = "/tmp/dotbm/dot-mobile-work.png"
OUTPUT_TEXT = "[View screenshot](/tmp/result.png)\n\n![Missing screenshot](/tmp/missing.png)"


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


def png_size(data_url: str) -> tuple[int, int]:
    raw = base64.b64decode(data_url.split(",", 1)[1])
    assert raw[12:16] == b"IHDR", raw[:16]
    return struct.unpack(">II", raw[16:24])


def event(sequence: int, event_type: str, payload: dict, message_id: str, turn: str = TURN) -> dict:
    return {
        "stream_sequence": sequence,
        "session_id": f"terminal-{TAB}",
        "tab_id": TAB,
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
    send_payloads: list[dict] = []
    report: dict = {
        "result": "FAIL",
        "api_requests": [],
        "blocked_external_requests": [],
        "blocked_websockets": [],
        "unknown_requests": [],
        "page_errors": [],
        "sends": [],
        "screenshots": [],
        "boundary": "Real SPA rendering with mocked APIs; no backend or provider traffic.",
    }
    failure: BaseException | None = None
    page = None
    output_image = png(1920, 1080)

    async with browser_run.context(
        viewport={"width": 1280, "height": 900},
        launch_args=["--disable-background-networking", "--disable-component-update"],
    ) as context:
        try:

            async def block_websocket(socket) -> None:
                report["blocked_websockets"].append(socket.url)
                await socket.close(code=1000, reason="mocked image smoke")

            async def route_all(route) -> None:
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
                report["api_requests"].append([request.method, path, parsed.query])

                body: object
                if path == "/api/auth/check":
                    body = {"auth_required": False, "user": None}
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
                        rows = []
                    next_sequence = rows[-1]["stream_sequence"] if rows else since
                    body = {
                        "events": rows,
                        "next_sequence": next_sequence,
                        "has_more": False,
                    }
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
            await page.goto(base + f"/?tab={TAB}", wait_until="domcontentloaded")

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
            dialog = page.get_by_role("dialog", name="Image preview", exact=True)
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
            await expect(dialog.get_by_role("button", name="Close image preview")).to_be_focused()
            await page.keyboard.press("Tab")
            assert await dialog.evaluate("el => el.contains(document.activeElement)")
            await expect(dialog.get_by_role("button", name="Actual size")).to_be_in_viewport()
            await expect(original).to_be_in_viewport()
            await page.screenshot(path=str(output / "assistant-image-mobile.png"), full_page=True)
            report["screenshots"].append("assistant-image-mobile.png")
            await dialog.get_by_role("button", name="Close image preview").click()
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
            await expect(dialog).to_have_count(0)
            active_composer = page.locator(".composer-textarea:visible").first
            await active_composer.focus()
            await page.keyboard.press("Tab")
            assert await page.evaluate(
                "!document.activeElement.closest('.structured-image-lightbox')"
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


if __name__ == "__main__":
    run_browser_main(main)
