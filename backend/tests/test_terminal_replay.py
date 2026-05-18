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
from typing import Any

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
    result: list[str] = page.evaluate("""
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
    return result


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


def wait_for_xterm_buffer_lines(page: Page, min_lines: int, timeout: float = 12.0) -> None:
    """Wait until xterm exposes enough buffer lines for tmux history."""
    page.wait_for_function(
        """(minLines) => {
            const term = window.term;
            if (!term) return false;
            const buf = term.buffer && term.buffer.active;
            return !!buf && buf.length >= minLines;
        }""",
        arg=min_lines,
        timeout=int(timeout * 1000),
    )


def normalize_xterm_lines(lines: list[str]) -> list[str]:
    """Normalize xterm buffer lines: rstrip, remove trailing blanks."""
    result = [l.rstrip() for l in lines if l is not None]
    while result and result[-1] == "":
        result.pop()
    return result


def load_terminal_page(page: Page, tab_id: str, min_buffer_lines: int | None = None) -> None:
    """Navigate to the terminal proxy page and wait for full rendering."""
    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab_id}/")
    page.wait_for_selector(".xterm", timeout=15000)
    wait_for_replay_done(page)
    wait_for_visible_screen(page)
    if min_buffer_lines is not None:
        wait_for_xterm_buffer_lines(page, min_buffer_lines)


def wait_for_cursor_matches_tmux(page: Page, tab_id: str) -> None:
    """Wait until xterm's cursor position matches tmux's pane cursor."""
    page.wait_for_function(
        """async (tabId) => {
            const term = window.term;
            if (!term) return false;

            const response = await fetch(`/api/terminal/history/${tabId}?lines=100000`);
            if (!response.ok) return false;
            const payload = await response.json();
            if (!Number.isInteger(payload.cursor_x) || !Number.isInteger(payload.cursor_y)) {
                return false;
            }

            const buffer = term.buffer.active;
            window.__claudeHubLastCursorMatch = {
                xtermX: buffer.cursorX,
                xtermY: buffer.cursorY,
                tmuxX: payload.cursor_x,
                tmuxY: payload.cursor_y,
            };
            return buffer.cursorX === payload.cursor_x && buffer.cursorY === payload.cursor_y;
        }""",
        arg=tab_id,
        timeout=10000,
    )


def read_scroll_alignment(page: Page) -> dict[str, Any] | None:
    """Read viewport scroll alignment state from xterm."""
    result: Any = page.evaluate("""() => {
            const term = window.term;
            const viewportEl = document.querySelector('.xterm-viewport');
            if (!term || !viewportEl) return null;
            const vpObj = term._core && term._core.viewport;
            const rowHeight = (vpObj && vpObj._currentRowHeight) || 15;
            const buffer = term.buffer.active;
            return {
                scrollTop: viewportEl.scrollTop,
                viewportY: buffer.viewportY,
                baseY: buffer.baseY,
                isUserScrolling: term._core._bufferService.isUserScrolling,
                rowHeight,
                scrollHeight: viewportEl.scrollHeight,
                clientHeight: viewportEl.clientHeight,
                bottomGap: viewportEl.scrollHeight - viewportEl.clientHeight - viewportEl.scrollTop,
                delta: Math.abs(viewportEl.scrollTop - buffer.viewportY * rowHeight),
            };
        }""")
    return result if isinstance(result, dict) else None


def scroll_terminal_to_top(page: Page) -> None:
    """Move the xterm viewport away from the latest output."""
    page.evaluate("""() => {
            const term = window.term;
            if (!term) return;
            if (typeof term.scrollToTop === 'function') {
                term.scrollToTop();
            } else if (typeof term.scrollToLine === 'function') {
                term.scrollToLine(0);
            }
        }""")
    page.wait_for_function(
        """() => {
            const term = window.term;
            if (!term) return false;
            const buffer = term.buffer.active;
            return buffer.viewportY < buffer.baseY;
        }""",
        timeout=5000,
    )


def read_xterm_text(page: Page) -> str:
    """Read the full xterm buffer as newline-delimited text."""
    lines: list[str] = page.evaluate("""() => {
            const buffer = window.term.buffer.active;
            const lines = [];
            for (let i = 0; i < buffer.length; i++) {
                const line = buffer.getLine(i);
                if (line) lines.push(line.translateToString(true));
            }
            return lines;
        }""")
    return "\n".join(lines)


def live_line_numbers(text: str) -> set[int]:
    """Extract LIVE_XXXX markers from terminal text."""
    return {int(match.group(1)) for match in re.finditer(r"LIVE_(\d{4})", text)}


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
    wait_for_xterm_buffer_lines(page, len(tmux_lines))

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
    wait_for_xterm_buffer_lines(page, len(tmux_lines))

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
    wait_for_xterm_buffer_lines(page, len(tmux_lines))

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


def test_initial_prompt_cursor_matches_tmux_position(terminal_tab: dict, page: Page) -> None:
    """Initial prompt replay restores the cursor to tmux's pane position."""
    tab = terminal_tab

    load_terminal_page(page, tab["id"])
    wait_for_cursor_matches_tmux(page, tab["id"])


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


def test_touch_scroll_alignment_during_live_output(terminal_tab: dict, page: Page) -> None:
    """Touch scrolling stays aligned while new terminal output is arriving.

    Regression guard for the mobile native-scroll hook: it must not suppress
    xterm's viewport refresh while live output advances the buffer, otherwise
    scrollTop and viewportY diverge and the rendered history appears mixed with
    new output until touchend.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)

    send_keys_sync(
        session_name,
        "for i in $(seq 0 99); do echo LIVE_$(printf '%04d' $i); sleep 0.03; done",
        "Enter",
    )
    time.sleep(0.2)

    box = page.locator(".xterm-viewport").bounding_box()
    assert box is not None, "xterm viewport not found"
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] * 0.65
    cdp = page.context.new_cdp_session(page)

    samples: list[dict[str, Any]] = []
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchStart", "touchPoints": [{"x": cx, "y": cy, "id": 1}]},
    )
    for i in range(1, 12):
        cdp.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchMove",
                "touchPoints": [{"x": cx, "y": cy + 24 * i, "id": 1}],
            },
        )
        time.sleep(0.06)
        state = read_scroll_alignment(page)
        if state:
            samples.append(state)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})

    assert samples, "no scroll alignment samples captured"
    max_delta = max(sample["delta"] for sample in samples)
    max_row_height = max(sample["rowHeight"] for sample in samples)

    assert max_delta <= max_row_height * 2, (
        f"xterm viewport lost scroll alignment during live output; "
        f"max delta={max_delta}, row height={max_row_height}, samples={samples}"
    )


def test_desktop_wheel_enters_user_scroll_during_live_output(
    terminal_tab: dict, page: Page
) -> None:
    """Desktop wheel-up should stop following live output immediately.

    Without the capture-phase user-scroll marker, fast output can keep the
    viewport pinned to the bottom for the first wheel events, mixing newly
    appended output into the history the user is trying to inspect.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=500)
    load_terminal_page(page, tab["id"], min_buffer_lines=500)

    send_keys_sync(
        session_name,
        (
            "for i in $(seq 0 300); do "
            "printf '\\rSTATUS_%04d' $i; "
            "echo LIVE_$(printf '%04d' $i); "
            "sleep 0.005; "
            "done"
        ),
        "Enter",
    )
    before = read_scroll_alignment(page)
    assert before is not None
    page.wait_for_function(
        f"() => window.term.buffer.active.baseY > {before['baseY']}", timeout=10000
    )

    box = page.locator(".xterm-viewport").bounding_box()
    assert box is not None, "xterm viewport not found"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -120)
    time.sleep(0.02)

    after = read_scroll_alignment(page)
    assert after is not None
    assert after["viewportY"] < after["baseY"], (
        f"wheel-up did not leave the live-output bottom after first event: "
        f"before={before}, after={after}"
    )


