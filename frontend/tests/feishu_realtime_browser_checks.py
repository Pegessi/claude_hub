"""Realtime execution settings acceptance, reused by the Bot settings scenario."""

from playwright.async_api import expect


async def check_realtime_agent_settings(settings, api_calls: list[dict]) -> None:
    panel = settings.get_by_test_id("feishu-realtime-agent-settings")
    provider = panel.get_by_label("Provider", exact=True)
    model = panel.get_by_label("Model", exact=True)
    effort = panel.get_by_label("Thinking intensity", exact=True)
    workspace = panel.get_by_label("Workspace", exact=True)
    await expect(provider).to_have_value("maso")
    await expect(model).to_have_value("")
    await expect(effort).to_have_value("high")
    await expect(workspace).to_have_value("")
    await expect(panel).to_contain_text("without interrupting Dot")
    # Real <select> controls use logical catalog IDs and submit effort separately.
    logical_model = await model.locator("option").nth(1).get_attribute("value")
    await model.select_option(logical_model)
    await effort.select_option("high")
    await workspace.select_option("workspace-a")
    await panel.get_by_role("button", name="Save execution agent", exact=True).click()
    await expect(panel.get_by_test_id("feishu-realtime-agent-saved")).to_be_visible()
    patches = [
        call["body"]
        for call in api_calls
        if call["method"] == "PATCH" and call["path"] == "/api/feishu/bot/realtime-events"
    ]
    assert patches[-1] == {
        "provider": "maso",
        "model": logical_model,
        "reasoning_effort": "high",
        "workspace_id": "workspace-a",
    }
    await panel.get_by_role("button", name="Reload execution settings", exact=True).click()
    await expect(model).to_have_value(logical_model)
    await expect(effort).to_have_value("high")
    await expect(workspace).to_have_value("workspace-a")
    await provider.select_option("claude")
    await expect(model).to_have_value("")
    await expect(effort).to_have_value("")
    await expect(effort).to_be_disabled()
    await provider.select_option("maso")
    await expect(model).to_have_value("")
    await expect(effort).to_have_value("high")
    await panel.get_by_role("button", name="Save execution agent", exact=True).click()
    await expect(panel.get_by_test_id("feishu-realtime-agent-saved")).to_be_visible()
    await panel.get_by_role("button", name="Reload execution settings", exact=True).click()
    await expect(model).to_have_value("")


async def check_realtime_mobile(settings) -> None:
    panel = settings.get_by_test_id("feishu-realtime-agent-settings")
    for field in ("Provider", "Model", "Thinking intensity", "Workspace"):
        control = panel.get_by_label(field, exact=True)
        await control.scroll_into_view_if_needed()
        box = await control.bounding_box()
        bounds = await settings.bounding_box()
        assert box and bounds and box["height"] >= 44
        assert box["x"] >= bounds["x"]
        assert box["x"] + box["width"] <= bounds["x"] + bounds["width"] + 1
    save = panel.get_by_role("button", name="Save execution agent", exact=True)
    await save.scroll_into_view_if_needed()
    await expect(save).to_be_visible()
    assert (await save.bounding_box())["height"] >= 44
