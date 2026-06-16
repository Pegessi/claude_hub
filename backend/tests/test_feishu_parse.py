"""Tests for :func:`parse_card_action`, the external-bot callback parser.

``parse_card_action`` is the single integration point for a user's own Feishu
bot: it turns a raw ``card.action.trigger`` callback into the normalized
decision the backend result store expects. It must work on the raw JSON dict a
webhook delivers AND on attribute-style lark-oapi event objects, and must never
raise.
"""

from __future__ import annotations

from types import SimpleNamespace

from claude_hub.cli.feishu_cards import ACTION_KEY, TOKEN_KEY, parse_card_action


def _raw_callback(
    value: object,
    *,
    form_value: object = None,
    operator_open_id: str = "ou_1",
    chat_id: str = "oc_1",
) -> dict:
    """Build a raw ``card.action.trigger`` callback body (plain JSON dict)."""
    return {
        "event": {
            "action": {"value": value, "form_value": form_value},
            "operator": {"open_id": operator_open_id},
            "context": {"open_chat_id": chat_id},
        }
    }


def _attr_callback(
    value: object,
    *,
    form_value: object = None,
    operator_open_id: str = "ou_1",
    chat_id: str = "oc_1",
) -> SimpleNamespace:
    """Build an attribute-style (lark-oapi-like) event object."""
    action = SimpleNamespace(value=value, form_value=form_value)
    operator = SimpleNamespace(open_id=operator_open_id)
    context = SimpleNamespace(open_chat_id=chat_id)
    return SimpleNamespace(event=SimpleNamespace(action=action, operator=operator, context=context))


def test_parse_raw_dict_button() -> None:
    payload = parse_card_action(_raw_callback({TOKEN_KEY: "tok1", ACTION_KEY: "approve"}))
    assert payload is not None
    assert payload["token"] == "tok1"
    assert payload["action"] == "approve"
    assert payload["form"] == {}
    assert payload["operator_id"] == "ou_1"
    assert payload["chat_id"] == "oc_1"


def test_parse_attr_object_button() -> None:
    payload = parse_card_action(_attr_callback({TOKEN_KEY: "tok1", ACTION_KEY: "approve"}))
    assert payload is not None
    assert payload["token"] == "tok1"
    assert payload["action"] == "approve"
    assert payload["operator_id"] == "ou_1"


def test_parse_inner_event_directly() -> None:
    # Callers may pass the inner ``event`` mapping rather than the full body.
    body = _raw_callback({TOKEN_KEY: "tok9", ACTION_KEY: "approve"})
    payload = parse_card_action(body["event"])
    assert payload is not None
    assert payload["token"] == "tok9"


def test_parse_form_submit_collects_fields() -> None:
    payload = parse_card_action(
        _raw_callback(
            {TOKEN_KEY: "tok2", ACTION_KEY: "submit"},
            form_value={"reply": "ship it"},
        )
    )
    assert payload is not None
    assert payload["action"] == "submit"
    assert payload["form"] == {"reply": "ship it"}


def test_parse_operator_id_falls_back_to_union_id() -> None:
    body = {
        "event": {
            "action": {"value": {TOKEN_KEY: "tok3", ACTION_KEY: "approve"}},
            "operator": {"union_id": "on_42"},
        }
    }
    payload = parse_card_action(body)
    assert payload is not None
    assert payload["operator_id"] == "on_42"


def test_parse_without_token_is_none() -> None:
    # A foreign card / control with no hub_token must be ignored.
    assert parse_card_action(_raw_callback({"other": "x"})) is None


def test_parse_non_dict_value_is_none() -> None:
    assert parse_card_action(_raw_callback("not-a-dict")) is None


def test_parse_missing_action_is_none() -> None:
    assert parse_card_action({"event": {}}) is None


def test_parse_malformed_payload_is_none() -> None:
    assert parse_card_action(None) is None
    assert parse_card_action(SimpleNamespace(event=None)) is None


def test_feishu_group_registered() -> None:
    from claude_hub.cli.main import cli

    assert "feishu" in cli.commands


def test_feishu_bot_command_removed() -> None:
    # The long-connection bot was removed; relay is handled by the user's own
    # bot via parse_card_action + the /api/feishu/cards/result endpoint.
    from claude_hub.cli.main import cli

    assert "feishu-bot" not in cli.commands
