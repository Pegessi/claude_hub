"""Runtime catalog governs Feishu model effort selection."""

import pytest

from claude_hub.models import StreamModelOption, StreamReasoningEffortOption
from claude_hub.services.feishu_ai_command_models import (
    parse_ai_command,
    register_live_maso_model_variants,
    resolve_ai_command_agent_env,
)


def test_live_default_and_effort_variants() -> None:
    option = StreamModelOption(
        id="example-model",
        label="Example",
        provider_model_id="example-model",
        supported_reasoning_efforts=[
            StreamReasoningEffortOption(
                id="default", label="Default", provider_model_id="example-model_reasoning_medium"
            ),
            StreamReasoningEffortOption(
                id="high", label="High", provider_model_id="example-model_reasoning_high"
            ),
        ],
    )
    try:
        register_live_maso_model_variants([option], "example-model")
        assert (
            resolve_ai_command_agent_env("maso", "example-model", "default")["MASO_MODEL"]
            == "example-model_reasoning_medium"
        )
        parsed = parse_ai_command(
            "/ai --effort high hello",
            default_provider="maso",
            default_model="",
            default_reasoning_effort="",
        )
        assert parsed is not None
        assert parsed.model == "example-model_reasoning_high"
    finally:
        register_live_maso_model_variants([])


def test_duplicate_default_modes_infer_advertised_effort() -> None:
    option = StreamModelOption(
        id="example",
        label="Example",
        provider_model_id="example",
        supported_reasoning_efforts=[
            StreamReasoningEffortOption(id="default", label="Default", provider_model_id="example"),
            StreamReasoningEffortOption(
                id="example_reasoning_high",
                label="High",
                provider_model_id="example_reasoning_high",
            ),
        ],
    )
    try:
        register_live_maso_model_variants([option], "example")
        assert resolve_ai_command_agent_env("maso", "example", "default")["MASO_MODEL"] == "example"
        assert (
            resolve_ai_command_agent_env("maso", "example", "high")["MASO_MODEL"]
            == "example_reasoning_high"
        )
    finally:
        register_live_maso_model_variants([])


@pytest.mark.asyncio
async def test_live_refresh_adds_fallback_efforts_and_preserves_configured_default(
    tmp_path, monkeypatch
) -> None:
    import json

    from claude_hub.services import feishu_ai_command as command
    from claude_hub.services import feishu_ai_command_models as models
    from claude_hub.services import maso_discovery

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"maso_default_model": "fixture-model"}))
    monkeypatch.setenv("CLAUDE_HUB_AI_MODEL_CATALOG_FILE", str(catalog))
    option = StreamModelOption(
        id="fixture-model",
        label="Fixture",
        provider_model_id="fixture-model",
        supported_reasoning_efforts=[
            StreamReasoningEffortOption(
                id="medium",
                label="Medium",
                provider_model_id="fixture-model_reasoning_medium",
            )
        ],
    )

    async def discover(_env):
        return "http://localhost", [option], "other-model"

    monkeypatch.setattr(maso_discovery, "discover_maso_service", discover)
    monkeypatch.setattr(command, "_AI_MASO_CATALOG_CACHE", None)
    try:
        await command._async_provider_model_options()
        for effort in ("default", "none", "low"):
            assert (
                resolve_ai_command_agent_env("maso", "fixture-model", effort)["MASO_MODEL"]
                == "fixture-model"
            )
        for effort in ("medium", "high"):
            assert (
                resolve_ai_command_agent_env("maso", "fixture-model", effort)["MASO_MODEL"]
                == "fixture-model_reasoning_medium"
            )
        assert models._DYNAMIC_MASO_DEFAULT_MODEL == "fixture-model"
    finally:
        register_live_maso_model_variants([])


def test_private_legacy_variants_survive_live_catalog_refresh(tmp_path, monkeypatch) -> None:
    import json

    from claude_hub.services.feishu_ai_command_models import _resolve_maso_model_with_effort

    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "maso_legacy_variants": {
                    "fixture-model": {
                        "none": "fixture-model",
                        "high": "fixture-model_special",
                    }
                }
            }
        )
    )
    monkeypatch.setenv("CLAUDE_HUB_AI_MODEL_CATALOG_FILE", str(catalog))
    try:
        register_live_maso_model_variants(
            [StreamModelOption(id="other-model", label="Other", provider_model_id="other-model")]
        )
        assert _resolve_maso_model_with_effort("fixture-model", "high") == "fixture-model_special"
        assert _resolve_maso_model_with_effort("fixture-model", "default") == "fixture-model"
        with pytest.raises(ValueError):
            _resolve_maso_model_with_effort("fixture-model", "max")
    finally:
        register_live_maso_model_variants([])
