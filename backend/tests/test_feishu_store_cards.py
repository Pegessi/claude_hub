"""Tests for the Feishu binding store, card builders, and card sender."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_hub.cli import feishu_store
from claude_hub.cli.feishu_cards import (
    ACTION_KEY,
    CARD_KINDS,
    DISPLAY_KINDS,
    INTERACTIVE_KINDS,
    TOKEN_KEY,
    build_approval_card,
    build_needs_input_card,
    build_plan_confirm_card,
    build_status_card,
    build_task_card,
)
from claude_hub.cli.feishu_sender import FeishuSendError, send_card

# -- feishu_store -----------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path, monkeypatch) -> Path:
    """Point the bindings store at a temp dir via $CLAUDE_HUB_CONFIG_DIR."""
    monkeypatch.setenv("CLAUDE_HUB_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_load_bindings_missing_returns_empty(config_dir: Path) -> None:
    assert feishu_store.load_bindings() == {}


def test_bind_then_load(config_dir: Path) -> None:
    feishu_store.set_binding("ops", "oc_123")
    assert feishu_store.load_bindings() == {"ops": "oc_123"}
    # File actually written under the temp config dir.
    assert (config_dir / "feishu_bindings.json").exists()


def test_resolve_known_binding(config_dir: Path) -> None:
    feishu_store.set_binding("ops", "oc_123")
    assert feishu_store.resolve_target("ops") == "oc_123"


def test_resolve_passthrough_raw_chat_id(config_dir: Path) -> None:
    assert feishu_store.resolve_target("oc_raw") == "oc_raw"


def test_remove_binding(config_dir: Path) -> None:
    feishu_store.set_binding("ops", "oc_123")
    assert feishu_store.remove_binding("ops") is True
    assert feishu_store.load_bindings() == {}
    # Removing a missing binding reports False.
    assert feishu_store.remove_binding("ops") is False


def test_load_bindings_malformed_file(config_dir: Path) -> None:
    path = config_dir / "feishu_bindings.json"
    path.write_text("not json{", encoding="utf-8")
    assert feishu_store.load_bindings() == {}


def test_load_bindings_non_dict(config_dir: Path) -> None:
    path = config_dir / "feishu_bindings.json"
    path.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert feishu_store.load_bindings() == {}


def test_set_binding_updates_existing(config_dir: Path) -> None:
    feishu_store.set_binding("ops", "oc_1")
    feishu_store.set_binding("ops", "oc_2")
    assert feishu_store.load_bindings() == {"ops": "oc_2"}


# -- card kinds -------------------------------------------------------------


def test_card_kind_partitions() -> None:
    assert set(CARD_KINDS) == set(INTERACTIVE_KINDS) | set(DISPLAY_KINDS)
    assert not (set(INTERACTIVE_KINDS) & set(DISPLAY_KINDS))


# -- interactive card builders embed the token ------------------------------


def _all_values(node, found):
    """Recursively collect every ``value`` dict in a card tree."""
    if isinstance(node, dict):
        if "value" in node and isinstance(node["value"], dict):
            found.append(node["value"])
        for v in node.values():
            _all_values(v, found)
    elif isinstance(node, list):
        for item in node:
            _all_values(item, found)
    return found


def test_approval_card_buttons_carry_token() -> None:
    card = build_approval_card("tok1", "Title", "Body")
    values = _all_values(card, [])
    assert values, "approval card should have actionable controls"
    for value in values:
        assert value[TOKEN_KEY] == "tok1"
        assert ACTION_KEY in value
    actions = {v[ACTION_KEY] for v in values}
    assert {"approve", "reject"} <= actions


def test_approval_card_custom_options() -> None:
    card = build_approval_card(
        "tok1",
        "Title",
        "Body",
        options=[{"text": "Yes", "action": "yes", "type": "primary"}],
    )
    values = _all_values(card, [])
    assert {v[ACTION_KEY] for v in values} == {"yes"}


def test_needs_input_card_has_named_form_field() -> None:
    card = build_needs_input_card("tok2", "Title", "Prompt", field_name="answer")
    # The form must contain an input named "answer".
    text = json.dumps(card)
    assert '"answer"' in text
    values = _all_values(card, [])
    submit = [v for v in values if v.get(ACTION_KEY) == "submit"]
    assert submit and submit[0][TOKEN_KEY] == "tok2"


def test_plan_confirm_card_buttons() -> None:
    card = build_plan_confirm_card("tok3", "Title", "Plan")
    values = _all_values(card, [])
    actions = {v[ACTION_KEY] for v in values}
    assert {"confirm", "reject"} <= actions
    for value in values:
        assert value[TOKEN_KEY] == "tok3"


# -- display card builders carry no token -----------------------------------


def test_status_card_no_token() -> None:
    board = {
        "tasks": [
            {"id": "t1", "title": "First", "status": "running"},
            {"id": "t2", "title": "Second", "status": "done"},
        ]
    }
    card = build_status_card("ws1", board)
    assert TOKEN_KEY not in json.dumps(card)
    # Workspace id surfaced somewhere in the card.
    assert "ws1" in json.dumps(card)


def test_status_card_empty_board() -> None:
    card = build_status_card("ws1", {"tasks": []})
    assert "ws1" in json.dumps(card)


def test_task_card_no_token() -> None:
    task = {"id": "t1", "title": "Do thing", "status": "running", "prompt": "go"}
    card = build_task_card(task)
    blob = json.dumps(card)
    assert TOKEN_KEY not in blob
    assert "t1" in blob


# -- card sender ------------------------------------------------------------


class _FakeResp:
    def __init__(self, ok: bool, code: int = 0, msg: str = "", message_id: str = "") -> None:
        self._ok = ok
        self.code = code
        self.msg = msg
        self.data = type("D", (), {"message_id": message_id})()

    def success(self) -> bool:
        return self._ok


def _install_fake_lark(monkeypatch, *, resp=None, raise_exc=None):
    """Install a fake lark_oapi module tree for send_card."""
    import sys
    import types

    captured = {}

    class _MsgBodyBuilder:
        def receive_id(self, v):
            captured["receive_id"] = v
            return self

        def msg_type(self, v):
            captured["msg_type"] = v
            return self

        def content(self, v):
            captured["content"] = v
            return self

        def build(self):
            return "body"

    class _MsgReqBuilder:
        def receive_id_type(self, v):
            captured["receive_id_type"] = v
            return self

        def request_body(self, v):
            return self

        def build(self):
            return "req"

    class _CreateMessageRequest:
        @staticmethod
        def builder():
            return _MsgReqBuilder()

    class _CreateMessageRequestBody:
        @staticmethod
        def builder():
            return _MsgBodyBuilder()

    class _Message:
        def create(self, req):
            if raise_exc is not None:
                raise raise_exc
            return resp

    class _V1:
        message = _Message()

    class _Im:
        v1 = _V1()

    class _Client:
        im = _Im()

        class _ClientBuilder:
            def app_id(self, v):
                return self

            def app_secret(self, v):
                return self

            def build(self):
                return _Client()

        @staticmethod
        def builder():
            return _Client._ClientBuilder()

    lark_mod = types.ModuleType("lark_oapi")
    lark_mod.Client = _Client  # type: ignore[attr-defined]
    im_v1_mod = types.ModuleType("lark_oapi.api.im.v1")
    im_v1_mod.CreateMessageRequest = _CreateMessageRequest  # type: ignore[attr-defined]
    im_v1_mod.CreateMessageRequestBody = _CreateMessageRequestBody  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "lark_oapi", lark_mod)
    monkeypatch.setitem(sys.modules, "lark_oapi.api", types.ModuleType("lark_oapi.api"))
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im", types.ModuleType("lark_oapi.api.im"))
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im.v1", im_v1_mod)
    return captured


def test_send_card_success(monkeypatch) -> None:
    captured = _install_fake_lark(monkeypatch, resp=_FakeResp(ok=True, message_id="om_123"))
    msg_id = send_card("app", "secret", "oc_1", {"k": "v"})
    assert msg_id == "om_123"
    assert captured["receive_id"] == "oc_1"
    assert captured["msg_type"] == "interactive"
    assert json.loads(captured["content"]) == {"k": "v"}


def test_send_card_non_success_raises(monkeypatch) -> None:
    _install_fake_lark(monkeypatch, resp=_FakeResp(ok=False, code=99, msg="boom"))
    with pytest.raises(FeishuSendError, match="99"):
        send_card("app", "secret", "oc_1", {"k": "v"})


def test_send_card_sdk_exception_normalized(monkeypatch) -> None:
    _install_fake_lark(monkeypatch, raise_exc=RuntimeError("network down"))
    with pytest.raises(FeishuSendError, match="network down"):
        send_card("app", "secret", "oc_1", {"k": "v"})
