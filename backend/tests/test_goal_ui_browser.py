"""Opt-in real Vue UI check against an isolated worktree Vite server.

Run with GOAL_UI_REVIEW_URL=http://127.0.0.1:<dedicated-port>.
The API is stubbed in-browser; this never connects to a Hub runtime/provider.
"""

import os
import re

import pytest
from playwright.sync_api import expect


@pytest.mark.skipif(not os.environ.get("GOAL_UI_REVIEW_URL"), reason="requires isolated Vite")
@pytest.mark.parametrize("width", [1280, 375])
def test_goal_budget_recovery_and_unconfirmed_stop_ui(page, tmp_path, width):
    origin = os.environ["GOAL_UI_REVIEW_URL"].rstrip("/")
    component = page.request.get(f"{origin}/src/components/GoalStatusBar.vue").text()
    vue_url = re.search(r'from "([^"]*/vue.js[^"]*)"', component).group(1)
    goal = {
        "id": "goal-review",
        "tab_id": "tab-review",
        "objective": "Verify Goal recovery and preserve the original objective",
        "status": "budget_limited",
        "dispatch_state": "idle",
        "token_budget": 100,
        "token_usage": 100,
        "usage_quality": "exact",
        "turns_completed": 2,
        "max_turns": 20,
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    clear_count = 0

    def api(route):
        nonlocal clear_count
        url = route.request.url
        if url.endswith("/budget"):
            assert route.request.post_data_json["token_budget"] == 500
            goal.update(token_budget=500, status="paused")
        elif url.endswith("/resume"):
            goal.update(status="active", dispatch_state="dispatched")
        elif url.endswith("/pause"):
            goal.update(status="paused", dispatch_state="idle")
        elif url.endswith("/clear"):
            clear_count += 1
            goal.update(
                status="cancelled", dispatch_state="uncertain" if clear_count == 1 else "idle"
            )
        route.fulfill(json=goal)

    page.route(f"{origin}/api/**", api)
    page.route(
        f"{origin}/goal-review",
        lambda route: route.fulfill(
            content_type="text/html",
            body=f"""<!doctype html><html><body style="margin:0;background:#171719;color:#eee">
        <div id="app" style="position:relative;height:100vh;display:flex;flex-direction:column;justify-content:end"></div>
        <script type="module">
        import {{createApp, h, ref}} from '{vue_url}';
        import GoalStatusBar from '/src/components/GoalStatusBar.vue';
        import {{useChatGoal}} from '/src/composables/useChatGoal.ts';
        createApp({{setup() {{
          const state = useChatGoal(ref('tab-review'));
          state.hydrate();
          return () => state.goal.value ? h(GoalStatusBar, {{
            goal:state.goal.value, busy:state.isMutating.value, error:state.error.value,
            onPause:state.pause, onResume:state.resume, onClear:state.clear, onBudget:state.updateBudget
          }}) : h('p', 'No Goal');
        }}}}).mount('#app');
        </script></body></html>""",
        ),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{origin}/goal-review")
    page.get_by_role("button", name="Adjust budget").click()
    page.get_by_label("Token budget (blank for no limit)").fill("500")
    page.get_by_role("button", name="Save budget").click()
    expect(page.get_by_role("button", name="Resume", exact=True)).to_be_visible()
    page.get_by_role("button", name="Resume", exact=True).click()
    page.get_by_role("button", name="Pause", exact=True).click()
    page.get_by_role("button", name="Clear", exact=True).click()
    expect(page.locator(".goal-status-summary strong")).to_have_text("Stop unconfirmed")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"goal-{width}.png"))
    page.get_by_role("button", name="Retry stop").click()
    expect(page.get_by_text("No Goal", exact=True)).to_be_visible()
    assert not errors
