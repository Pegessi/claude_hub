"""Send a Feishu interactive card to a chat via the IM CreateMessage API.

Isolated here so the card-push IO (the only part that needs ``lark-oapi``) stays
out of the pure card builders (:mod:`claude_hub.cli.feishu_cards`) and the
command wiring. ``lark_oapi`` is imported lazily inside the function so importing
the ``feishu`` command group never hard-depends on the SDK being installed.
"""

from __future__ import annotations

import json
from typing import Any, Dict


class FeishuSendError(Exception):
    """Raised when pushing a card to Feishu fails."""


def send_card(app_id: str, app_secret: str, chat_id: str, card: Dict[str, Any]) -> str:
    """Push ``card`` (interactive-card JSON) to ``chat_id``; return the message id.

    Raises :class:`FeishuSendError` on any SDK/transport failure or non-success
    response so callers can surface a clean CLI error.
    """
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    try:
        resp = client.im.v1.message.create(req)
    except Exception as e:  # noqa: BLE001 - normalize any SDK error into our type
        raise FeishuSendError(str(e)) from e

    if not resp.success():
        raise FeishuSendError(f"feishu send failed: code={resp.code} msg={resp.msg}")

    message_id = getattr(getattr(resp, "data", None), "message_id", None)
    return message_id or ""
