"""
Terminal switch benchmark — measures time from mode switch to actual
xterm fit completion, using a CAUSAL fit-call counter that works on both
main and feature branches.

Causal fit-call counter: before each mode switch (workspace -> terminal),
the benchmark injects a counter into every visible terminal iframe that
increments every time `term.resize()` is called (which is what the fit
addon calls internally). The counter's value is recorded as the baseline
*before* the switch. After switching back to terminal mode, the benchmark
waits for the counter to INCREASE past that baseline — proving a fit ran
*after* the switch. This is causal on both main and feature: on main,
the terminal is hidden with `display: none`, which collapses the layout
box to zero size; returning to Terminal mode forces xterm.js to fit
from a zero-size viewport, so the fit-call count always increases.

Two metrics are collected:

1. **first-fit time** (apples-to-apples, BOTH main and feature): time from
   the mode switch to the first fit that ran after the switch AND produced
   valid (nonzero) dimensions. This is the primary main-vs-feature
   comparison metric.

2. **nonce-ack time** (feature only): time from the mode switch to the
   terminal's `__claudeHubLastFitNonce` matching the nonce dispatched with
   the mode-return resize message. This proves the *current* request ran
   (not a delayed unrelated fit). On main (no nonce protocol) this metric
   is not populated.

Scroll-to-bottom is NOT measured: the feature branch no longer dispatches
a forced scroll-to-bottom on mode return (xterm.js preserves the user's
scroll position and only auto-scrolls on new data if the user was at the
bottom).

Also requires that EVERY requested visible tab's iframe is found; if a
frame is missing the benchmark fails rather than silently succeeding on
a subset.

Runs for both 1x1 and split (2x1) layouts.
"""

import json
import sys
import time

from playwright.sync_api import sync_playwright

APP_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5173"
POLL_INTERVAL_MS = 30
TIMEOUT_MS = 15000


def get_term_state(frame):
    """Return terminal state including request-correlation nonces and the
    fit-call counter injected by `inject_fit_counter`."""
    return frame.evaluate("""
        () => {
          const term = (window.ttyd && window.ttyd.terminal) || window.term;
          if (!term) return null;
          return {
            cols: term.cols || 0,
            rows: term.rows || 0,
            lastFitNonce: term.__claudeHubLastFitNonce || null,
            fitCallCount: term.__claudeHubFitCallCount || 0,
            // lastScrollNonce kept for backward compat; the benchmark no
            // longer measures scroll-to-bottom (it is not dispatched).
            lastScrollNonce: term.__claudeHubLastScrollNonce || null,
          };
        }
        """)


def inject_fit_counter(frame):
    """
    Inject a fit-call counter into the terminal inside `frame`.

    The counter increments every time a fit is requested. We wrap both
    `term.fit` (the public fit entry point that ttyd exposes, which calls
    the fit addon's fit) and `term.resize` (which the fit addon calls when
    dimensions actually change). Wrapping only `term.resize` is insufficient
    because the fit addon skips `resize` when the terminal's cols/rows
    already match the proposed dimensions — which is the common case on
    mode return where the terminal shell preserves its layout box.

    If the counter was already installed, this is a no-op. Returns True if
    the terminal was found and the counter is installed, False otherwise.
    """
    return frame.evaluate("""
        () => {
          const term = (window.ttyd && window.ttyd.terminal) || window.term;
          if (!term) return false;
          if (term.__claudeHubFitCallCount === undefined) {
            term.__claudeHubFitCallCount = 0;
            // bump() runs AFTER the original fit/resize returns, so the
            // counter observes fit *completion*, not invocation. Since
            // fitAddon.fit / term.fit are synchronous, this is the moment
            // xterm.js has finished recomputing cols/rows for the current
            // container size.
            const bump = () => { term.__claudeHubFitCallCount++; };
            // Wrap term.fit (ttyd's public fit entry point).
            if (typeof term.fit === 'function') {
              const origFit = term.fit.bind(term);
              term.fit = function() { const r = origFit(); bump(); return r; };
            }
            // Wrap term.fitAddon.fit if present (callFit prefers it).
            if (term.fitAddon && typeof term.fitAddon.fit === 'function') {
              const origAddonFit = term.fitAddon.fit.bind(term.fitAddon);
              term.fitAddon.fit = function() { const r = origAddonFit(); bump(); return r; };
            }
            // Also wrap term.resize for completeness (fit addon calls it
            // when dimensions change).
            const origResize = term.resize.bind(term);
            term.resize = function(cols, rows) {
              const r = origResize(cols, rows);
              bump();
              return r;
            };
          }
          return true;
        }
        """)


