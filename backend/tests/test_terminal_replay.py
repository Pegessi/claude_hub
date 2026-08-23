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

from claude_hub.api.terminal import (
    FULL_HISTORY_LINES,
    INITIAL_AGENT_HISTORY_LINES,
    INITIAL_AGENT_REPLAY_MIN_LINES,
)

from .conftest import (
    BACKEND_URL,
    capture_pane_sync,
    diff_summary,
    local_requests_session,
    normalize_terminal_output,
    scale_timeout,
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
    for _ in range(int(scale_timeout(30))):
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
    for _ in range(int(scale_timeout(50))):
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


def read_visible_xterm_text(page: Page) -> str:
    """Read only the currently visible xterm viewport."""
    lines: list[str] = page.evaluate("""() => {
            const term = window.term;
            if (!term) return [];
            const buffer = term.buffer.active;
            const rows = term.rows || 24;
            const lines = [];
            for (let i = buffer.viewportY; i < Math.min(buffer.length, buffer.viewportY + rows); i++) {
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

    for _ in range(int(scale_timeout(120))):
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


def test_internal_scroll_event_does_not_cancel_live_bottom_follow(
    terminal_tab: dict, page: Page
) -> None:
    """xterm's own scroll events should not look like user history scrolling.

    Dynamic terminal UIs can update the viewport while live data is still
    rendering. A plain scroll event from that path must not cancel the
    bottom-follow scheduled for an already-bottom viewport.
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"
    line_count = 220

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)

    page.evaluate("""() => {
            const term = window.term;
            const originalWrite = term.write.bind(term);
            let injected = false;
            term.write = function(data, cb) {
                return originalWrite(data, function() {
                    if (!injected && String(data).includes('DYNAMIC_')) {
                        injected = true;
                        const viewportEl = document.querySelector('.xterm-viewport');
                        const vpObj = term._core && term._core.viewport;
                        const rowHeight = (vpObj && vpObj._currentRowHeight) || 15;
                        if (viewportEl) {
                            viewportEl.scrollTop = Math.max(0, viewportEl.scrollTop - rowHeight * 8);
                            viewportEl.dispatchEvent(new Event('scroll'));
                        }
                    }
                    if (cb) cb();
                });
            };
        }""")

    send_keys_sync(
        session_name,
        (
            f"for i in $(seq 0 {line_count - 1}); do "
            "printf '\\rDYNAMIC_%04d' $i; "
            "echo DYNAMIC_$(printf '%04d' $i); "
            "sleep 0.004; "
            "done"
        ),
        "Enter",
    )

    for _ in range(int(scale_timeout(120))):
        if f"DYNAMIC_{line_count - 1:04d}" in capture_pane_sync(session_name):
            break
        time.sleep(0.1)
    else:
        pytest.fail("dynamic live output did not finish in tmux")

    page.wait_for_function(
        f"""() => {{
            const buffer = window.term.buffer.active;
            const rows = window.term.rows || 24;
            if (buffer.viewportY !== buffer.baseY) return false;
            const visible = [];
            for (let i = buffer.viewportY; i < buffer.viewportY + rows; i++) {{
                const line = buffer.getLine(i);
                if (line) visible.push(line.translateToString(true));
            }}
            return visible.join('\\n').includes('DYNAMIC_{line_count - 1:04d}');
        }}""",
        timeout=10000,
    )

    final = read_scroll_alignment(page)
    assert final is not None
    assert final["viewportY"] == final["baseY"], f"internal scroll event cancelled follow: {final}"
    assert (
        final["bottomGap"] <= final["rowHeight"] * 2
    ), f"internal scroll event left DOM viewport away from bottom: {final}"


def test_agent_tui_tab_does_not_auto_resync_after_live_writes_or_activation(
    backend_server: None, page: Page
) -> None:
    """Claude/Codex-style TUI tabs must not auto-replay tmux implicitly.

    Agent TUIs use relative cursor operations to update status blocks. Replaying
    a plain tmux snapshot while those updates are still active corrupts xterm's
    screen state, so automatic idle history resync and scroll-only activation
    paths must avoid fetching history for agent tabs. The tab runs zsh for
    determinism but is tagged as a Codex tab, which exercises the injected
    agent-type gate without depending on a real agent login in CI.
    """
    session = local_requests_session()
    resp = session.post(
        f"{BACKEND_URL}/api/tabs",
        json={
            "name": "test-agent-tui-no-auto-resync",
            "agent_type": "codex",
            "shell": "/bin/zsh",
        },
    )
    assert resp.status_code == 201, f"Failed to create tab: {resp.text}"
    tab = resp.json()
    session_name = f"claude-hub-{tab['id'][:8]}"
    history_count = INITIAL_AGENT_REPLAY_MIN_LINES + 80

    try:
        ensure_tmux_session(page, tab["id"], session_name)
        produce_scrollback(session_name, count=history_count)
        initial_history_fetches: list[str] = []
        page.on(
            "request",
            lambda request: (
                initial_history_fetches.append(request.url)
                if f"/api/terminal/history/{tab['id']}" in request.url
                else None
            ),
        )
        load_terminal_page(page, tab["id"], min_buffer_lines=history_count)
        page.wait_for_timeout(1200)
        assert len(initial_history_fetches) == 1, (
            "agent-tagged terminal performed implicit post-replay history refreshes: "
            f"{initial_history_fetches}"
        )
        assert f"lines={INITIAL_AGENT_HISTORY_LINES}" in initial_history_fetches[0]
        agent_text = read_xterm_text(page)
        agent_history_numbers = {
            int(match.group(1)) for match in re.finditer(r"LINE_(\d{4})", agent_text)
        }
        assert agent_history_numbers == set(range(history_count)), (
            "agent-tagged terminal lost or duplicated replayed scrollback "
            f"without a corrective history refresh: {sorted(agent_history_numbers)}"
        )

        page.evaluate("""() => {
                window.__claudeHubHistoryFetches = [];
                const originalFetch = window.fetch.bind(window);
                window.fetch = function(input, init) {
                    const url = typeof input === 'string' ? input : (input && input.url) || '';
                    if (url.indexOf('/api/terminal/history/') >= 0) {
                        window.__claudeHubHistoryFetches.push(url);
                    }
                    return originalFetch(input, init);
                };
            }""")

        send_keys_sync(
            session_name,
            (
                "for i in $(seq 0 5); do "
                "echo AGENT_TUI_NO_RESYNC_$(printf '%04d' $i); "
                "sleep 1; "
                "done"
            ),
            "Enter",
        )

        for _ in range(int(scale_timeout(40))):
            if "AGENT_TUI_NO_RESYNC_0005" in capture_pane_sync(session_name):
                break
            time.sleep(0.2)
        else:
            pytest.fail("agent-tagged shell output did not finish in tmux")

        page.wait_for_timeout(1500)
        history_fetches = page.evaluate("() => window.__claudeHubHistoryFetches || []")
        assert history_fetches == [], (
            "agent-tagged terminal performed automatic history resync during live output: "
            f"{history_fetches}"
        )

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
        page.wait_for_timeout(600)
        history_fetches = page.evaluate("() => window.__claudeHubHistoryFetches || []")
        assert history_fetches == [], (
            "scroll-only activation fetched history for an agent-tagged terminal: "
            f"{history_fetches}"
        )
    finally:
        try:
            session.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
        except Exception:
            pass


def test_agent_tui_short_history_skips_initial_replay(backend_server: None, page: Page) -> None:
    """Short/new agent sessions should let ttyd render startup content live."""
    session = local_requests_session()
    resp = session.post(
        f"{BACKEND_URL}/api/tabs",
        json={
            "name": "test-agent-tui-short-history",
            "agent_type": "codex",
            "shell": "/bin/zsh",
        },
    )
    assert resp.status_code == 201, f"Failed to create tab: {resp.text}"
    tab = resp.json()
    session_name = f"claude-hub-{tab['id'][:8]}"

    try:
        ensure_tmux_session(page, tab["id"], session_name)
        produce_scrollback(session_name, count=20)
        history_fetches: list[str] = []
        page.on(
            "request",
            lambda request: (
                history_fetches.append(request.url)
                if f"/api/terminal/history/{tab['id']}" in request.url
                else None
            ),
        )
        load_terminal_page(page, tab["id"])

        assert len(history_fetches) == 1
        assert f"lines={INITIAL_AGENT_HISTORY_LINES}" in history_fetches[0]
        skipped = page.evaluate(
            "() => window.term && window.term.__claudeHubReplaySkippedForShortHistory === true"
        )
        assert skipped is True
        assert "LINE_0019" in read_xterm_text(page)
    finally:
        try:
            session.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
        except Exception:
            pass


def test_agent_tui_initial_replay_keeps_live_frames(backend_server: None, page: Page) -> None:
    """Agent replay filters duplicate initial frames without swallowing new output."""
    session = local_requests_session()
    resp = session.post(
        f"{BACKEND_URL}/api/tabs",
        json={
            "name": "test-agent-tui-live-frames",
            "agent_type": "codex",
            "shell": "/bin/zsh",
        },
    )
    assert resp.status_code == 201, f"Failed to create tab: {resp.text}"
    tab = resp.json()
    session_name = f"claude-hub-{tab['id'][:8]}"

    try:
        ensure_tmux_session(page, tab["id"], session_name)
        produce_scrollback(session_name, count=INITIAL_AGENT_REPLAY_MIN_LINES + 80)
        send_keys_sync(
            session_name,
            (
                "for i in $(seq 0 29); do "
                "echo AGENT_HELD_FRAME_$(printf '%04d' $i); "
                "sleep 0.08; "
                "done"
            ),
            "Enter",
        )

        history_fetches: list[str] = []
        page.on(
            "request",
            lambda request: (
                history_fetches.append(request.url)
                if f"/api/terminal/history/{tab['id']}" in request.url
                else None
            ),
        )
        page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab['id']}/")
        page.wait_for_selector(".xterm", timeout=15000)
        wait_for_replay_done(page, timeout=30)

        for _ in range(int(scale_timeout(50))):
            if "AGENT_HELD_FRAME_0029" in capture_pane_sync(session_name):
                break
            time.sleep(0.2)
        else:
            pytest.fail("agent held-frame output did not finish in tmux")

        page.wait_for_function(
            """() => {
                const term = window.term;
                if (!term) return false;
                const buffer = term.buffer.active;
                const lines = [];
                for (let i = 0; i < buffer.length; i++) {
                    const line = buffer.getLine(i);
                    if (line) lines.push(line.translateToString(true));
                }
                const text = lines.join('\\n');
                return text.includes('AGENT_HELD_FRAME_0000') &&
                    text.includes('AGENT_HELD_FRAME_0029');
            }""",
            timeout=10000,
        )
        assert len(history_fetches) == 1, (
            "agent initial replay needed implicit history refresh to recover live frames: "
            f"{history_fetches}"
        )
    finally:
        try:
            session.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
        except Exception:
            pass


