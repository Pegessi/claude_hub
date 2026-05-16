import os
import re
from difflib import unified_diff
from pathlib import Path
from typing import AsyncGenerator, Generator
from urllib.parse import urlparse

import pytest
import requests
from httpx import ASGITransport, AsyncClient
from playwright.sync_api import Page

BACKEND_URL = os.environ.get("CLAUDE_HUB_TEST_BACKEND_URL", "http://127.0.0.1:8173").rstrip("/")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# ── helpers ──────────────────────────────────────────────────────────────


def normalize_terminal_output(text: str) -> list[str]:
    """Normalize terminal output for comparison.

    Strip CR, trim trailing whitespace per line, remove trailing blank lines.
    """
    lines = ANSI_ESCAPE_RE.sub("", text).replace("\r", "").split("\n")
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


def local_requests_session() -> requests.Session:
    """Create a session for loopback requests that ignores proxy env vars."""
    session = requests.Session()
    session.trust_env = False
    return session


def backend_health_ok(session: requests.Session) -> bool:
    try:
        resp = session.get(f"{BACKEND_URL}/health", timeout=1)
    except requests.RequestException:
        return False
    return resp.status_code == 200


def read_tail(path: Path, max_chars: int = 4000) -> str:
    try:
        return path.read_text(errors="ignore")[-max_chars:].strip()
    except OSError:
        return ""


def backend_bind() -> tuple[str, int]:
    """Return host/port for the real test backend server."""
    parsed = urlparse(BACKEND_URL)
    return parsed.hostname or "127.0.0.1", parsed.port or 8173


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def backend_server() -> Generator[None, None, None]:
    """Start the real FastAPI backend on port 8173 in a background process."""
    import subprocess
    import time

    session = local_requests_session()
    if backend_health_ok(session):
        yield
        return

    backend_dir = Path(__file__).parent.parent
    venv_python = backend_dir / ".venv" / "bin" / "python"

    # Fallback: use sys.executable if venv not found
    if not venv_python.exists():
        import sys

        venv_python = Path(sys.executable)

    log_path = backend_dir / ".pytest-backend.log"
    log_file = log_path.open("wb")
    backend_host, backend_port = backend_bind()

    proc = subprocess.Popen(
        [
            str(venv_python),
            "-m",
            "uvicorn",
            "claude_hub.main:app",
            "--host",
            backend_host,
            "--port",
            str(backend_port),
            "--log-level",
            "error",
        ],
        cwd=str(backend_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )

    # Wait for server to be ready
    for _ in range(50):
        if backend_health_ok(session):
            break
        time.sleep(0.2)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        finally:
            log_file.close()
        log_tail = read_tail(log_path)
        raise RuntimeError(
            f"Backend server failed to start on {BACKEND_URL}."
            f"{chr(10) + log_tail if log_tail else ''}"
        )

    try:
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()


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
    session = local_requests_session()

    # Create tab via API
    resp = session.post(
        f"{BACKEND_URL}/api/tabs",
        json={"name": "test-replay", "agent_type": "cursor"},
    )
    assert resp.status_code == 201, f"Failed to create tab: {resp.text}"
    tab = resp.json()

    yield tab

    # Cleanup: delete tab (kills tmux session too)
    try:
        session.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
    except requests.RequestException:
        pass