def find_frame_for_tab(page, tab_id):
    """Return the iframe frame whose URL contains tab_id, or None."""
    for f in page.frames:
        if tab_id in f.url:
            return f
    return None


def wait_for_fit(page, tab_ids, expected_nonces, baseline_fit_counts, t0):
    """
    Wait until every visible pane's terminal has completed a fit after the
    mode switch.

    Two metrics are collected:

    1. **first-fit time** (apples-to-apples, works on BOTH main and feature):
       the time from the mode switch to the first fit that ran *after* the
       switch and produced valid (nonzero) dimensions. The causal signal is
       `fitCallCount > baseline_fit_counts[tid]` — the baseline is recorded
       *before* the switch, so an increase proves a fit ran after the switch.
       This is causal on both branches: on main, the terminal is hidden with
       `display: none`, which collapses the layout box to zero size; returning
       to Terminal mode forces xterm.js to fit from a zero-size viewport, so
       the fit-call count always increases.

    2. **nonce-ack time** (feature only): the time from the mode switch to
       the terminal's `__claudeHubLastFitNonce` matching the nonce dispatched
       with the mode-return resize message. This proves the *current*
       request ran (not a delayed unrelated fit from a ResizeObserver
       callback). On main, where no nonce protocol exists, this metric is
       not populated.

    The primary main-vs-feature comparison MUST use the first-fit time for
    both branches. The nonce-ack time is a supplementary feature-only metric.

    Fails (raises) if any requested tab's iframe cannot be found.
    Returns (elapsed_ms, settled_dict, first_fit_times, nonce_ack_times).
    """
    frames_by_tab = {}
    missing = []
    for tab_id in tab_ids:
        f = find_frame_for_tab(page, tab_id)
        if f is None:
            missing.append(tab_id)
        else:
            frames_by_tab[tab_id] = f

    if missing:
        raise RuntimeError(
            f"Missing iframe frames for tabs: {missing}. "
            f"All requested visible tabs must have a frame; refusing to "
            f"succeed on a subset."
        )

    first_fit_done = {tid: False for tid in frames_by_tab}
    first_fit_times = {tid: None for tid in frames_by_tab}
    nonce_ack_done = {tid: False for tid in frames_by_tab}
    nonce_ack_times = {tid: None for tid in frames_by_tab}

    deadline = time.time() + TIMEOUT_MS / 1000
    while time.time() < deadline:
        all_first_fit_done = True
        now = time.time()
        for tid, frame in frames_by_tab.items():
            state = get_term_state(frame)
            if state is None:
                all_first_fit_done = False
                continue

            cols, rows = state["cols"], state["rows"]
            fit_call_count = state["fitCallCount"]
            last_fit_nonce = state["lastFitNonce"]
            expected = expected_nonces.get(tid, {"fit": None})
            expected_fit_nonce = expected.get("fit")
            baseline = baseline_fit_counts.get(tid, 0)

            # --- First-fit (common metric, both branches) ---
            if not first_fit_done[tid]:
                fit_ran_after_switch = fit_call_count > baseline
                fit_valid = cols > 0 and rows > 0
                if fit_ran_after_switch and fit_valid:
                    first_fit_done[tid] = True
                    first_fit_times[tid] = round((now - t0) * 1000, 1)

            # --- Nonce-ack (feature only) ---
            if not nonce_ack_done[tid] and expected_fit_nonce is not None:
                if last_fit_nonce == expected_fit_nonce:
                    nonce_ack_done[tid] = True
                    nonce_ack_times[tid] = round((now - t0) * 1000, 1)

            if not first_fit_done[tid]:
                all_first_fit_done = False

        if all_first_fit_done:
            break
        time.sleep(POLL_INTERVAL_MS / 1000)

    elapsed = (time.time() - t0) * 1000
    settled = {tid: first_fit_done[tid] for tid in frames_by_tab}
    return elapsed, settled, first_fit_times, nonce_ack_times