def test_agent_tui_history_view_is_stable_during_live_redraws(
    backend_server: None, page: Page
) -> None:
    """Claude/Codex live redraws must not overwrite a user-selected history viewport."""
    session = local_requests_session()
    resp = session.post(
        f"{BACKEND_URL}/api/tabs",
        json={
            "name": "test-agent-tui-history-view-freeze",
            "agent_type": "codex",
            "shell": "/bin/zsh",
        },
    )
    assert resp.status_code == 201, f"Failed to create tab: {resp.text}"
    tab = resp.json()
    session_name = f"claude-hub-{tab['id'][:8]}"
    line_count = 40
    history_count = INITIAL_AGENT_REPLAY_MIN_LINES + 80

    try:
        ensure_tmux_session(page, tab["id"], session_name)
        produce_scrollback(session_name, count=history_count)
        load_terminal_page(page, tab["id"], min_buffer_lines=history_count)

        page.evaluate("""() => {
                const term = window.term;
                const buffer = term.buffer.active;
                const rows = term.rows || 24;
                const target = Math.max(0, buffer.baseY - Math.max(4, Math.floor(rows / 2)));
                term.scrollToLine(target);
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
        before = read_visible_xterm_text(page)
        assert "AGENT_HISTORY_VIEW_LIVE_" not in before

        send_keys_sync(
            session_name,
            (
                f"for i in $(seq 0 {line_count - 1}); do "
                "printf '\\rAGENT_HISTORY_VIEW_LIVE_%04d' $i; "
                "echo AGENT_HISTORY_VIEW_LIVE_$(printf '%04d' $i); "
                "sleep 0.03; "
                "done"
            ),
            "Enter",
        )

        for _ in range(int(scale_timeout(80))):
            if f"AGENT_HISTORY_VIEW_LIVE_{line_count - 1:04d}" in capture_pane_sync(session_name):
                break
            time.sleep(0.2)
        else:
            pytest.fail("agent live redraw output did not finish in tmux")

        page.wait_for_timeout(500)
        while_scrolled = read_visible_xterm_text(page)
        assert while_scrolled == before, (
            "agent live redraws changed the visible history viewport while the user "
            "was scrolled away from the bottom"
        )
        assert "AGENT_HISTORY_VIEW_LIVE_" not in while_scrolled

        page.evaluate("() => window.term.scrollToBottom()")
        page.wait_for_function(
            f"""() => {{
                const term = window.term;
                if (!term) return false;
                const buffer = term.buffer.active;
                const text = [];
                for (let i = 0; i < buffer.length; i++) {{
                    const line = buffer.getLine(i);
                    if (line) text.push(line.translateToString(true));
                }}
                return text.join('\\n').includes('AGENT_HISTORY_VIEW_LIVE_{line_count - 1:04d}') &&
                    buffer.viewportY === buffer.baseY;
            }}""",
            timeout=10000,
        )
    finally:
        try:
            session.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
        except Exception:
            pass


def test_manual_history_refresh_message_scrolls_to_latest(terminal_tab: dict, page: Page) -> None:
    """Manual history refresh replays tmux history and returns to the latest output."""
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)
    scroll_terminal_to_top(page)

    page.evaluate("""() => {
            window.__claudeHubHistoryFetches = [];
            const originalFetch = window.fetch.bind(window);
            window.fetch = function(input, init) {
                const url = typeof input === 'string' ? input : (input && input.url) || '';
                if (url.indexOf('/api/terminal/history/') >= 0) {
                    window.__claudeHubHistoryFetches.push(url);
                }
                return originalFetch(input, init);
            };
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
    history_fetches = page.evaluate("() => window.__claudeHubHistoryFetches || []")
    assert any(f"lines={FULL_HISTORY_LINES}" in url for url in history_fetches), (
        "manual history refresh did not request the full tmux scrollback: " f"{history_fetches}"
    )

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


def test_terminal_typing_does_not_auto_resync_history(terminal_tab: dict, page: Page) -> None:
    """Plain terminal input echo should not trigger full tmux history replay."""
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    ensure_tmux_session(page, tab["id"], session_name)
    load_terminal_page(page, tab["id"])
    page.wait_for_timeout(9000)
    page.evaluate("""() => {
        window.__claudeHubHistoryFetches = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/api/terminal/history/') >= 0) {
                window.__claudeHubHistoryFetches.push(url);
            }
            return originalFetch(input, init);
        };
    }""")

    box = page.locator(".xterm").bounding_box()
    assert box is not None, "xterm not found"
    page.mouse.click(box["x"] + 24, box["y"] + 24)
    for char in "abcd":
        page.keyboard.type(char)
        page.wait_for_timeout(850)
    page.wait_for_timeout(1400)

    assert "abcd" in capture_pane_sync(session_name)
    history_fetches = page.evaluate("() => window.__claudeHubHistoryFetches || []")
    assert history_fetches == [], (
        "plain terminal typing echo performed automatic history resync: " f"{history_fetches}"
    )


def test_typing_releases_initial_replay_buffer(terminal_tab: dict, page: Page) -> None:
    """Typing during tab open should not wait behind the replay hold window."""
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"
    typed_text = "openinputfast"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)

    history_pattern = f"**/api/terminal/history/{tab['id']}?**"

    def delay_history(route) -> None:
        time.sleep(1.0)
        route.continue_()

    page.route(history_pattern, delay_history)
    try:
        page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab['id']}/")
        page.wait_for_selector(".xterm", timeout=15000)
        page.wait_for_function(
            "() => window.term && window.term.__claudeHubReplayBuffering === true",
            timeout=10000,
        )

        box = page.locator(".xterm").bounding_box()
        assert box is not None, "xterm not found"
        page.mouse.click(box["x"] + 24, box["y"] + 24)
        page.keyboard.type(typed_text, delay=1)

        page.wait_for_function(
            """(text) => {
                const term = window.term;
                if (!term) return false;
                const buffer = term.buffer.active;
                const lines = [];
                for (let i = 0; i < buffer.length; i++) {
                    const line = buffer.getLine(i);
                    if (line) lines.push(line.translateToString(true));
                }
                return lines.join('\\n').includes(text);
            }""",
            arg=typed_text,
            timeout=1000,
        )
    finally:
        page.unroute(history_pattern, delay_history)

    assert typed_text in capture_pane_sync(session_name)


