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


def _e2e_timeout_scale() -> float:
    """Multiplier applied to Playwright wait deadlines in the replay E2E tests.

    The browser-driven terminal tests assert that history renders within fixed
    deadlines (mostly 12s). Those budgets are comfortable on a developer laptop
    but too tight on shared CI runners, where CPU contention slows xterm.js
    rendering and tmux/ttyd startup enough to blow past them deterministically.
    Set ``CLAUDE_HUB_E2E_TIMEOUT_SCALE`` (e.g. 3) in CI to widen every deadline
    without changing local behaviour (default 1.0). Values below 1.0 are
    clamped to 1.0 so the env var can only ever relax, never tighten, deadlines.
    """
    raw = os.environ.get("CLAUDE_HUB_E2E_TIMEOUT_SCALE", "1")
    try:
        scale = float(raw)
    except ValueError:
        return 1.0
    return scale if scale > 1.0 else 1.0


E2E_TIMEOUT_SCALE = _e2e_timeout_scale()


def scale_timeout(timeout: float) -> float:
    """Scale a raw timeout/poll budget by ``E2E_TIMEOUT_SCALE``."""
    return timeout * E2E_TIMEOUT_SCALE


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


@pytest.fixture(scope="session", autouse=True)
def _scale_playwright_timeouts() -> Generator[None, None, None]:
    """Widen Playwright wait deadlines by ``E2E_TIMEOUT_SCALE`` on slow runners.

    When ``CLAUDE_HUB_E2E_TIMEOUT_SCALE`` is left at its default (1.0) this is a
    no-op, so local runs behave exactly as before. In CI we set it to a larger
    value to multiply every ``page.wait_for_*`` deadline at once instead of
    editing ~40 individual ``timeout=`` literals. Only true deadlines
    (``wait_for_function`` / ``wait_for_selector``) are scaled; deliberate fixed
    pauses (``wait_for_timeout`` observation windows) are left untouched.
    Patching the Page class (not an instance) means it never forces a browser
    launch for the pure backend functional tests — the wrappers only run when an
    E2E test actually calls these methods.
    """
    if E2E_TIMEOUT_SCALE == 1.0:
        yield
        return

    from playwright.sync_api import Page as _Page

    originals = {name: getattr(_Page, name) for name in ("wait_for_function", "wait_for_selector")}

    def make_kwarg_wrapper(orig):  # type: ignore[no-untyped-def]
        def wrapper(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("timeout") is not None:
                kwargs["timeout"] = kwargs["timeout"] * E2E_TIMEOUT_SCALE
            return orig(self, *args, **kwargs)

        return wrapper

    _Page.wait_for_function = make_kwarg_wrapper(originals["wait_for_function"])  # type: ignore[method-assign]
    _Page.wait_for_selector = make_kwarg_wrapper(originals["wait_for_selector"])  # type: ignore[method-assign]

    try:
        yield
    finally:
        for name, orig in originals.items():
            setattr(_Page, name, orig)


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
        json={"name": "test-replay", "agent_type": "terminal"},
    )
    assert resp.status_code == 201, f"Failed to create tab: {resp.text}"
    tab = resp.json()

    yield tab

    # Cleanup: delete tab (kills tmux session too)
    try:
        session.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
    except requests.RequestException:
        pass
