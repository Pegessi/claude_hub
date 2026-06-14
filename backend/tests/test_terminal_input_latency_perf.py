"""Diagnostic harness: quantify keystroke-to-glyph latency in the live terminal.

This is **not** a CI timing gate — absolute latency numbers are machine
dependent and would flake on shared runners. It is an opt-in, run-on-demand
measurement script used to (a) capture a real before-number for the terminal
input-latency work and (b) prove a fix actually moved the needle.

Run it explicitly and pass ``-s`` to see the table::

    cd backend && uv run pytest tests/test_terminal_input_latency_perf.py -v -s

What it measures
----------------
The real keystroke path for focused desktop typing is
``xterm.js -> ttyd WebSocket -> FastAPI proxy -> tmux -> echo back ->
term.write -> render``. We mark ``t0`` at the in-page ``keydown`` (capture
phase) and ``t1`` inside ``term.onRender`` once the typed glyph is painted —
both timestamps via the page's own ``performance.now()`` so there is no
clock skew. We report p50/p95 for two conditions:

* **idle** — the terminal is quiet.
* **under-load** — a tmux-side loop floods the pane with wide wrapped lines,
  stressing the per-output-frame work on the ``term.write`` hot path. The
  regression shows up as a large idle -> under-load p95 gap.

The tab is created with ``agent_type="terminal"`` on purpose: agent (TUI)
tabs gate the auto-history-resync path off, so only a terminal tab exercises
the per-frame ``terminalDataStats`` cost this harness is built to surface.
"""

import statistics
import time
from typing import Any

import pytest
from playwright.sync_api import Page

from .conftest import (
    BACKEND_URL,
    capture_pane_sync,
    local_requests_session,
    scale_timeout,
    send_keys_sync,
)
from .test_terminal_replay import ensure_tmux_session, load_terminal_page

# Distinct printable sentinels, none of which appear in the load output
# (which is only the byte 'X', digits and newlines). Cycling avoids a freshly
# typed glyph colliding with one still lingering on screen from a prior sample.
SENTINELS = "abcdefghijklmnopqrstuvwy"  # note: no 'x'/'z' to stay clear of load
WARMUP = 5
SAMPLES = 45
PER_SAMPLE_TIMEOUT_MS = int(scale_timeout(2500))

# A wide wrapped-line flood: maximize bytes-per-frame to stress the
# per-output-frame stats scan on the term.write hot path.
LOAD_CMD = "while true; do printf 'X%.0s' $(seq 1 400); echo; done"


# ── in-page measurement harness ──────────────────────────────────────────

_INSTALL_HARNESS_JS = """
() => {
    const term = window.term;
    if (!term) return false;

    const state = {
        pending: null,   // { char, t0, baseline }
        lastLatency: null,
        recorded: 0,
    };
    window.__latState = state;

    // Count how many times `char` shows on the visible rows right now. The
    // load output contains no lowercase letters, so for a quiet/streaming
    // pane the baseline for a fresh sentinel is ~0 and the typed echo is the
    // single increment we detect.
    function visibleCount(char) {
        const buffer = term.buffer.active;
        const rows = term.rows || 24;
        const startY = buffer.baseY;
        let count = 0;
        for (let i = startY; i < startY + rows; i++) {
            const line = buffer.getLine(i);
            if (!line) continue;
            const text = line.translateToString(true);
            for (let k = 0; k < text.length; k++) {
                if (text[k] === char) count++;
            }
        }
        return count;
    }
    window.__latVisibleCount = visibleCount;

    // t0: truest keypress moment (capture phase, before xterm handles it).
    document.addEventListener('keydown', function(e) {
        const p = state.pending;
        if (p && p.t0 === null && e.key === p.char) {
            p.t0 = performance.now();
        }
    }, true);

    // t1: glyph painted. onRender is the closest proxy to "on screen".
    term.onRender(function() {
        const p = state.pending;
        if (!p || p.t0 === null) return;
        if (visibleCount(p.char) > p.baseline) {
            state.lastLatency = performance.now() - p.t0;
            state.recorded++;
            state.pending = null;
        }
    });

    return true;
}
"""