def test_typing_interrupts_pending_history_resync(terminal_tab: dict, page: Page) -> None:
    """Typing while live-output history repair is fetching should echo immediately."""
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"
    line_count = 80
    typed_text = "resyncinputfast"

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)
    page.wait_for_timeout(9000)
    page.evaluate("""() => {
        window.__delayNextHistoryFetch = true;
        window.__historyFetchStarted = false;
        window.__releaseHistoryFetch = null;
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (window.__delayNextHistoryFetch && url.indexOf('/api/terminal/history/') >= 0) {
                window.__delayNextHistoryFetch = false;
                window.__historyFetchStarted = true;
                return new Promise(function(resolve, reject) {
                    window.__releaseHistoryFetch = function() {
                        originalFetch(input, init).then(resolve, reject);
                    };
                });
            }
            return originalFetch(input, init);
        };
    }""")

    send_keys_sync(
        session_name,
        (
            f"for i in $(seq 0 {line_count - 1}); do "
            "echo LIVE_$(printf '%04d' $i)_$(printf 'x%.0s' $(seq 1 220)); "
            "done"
        ),
        "Enter",
    )

    try:
        for _ in range(int(scale_timeout(80))):
            if f"LIVE_{line_count - 1:04d}" in capture_pane_sync(session_name):
                break
            time.sleep(0.2)
        else:
            pytest.fail("wrapped live output did not finish in tmux")

        page.wait_for_function(
            "() => window.__historyFetchStarted === true",
            timeout=10000,
        )

        box = page.locator(".xterm").bounding_box()
        assert box is not None, "xterm not found"
        page.mouse.click(box["x"] + 24, box["y"] + 24)
        page.keyboard.type(typed_text, delay=1)

        page.wait_for_function(
            """(text) => {
                const term = window.term;
                if (!term) return false;
                const buffer = term.buffer.active;
                const lines = [];
                for (let i = 0; i < buffer.length; i++) {
                    const line = buffer.getLine(i);
                    if (line) lines.push(line.translateToString(true));
                }
                return lines.join('\\n').includes(text);
            }""",
            arg=typed_text,
            timeout=1000,
        )
    finally:
        page.evaluate("""() => {
            if (typeof window.__releaseHistoryFetch === 'function') {
                window.__releaseHistoryFetch();
            }
        }""")

    assert typed_text in capture_pane_sync(session_name)


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

    for _ in range(int(scale_timeout(120))):
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


