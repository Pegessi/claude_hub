"""Behavior and legacy lookup contracts for extracted native provider helpers."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    StreamModelOption,
    WorkspaceSessionRole,
)
from claude_hub.services.agent_stream import (
    codex_jsonl,
    native,
    provider_guidance,
    provider_model_options,
    provider_questions,
)


@pytest.fixture
def session() -> ManagedSession:
    return ManagedSession(
        id="helper-session",
        workspace_id="helper-workspace",
        tab_id="helper-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CODEX,
        status=ManagedSessionStatus.IDLE,
        title="helper test",
        workspace_path="/tmp",
        tmux_session="helper-tmux",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "agent_type,transport_type",
    [
        (AgentType.CLAUDE, native.ClaudeNativeSession),
        (AgentType.CODEX, native.CodexNativeSession),
        (AgentType.TRAEX, native.TraexNativeSession),
        (AgentType.CURSOR, native.CursorNativeSession),
    ],
)
def test_factory_keeps_native_class_identity(agent_type, transport_type, session) -> None:
    session.agent_type = agent_type
    assert type(native.create_native_session(session)) is transport_type
    assert issubclass(transport_type, native.ProviderSession)
    assert issubclass(native.TraexNativeSession, native.CodexNativeSession)


def test_legacy_question_imports_share_the_extracted_implementation() -> None:
    assert native.codex_normalize_questions is provider_questions.codex_normalize_questions
    assert codex_jsonl.codex_normalize_questions is native.codex_normalize_questions
    assert native.parse_ask_question_response is provider_questions.parse_ask_question_response


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain text",
        "[1]",
        "{malformed",
        '{"type": "other", "answers": []}',
        '{"type": "ask_question_response", "answers": null}',
        '{"type": "ask_question_response", "answers": {}}',
    ],
)
def test_question_response_non_envelopes_fall_through(text: str) -> None:
    assert native.parse_ask_question_response(text) is None


def test_question_response_parser_keeps_envelope_contents_for_the_owner() -> None:
    # Parsing recognizes the envelope; the answering transport validates each item.
    answers = [{"questionId": "scope", "selected": ["Local"], "extra": True}, None]
    text = " \n" + json.dumps({"type": "ask_question_response", "answers": answers})
    assert native.parse_ask_question_response(text) == answers
    assert native.parse_ask_question_response('{"type":"ask_question_response","answers":[]}') == []


def test_question_normalization_keeps_free_text_and_does_not_mutate_provider_input() -> None:
    raw = [
        None,
        {"id": "", "question": "Missing id"},
        {"id": "missing-prompt", "question": None},
        {"id": "bad-options", "question": "Malformed", "options": [{"label": 7}]},
        {"id": "not-a-list", "question": "Malformed", "options": {}},
        {"id": "free", "header": "Type a secret", "options": None, "isSecret": True},
        {
            "id": "choose",
            "question": "Choose scope",
            "header": "Unused header",
            "options": [None, {"label": "Local", "description": "Current checkout"}],
            "multiSelect": True,
        },
        {"id": "empty", "question": "Type here", "options": [], "multiSelect": 1},
    ]
    before = copy.deepcopy(raw)

    assert provider_questions.codex_normalize_questions(raw) == [
        {
            "id": "free",
            "prompt": "Type a secret",
            "options": [],
            "allow_multiple": False,
            "is_secret": True,
        },
        {
            "id": "choose",
            "prompt": "Choose scope",
            "options": [{"id": "Local", "label": "Local", "description": "Current checkout"}],
            "allow_multiple": True,
        },
        {"id": "empty", "prompt": "Type here", "options": [], "allow_multiple": False},
    ]
    assert raw == before


def test_guidance_legacy_constants_are_resolved_at_call_time(monkeypatch, session) -> None:
    monkeypatch.setattr(native, "QUESTION_PROTOCOL_GUIDANCE", "test question advice")
    monkeypatch.setattr(native, "_HUB_QUESTION_PROTOCOL_START", "<question>")
    monkeypatch.setattr(native, "_HUB_QUESTION_PROTOCOL_END", "</question>")
    wrapped = native.wrap_question_protocol_guidance("  user text\n")
    assert wrapped == "<question>\ntest question advice\n</question>\n\n  user text\n"
    assert native.strip_question_protocol_guidance(wrapped) == "  user text\n"
    command = native.ClaudeNativeSession(session)._build_command()
    assert command[command.index("--append-system-prompt") + 1] == "test question advice"


def test_image_guidance_uses_legacy_markers_without_file_io(monkeypatch) -> None:
    monkeypatch.setattr(native, "_HUB_IMAGE_ATTACHMENT_START", "<images>")
    monkeypatch.setattr(native, "_HUB_IMAGE_ATTACHMENT_END", "</images>")
    monkeypatch.setattr(native, "_IMAGE_ATTACHMENT_GUIDANCE", "Read these paths")
    paths = [Path("/not-created/one.png"), Path("/not-created/two with spaces.jpg")]
    wrapped = native.wrap_image_attachment_guidance("user", paths)
    assert wrapped == (
        "<images>\nRead these paths\n- /not-created/one.png\n"
        "- /not-created/two with spaces.jpg\n</images>\n\nuser"
    )
    assert native.strip_image_attachment_guidance(wrapped) == "user"
    assert native.wrap_image_attachment_guidance("user", []) == "user"


@pytest.mark.parametrize("guidance", [None, "", "runtime advice"])
def test_runtime_context_hides_quoted_current_and_legacy_closings(guidance) -> None:
    context = "quoted </claude_hub_instructions> and <<<END_HUB_RUNTIME_V1>>> then secret"
    wrapped = native.wrap_hub_runtime_guidance("  user\n", guidance=guidance, context=context)
    assert "</ claude_hub_instructions>" in wrapped
    assert "<< <END_HUB_RUNTIME_V1>>>" in wrapped
    assert "then secret" in wrapped
    assert native.strip_hub_runtime_guidance(wrapped) == "  user\n"


def test_runtime_wrapper_keeps_legacy_default_binding_and_marker_lookups(monkeypatch) -> None:
    original_guidance = native.HUB_RUNTIME_GUIDANCE
    monkeypatch.setattr(native, "HUB_RUNTIME_GUIDANCE", "new explicit advice")
    monkeypatch.setattr(native, "_HUB_RUNTIME_START", "<runtime>")
    monkeypatch.setattr(native, "_HUB_RUNTIME_END", "</runtime>")
    monkeypatch.setattr(native, "_HUB_RUNTIME_MARKERS", (("<runtime>", "</runtime>"),))
    # The old function bound the default at definition time, not on each call.
    wrapped = native.wrap_hub_runtime_guidance("user", context="quote </runtime> hidden")
    assert (
        wrapped == f"<runtime>\n{original_guidance}\n\nquote </ runtime> hidden\n</runtime>\n\nuser"
    )
    assert native.strip_hub_runtime_guidance(wrapped) == "user"
    assert "new explicit advice" in native.wrap_hub_runtime_guidance(
        "user", guidance=native.HUB_RUNTIME_GUIDANCE
    )


def test_generic_strip_preserves_earliest_malformed_block() -> None:
    markers = (("<new>", "</new>"), ("<old>", "</old>"))
    malformed = "prefix <old>unclosed <new>complete</new>\n\nuser"
    assert provider_guidance.strip_guidance_block(malformed, markers=markers) == malformed
    complete = "prefix <old>hidden</old>\n\n  user <new>later</new>"
    assert provider_guidance.strip_guidance_block(complete, markers=markers) == (
        "prefix   user <new>later</new>"
    )


@pytest.mark.asyncio
async def test_first_turn_and_cursor_send_still_resolve_native_wrappers(
    monkeypatch, session
) -> None:
    runtime_wrap = MagicMock(return_value="runtime user")
    question_wrap = MagicMock(return_value="question user")
    monkeypatch.setattr(native, "wrap_hub_runtime_guidance", runtime_wrap)
    monkeypatch.setattr(native, "wrap_question_protocol_guidance", question_wrap)
    transport = native.CursorNativeSession(session)
    spawn = AsyncMock()
    monkeypatch.setattr(transport, "_spawn_oneshot", spawn)

    await transport.send_message("user", [])
    transport.acknowledge_turn_complete()
    await transport.send_message("next", [])

    runtime_wrap.assert_called_once_with("user", context="")
    assert question_wrap.call_args_list[0].args == ("runtime user",)
    assert question_wrap.call_args_list[1].args == ("next",)
    assert spawn.await_args_list[0].args[1] == "question user"


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_type", [native.CodexNativeSession, native.TraexNativeSession])
async def test_question_dispatch_keeps_native_normalizer_patch(
    monkeypatch, session, transport_type
) -> None:
    normalize = MagicMock(return_value=[])
    monkeypatch.setattr(native, "codex_normalize_questions", normalize)
    transport = transport_type(session)
    response = AsyncMock()
    monkeypatch.setattr(transport, "_send_jsonrpc_response", response)
    raw = [{"id": "q", "question": "Choose", "options": None}]

    await transport._handle_server_request(
        {"id": 7, "method": "item/tool/requestUserInput", "params": {"questions": raw}}
    )

    normalize.assert_called_once_with(raw)
    response.assert_awaited_once_with(7, result={"answers": {}})
    assert transport._pending_questions == {}
    assert transport._notification_queue.empty()


def test_model_options_honor_rebound_native_configuration(monkeypatch) -> None:
    monkeypatch.setattr(native, "_STATIC_MODELS", {"custom": ["model-a", "model-b"]})
    monkeypatch.setattr(native, "_CLAUDE_MODEL_ENV_KEYS", ("CUSTOM_MODEL", "FALLBACK_MODEL"))
    monkeypatch.setattr(native, "_MARKER_RE", re.compile(r"\s*\[selected\]$"))
    assert [option.id for option in native._static_options("custom")] == ["model-a", "model-b"]
    assert [
        option.id
        for option in native._claude_env_options(
            {"CUSTOM_MODEL": " custom ", "FALLBACK_MODEL": "custom", "ANTHROPIC_MODEL": "ignored"}
        )
    ] == ["custom"]
    assert native._parse_list_models("model - Custom [selected]")[0].label == "Custom"


def test_grouping_uses_rebound_effort_definitions_and_keeps_input_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(native, "_REASONING_EFFORTS", {"brief", "deep"})
    monkeypatch.setattr(native, "_REASONING_LABELS", {"brief": "Brief", "deep": "Deep"})
    options = native._parse_list_models("model-brief - Model\nmodel-deep - Model\n")
    before = [option.model_dump() for option in options]
    grouped = native._group_cursor_model_options(options)
    assert len(grouped) == 1
    assert [(effort.id, effort.label) for effort in grouped[0].supported_reasoning_efforts] == [
        ("brief", "Brief"),
        ("deep", "Deep"),
    ]
    assert [option.model_dump() for option in options] == before


def test_grouping_keeps_fast_family_separate_and_prefers_explicit_base() -> None:
    options = native._parse_list_models(
        "model-extra-high - Model Extra High\n"
        "model - Model\n"
        "model-high - Model High\n"
        "model-low-fast - Model Low\n"
        "model-high-fast - Model High\n"
        "standalone-medium - Standalone Medium\n"
    )
    before = [option.model_dump() for option in options]
    grouped = native._group_cursor_model_options(options)
    assert [option.id for option in grouped] == ["model", "model-fast", "standalone-medium"]
    assert grouped[0].provider_model_id == "model"
    assert grouped[0].default_reasoning_effort == "medium"
    assert [
        (effort.id, effort.provider_model_id) for effort in grouped[0].supported_reasoning_efforts
    ] == [
        ("xhigh", "model-extra-high"),
        ("high", "model-high"),
        ("medium", "model"),
    ]
    assert grouped[1].label == "Model Fast"
    assert grouped[1].provider_model_id == "model-high-fast"
    assert grouped[2].supported_reasoning_efforts == []
    assert [option.model_dump() for option in options] == before


def test_stateless_model_options_are_fresh_per_call() -> None:
    first = provider_model_options.model_options_from_ids(["model"])
    first[0].label = "changed"
    second = provider_model_options.model_options_from_ids(["model"])
    assert second[0].label == "model"
    assert second[0] is not first[0]


@pytest.mark.asyncio
async def test_cursor_probe_keeps_native_parser_and_grouping_patches(monkeypatch) -> None:
    process = MagicMock()
    process.communicate = AsyncMock(return_value=(b"provider output", b""))
    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    parsed = [StreamModelOption(id="parsed", label="Parsed", description="")]
    grouped = [StreamModelOption(id="grouped", label="Grouped", description="")]
    parse = MagicMock(return_value=parsed)
    group = MagicMock(return_value=grouped)
    monkeypatch.setattr(native, "_parse_list_models", parse)
    monkeypatch.setattr(native, "_group_cursor_model_options", group)

    assert await native._probe_cursor_models() == (grouped, False)
    parse.assert_called_once_with("provider output")
    group.assert_called_once_with(parsed)


@pytest.mark.asyncio
async def test_model_cache_and_probe_remain_owned_by_native(monkeypatch) -> None:
    options = [StreamModelOption(id="cached", label="Cached", description="")]
    cache = {"cursor": native._ModelsCacheEntry(options, expires_at=float("inf"))}
    monkeypatch.setattr(native, "_models_cache", cache)
    probe = AsyncMock(return_value=(options, False))
    monkeypatch.setattr(native, "_probe_cursor_models", probe)

    assert await native.available_models_for("cursor") is options
    probe.assert_not_awaited()
    cache.clear()
    assert await native.available_models_for("cursor") is options
    assert await native.available_models_for("cursor") is options
    probe.assert_awaited_once()
    assert cache["cursor"].options is options
