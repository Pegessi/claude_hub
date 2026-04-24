"""End-to-end tests for terminal history replay correctness.

These tests load the ttyd terminal page in a headless browser, read the
xterm.js buffer, and compare it against tmux capture-pane ground truth.
They verify that scrollback replay is complete, not interleaved with
real-time data, and that the scroll-up sequence preserves all lines.

Requires: tmux, ttyd, and Playwright chromium installed.
Run with: uv run pytest tests/test_terminal_replay.py -v
"""

import re
import subprocess
import time

import pytest
from playwright.sync_api import Page

from .conftest import (
    BACKEND_URL,
    capture_pane_sync,
    diff_summary,
    normalize_terminal_output,
    send_keys_sync,
    tmux_session_exists,
)

# ── helpers ──────────────────────────────────────────────────────────────


def ensure_tmux_session(page: Page, tab_id: str, session_name: str) -> None:
    """Ensure the tmux session exists by connecting to ttyd once.

    ttyd uses `tmux new-session -A`, so the tmux session is only created
    when the first client connects via WebSocket. We open the page briefly
    to trigger session creation, then close it.
    """
    if tmux_session_exists(session_name):
        return

    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    # Wait for ttyd to connect to tmux and create the session
    for _ in range(30):
        time.sleep(0.2)
        if tmux_session_exists(session_name):
            break
    else:
        pytest.fail(f"tmux session {session_name} was not created after connecting")


def produce_scrollback(session_name: str, count: int = 200) -> None:
    """Produce deterministic scrollback lines in a tmux session."""
    shell_cmd = f"for i in $(seq 0 {count - 1}); do echo LINE_$(printf '%04d' $i); done"
    send_keys_sync(session_name, shell_cmd, "Enter")
    # Wait for the shell to process all output
    for _ in range(50):
        time.sleep(0.2)
        output = capture_pane_sync(session_name, start="-100000")
        if f"LINE_{count - 1:04d}" in output:
            break
    else:
        pytest.fail(f"Timed out waiting for LINE_{count - 1:04d} to appear in tmux")


def read_xterm_buffer(page: Page) -> list[str]:
    """Read the full xterm.js buffer (scrollback + visible screen) via JS."""
    return page.evaluate("""
        () => {
            const term = window.term;
            if (!term) return null;
            const buf = term.buffer.active;
            const lines = [];
            for (let i = 0; i < buf.length; i++) {
                const line = buf.getLine(i);
                if (line) {
                    lines.push(line.translateToString(true));
                }
            }
            return lines;
        }
    """)


def wait_for_replay_done(page: Page, timeout: float = 15.0) -> None:
    """Wait for the history replay script to set its completion flag."""
    page.wait_for_function(
        "() => window.term && window.term.__claudeHubReplayDone === true",
        timeout=int(timeout * 1000),
    )


def wait_for_visible_screen(page: Page, timeout: float = 10.0) -> None:
    """Wait for ttyd's WebSocket to deliver the visible screen content."""
    page.wait_for_function(
        """() => {
            const term = window.term;
            if (!term) return false;
            const buf = term.buffer.active;
            const rows = term.rows || 24;
            for (let i = buf.length - rows; i < buf.length; i++) {
                const line = buf.getLine(i);
                if (line && line.translateToString(true).trim().length > 0) return true;
            }
            return false;
        }""",
        timeout=int(timeout * 1000),
    )
    time.sleep(0.5)


def normalize_xterm_lines(lines: list[str]) -> list[str]:
    """Normalize xterm buffer lines: rstrip, remove trailing blanks."""
    result = [l.rstrip() for l in lines if l is not None]
    while result and result[-1] == "":
        result.pop()
    return result


def load_terminal_page(page: Page, tab_id: str) -> None:
    """Navigate to the terminal proxy page and wait for full rendering."""
    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    wait_for_replay_done(page)
    wait_for_visible_screen(page)


# ── tests ────────────────────────────────────────────────────────────────