def test_typed_wrapped_output_resyncs_after_input_quiets(terminal_tab: dict, page: Page) -> None:
    """User-typed high-volume output should resync after the input quiet window."""
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"
    line_count = 120

    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=260)
    load_terminal_page(page, tab["id"], min_buffer_lines=260)
    page.wait_for_timeout(9000)
    page.evaluate("""() => {
        window.__claudeHubHistoryFetches = [];
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/api/terminal/history/') >= 0) {
                window.__claudeHubHistoryFetches.push(url);
            }
            return originalFetch(input, init);
        };
    }""")

    box = page.locator(".xterm").bounding_box()
    assert box is not None, "xterm not found"
    page.mouse.click(box["x"] + 24, box["y"] + 24)
    page.keyboard.type(
        (
            f"for i in $(seq 0 {line_count - 1}); do "
            "echo LIVE_$(printf '%04d' $i)_$(printf 'x%.0s' $(seq 1 220)); "
            "sleep 0.01; "
            "done"
        ),
        delay=1,
    )
    page.keyboard.press("Enter")

    for _ in range(int(scale_timeout(120))):
        if f"LIVE_{line_count - 1:04d}" in capture_pane_sync(session_name):
            break
        time.sleep(0.2)
    else:
        pytest.fail("typed wrapped output did not finish in tmux")

    page.wait_for_function(
        "() => (window.__claudeHubHistoryFetches || []).length > 0",
        timeout=8000,
    )
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
        f"typed xterm live output markers are discontinuous after delayed resync; "
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

    for _ in range(int(scale_timeout(120))):
        if f"LIVE_{line_count - 1:04d}" in capture_pane_sync(session_name):
            break
        time.sleep(0.2)
    else:
        pytest.fail("wrapped live output did not finish in tmux")

    box = page.locator(".xterm-viewport").bounding_box()
    assert box is not None, "xterm viewport not found"
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -240)
    page.wait_for_function(
        """() => {
            const term = window.term;
            if (!term) return false;
            const buffer = term.buffer.active;
            return buffer.viewportY < buffer.baseY;
        }""",
        timeout=5000,
    )
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


