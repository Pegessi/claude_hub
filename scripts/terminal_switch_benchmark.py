"""
Terminal switch benchmark — measures time from mode switch to actual
xterm fit completion + scroll-bottom settlement, using CAUSAL counters.

Causal signal: the terminal exposes __claudeHubFitCount (incremented
every time fitAddon.fit() runs) and __claudeHubScrollCount (incremented
when a scroll-bottom settle loop finishes). The benchmark records these
counts BEFORE the mode switch, then waits for them to INCREASE — proving
the current resize/scroll request was processed, not just that the
terminal happened to already be at a stable size.

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
SETTLE_TOLERANCE = 1  # px from bottom counts as "at bottom"
TIMEOUT_MS = 15000


def get_term_state(frame):
    """Return (cols, rows, at_bottom, fit_count, scroll_count) for the terminal."""
    return frame.evaluate(
        """
        () => {
          const term = (window.ttyd && window.ttyd.terminal) || window.term;
          if (!term) return null;
          const vp = document.querySelector('.xterm-viewport');
          const atBottom = vp ? (vp.scrollTop + vp.clientHeight >= vp.scrollHeight - 1) : false;
          return {
            cols: term.cols || 0,
            rows: term.rows || 0,
            atBottom,
            fitCount: term.__claudeHubFitCount || 0,
            scrollCount: term.__claudeHubScrollCount || 0,
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


def wait_for_fit_and_scroll(page, tab_ids, baseline, t0):
    """
    Wait until every visible pane's terminal has:
      - fitCount > baseline[tab_id].fitCount  (current fit ran — CAUSAL)
      - cols > 0 and rows > 0  (fit produced valid dimensions)
      - scrollCount > baseline[tab_id].scrollCount  (current scroll ran — CAUSAL)
      - atBottom true (scroll settled)

    The fit completion signal is purely CAUSAL: we require the fit counter to
    increase relative to the pre-switch baseline. We do NOT require cols/rows
    to be stable across consecutive polls, because that is non-causal — the
    dimensions could already have been stable before the mode switch. We only
    check that the fit produced valid (nonzero) dimensions.

    Tracks fit_completed_at and scroll_completed_at per tab so that even if
    one signal never fires (e.g. main does not send scroll-bottom on mode
    return), the other's timing is still reported.

    Fails (raises) if any requested tab's iframe cannot be found.
    Returns (elapsed_ms, settled_dict, fit_times, scroll_times).
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
    scroll_done = {tid: False for tid in frames_by_tab}
    fit_times = {tid: None for tid in frames_by_tab}
    scroll_times = {tid: None for tid in frames_by_tab}

    deadline = time.time() + TIMEOUT_MS / 1000
    while time.time() < deadline:
        all_done = True
        now = time.time()
        for tid, frame in frames_by_tab.items():
            if fit_done[tid] and scroll_done[tid]:
                continue
            state = get_term_state(frame)
            if state is None:
                all_done = False
                continue
            cols, rows, at_bottom = state["cols"], state["rows"], state["atBottom"]
            fit_count = state["fitCount"]
            scroll_count = state["scrollCount"]
            base = baseline.get(tid, {"fitCount": 0, "scrollCount": 0})

            fit_ran = fit_count > base["fitCount"]
            scroll_ran = scroll_count > base["scrollCount"]

            # Fit done: the causal signal is that the fit counter increased
            # past the pre-switch baseline, AND the fit produced valid
            # (nonzero) dimensions. We do NOT require cols/rows to be stable
            # across polls — that is non-causal.
            if fit_ran and cols > 0 and rows > 0 and not fit_done[tid]:
                fit_done[tid] = True
                fit_times[tid] = round((now - t0) * 1000, 1)

            if scroll_ran and at_bottom and not scroll_done[tid]:
                scroll_done[tid] = True
                scroll_times[tid] = round((now - t0) * 1000, 1)

            if not (fit_done[tid] and scroll_done[tid]):
                all_done = False
        if all_done:
            break
        time.sleep(POLL_INTERVAL_MS / 1000)

    elapsed = (time.time() - t0) * 1000
    settled = {tid: fit_done[tid] and scroll_done[tid] for tid in frames_by_tab}
    return elapsed, settled, fit_times, scroll_times


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


def read_baseline(page, tab_ids):
    """Read fitCount/scrollCount for each tab BEFORE the mode switch."""
    baseline = {}
    for tid in tab_ids:
        f = find_frame_for_tab(page, tid)
        if f is None:
            baseline[tid] = {"fitCount": 0, "scrollCount": 0}
            continue
        state = get_term_state(f)
        if state is None:
            baseline[tid] = {"fitCount": 0, "scrollCount": 0}
        else:
            baseline[tid] = {
                "fitCount": state["fitCount"],
                "scrollCount": state["scrollCount"],
            }
    return baseline


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
        baseline = read_baseline(page, tab_ids)
        t0 = time.time()
        switch_mode(page, "terminal")
        elapsed_1x1, settled_1x1, fit_times_1x1, scroll_times_1x1 = wait_for_fit_and_scroll(
            page, tab_ids, baseline, t0
        )
        results["1x1"] = {
            "elapsed_ms": round(elapsed_1x1, 1),
            "settled": settled_1x1,
            "fit_times_ms": fit_times_1x1,
            "scroll_times_ms": scroll_times_1x1,
            "tab_ids": tab_ids,
            "baseline": baseline,
        }
        print(
            f"1x1: total={elapsed_1x1:.1f}ms, "
            f"fit={fit_times_1x1}, scroll={scroll_times_1x1}, "
            f"settled={settled_1x1}"
        )

        # ---- split (2x1) layout ----
        ensure_split_layout_with_tabs(page)
        time.sleep(1.5)
        switch_mode(page, "workspace")
        time.sleep(0.8)
        tab_ids = get_visible_tab_ids(page)
        baseline = read_baseline(page, tab_ids)
        t0 = time.time()
        switch_mode(page, "terminal")
        elapsed_split, settled_split, fit_times_split, scroll_times_split = wait_for_fit_and_scroll(
            page, tab_ids, baseline, t0
        )
        results["2x1"] = {
            "elapsed_ms": round(elapsed_split, 1),
            "settled": settled_split,
            "fit_times_ms": fit_times_split,
            "scroll_times_ms": scroll_times_split,
            "tab_ids": tab_ids,
            "baseline": baseline,
        }
        print(
            f"2x1: total={elapsed_split:.1f}ms, "
            f"fit={fit_times_split}, scroll={scroll_times_split}, "
            f"settled={settled_split}"
        )

        browser.close()
    return results


if __name__ == "__main__":
    res = run_benchmark()
    print(json.dumps(res, indent=2))
