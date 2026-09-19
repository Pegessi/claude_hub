"""Real-browser regression for Chat sidebar pointer reordering."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Generator

import pytest
import requests
from playwright.sync_api import Page, Route

_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _ROOT / "frontend"
pytestmark = pytest.mark.skipif(
    os.environ.get("CLAUDE_HUB_RUN_CHAT_SIDEBAR_E2E") != "1",
    reason="opt-in real-browser test; requires Chromium and installed frontend dependencies",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def sidebar_vite(backend_server: None, tmp_path: Path) -> Generator[str, None, None]:
    """Serve the real Vue app while the isolated backend owns all state."""
    from tests.conftest import BACKEND_URL

    port = _free_port()
    log_path = tmp_path / "chat-sidebar-vite.log"
    log_file = log_path.open("wb")
    env = {
        **os.environ,
        "VITE_API_TARGET": BACKEND_URL,
        "VITE_PORT": str(port),
    }
    proc = subprocess.Popen(
        ["pnpm", "dev", "--host", "127.0.0.1"],
        cwd=_FRONTEND,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    session = requests.Session()
    session.trust_env = False
    for _ in range(50):
        try:
            if session.get(url, timeout=0.5).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(0.1)
    else:
        proc.terminate()
        proc.wait(timeout=5)
        log_file.close()
        pytest.fail(f"Vite failed to start: {log_path.read_text(errors='ignore')[-2000:]}")

    try:
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


def test_pointer_drag_reorders_rows_and_sends_complete_order(
    sidebar_vite: str,
    page: Page,
) -> None:
    from tests.conftest import BACKEND_URL

    session = requests.Session()
    session.trust_env = False
    tabs = []
    for index in range(3):
        response = session.post(
            f"{BACKEND_URL}/api/tabs",
            json={
                "name": f"pointer-reorder-{index}",
                "agent_type": "traex",
                "session_kind": "chat",
                "cwd": "/tmp/sidebar-pointer-e2e",
            },
            timeout=5,
        )
        assert response.status_code == 201, response.text
        tabs.append(response.json())

    intercepted: list[dict[str, list[str]]] = []

    def capture_order(route: Route) -> None:
        intercepted.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/tabs/order", capture_order)
    try:
        page.goto(sidebar_vite, wait_until="domcontentloaded")
        rows = page.locator(".chat-sidebar__item[data-tab-id]")
        created_ids = {tab["id"] for tab in tabs}
        for tab_id in created_ids:
            page.locator(f'.chat-sidebar__item[data-tab-id="{tab_id}"]').wait_for(state="visible")
        visible_ids = [
            tab_id
            for tab_id in rows.evaluate_all(
                "elements => elements.map(element => element.dataset.tabId)"
            )
            if tab_id in created_ids
        ]
        assert len(visible_ids) == 3
        source_id, target_id = visible_ids[0], visible_ids[-1]
        source = page.locator(f'.chat-sidebar__item[data-tab-id="{source_id}"]')
        target = page.locator(f'.chat-sidebar__item[data-tab-id="{target_id}"]')
        source_box = source.bounding_box()
        target_box = target.bounding_box()
        assert source_box and target_box

        page.mouse.move(source_box["x"] + 40, source_box["y"] + 17)
        page.mouse.down()
        page.mouse.move(source_box["x"] + 42, source_box["y"] + 25)
        page.mouse.move(
            target_box["x"] + 40,
            target_box["y"] + target_box["height"] - 2,
            steps=8,
        )
        page.mouse.up()

        page.wait_for_timeout(100)
        assert intercepted
        ids = intercepted[-1]["tab_ids"]
        assert ids.index(target_id) < ids.index(source_id)
        reordered_visible_ids = [
            tab_id
            for tab_id in rows.evaluate_all(
                "elements => elements.map(element => element.dataset.tabId)"
            )
            if tab_id in created_ids
        ]
        assert reordered_visible_ids[-1] == source_id
        assert "chat-sidebar-dragging" not in (page.locator("body").get_attribute("class") or "")
    finally:
        for tab in tabs:
            session.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
