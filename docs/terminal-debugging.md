# Terminal Debugging Guide

This guide holds the terminal replay and Playwright debugging details that used
to live in the root agent files. Keep `AGENTS.md` and `CLAUDE.md` as short
navigation entry points; update this document when terminal replay behavior
changes.

## Terminal History Replay

When modifying `backend/claude_hub/api/terminal.py` or
`backend/claude_hub/services/ttyd_manager.py`, preserve the Phase A/B replay
model:

- **Phase A**: `term.open()` has not been called. Write scrollback only, use SU
  escape to push bottom rows into scrollback, and leave the visible screen blank
  for ttyd WebSocket data.
- **Phase B**: `term.open()` has already been called and the element is
  attached. Clear the entire buffer, write full content, and discard buffered
  WebSocket data that would duplicate the screen.
- **Write buffer**: `term.write()` is overridden during replay. Live WebSocket
  data is buffered and flushed after history write completes, with a 5 second
  safety timeout.
- **Backend capture**: `capture-pane -p -e -S -100000` returns scrollback plus
  visible screen. Do not add the `-J` flag.

## Playwright Terminal Debugging

Playwright is installed in the backend venv and can simulate mobile touch events
for debugging terminal UI issues such as injected JavaScript/CSS, scroll
behavior, and touch handling.

```python
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
browser = p.chromium.launch(headless=True)
page = browser.new_page(
    viewport={"width": 390, "height": 844},
    is_mobile=True,
    has_touch=True,
)

errors = []
page.on("pageerror", lambda err: errors.append(str(err)))

page.goto("http://localhost:8173/api/terminal/proxy/<tab_id>/", timeout=10000)
page.wait_for_timeout(5000)

result = page.evaluate(
    """() => {
      var vp = document.querySelector('.xterm-viewport');
      var vpObj = window.term._core.viewport;
      return { scrollTop: vp.scrollTop, overflowY: getComputedStyle(vp).overflowY };
    }"""
)

cdp = page.context.new_cdp_session(page)
cdp.send(
    "Input.dispatchTouchEvent",
    {"type": "touchStart", "touchPoints": [{"x": cx, "y": cy, "id": 0}]},
)
for i in range(1, 11):
    cdp.send(
        "Input.dispatchTouchEvent",
        {"type": "touchMove", "touchPoints": [{"x": cx, "y": cy - 20 * i, "id": 0}]},
    )
cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
```

Key notes:

- xterm viewport object is `term._core.viewport`, not `term.viewport`.
- CDP `Input.dispatchTouchEvent` simulates real touch events; plain
  `dispatchEvent` is not enough.
- `document.body` can be null when injected scripts run. Use
  `document.documentElement`.
- Playwright headless does not simulate browser inertial scroll. Real device
  testing is still needed for inertia verification.