def get_terminal_store(page):
    """Access the Pinia terminal store via the Vue app instance."""
    return page.evaluate("""
        () => {
          const app = document.querySelector('#app').__vue_app__;
          if (!app) return null;
          const pinia = app.config.globalProperties.$pinia;
          if (!pinia) return null;
          return pinia._s.get('terminal') || null;
        }
        """)


def switch_mode(page, mode):
    """Click the mode button to switch to 'terminal' or 'workspace'."""
    page.evaluate(
        """
        (mode) => {
          const buttons = document.querySelectorAll('.mode-button');
          for (const btn of buttons) {
            if (mode === 'terminal' && btn.textContent.trim() === 'Terminal') {
              btn.click();
              return;
            }
            if (mode === 'workspace' && btn.textContent.trim() === 'Agent Workspace') {
              btn.click();
              return;
            }
          }
        }
        """,
        mode,
    )


def get_visible_tab_ids(page):
    """Return the tab IDs currently assigned to visible panes."""
    return page.evaluate("""
        () => {
          const app = document.querySelector('#app').__vue_app__;
          const pinia = app.config.globalProperties.$pinia;
          const store = pinia._s.get('terminal');
          if (!store) return [];
          return store.panes.map(p => p.tabId).filter(Boolean);
        }
        """)


def set_layout(page, layout_type):
    """Set the terminal layout (e.g. '1x1', '2x1')."""
    page.evaluate(
        """
        (layout) => {
          const app = document.querySelector('#app').__vue_app__;
          const pinia = app.config.globalProperties.$pinia;
          const store = pinia._s.get('terminal');
          if (store && store.setLayout) {
            store.setLayout(layout);
          }
        }
        """,
        layout_type,
    )


def ensure_split_layout_with_tabs(page):
    """Set up a 2x1 split layout with both panes having tabs."""
    page.evaluate("""
        async () => {
          const app = document.querySelector('#app').__vue_app__;
          const pinia = app.config.globalProperties.$pinia;
          const store = pinia._s.get('terminal');
          if (!store) return;
          store.setLayout('2x1');
          await new Promise(r => setTimeout(r, 100));
          const panes = store.panes;
          const tabs = store.tabs || [];
          if (tabs.length < 2 && store.createTab) {
            await store.createTab({ name: 'tab-2' });
          }
          const tabIds = (store.tabs || []).map(t => t.id);
          for (let i = 0; i < panes.length && i < tabIds.length; i++) {
            if (!panes[i].tabId) {
              store.assignTabToPane(tabIds[i], panes[i].id);
            }
          }
        }
        """)


def read_dispatched_nonces(page):
    """
    Read the nonces that the frontend's dispatchTerminalReturnResize stored
    on window.__claudeHubTerminalReturnNonces after the last mode-return
    dispatch. Returns {tab_id: {fit: nonce}}.

    On main (no nonce protocol), this returns an empty dict.
    """
    return page.evaluate("""
        () => {
          const w = window;
          return w.__claudeHubTerminalReturnNonces || {};
        }
        """)


CHROMIUM_PATH = "/Users/bytedance/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


