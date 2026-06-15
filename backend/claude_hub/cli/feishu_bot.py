"""Feishu (Lark) long-connection bot CLI subcommand.

Instead of exposing a public webhook, this subcommand opens an OUTBOUND
WebSocket long connection to Feishu via the official ``lark-oapi`` SDK,
receives ``im.message.receive_v1`` events, dispatches whitelisted ``/hub``
chat commands through the local backend (via :class:`HubClient`), and replies
into the originating chat. No public ingress is required.

The connection authenticates with ``app_id`` / ``app_secret`` only; verify
token and encrypt key are webhook concepts and are not used here.

``lark_oapi`` is imported lazily (inside the command callback and the functions
that need it) so that importing ``claude_hub.cli.main`` -- and therefore every
unrelated ``claude-hub`` command -- never hard-depends on ``lark-oapi`` being
installed.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Callable, Optional

import click

from claude_hub.cli.client import HubClient
from claude_hub.cli.config import Settings
from claude_hub.cli.feishu_cards import ACTION_KEY, TOKEN_KEY
from claude_hub.cli.hub_commands import run_hub_chat_command

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime lark dependency
    import lark_oapi as lark

logger = logging.getLogger(__name__)

# Reply sender signature: (chat_id, reply_text) -> None.
ReplyFn = Callable[[str, str], None]
# HubClient factory: built fresh per message so concurrent worker threads never
# share a single (not guaranteed thread-safe) httpx.Client.
HubClientFactory = Callable[[], HubClient]


def _extract_text(message: object) -> Optional[str]:
    """Extract plain text from a Feishu message object.

    Returns ``None`` for non-text messages or malformed content payloads.
    """
    if getattr(message, "message_type", None) != "text":
        return None
    content = getattr(message, "content", None)
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return None
    text = parsed.get("text") if isinstance(parsed, dict) else None
    return text if isinstance(text, str) else None


def handle_message_event(
    data: Any,
    hub_client_factory: HubClientFactory,
    reply_fn: ReplyFn,
) -> None:
    """Pure adapter for an ``im.message.receive_v1`` event.

    Extracts the chat id and text from ``data`` (a ``P2ImMessageReceiveV1``-like
    object), runs the whitelisted ``/hub`` command against a freshly built
    :class:`HubClient`, and -- only when there is a non-empty reply -- calls
    ``reply_fn(chat_id, reply)``. Non-text messages, missing chat ids, and
    empty replies do NOT trigger a send.

    This function takes no lark dependency and never raises, so it is unit
    testable with injected fakes and safe to run on a worker thread.
    """
    try:
        message = getattr(getattr(data, "event", None), "message", None)
        chat_id = getattr(message, "chat_id", None)
        text = _extract_text(message)
        if not chat_id or text is None:
            return
        client = hub_client_factory()
        try:
            reply = run_hub_chat_command(client, text)
        finally:
            client.close()
        if reply:
            reply_fn(chat_id, reply)
    except Exception:  # noqa: BLE001 - never crash the worker / listener on one message
        logger.exception("feishu: failed to handle incoming message")


def _extract_card_action(data: Any) -> Optional[dict]:
    """Extract a normalized card-action payload from a ``card.action.trigger`` event.

    Returns a dict with ``token`` (required), ``action``, ``form``, ``operator_id``,
    and ``chat_id`` — or ``None`` when the event carries no correlation token (a
    foreign card, or a non-interactive control). Never raises.

    The lark SDK exposes the action under ``event.action`` with the control's
    ``value`` (our reserved ``hub_token`` / ``hub_action`` payload) and, for form
    submits, ``form_value`` mapping field-name -> entered text. ``operator`` and
    the source chat are read defensively because their exact shape varies by SDK
    version and event source.
    """
    try:
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        if action is None:
            return None

        value = getattr(action, "value", None)
        if not isinstance(value, dict):
            return None
        token = value.get(TOKEN_KEY)
        if not token:
            return None

        form = getattr(action, "form_value", None)
        if not isinstance(form, dict):
            form = {}

        operator = getattr(event, "operator", None)
        operator_id = (
            getattr(operator, "open_id", None)
            or getattr(operator, "operator_id", None)
            or getattr(operator, "union_id", None)
        )

        context = getattr(event, "context", None)
        chat_id = getattr(context, "open_chat_id", None) or getattr(event, "open_chat_id", None)

        return {
            "token": str(token),
            "action": value.get(ACTION_KEY),
            "form": dict(form),
            "operator_id": operator_id,
            "chat_id": chat_id,
        }
    except Exception:  # noqa: BLE001 - never crash on a malformed callback
        logger.exception("feishu: failed to parse card action event")
        return None


def handle_card_action_event(
    data: Any,
    hub_client_factory: HubClientFactory,
) -> None:
    """Pure adapter for a ``card.action.trigger`` event.

    Extracts the correlation token and the human's decision and POSTs it to the
    backend result store (keyed by token), unblocking a waiting CLI poll. Events
    without a token (foreign cards) are ignored. Never raises, so it is safe on a
    worker thread.
    """
    payload = _extract_card_action(data)
    if payload is None:
        return
    try:
        client = hub_client_factory()
        try:
            client.submit_card_result(
                {
                    "token": payload["token"],
                    "action": payload.get("action"),
                    "form": payload.get("form", {}),
                    "operator_id": payload.get("operator_id"),
                }
            )
        finally:
            client.close()
    except Exception:  # noqa: BLE001 - a 409 (already resolved) or transport error is non-fatal
        logger.exception("feishu: failed to submit card action result")


def _send_reply(api_client: "lark.Client", chat_id: str, reply: str) -> None:
    """Send a plain-text reply into ``chat_id`` via the IM CreateMessage API."""
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": reply}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = api_client.im.v1.message.create(req)
    if not resp.success():
        logger.warning(
            "feishu reply failed: code=%s msg=%s",
            resp.code,
            resp.msg,
        )


def _build_message_handler(
    executor: ThreadPoolExecutor,
    hub_client_factory: HubClientFactory,
    api_client: "lark.Client",
) -> Callable[[Any], None]:
    """Build a non-blocking ``im.message.receive_v1`` callback.

    lark 1.6.8 invokes the handler synchronously on its single asyncio loop, so
    the callback must return immediately. It extracts only the event data and
    submits the (potentially slow, up to ~30s HTTP) command execution + reply to
    a worker thread, keeping message receipt and heartbeats responsive.
    """

    def reply_fn(chat_id: str, reply: str) -> None:
        _send_reply(api_client, chat_id, reply)

    def on_message(data: Any) -> None:
        try:
            executor.submit(handle_message_event, data, hub_client_factory, reply_fn)
        except Exception:  # noqa: BLE001 - never crash the listener on submit failure
            logger.exception("feishu: failed to dispatch incoming message")

    return on_message


def _build_card_action_handler(
    executor: ThreadPoolExecutor,
    hub_client_factory: HubClientFactory,
) -> Callable[[Any], Any]:
    """Build a non-blocking ``card.action.trigger`` callback.

    Submits the decision-recording work to a worker thread and returns
    immediately so the lark loop stays responsive. The return value (``None``)
    tells lark to leave the card unchanged; the human's client already reflects
    the click.
    """

    def on_card_action(data: Any) -> Any:
        try:
            executor.submit(handle_card_action_event, data, hub_client_factory)
        except Exception:  # noqa: BLE001 - never crash the listener on submit failure
            logger.exception("feishu: failed to dispatch card action")
        return None

    return on_card_action


def _run_bot(
    executor: ThreadPoolExecutor,
    hub_client_factory: HubClientFactory,
    app_id: str,
    app_secret: str,
) -> None:
    """Build the lark WS client and run the blocking long connection."""
    import lark_oapi as lark

    api_client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        _build_message_handler(executor, hub_client_factory, api_client)
    )
    # Card action callbacks (``card.action.trigger``) route a human's button /
    # form decision back to the blocked CLI. Registration differs across lark
    # SDK builds, so probe for the available registrar and skip gracefully if
    # this SDK build does not expose one (the message path still works).
    card_handler = _build_card_action_handler(executor, hub_client_factory)
    registrar = getattr(builder, "register_p2_card_action_trigger", None) or getattr(
        builder, "register_p2_application_card_action_trigger", None
    )
    if registrar is not None:
        builder = registrar(card_handler)
    else:
        logger.warning(
            "feishu: this lark-oapi build exposes no card.action.trigger registrar; "
            "interactive card replies will not be collected"
        )

    handler = builder.build()

    ws_client = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    ws_client.start()  # blocking; auto-reconnecting long connection


@click.command("feishu-bot")
@click.option(
    "--app-id",
    default=None,
    help="Feishu app id (falls back to settings.feishu_app_id / $FEISHU_APP_ID).",
)
@click.option(
    "--app-secret",
    default=None,
    help="Feishu app secret (falls back to settings.feishu_app_secret / $FEISHU_APP_SECRET).",
)
@click.pass_context
def feishu_bot(ctx: click.Context, app_id: Optional[str], app_secret: Optional[str]) -> None:
    """Run the Feishu long-connection bot, dispatching /hub chat commands."""
    from claude_hub.config import settings as backend_settings

    resolved_app_id = app_id or os.environ.get("FEISHU_APP_ID") or backend_settings.feishu_app_id
    resolved_app_secret = (
        app_secret or os.environ.get("FEISHU_APP_SECRET") or backend_settings.feishu_app_secret
    )

    if not resolved_app_id or not resolved_app_secret:
        raise click.ClickException(
            "Feishu app credentials missing: set --app-id/--app-secret, "
            "$FEISHU_APP_ID/$FEISHU_APP_SECRET, or feishu_app_id/feishu_app_secret in config."
        )

    # Capture resolved connection settings; each worker builds its own
    # short-lived HubClient from them (httpx.Client is not guaranteed safe for
    # concurrent cross-thread use).
    cli_settings: Settings = ctx.obj

    def hub_client_factory() -> HubClient:
        return HubClient(
            base_url=cli_settings.base_url,
            token=cli_settings.token,
            cookie=cli_settings.cookie,
            verbose=cli_settings.verbose,
        )

    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu-bot")
    click.echo("Connecting to Feishu via long connection; press Ctrl-C to stop")
    try:
        _run_bot(executor, hub_client_factory, resolved_app_id, resolved_app_secret)
    except KeyboardInterrupt:
        click.echo("Stopping Feishu bot.")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