def test_fit_during_replay_preserves_content_and_scroll(terminal_tab: dict, page: Page) -> None:
    """A mode-return fit fired while history is replaying must not corrupt
    terminal content or scroll state.

    Regression guard for the loading-speed optimization: ``resizeWhenReady``
    no longer blocks on ``__claudeHubReplayBuffering``. If a
    ``terminal-resize`` message arrives while the initial history replay is
    still buffering, the fit runs immediately. This test proves that:

    1. The fit actually runs during buffering (nonce is recorded, proving
       the request was not queued behind the replay).
    2. After the replay completes, the full terminal content matches the
       tmux ground truth (the fit did not truncate or duplicate lines).
    3. The viewport ends at the bottom (the fit did not leave the user
       scrolled into the middle of history).
    """
    tab = terminal_tab
    session_name = f"claude-hub-{tab['id'][:8]}"

    # Step 1: produce deterministic scrollback
    ensure_tmux_session(page, tab["id"], session_name)
    produce_scrollback(session_name, count=200)
    ground_truth = capture_pane_sync(session_name)
    tmux_lines = normalize_terminal_output(ground_truth)

    # Step 2: load the terminal. The initial history replay sets
    # __claudeHubReplayBuffering = true for at least FULL_REPLAY_MIN_HOLD_MS
    # (2500ms for local terminals). We fire the mode-return resize inside
    # that window.
    page.goto(f"{BACKEND_URL}/api/terminal/proxy/{tab['id']}/")
    page.wait_for_selector(".xterm", timeout=15000)
    # Wait until the replay buffering flag is set (history fetch returned
    # and replayHistory started buffering writes).
    page.wait_for_function(
        "() => window.term && window.term.__claudeHubReplayBuffering === true",
        timeout=10000,
    )

    # Step 3: fire a mode-return resize with a unique nonce while
    # buffering is still active.
    nonce = f"fit-during-replay-{int(time.time() * 1000)}"
    page.evaluate(
        """(args) => {
            window.postMessage({
                type: 'terminal-resize',
                tabId: args.tabId,
                nonce: args.nonce,
            }, '*');
        }""",
        arg={"tabId": tab["id"], "nonce": nonce},
    )

    # Step 4: the fit must complete during buffering — the nonce is
    # recorded as soon as callFit runs. If resizeWhenReady still blocked
    # on __claudeHubReplayBuffering, this would time out.
    page.wait_for_function(
        """(expectedNonce) => {
            const term = window.term;
            return term && term.__claudeHubLastFitNonce === expectedNonce;
        }""",
        arg=nonce,
        timeout=5000,
    )

    # Confirm buffering is still active at fit-completion time — the
    # fit really did run during replay, not after it.
    still_buffering = page.evaluate(
        "() => window.term && window.term.__claudeHubReplayBuffering === true"
    )
    assert still_buffering is True, (
        "fit completed after replay buffering ended; the test did not "
        "exercise the fit-during-replay path"
    )

    # Step 5: let the replay finish and the visible screen settle.
    wait_for_replay_done(page)
    wait_for_visible_screen(page)
    wait_for_xterm_buffer_lines(page, len(tmux_lines))

    # Step 6: content must match tmux ground truth exactly.
    xterm_content = read_xterm_buffer(page)
    assert xterm_content is not None, "window.term not found after replay"
    xterm_lines = normalize_xterm_lines(xterm_content)
    assert xterm_lines == tmux_lines, (
        f"fit-during-replay corrupted terminal content "
        f"({len(xterm_lines)} vs {len(tmux_lines)} lines):\n"
        f"{diff_summary(xterm_lines, tmux_lines)}"
    )

    # Step 7: viewport must be at the bottom (scroll state preserved).
    alignment = read_scroll_alignment(page)
    assert alignment is not None
    assert (
        alignment["viewportY"] == alignment["baseY"]
    ), f"fit-during-replay left viewport away from bottom: {alignment}"
    assert (
        alignment["bottomGap"] <= alignment["rowHeight"] * 2
    ), f"fit-during-replay left DOM viewport away from bottom: {alignment}"