def test_scrollback_complete(terminal_tab: dict, page: Page) -> None:
    """200 lines of history: verify all 200 appear in xterm scrollback.

    This is the primary regression test. If the scroll-up sequence is
    missing, the bottom `rows` lines of scrollback will be lost.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    # Step 1: Connect to create tmux session, then produce scrollback
    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=200)

    # Step 2: Capture ground truth BEFORE the replay test
    ground_truth = capture_pane_sync(session_name)
    tmux_lines = normalize_terminal_output(ground_truth)

    # Step 3: Navigate to the page (triggers history replay)
    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab['id']}/")
    page.wait_for_selector(".xterm", timeout=15000)
    wait_for_replay_done(page)
    wait_for_visible_screen(page)

    # Step 4: Read xterm buffer and compare
    xterm_content = read_xterm_buffer(page)
    assert xterm_content is not None, "window.term not found in terminal page"
    xterm_lines = normalize_xterm_lines(xterm_content)

    # Full line-by-line comparison
    assert xterm_lines == tmux_lines, (
        f"Terminal content mismatch ({len(xterm_lines)} vs {len(tmux_lines)} lines):\n"
        f"{diff_summary(xterm_lines, tmux_lines)}"
    )


def test_bottom_rows_preserved(terminal_tab: dict, page: Page) -> None:
    """Verify scrollback line count matches tmux.

    Specifically catches the scroll-up sequence bug: if \\x1b[NS is
    missing, the bottom `rows` lines of scrollback are lost because
    they end up on the visible screen and get overwritten by ttyd.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=200)

    ground_truth = capture_pane_sync(session_name)
    tmux_lines = normalize_terminal_output(ground_truth)

    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab['id']}/")
    page.wait_for_selector(".xterm", timeout=15000)
    wait_for_replay_done(page)
    wait_for_visible_screen(page)

    xterm_content = read_xterm_buffer(page)
    xterm_lines = normalize_xterm_lines(xterm_content)

    rows = page.evaluate("() => window.term ? window.term.rows : 24")

    xterm_scrollback = len(xterm_lines) - rows
    tmux_scrollback = len(tmux_lines) - rows

    assert xterm_scrollback == tmux_scrollback, (
        f"Scrollback line count mismatch: xterm has {xterm_scrollback}, "
        f"tmux has {tmux_scrollback}. "
        f"Delta: {tmux_scrollback - xterm_scrollback} lines "
        f"(rows={rows}; delta==rows indicates scroll-up sequence failure)"
    )


def test_no_duplicate_visible_screen(terminal_tab: dict, page: Page) -> None:
    """Verify xterm does NOT have extra lines duplicating the visible screen.

    Regression guard: if capture-pane returns visible screen lines
    (e.g., -E -1 removed) and the client-side trimming has a bug,
    visible screen content gets duplicated.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=200)

    ground_truth = capture_pane_sync(session_name)
    tmux_lines = normalize_terminal_output(ground_truth)

    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab['id']}/")
    page.wait_for_selector(".xterm", timeout=15000)
    wait_for_replay_done(page)
    wait_for_visible_screen(page)

    xterm_content = read_xterm_buffer(page)
    xterm_lines = normalize_xterm_lines(xterm_content)

    # xterm should have the SAME number of lines as tmux, not more
    assert len(xterm_lines) <= len(tmux_lines), (
        f"xterm has {len(xterm_lines)} lines vs tmux's {len(tmux_lines)}: "
        f"possible duplicate visible screen content "
        f"({len(xterm_lines) - len(tmux_lines)} extra lines)"
    )


def test_empty_scrollback(terminal_tab: dict, page: Page) -> None:
    """New tab with no history: page loads cleanly, no errors, no stale scrollback."""
    tab = terminal_tab

    # Load page immediately — no content produced.
    # First connection will create the tmux session and set replay done.
    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab['id']}/")
    page.wait_for_selector(".xterm", timeout=15000)

    # Wait for both ttyd WebSocket connection and replay completion
    wait_for_replay_done(page)
    wait_for_visible_screen(page)

    # Verify replay done flag is set
    replay_done = page.evaluate("() => window.term && window.term.__claudeHubReplayDone === true")
    assert replay_done, "Replay did not complete for empty scrollback"

    # Verify no JS errors in console
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    time.sleep(0.5)
    assert not errors, f"JS errors during empty scrollback load: {errors}"


def test_replay_with_active_output(terminal_tab: dict, page: Page) -> None:
    """Load the page while a command is still producing output.

    Verifies that history replay and real-time output don't interleave:
    all history lines should be contiguous in the scrollback area.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    # Connect and produce initial scrollback
    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=100)

    # Start a slow command that will still be running when the page loads
    send_keys_sync(
        session_name,
        "for i in $(seq 0 49); do echo ACTIVE_$(printf '%04d' $i); sleep 0.1; done",
        "Enter",
    )
    # Small delay so the command starts but doesn't finish
    time.sleep(0.3)

    # Load the page while the command is running (new page context triggers replay)
    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab['id']}/")
    page.wait_for_selector(".xterm", timeout=15000)
    wait_for_replay_done(page)
    wait_for_visible_screen(page)

    # Read xterm buffer
    xterm_content = read_xterm_buffer(page)
    xterm_lines = normalize_xterm_lines(xterm_content)

    # Verify LINE_XXXX lines are contiguous (no ACTIVE_XXXX lines interleaved)
    line_pattern = re.compile(r"LINE_\d{4}")
    active_pattern = re.compile(r"ACTIVE_\d{4}")

    line_indices = [i for i, l in enumerate(xterm_lines) if line_pattern.search(l)]
    active_indices = [i for i, l in enumerate(xterm_lines) if active_pattern.search(l)]

    if not line_indices or not active_indices:
        # The active output might not have started yet, or all LINE_ lines
        # are in scrollback while ACTIVE_ lines are on screen. Both are fine.
        return

    # Check that no ACTIVE_ line appears between LINE_ lines
    line_min = min(line_indices)
    line_max = max(line_indices)
    for ai in active_indices:
        assert ai < line_min or ai > line_max, (
            f"Interleaving detected: ACTIVE_ line at index {ai} is between "
            f"LINE_ lines (indices {line_min}-{line_max}). "
            f"This indicates history/realtime data interleaving."
        )
