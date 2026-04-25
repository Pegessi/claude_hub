from difflib import unified_diff
from typing import AsyncGenerator, Generator

import pytest
import requests
from httpx import ASGITransport, AsyncClient
from playwright.sync_api import Page

BACKEND_URL = "http://127.0.0.1:8173"

# ── helpers ──────────────────────────────────────────────────────────────


def normalize_terminal_output(text: str) -> list[str]:
    """Normalize terminal output for comparison.

    Strip CR, trim trailing whitespace per line, remove trailing blank lines.
    """
    lines = text.replace("\r", "").split("\n")
    lines = [l.rstrip() for l in lines]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def diff_summary(actual: list[str], expected: list[str]) -> str:
    """Produce readable diff for assertion failure."""
    diff = unified_diff(expected, actual, lineterm="", fromfile="tmux", tofile="xterm")
    return "\n".join(diff)


def capture_pane_sync(session_name: str, start: str = "-100000", end: str = "") -> str:
    """Run tmux capture-pane synchronously and return stdout."""
    import subprocess

    args = ["tmux", "capture-pane", "-p", "-e", "-S", start, "-t", session_name]
    if end:
        args.extend(["-E", end])
    result = subprocess.run(args, capture_output=True, text=True)
    return result.stdout


def send_keys_sync(session_name: str, *keys: str) -> None:
    """Send keys to a tmux session synchronously."""
    import subprocess

    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, *keys],
        capture_output=True,
    )


def tmux_session_exists(session_name: str) -> bool:
    """Check if a tmux session exists."""
    import subprocess

    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def backend_server() -> Generator[None, None, None]:
    """Start the real FastAPI backend on port 8173 in a background process."""
    import pathlib
    import subprocess
    import time

    # Check if backend is already running
    try:
        resp = requests.get(f"{BACKEND_URL}/health", timeout=1)
        if resp.status_code == 200:
            yield  # Backend already running, nothing to start/stop
            return
    except requests.ConnectionError:
        pass

    backend_dir = pathlib.Path(__file__).parent.parent
    venv_python = backend_dir / ".venv" / "bin" / "python"

    # Fallback: use sys.executable if venv not found
    if not venv_python.exists():
        import sys

        venv_python = pathlib.Path(sys.executable)

    proc = subprocess.Popen(
        [
            str(venv_python),
            "-m",
            "uvicorn",
            "claude_hub.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8173",
            "--log-level",
            "error",
        ],
        cwd=str(backend_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for server to be ready
    for _ in range(50):
        try:
            resp = requests.get(f"{BACKEND_URL}/health", timeout=1)
            if resp.status_code == 200:
                break
        except requests.ConnectionError:
            time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError("Backend server failed to start")

    yield

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Provide an httpx AsyncClient pointing at the FastAPI app."""
    from claude_hub.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def terminal_tab(backend_server: None) -> Generator[dict, None, None]:
    """Create a terminal tab for testing, with guaranteed cleanup.

    Also ensures the tmux session exists by making an initial connection
    via Playwright (ttyd creates the tmux session lazily on first WS connect).
    """
    import time

    # Create tab via API
    resp = requests.post(
        f"{BACKEND_URL}/api/tabs",
        json={"name": "test-replay", "agent_type": "cursor"},
    )
    assert resp.status_code == 201, f"Failed to create tab: {resp.text}"
    tab = resp.json()

    yield tab

    # Cleanup: delete tab (kills tmux session too)
    try:
        requests.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
    except Exception:
        pass