def test_live_output_keeps_viewport_pinned_to_latest(terminal_tab: dict, page: Page) -> None:
    """Live output should keep following the bottom when the user has not scrolled away.

    Regression guard for the terminal appearing to freeze on an intermediate
    section while xterm keeps adding rows below the visible viewport.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"
    line_count = 700

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)

    initial = read_scroll_alignment(page)
    assert initial is not None
    assert initial["viewportY"] == initial["baseY"], f"terminal did not start at bottom: {initial}"

    send_keys_sync(
        session_name,
        (
            f"for i in $(seq 0 {line_count - 1}); do "
            "echo FOLLOW_$(printf '%04d' $i); "
            "sleep 0.003; "
            "done"
        ),
        "Enter",
    )

    samples: list[dict[str, Any]] = []
    deadline = time.time() + 8
    while time.time() < deadline:
        state = read_scroll_alignment(page)
        if state and state["baseY"] > initial["baseY"]:
            samples.append(state)
            if state["baseY"] >= initial["baseY"] + 80:
                break
        time.sleep(0.05)
    else:
        pytest.fail("live output did not advance the xterm buffer")

    drifted = [
        sample
        for sample in samples
        if sample["viewportY"] != sample["baseY"] or sample["bottomGap"] > sample["rowHeight"] * 2
    ]
    assert not drifted, f"viewport drifted away from latest live output: {drifted[:5]}"

    for _ in range(120):
        if f"FOLLOW_{line_count - 1:04d}" in capture_pane_sync(session_name):
            break
        time.sleep(0.1)
    else:
        pytest.fail("live output did not finish in tmux")

    page.wait_for_function(
        f"""() => {{
            const buffer = window.term.buffer.active;
            const rows = window.term.rows || 24;
            const visible = [];
            for (let i = buffer.viewportY; i < buffer.viewportY + rows; i++) {{
                const line = buffer.getLine(i);
                if (line) visible.push(line.translateToString(true));
            }}
            return visible.join('\\n').includes('FOLLOW_{line_count - 1:04d}');
        }}""",
        timeout=10000,
    )

    final = read_scroll_alignment(page)
    assert final is not None
    assert final["viewportY"] == final["baseY"], f"terminal ended away from latest output: {final}"
    assert (
        final["bottomGap"] <= final["rowHeight"] * 2
    ), f"terminal DOM viewport ended away from bottom: {final}"


def test_manual_history_refresh_message_scrolls_to_latest(terminal_tab: dict, page: Page) -> None:
    """Manual history refresh replays tmux history and returns to the latest output."""
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)
    scroll_terminal_to_top(page)

    page.evaluate("""() => {
            window.__claudeHubRefreshEvents = [];
            window.addEventListener('message', function(event) {
                if (event.data && event.data.type === 'terminal-history-refresh-done') {
                    window.__claudeHubRefreshEvents.push(event.data);
                }
            });
            window.postMessage({
                type: 'terminal-history-refresh',
                reason: 'test-manual',
                scrollToBottom: true
            }, '*');
        }""")

    page.wait_for_function(
        """() => Array.isArray(window.__claudeHubRefreshEvents) &&
            window.__claudeHubRefreshEvents.some(function(event) {
                return event.reason === 'test-manual' && event.ok === true;
            })""",
        timeout=10000,
    )
    wait_for_xterm_buffer_lines(page, 260)

    alignment = read_scroll_alignment(page)
    assert alignment is not None
    assert (
        alignment["viewportY"] == alignment["baseY"]
    ), f"manual history refresh did not return to latest output: {alignment}"


def test_terminal_activate_message_scrolls_to_latest(terminal_tab: dict, page: Page) -> None:
    """Mobile tab activation can force a cached terminal back to the latest output."""
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)
    scroll_terminal_to_top(page)

    page.evaluate(
        """(tabId) => {
            window.postMessage({
                type: 'terminal-activate',
                tabId,
                refreshHistory: false,
                scrollToBottom: true
            }, '*');
        }""",
        arg=tab["id"],
    )

    page.wait_for_function(
        """() => {
            const term = window.term;
            if (!term) return false;
            const buffer = term.buffer.active;
            return buffer.viewportY === buffer.baseY;
        }""",
        timeout=5000,
    )


def test_wrapped_live_output_resyncs_complete_history(terminal_tab: dict, page: Page) -> None:
    """Fast wrapped output is reconciled from tmux history after it goes idle.

    tmux can optimize the terminal-client update stream for very long wrapped
    lines, so xterm may only receive the final screen worth of live output.
    The idle history resync should restore the complete LIVE_XXXX sequence.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"
    line_count = 120

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)

    send_keys_sync(
        session_name,
        (
            f"for i in $(seq 0 {line_count - 1}); do "
            "echo LIVE_$(printf '%04d' $i)_$(printf 'x%.0s' $(seq 1 220)); "
            "sleep 0.01; "
            "done"
        ),
        "Enter",
    )

    for _ in range(120):
        if f"LIVE_{line_count - 1:04d}" in capture_pane_sync(session_name):
            break
        time.sleep(0.2)
    else:
        pytest.fail("wrapped live output did not finish in tmux")

    page.wait_for_function(
        f"""() => {{
            const buffer = window.term.buffer.active;
            const text = [];
            for (let i = 0; i < buffer.length; i++) {{
                const line = buffer.getLine(i);
                if (line) text.push(line.translateToString(true));
            }}
            const joined = text.join('\\n');
            return joined.includes('LIVE_0000') && joined.includes('LIVE_{line_count - 1:04d}');
        }}""",
        timeout=20000,
    )

    xterm_numbers = live_line_numbers(read_xterm_text(page))
    expected = set(range(line_count))

    assert xterm_numbers == expected, (
        f"xterm live output markers are discontinuous after idle resync; "
        f"missing={sorted(expected - xterm_numbers)[:30]}, "
        f"extra={sorted(xterm_numbers - expected)[:30]}"
    )