def _arm_sample(page: Page, char: str) -> None:
    """Capture the pre-keystroke baseline and arm the pending sample."""
    page.evaluate(
        """(char) => {
            const s = window.__latState;
            s.pending = { char: char, t0: null, baseline: window.__latVisibleCount(char) };
        }""",
        char,
    )


def _measure_one(page: Page, char: str) -> float | None:
    """Type one sentinel char and return its keystroke-to-glyph latency (ms)."""
    _arm_sample(page, char)
    page.keyboard.press(char)
    try:
        page.wait_for_function(
            "() => window.__latState && window.__latState.pending === null",
            timeout=PER_SAMPLE_TIMEOUT_MS,
        )
    except Exception:
        page.evaluate("() => { window.__latState.pending = null; }")
        return None
    latency: float = page.evaluate("() => window.__latState.lastLatency")
    return latency


def _collect(page: Page, label: str) -> list[float]:
    """Collect `SAMPLES` latencies (after `WARMUP` discarded) for one condition."""
    samples: list[float] = []
    attempts = 0
    max_attempts = (WARMUP + SAMPLES) * 3
    idx = 0
    while len(samples) < (WARMUP + SAMPLES) and attempts < max_attempts:
        attempts += 1
        char = SENTINELS[idx % len(SENTINELS)]
        idx += 1
        latency = _measure_one(page, char)
        # Clear the typed glyph so the next sentinel starts from a clean line.
        page.keyboard.press("Backspace")
        page.wait_for_timeout(60)
        if latency is not None and latency >= 0:
            samples.append(latency)
    measured = samples[WARMUP:]
    print(
        f"  [{label}] collected {len(measured)} samples "
        f"({attempts} attempts, {attempts - len(samples)} dropped)"
    )
    return measured


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = q * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _summary(label: str, values: list[float]) -> dict[str, Any]:
    return {
        "condition": label,
        "p50": _pct(values, 0.50),
        "p95": _pct(values, 0.95),
        "mean": statistics.fmean(values) if values else float("nan"),
        "n": len(values),
    }


def test_keystroke_to_glyph_latency(page: Page) -> None:
    """Measure keystroke-to-glyph latency, idle vs under heavy output."""
    session = local_requests_session()
    resp = session.post(
        f"{BACKEND_URL}/api/tabs",
        json={"name": "perf-input-latency", "agent_type": "terminal"},
    )
    assert resp.status_code == 201, f"Failed to create tab: {resp.text}"
    tab = resp.json()
    session_name = f"claude-hub-{tab['id'][:8]}"

    rows: list[dict[str, Any]] = []
    try:
        ensure_tmux_session(page, tab["id"], session_name)
        load_terminal_page(page, tab["id"])

        # Focus the terminal textarea.
        box = page.locator(".xterm").bounding_box()
        assert box is not None, "xterm not found"
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.wait_for_timeout(300)

        installed = page.evaluate(_INSTALL_HARNESS_JS)
        assert installed is True, "measurement harness failed to install"

        # ── idle ──
        idle = _collect(page, "idle")
        rows.append(_summary("idle", idle))

        # ── under load ──
        send_keys_sync(session_name, LOAD_CMD, "Enter")
        # Let the flood ramp up before measuring.
        for _ in range(int(scale_timeout(25))):
            time.sleep(0.2)
            if capture_pane_sync(session_name).count("X") > 800:
                break
        page.wait_for_timeout(800)

        under_load = _collect(page, "under-load")
        rows.append(_summary("under-load", under_load))

        # Stop the flood.
        send_keys_sync(session_name, "C-c")
        time.sleep(0.3)
    finally:
        try:
            session.delete(f"{BACKEND_URL}/api/tabs/{tab['id']}", timeout=5)
        except Exception:
            pass

    # ── report ──
    print("\n\nkeystroke-to-glyph latency (ms)")
    print(f"{'condition':<12}{'p50':>10}{'p95':>10}{'mean':>10}{'n':>6}")
    for r in rows:
        print(
            f"{r['condition']:<12}{r['p50']:>10.2f}{r['p95']:>10.2f}"
            f"{r['mean']:>10.2f}{r['n']:>6}"
        )
    print()

    # Light sanity assertions only — this is a diagnostic, not a timing gate.
    for r in rows:
        assert r["n"] >= 10, f"too few samples for {r['condition']}: {r['n']}"
