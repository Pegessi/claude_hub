"""Opt-in real Vue UI check against an isolated worktree Vite server.

Run with GOAL_UI_REVIEW_URL=http://127.0.0.1:<dedicated-port>.
The API is stubbed in-browser; this never connects to a Hub runtime/provider.
"""

import json
import os
import re

import pytest
from playwright.sync_api import expect


@pytest.mark.skipif(not os.environ.get("GOAL_UI_REVIEW_URL"), reason="requires isolated Vite")
@pytest.mark.parametrize("width", [1280, 375])
def test_goal_pause_resume_and_unconfirmed_stop_ui(page, tmp_path, width):
    origin = os.environ["GOAL_UI_REVIEW_URL"].rstrip("/")
    component = page.request.get(f"{origin}/src/components/GoalStatusBar.vue").text()
    vue_url = re.search(r'from "([^"]*/vue.js[^"]*)"', component).group(1)
    goal = {
        "id": "goal-review",
        "tab_id": "tab-review",
        "objective": "Verify Goal recovery and preserve the original objective",
        "status": "paused",
        "dispatch_state": "idle",
        "token_usage": 100,
        "usage_quality": "exact",
        "turns_completed": 2,
        "created_at": "2026-09-18T00:00:00Z",
        "updated_at": "2026-09-18T00:00:00Z",
    }
    clear_count = 0

    def api(route):
        nonlocal clear_count
        url = route.request.url
        if url.endswith("/resume"):
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
        import '/src/App.vue';
        import {{useChatGoal}} from '/src/composables/useChatGoal.ts';
        createApp({{setup() {{
          const state = useChatGoal(ref('tab-review'));
          state.hydrate();
          return () => state.goal.value ? h(GoalStatusBar, {{
            goal:state.goal.value, busy:state.isMutating.value, error:state.error.value,
            onPause:state.pause, onResume:state.resume, onClear:state.clear
          }}) : h('p', 'No Goal');
        }}}}).mount('#app');
        </script></body></html>""",
        ),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{origin}/goal-review")
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


@pytest.mark.skipif(not os.environ.get("GOAL_UI_REVIEW_URL"), reason="requires isolated Vite")
@pytest.mark.parametrize("width,theme", [(1280, "dark"), (375, "dark"), (375, "light")])
def test_composer_add_menu_and_objective_only_goal(page, tmp_path, width, theme):
    origin = os.environ["GOAL_UI_REVIEW_URL"].rstrip("/")
    component = page.request.get(f"{origin}/src/components/StructuredPane.vue").text()
    vue_url = re.search(r'from "([^"]*/vue.js[^"]*)"', component).group(1)
    pinia_url = re.search(
        r'from "([^"]*/pinia.js[^"]*)"',
        page.request.get(f"{origin}/src/stores/terminalStore.ts").text(),
    ).group(1)
    goal = None
    creates = []
    capabilities = {
        "structured": True,
        "adapter_id": "review",
        "schema_version": 1,
        "sources": [],
        "supports_approval_ui": True,
        "supports_tool_timeline": True,
        "supports_images": True,
        "supports_goals": True,
        "supports_dynamic_modes": True,
        "goal_execution_owner": "hub_managed",
        "goal_usage_quality": "exact",
        "current_mode": "default",
        "available_modes": [{"id": "default", "label": "Agent"}, {"id": "plan", "label": "Plan"}],
        "available_models": [
            {"id": "model-review", "label": "Review model", "supported_reasoning_efforts": []}
        ],
        "current_model": "model-review",
        "current_reasoning_effort": None,
    }

    def api(route):
        nonlocal goal
        url = route.request.url
        if url.endswith("/stream/wait") or "/stream/live" in url:
            return  # Hold the live poll; never reach a real runtime/provider.
        if url.endswith("/stream/capabilities"):
            route.fulfill(json=capabilities)
        elif "/stream/events" in url:
            route.fulfill(json={"events": [], "next_sequence": -1, "has_more": False})
        elif url.endswith("/stream/mode"):
            capabilities["current_mode"] = route.request.post_data_json["mode"]
            route.fulfill(json=capabilities)
        elif url.endswith("/goal/current"):
            route.fulfill(body=json.dumps(goal), content_type="application/json")
        elif url.endswith("/goal"):
            body = route.request.post_data_json
            creates.append(body)
            assert set(body) == {"objective", "client_request_id"}
            goal = {
                "id": "goal-review",
                "tab_id": "tab-review",
                "objective": body["objective"],
                "status": "active",
                "dispatch_state": "dispatched",
                "token_usage": None,
                "usage_quality": "unavailable",
                "turns_completed": 0,
                "created_at": "2026-09-19T00:00:00Z",
                "updated_at": "2026-09-19T00:00:00Z",
            }
            route.fulfill(status=201, json=goal)
        elif url.endswith("/pause"):
            goal.update(status="paused", dispatch_state="idle")
            route.fulfill(json=goal)
        else:
            route.fulfill(json={})

    page.route(f"{origin}/api/**", api)
    page.add_init_script("window.EventSource = undefined")
    page.route(
        f"{origin}/composer-review",
        lambda route: route.fulfill(
            content_type="text/html",
            body=f"""<!doctype html><html data-theme="{theme}"><body>
        <div id="app" style="height:100dvh"></div>
        <script type="module">
        import {{createApp, h, KeepAlive}} from '{vue_url}';
        import {{createPinia}} from '{pinia_url}';
        import '/src/App.vue';
        import StructuredPane from '/src/components/StructuredPane.vue';
        import {{useTerminalStore}} from '/src/stores/terminalStore.ts';
        const pinia = createPinia();
        const app = createApp({{setup() {{
          useTerminalStore().tabs = [{{id:'tab-review', agent_type:'codex', env:{{}}, session_kind:'chat'}}];
          return () => h(KeepAlive, null, {{default: () => h(StructuredPane, {{tabId:'tab-review'}})}});
        }}}});
        app.use(pinia).mount('#app');
        </script></body></html>""",
        ),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(f"{origin}/composer-review")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_enabled()
    trigger = page.get_by_role("button", name="Add attachment or Goal", exact=True)
    menu = page.get_by_role("menu", name="Add to chat")
    attach = page.get_by_role("menuitem", name="Add attachment", exact=True)
    setup = page.get_by_role("menuitem", name="Set a Goal", exact=True)
    trigger.focus()
    trigger.press("ArrowDown")
    expect(attach).to_be_focused()
    attach.press("End")
    expect(setup).to_be_focused()
    setup.press("Home")
    expect(attach).to_be_focused()
    attach.press("Escape")
    expect(menu).not_to_be_visible()
    expect(trigger).to_be_focused()
    trigger.press("ArrowUp")
    expect(setup).to_be_focused()
    setup.press("Tab")
    expect(menu).not_to_be_visible()
    expect(page.get_by_role("button", name="Chat mode: Agent", exact=True)).to_be_focused()
    trigger.click()
    # The upward menu overlays part of the textarea on a narrow viewport.
    # Exercise outside dismissal at a point actually outside the popup.
    page.get_by_role("log", name="Chat conversation").click(position={"x": 20, "y": 200})
    expect(menu).not_to_be_visible()

    # The native file chooser still opens from a synchronous click chain.
    trigger.click()
    with page.expect_file_chooser() as chosen:
        attach.click()
    chosen.value.set_files([])
    expect(menu).not_to_be_visible()

    # Plan mode exposes the reason instead of making an invalid create request.
    mode = page.get_by_role("button", name="Chat mode: Agent", exact=True)
    mode.click()
    page.get_by_role("menuitemradio", name="Plan", exact=True).click()
    trigger.click()
    expect(setup).to_have_attribute("aria-disabled", "true")
    expect(setup).to_have_attribute("title", "Switch to Agent mode before setting a Goal")
    setup.press("Enter")
    expect(page.get_by_role("dialog", name="Set a Goal")).not_to_be_visible()
    page.get_by_role("button", name="Chat mode: Plan", exact=True).click()
    expect(menu).not_to_be_visible()
    page.get_by_role("menuitemradio", name="Agent", exact=True).click()

    trigger.click()
    expect(setup).to_have_attribute("aria-disabled", "false")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width == 375:
        assert trigger.bounding_box()["height"] >= 44
        assert attach.bounding_box()["height"] >= 44
    page.screenshot(path=str(tmp_path / f"composer-menu-{width}-{theme}.png"))
    setup.click()
    dialog = page.get_by_role("dialog", name="Set a Goal")
    objective = page.get_by_role("textbox", name="Objective", exact=True)
    expect(objective).to_be_focused()
    expect(dialog.get_by_role("textbox")).to_have_count(1)
    expect(dialog.get_by_role("spinbutton")).to_have_count(0)
    objective.press("Shift+Tab")
    expect(page.get_by_role("button", name="Start Goal", exact=True)).to_be_focused()
    page.keyboard.press("Tab")
    expect(objective).to_be_focused()
    objective.fill("Ship the requested feature and verify the result")
    page.screenshot(path=str(tmp_path / f"goal-dialog-{width}-{theme}.png"))
    objective.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(trigger).to_be_focused()
    trigger.click()
    setup.click()
    expect(objective).to_have_value("Ship the requested feature and verify the result")
    page.get_by_role("button", name="Start Goal", exact=True).click()
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("button", name="Pause", exact=True)).to_be_visible()
    expect(composer).to_be_disabled()
    trigger.click()
    expect(setup).to_have_count(0)
    expect(attach).to_have_attribute("aria-disabled", "true")
    attach.press("Escape")
    page.get_by_role("button", name="Pause", exact=True).click()
    expect(composer).to_be_enabled()
    expect(page.get_by_role("button", name="Resume", exact=True)).to_be_visible()
    assert len(creates) == 1
    assert not errors
