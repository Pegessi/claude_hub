"""
Terminal switch benchmark — measures time from mode switch to actual
xterm fit completion, using a NEUTRAL readiness signal that works on both
main and feature branches.

Neutral readiness signal: fit completion (cols > 0 AND rows > 0). This
works on both main and feature because every terminal reports its
dimensions once it has been laid out and fitted.

Causal correlation (feature only): when nonces are present, the benchmark
additionally requires `lastFitNonce == expected_fit_nonce` to prove the
*current* mode-return resize request ran (not a delayed unrelated fit).
On main, where no nonce protocol exists, the benchmark falls back to the
neutral cols>0/rows>0 signal alone.

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
    """Return terminal state including request-correlation nonces."""
    return frame.evaluate(
        """
        () => {
          const term = (window.ttyd && window.ttyd.terminal) || window.term;
          if (!term) return null;
          return {
            cols: term.cols || 0,
            rows: term.rows || 0,
            lastFitNonce: term.__claudeHubLastFitNonce || null,
            // lastScrollNonce kept for backward compat; the benchmark no
            // longer measures scroll-to-bottom (it is not dispatched).
            lastScrollNonce: term.__claudeHubLastScrollNonce || null,
          };
        }
        """
    )


def find_frame_for_tab(page, tab_id):
    """Return the iframe frame whose URL contains tab_id, or None."""
    for f in page.frames:
        if tab_id in f.url:
            return f
    return None


def wait_for_fit(page, tab_ids, expected_nonces, t0):
    """
    Wait until every visible pane's terminal has completed a fit after the
    mode switch.

    Neutral readiness signal (works on both main and feature): cols > 0
    AND rows > 0. Every terminal reports its dimensions once it has been
    laid out and fitted, so this works regardless of whether the branch
    dispatches nonce-correlated resize messages.

    Causal correlation (feature only): when nonces are present for a tab
    (i.e. `expected_nonces[tab_id]["fit"]` is not None), the benchmark
    additionally requires `lastFitNonce == expected_fit_nonce` to prove
    the *current* mode-return resize request ran (not a delayed unrelated
    fit from a ResizeObserver callback that fired before the switch).

    On main, where no nonce protocol exists, `expected_nonces` is empty
    and the benchmark falls back to the neutral cols>0/rows>0 signal
    alone.

    Fails (raises) if any requested tab's iframe cannot be found.
    Returns (elapsed_ms, settled_dict, fit_times).
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

    fit_done = {tid: False for tid in frames_by_tab}
    fit_times = {tid: None for tid in frames_by_tab}

    deadline = time.time() + TIMEOUT_MS / 1000
    while time.time() < deadline:
        all_done = True
        now = time.time()
        for tid, frame in frames_by_tab.items():
            if fit_done[tid]:
                continue
            state = get_term_state(frame)
            if state is None:
                all_done = False
                continue
            cols, rows = state["cols"], state["rows"]
            last_fit_nonce = state["lastFitNonce"]
            expected = expected_nonces.get(tid, {"fit": None})
            expected_fit_nonce = expected.get("fit")

            # Neutral signal: fit produced valid (nonzero) dimensions.
            fit_valid = cols > 0 and rows > 0

            # Causal correlation (feature only): when a nonce was
            # dispatched for this tab, require the terminal's lastFitNonce
            # to match it. On main (no nonces), this check is skipped.
            nonce_matched = (
                expected_fit_nonce is None
                or last_fit_nonce == expected_fit_nonce
            )

            if fit_valid and nonce_matched and not fit_done[tid]:
                fit_done[tid] = True
                fit_times[tid] = round((now - t0) * 1000, 1)

            if not fit_done[tid]:
                all_done = False
        if all_done:
            break
        time.sleep(POLL_INTERVAL_MS / 1000)

    elapsed = (time.time() - t0) * 1000
    settled = {tid: fit_done[tid] for tid in frames_by_tab}
    return elapsed, settled, fit_times


def get_terminal_store(page):
    """Access the Pinia terminal store via the Vue app instance."""
    return page.evaluate(
        """
        () => {
          const app = document.querySelector('#app').__vue_app__;
          if (!app) return null;
          const pinia = app.config.globalProperties.$pinia;
          if (!pinia) return null;
          return pinia._s.get('terminal') || null;
        }
        """
    )


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
    return page.evaluate(
        """
        () => {
          const app = document.querySelector('#app').__vue_app__;
          const pinia = app.config.globalProperties.$pinia;
          const store = pinia._s.get('terminal');
          if (!store) return [];
          return store.panes.map(p => p.tabId).filter(Boolean);
        }
        """
    )


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
    page.evaluate(
        """
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
            await store.createTab({ title: 'tab-2' });
          }
          const tabIds = (store.tabs || []).map(t => t.id);
          for (let i = 0; i < panes.length && i < tabIds.length; i++) {
            if (!panes[i].tabId) {
              store.assignTabToPane(tabIds[i], panes[i].id);
            }
          }
        }
        """
    )


def read_dispatched_nonces(page):
    """
    Read the nonces that the frontend's dispatchTerminalReturnResize stored
    on window.__claudeHubTerminalReturnNonces after the last mode-return
    dispatch. Returns {tab_id: {fit: nonce}}.

    On main (no nonce protocol), this returns an empty dict.
    """
    return page.evaluate(
        """
        () => {
          const w = window;
          return w.__claudeHubTerminalReturnNonces || {};
        }
        """
    )


CHROMIUM_PATH = "/Users/bytedance/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


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
        t0 = time.time()
        switch_mode(page, "terminal")
        # The frontend defers dispatchTerminalReturnResize to the next rAF;
        # wait briefly for it to run and store the nonces on window.
        time.sleep(0.1)
        expected_nonces = read_dispatched_nonces(page)
        elapsed_1x1, settled_1x1, fit_times_1x1 = wait_for_fit(
            page, tab_ids, expected_nonces, t0
        )
        results["1x1"] = {
            "elapsed_ms": round(elapsed_1x1, 1),
            "settled": settled_1x1,
            "fit_times_ms": fit_times_1x1,
            "tab_ids": tab_ids,
            "expected_nonces": expected_nonces,
        }
        print(
            f"1x1: total={elapsed_1x1:.1f}ms, "
            f"fit={fit_times_1x1}, settled={settled_1x1}"
        )

        # ---- split (2x1) layout ----
        ensure_split_layout_with_tabs(page)
        time.sleep(1.5)
        switch_mode(page, "workspace")
        time.sleep(0.8)
        tab_ids = get_visible_tab_ids(page)
        t0 = time.time()
        switch_mode(page, "terminal")
        time.sleep(0.1)
        expected_nonces = read_dispatched_nonces(page)
        elapsed_split, settled_split, fit_times_split = wait_for_fit(
            page, tab_ids, expected_nonces, t0
        )
        results["2x1"] = {
            "elapsed_ms": round(elapsed_split, 1),
            "settled": settled_split,
            "fit_times_ms": fit_times_split,
            "tab_ids": tab_ids,
            "expected_nonces": expected_nonces,
        }
        print(
            f"2x1: total={elapsed_split:.1f}ms, "
            f"fit={fit_times_split}, settled={settled_split}"
        )

        browser.close()
    return results


if __name__ == "__main__":
    res = run_benchmark()
    print(json.dumps(res, indent=2))