def test_history_resync_does_not_replace_near_bottom_view(terminal_tab: dict, page: Page) -> None:
    """Idle history resync must not rewrite while the user is near bottom.

    A near-bottom view can show both older history and the newest output. It is
    still a user-selected historical viewport, so resync should wait until the
    user actually reaches the bottom instead of clearing and replaying over it.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"
    line_count = 80

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)

    send_keys_sync(
        session_name,
        (
            f"for i in $(seq 0 {line_count - 1}); do "
            "echo LIVE_$(printf '%04d' $i)_$(printf 'x%.0s' $(seq 1 220)); "
            "sleep 0.01; "
            "done"
        ),
        "Enter",
    )

    for _ in range(120):
        if f"LIVE_{line_count - 1:04d}" in capture_pane_sync(session_name):
            break
        time.sleep(0.2)
    else:
        pytest.fail("wrapped live output did not finish in tmux")

    page.evaluate("""() => {
            const vp = document.querySelector('.xterm-viewport');
            vp.scrollTop = Math.max(0, vp.scrollHeight - vp.clientHeight - 240);
            vp.dispatchEvent(new Event('scroll'));
        }""")
    time.sleep(0.2)
    before = read_scroll_alignment(page)
    assert before is not None
    assert before["viewportY"] < before["baseY"]

    time.sleep(1.5)
    after = read_scroll_alignment(page)
    assert after is not None

    assert after["viewportY"] < after["baseY"], (
        f"near-bottom historical view was replaced by idle resync: "
        f"before={before}, after={after}"
    )
