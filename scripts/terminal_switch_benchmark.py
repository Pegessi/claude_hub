"""
Terminal switch benchmark — measures time from mode switch to actual
xterm fit completion + scroll-bottom settlement.

Unlike a naive "wait for nonzero rect" check, this benchmark polls the
terminal's actual rendered state inside the iframe:
  - fit completion: term.cols / term.rows are nonzero and stable across
    two consecutive polls (the debounced double-fit has settled)
  - scroll settlement: .xterm-viewport scrollTop + clientHeight >=
    scrollHeight - 1

Runs for both 1x1 and split (2x1) layouts.
"""
import json
import sys
import time
from playwright.sync_api import sync_playwright

APP_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5173"
POLL_INTERVAL_MS = 30
STABLE_POLLS = 2  # consecutive identical cols/rows => fit settled
SETTLE_TOLERANCE = 1  # px from bottom counts as "at bottom"
TIMEOUT_MS = 15000


def get_term_state(frame):
    """Return (cols, rows, at_bottom) for the terminal in this iframe frame."""
    return frame.evaluate(
        """
        () => {
          const term = (window.ttyd && window.ttyd.terminal) || window.term;
          if (!term) return null;
          const vp = document.querySelector('.xterm-viewport');
          const atBottom = vp ? (vp.scrollTop + vp.clientHeight >= vp.scrollHeight - 1) : false;
          return { cols: term.cols || 0, rows: term.rows || 0, atBottom };
        }
        """
    )


def wait_for_fit_and_scroll(page, tab_ids, t0):
    """
    Wait until every visible pane's terminal has completed fit (cols/rows
    stable) and scroll-bottom settlement. Returns the elapsed ms from t0.
    """
    frames_by_tab = {}
    for tab_id in tab_ids:
        for f in page.frames:
            if tab_id in f.url:
                frames_by_tab[tab_id] = f
                break

    if not frames_by_tab:
        raise RuntimeError(f"No iframe frames found for tabs: {tab_ids}")

    last_state = {tid: None for tid in frames_by_tab}
    stable_count = {tid: 0 for tid in frames_by_tab}
    settled = {tid: False for tid in frames_by_tab}

    deadline = time.time() + TIMEOUT_MS / 1000
    while time.time() < deadline:
        all_done = True
        for tid, frame in frames_by_tab.items():
            if settled[tid]:
                continue
            state = get_term_state(frame)
            if state is None:
                all_done = False
                continue
            cols, rows, at_bottom = state["cols"], state["rows"], state["atBottom"]
            # Fit done: nonzero and stable across consecutive polls.
            if cols > 0 and rows > 0:
                if last_state[tid] == (cols, rows):
                    stable_count[tid] += 1
                else:
                    stable_count[tid] = 0
                last_state[tid] = (cols, rows)
            else:
                stable_count[tid] = 0
                last_state[tid] = None
            fit_done = stable_count[tid] >= STABLE_POLLS
            if fit_done and at_bottom:
                settled[tid] = True
            else:
                all_done = False
        if all_done:
            break
        time.sleep(POLL_INTERVAL_MS / 1000)

    elapsed = (time.time() - t0) * 1000
    return elapsed, settled


def get_terminal_store(page):
    """Access the Pinia terminal store via the Vue app instance."""
    return page.evaluate(
        """
        () => {
          const app = document.querySelector('#app').__vue_app__;
          if (!app) return null;
          const pinia = app.config.globalProperties.$pinia;
          if (!pinia) return null;
          // The terminal store is registered under the id 'terminal'.
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
          // Wait a tick for panes to initialize.
          await new Promise(r => setTimeout(r, 100));
          const panes = store.panes;
          const tabs = store.tabs || [];
          // Ensure we have at least 2 tabs.
          if (tabs.length < 2 && store.createTab) {
            await store.createTab({ title: 'tab-2' });
          }
          // Assign tabs to panes.
          const tabIds = (store.tabs || []).map(t => t.id);
          for (let i = 0; i < panes.length && i < tabIds.length; i++) {
            if (!panes[i].tabId) {
              store.assignTabToPane(tabIds[i], panes[i].id);
            }
          }
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
        # Wait for terminal store and iframes to be ready.
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
        t0 = time.time()
        switch_mode(page, "terminal")
        tab_ids = get_visible_tab_ids(page)
        elapsed_1x1, settled_1x1 = wait_for_fit_and_scroll(page, tab_ids, t0)
        results["1x1"] = {
            "elapsed_ms": round(elapsed_1x1, 1),
            "settled": settled_1x1,
            "tab_ids": tab_ids,
        }
        print(f"1x1: {elapsed_1x1:.1f}ms, settled={settled_1x1}")

        # ---- split (2x1) layout ----
        ensure_split_layout_with_tabs(page)
        time.sleep(1.5)
        switch_mode(page, "workspace")
        time.sleep(0.8)
        t0 = time.time()
        switch_mode(page, "terminal")
        tab_ids = get_visible_tab_ids(page)
        elapsed_split, settled_split = wait_for_fit_and_scroll(page, tab_ids, t0)
        results["2x1"] = {
            "elapsed_ms": round(elapsed_split, 1),
            "settled": settled_split,
            "tab_ids": tab_ids,
        }
        print(f"2x1: {elapsed_split:.1f}ms, settled={settled_split}")

        browser.close()
    return results


if __name__ == "__main__":
    res = run_benchmark()
    print(json.dumps(res, indent=2))