def collect_baseline_fit_counts(page, tab_ids):
    """
    For each visible tab, inject the fit-call counter into its iframe and
    return the current (baseline) fit-call count keyed by tab ID.

    Tabs whose iframe cannot be found are skipped (they will cause
    `wait_for_fit` to fail, which is the desired behavior).
    """
    baseline = {}
    for tab_id in tab_ids:
        frame = find_frame_for_tab(page, tab_id)
        if frame is None:
            continue
        inject_fit_counter(frame)
        state = get_term_state(frame)
        if state is not None:
            baseline[tab_id] = state["fitCallCount"]
    return baseline


def run_benchmark():
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=CHROMIUM_PATH)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.goto(APP_URL)
        page.wait_for_load_state("networkidle")
        page.wait_for_function(
            "() => { const app = document.querySelector('#app')?.__vue_app__; "
            "if (!app) return false; const pinia = app.config.globalProperties.$pinia; "
            "const store = pinia?._s?.get('terminal'); "
            "return store && store.panes && store.panes.length > 0; }",
            timeout=15000,
        )
        time.sleep(1.5)

        # ---- 1x1 layout ----
        set_layout(page, "1x1")
        time.sleep(0.5)
        switch_mode(page, "workspace")
        time.sleep(0.8)
        tab_ids = get_visible_tab_ids(page)
        # Inject the fit-call counter and record the baseline count BEFORE
        # the mode switch, so any fit that runs after the switch will
        # increment the count past this baseline.
        baseline_fit_counts = collect_baseline_fit_counts(page, tab_ids)
        t0 = time.time()
        switch_mode(page, "terminal")
        # The frontend defers dispatchTerminalReturnResize to the next rAF;
        # wait briefly for it to run and store the nonces on window.
        time.sleep(0.1)
        expected_nonces = read_dispatched_nonces(page)
        elapsed_1x1, settled_1x1, first_fit_times_1x1, nonce_ack_times_1x1 = (
            wait_for_fit(page, tab_ids, expected_nonces, baseline_fit_counts, t0)
        )
        results["1x1"] = {
            "elapsed_ms": round(elapsed_1x1, 1),
            "settled": settled_1x1,
            "first_fit_times_ms": first_fit_times_1x1,
            "nonce_ack_times_ms": nonce_ack_times_1x1,
            "tab_ids": tab_ids,
            "expected_nonces": expected_nonces,
            "baseline_fit_counts": baseline_fit_counts,
        }
        print(
            f"1x1: total={elapsed_1x1:.1f}ms, "
            f"first_fit={first_fit_times_1x1}, "
            f"nonce_ack={nonce_ack_times_1x1}, settled={settled_1x1}"
        )

        # ---- split (2x1) layout ----
        ensure_split_layout_with_tabs(page)
        time.sleep(1.5)
        switch_mode(page, "workspace")
        time.sleep(0.8)
        tab_ids = get_visible_tab_ids(page)
        # Re-inject the counter (no-op if already present) and record the
        # baseline count before this second mode switch.
        baseline_fit_counts = collect_baseline_fit_counts(page, tab_ids)
        t0 = time.time()
        switch_mode(page, "terminal")
        time.sleep(0.1)
        expected_nonces = read_dispatched_nonces(page)
        elapsed_split, settled_split, first_fit_times_split, nonce_ack_times_split = (
            wait_for_fit(page, tab_ids, expected_nonces, baseline_fit_counts, t0)
        )
        results["2x1"] = {
            "elapsed_ms": round(elapsed_split, 1),
            "settled": settled_split,
            "first_fit_times_ms": first_fit_times_split,
            "nonce_ack_times_ms": nonce_ack_times_split,
            "tab_ids": tab_ids,
            "expected_nonces": expected_nonces,
            "baseline_fit_counts": baseline_fit_counts,
        }
        print(
            f"2x1: total={elapsed_split:.1f}ms, "
            f"first_fit={first_fit_times_split}, "
            f"nonce_ack={nonce_ack_times_split}, settled={settled_split}"
        )

        browser.close()
    return results


if __name__ == "__main__":
    res = run_benchmark()
    print(json.dumps(res, indent=2))
