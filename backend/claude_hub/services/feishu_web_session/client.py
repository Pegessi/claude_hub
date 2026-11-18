"""Feishu web-session client, QR login state machine, gateway caller, and msg-frontier WebSocket listener.

Ported from ``[redacted]`` ``extensions/lark-desk/internal/web/``
(``client.go``, ``messages.go``, ``feed.go``, ``reactions.go``, ``threads.go``, ``push.go``).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import logging
import random
import re
import string
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import quote, urlencode, urlparse

import httpx

from claude_hub.services.feishu_bot import (
    FeishuBotConfigurationError,
    FeishuBotError,
    FeishuDeliveryUncertainError,
)
from claude_hub.services.feishu_web_session.codec import command_of, decode, encode
from claude_hub.services.feishu_web_session.qr import (
    QRCodeMatrix,
    encode_qr_matrix,
    render_qr_svg_data_url,
    render_qr_unicode,
)
from claude_hub.services.feishu_web_session.rich import (
    FEED_INBOX,
    REACT_DONE,
    REACT_ON_IT,
    TYPE_POST,
    TYPE_TEXT,
    FeedPage,
    WebChat,
    WebChatter,
    WebMessage,
    WebOut,
    _i64,
    _num,
    _str,
    chat_of,
    feed_page_of,
    message_of,
    rich_blocks,
    rich_text,
)
from claude_hub.services.feishu_web_session.store import (
    FeishuWebSession,
    FeishuWebSessionStore,
    get_feishu_web_session_store,
    normalize_web_region,
)

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)

CMD_ACK = 1
CMD_HEARTBEAT = 4
CMD_PUSH_MESSAGES = 6

_TEA_APP_ID_RE = re.compile(rb"""teaAppId["']?\s*:\s*["']?(\d+)""")
_APP_KEY_RE = re.compile(rb"""appKey:\s*"(.*?)" """)
_APP_KEY_FALLBACK_RE = re.compile(rb"""appKey["']?\s*:\s*["']([^"']+)["']""")
_USER_ID_JSON_RE = re.compile(r'"user"\s*:\s*\{[^{}]*?"id"\s*:\s*"?(\d+)"?')


class FeishuWebSessionError(FeishuBotError):
    """Base error for Feishu web-session operations."""


class FeishuWebSignedOutError(FeishuBotConfigurationError, FeishuWebSessionError):
    """Raised when the Feishu web session is missing or expired (`ErrSignedOut`)."""


class FeishuWebGatewayError(FeishuWebSessionError):
    """Raised when the Feishu web gateway returns a definite HTTP or status error."""

    def __init__(self, message: str, *, status_code: int = 0, gateway_status: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.gateway_status = gateway_status


class FeishuWebQRLoginExpiredError(FeishuWebSessionError):
    """Raised when a QR login flow expires or is cancelled on the phone."""


@dataclass(frozen=True)
class WebTopic:
    """A thread/topic's state from ``PullThreadsByIDsResponse``."""

    id: str
    chat: str
    last: int = -1
    start: int = 0
    replies: int = 0
    latest_reply_s: int = 0


@dataclass(frozen=True)
class PushedMessage:
    """One message delivered over the ``msg-frontier`` WebSocket long connection."""

    message: WebMessage
    chat_type: int = 0
    at_me: bool = False
    removed: bool = False


@dataclass
class FeishuWebQRLoginSession:
    """An active QR sign-in flow initiated via ``/accounts/qrlogin/init``."""

    login_id: str
    region: str
    token: str
    flow: str
    qr_content: str
    qr_matrix: QRCodeMatrix
    qr_unicode: str
    qr_svg_data_url: str
    expires_at: float
    cookies: dict[str, str]

    def __repr__(self) -> str:
        return (
            f"FeishuWebQRLoginSession(login_id={self.login_id!r}, region={self.region!r}, "
            f"expires_at={self.expires_at!r}, cookies='[REDACTED]')"
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "login_id": self.login_id,
            "region": self.region,
            "qr_content": self.qr_content,
            "qr": self.qr_matrix.to_dict(),
            "qr_unicode": self.qr_unicode,
            "qr_svg_data_url": self.qr_svg_data_url,
            "expires_at": self.expires_at,
            "poll_interval_seconds": 2.0,
            "hint": "用飞书 App 扫码，在手机上确认登录",
        }


def feishu_host(hostname: str) -> bool:
    """Return True if ``hostname`` is a Feishu or Lark domain (including tenant subdomains)."""
    h = (hostname or "").strip().lower().rstrip(".")
    if not h:
        return False
    for d in ("feishu.cn", "larkoffice.com", "larksuite.com"):
        if h == d or h.endswith("." + d):
            return True
    return False


def carry_cookies(
    jar: dict[str, dict[str, str]],
    url: str | httpx.URL,
    cookies: Mapping[str, str],
) -> None:
    """Give a redirect to a Feishu/Lark tenant domain host-only session cookies if it has none yet."""
    parsed = urlparse(str(url))
    host = (parsed.hostname or "").strip().lower()
    if not feishu_host(host) or host in jar and jar[host]:
        return
    jar[host] = {str(k): str(v) for k, v in cookies.items() if str(k) and v is not None}


def cookie_header_from_map(cookies: Mapping[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v is not None)


def build_frontier_frame(packet: Mapping[str, Any], *, now_ms: int | None = None) -> bytes:
    """Encode a ``Packet`` inside a ``msg-frontier`` WebSocket ``Frame``."""
    raw_packet = encode("Packet", packet)
    ms = now_ms if now_ms is not None else int(time.time() * 1000)
    sec = ms // 1000
    return encode(
        "Frame",
        {
            "seqid": sec,
            "logid": sec,
            "service": 1,
            "method": 1,
            "headers": [{"key": "x-request-time", "value": str(ms)}],
            "payloadType": "pb",
            "payload": base64.b64encode(raw_packet).decode("ascii"),
        },
    )


def parse_frontier_frame(
    raw: bytes,
) -> tuple[bytes | None, int, list[PushedMessage]]:
    """Decode one ``msg-frontier`` binary frame into ``(ack_frame_bytes, cmd, pushed_messages)``."""
    fr = decode("Frame", raw)
    raw_b64 = _str(fr.get("payload"))
    payload = base64.b64decode(raw_b64.encode("ascii")) if raw_b64 else b""
    if _str(fr.get("payloadEncoding")) == "gzip" and payload:
        payload = gzip.decompress(payload)
    pkt = decode("Packet", payload)
    sid = _str(pkt.get("sid"))
    ack_bytes: bytes | None = None
    if sid:
        ack_bytes = build_frontier_frame(
            {
                "cmd": CMD_ACK,
                "payloadType": 1,
                "sid": sid,
                "payload": base64.b64encode(b"\x08\x00").decode("ascii"),
            }
        )
    cmd = _num(pkt.get("cmd"))
    if cmd != CMD_PUSH_MESSAGES:
        return ack_bytes, cmd, []

    body_b64 = _str(pkt.get("payload"))
    body = base64.b64decode(body_b64.encode("ascii")) if body_b64 else b""
    push = decode("PushMessagesRequest", body)
    raw_at_me = push.get("messagesAtMe")
    at_me_map: Mapping[str, Any] = raw_at_me if isinstance(raw_at_me, Mapping) else {}
    raw_msgs = push.get("messages")
    msgs_map: Mapping[str, Any] = raw_msgs if isinstance(raw_msgs, Mapping) else {}
    pushed_list: list[PushedMessage] = []
    for key, v in msgs_map.items():
        if not isinstance(v, Mapping):
            continue
        m = message_of(v)
        at = bool(at_me_map.get(key)) or bool(at_me_map.get(m.id))
        removed = bool(v.get("isRemoved")) or (_num(v.get("status")) == 2)
        pushed_list.append(
            PushedMessage(
                message=m,
                chat_type=_num(v.get("chatType")),
                at_me=at,
                removed=removed,
            )
        )
    return ack_bytes, cmd, pushed_list


def compute_frontier_access_key(app_key: str, device_id: str) -> str:
    raw = f"2{app_key}{device_id}f8a69f1719916z".encode("utf-8")
    return hashlib.md5(raw).hexdigest()


class FeishuWebClient:
    """Async client for the Feishu web gateway and QR login protocol."""

    def __init__(
        self,
        store: FeishuWebSessionStore | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 25.0,
    ) -> None:
        self._store = store or get_feishu_web_session_store()
        self._external_http = http_client
        self._timeout = timeout_seconds
        self._tenant_host: str = ""
        self._tenant_of_session_uid: str = ""

    @property
    def store(self) -> FeishuWebSessionStore:
        return self._store

    def session(self, *, auto_import_larkx: bool = False) -> FeishuWebSession | None:
        return self._store.load(auto_import_larkx=auto_import_larkx)

    def user_id(self) -> str:
        s = self.session()
        return s.user_id if s is not None else ""

    def set_session(self, sess: FeishuWebSession | None) -> FeishuWebSession | None:
        return self._store.save(sess)

    def _require_session(self, session: FeishuWebSession | None = None) -> FeishuWebSession:
        s = session or self.session()
        if s is None or not s.is_valid_structure():
            raise FeishuWebSignedOutError("feishu web session missing or expired")
        return s

    @staticmethod
    def headers(s: FeishuWebSession) -> dict[str, str]:
        origin = f"https://open-dev.{s.domain()}"
        app_id = s.web_app_id or "161471"
        return {
            "user-agent": UA,
            "origin": origin,
            "referer": f"{origin}/",
            "x-appid": app_id,
            "x-source": "web",
            "x-web-version": "3.9.32",
            "x-lgw-os-type": "1",
            "x-lgw-terminal-type": "2",
            "cookie": cookie_header_from_map(s.cookies),
        }

    async def _do_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        json_body: Any = None,
        follow_redirects: bool = False,
    ) -> httpx.Response:
        if self._external_http is not None:
            return await self._external_http.request(
                method,
                url,
                headers=headers,
                content=content,
                json=json_body,
                follow_redirects=follow_redirects,
            )
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=follow_redirects,
        ) as client:
            return await client.request(
                method,
                url,
                headers=headers,
                content=content,
                json=json_body,
            )

    async def page(
        self,
        url: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
        session: FeishuWebSession | None = None,
    ) -> tuple[bytes, str]:
        """Fetch an HTML page following redirects across Feishu/Lark tenant hosts with host-only cookies."""
        s = self._require_session(session)
        host_jar: dict[str, dict[str, str]] = {}
        current_url = url
        for _ in range(11):
            parsed = urlparse(current_url)
            host = (parsed.hostname or "").strip().lower()
            carry_cookies(host_jar, current_url, s.cookies)
            req_headers = {"user-agent": UA}
            if extra_headers:
                req_headers.update(extra_headers)
            if host in host_jar:
                req_headers["cookie"] = cookie_header_from_map(host_jar[host])
            resp = await self._do_request(
                "GET",
                current_url,
                headers=req_headers,
                follow_redirects=False,
            )
            # Capture any Set-Cookie headers for this Feishu host
            if feishu_host(host) and resp.cookies:
                bucket = host_jar.setdefault(host, dict(s.cookies))
                for ck_name, ck_val in resp.cookies.items():
                    bucket[ck_name] = ck_val
            if resp.status_code in {301, 302, 303, 307, 308}:
                loc = resp.headers.get("location")
                if not loc:
                    break
                next_url = str(httpx.URL(current_url).join(loc))
                carry_cookies(host_jar, next_url, s.cookies)
                current_url = next_url
                continue
            return resp.content[: 8 << 20], str(resp.url or current_url)
        raise FeishuWebSessionError("stopped after 10 redirects")

    async def tenant_host(self, *, session: FeishuWebSession | None = None) -> str:
        s = self._require_session(session)
        if self._tenant_host and self._tenant_of_session_uid == s.user_id and s.user_id:
            return self._tenant_host
        _, final_url = await self.page(f"https://www.{s.domain()}/messenger/", session=s)
        host = (urlparse(final_url).hostname or "").strip().lower()
        if not host or not feishu_host(host) or host.startswith(("www.", "accounts.")):
            raise FeishuWebSignedOutError("no tenant host: the sign-in page answered")
        self._tenant_host = host
        self._tenant_of_session_uid = s.user_id
        return host

    async def ticket(self, *, session: FeishuWebSession | None = None) -> str:
        """Fetch a ``msg-frontier`` ticket for the session (`GET /suite/passport/frontier_ticket/`)."""
        s = self._require_session(session)
        did = quote(s.effective_device_id(), safe="")
        url = f"https://login.{s.domain()}/suite/passport/frontier_ticket/?local_device_id={did}"
        resp = await self._do_request(
            "GET",
            url,
            headers={
                "cookie": cookie_header_from_map(s.cookies),
                "user-agent": UA,
            },
        )
        if resp.status_code in {401, 403}:
            raise FeishuWebSignedOutError("feishu web session rejected (401/403)")
        try:
            data = resp.json()
        except Exception as exc:
            raise FeishuWebSignedOutError("frontier_ticket returned non-JSON") from exc
        ticket_val = str(data.get("ticket") or "").strip() if isinstance(data, Mapping) else ""
        if not ticket_val:
            raise FeishuWebSignedOutError("frontier_ticket returned empty ticket")
        return ticket_val

    async def valid(self, *, session: FeishuWebSession | None = None) -> bool:
        """Return True if Feishu accepts the session and issues a frontier ticket."""
        s = session or self.session()
        if s is None or not s.is_valid_structure():
            return False
        try:
            await self.ticket(session=s)
            return True
        except FeishuWebSignedOutError:
            return False

    async def profile(self, *, session: FeishuWebSession | None = None) -> FeishuWebSession:
        """Fetch `/accounts/web/user?app_id=12` and update `user_id` and `name` on the session."""
        s = self._require_session(session)
        url = f"https://internal-api-lark-api.{s.domain()}/accounts/web/user?app_id=12"
        hdrs = self.headers(s)
        hdrs.update(
            {
                "x-app-id": "12",
                "x-api-version": "1.0.8",
                "x-csrf-token": s.effective_csrf_token(),
                "x-device-info": "platform=websdk",
            }
        )
        resp = await self._do_request("GET", url, headers=hdrs)
        if resp.status_code in {401, 403}:
            raise FeishuWebSignedOutError("feishu web profile rejected (401/403)")
        raw_text = resp.text
        try:
            payload = json.loads(raw_text)
        except Exception as exc:
            raise FeishuWebSignedOutError("web user profile returned non-JSON") from exc
        data = payload.get("data") if isinstance(payload, Mapping) else None
        user = data.get("user") if isinstance(data, Mapping) else None
        if not isinstance(user, Mapping):
            raise FeishuWebSignedOutError("web user profile missing data.user")
        # Extract exact integer digits from raw JSON first so 64-bit user IDs never lose precision
        m_id = _USER_ID_JSON_RE.search(raw_text)
        uid = m_id.group(1) if m_id else _str(user.get("id")).strip()
        if not uid:
            raise FeishuWebSignedOutError("web user profile returned empty user id")
        uname = _str(user.get("name")).strip()
        updated = replace(s, user_id=uid, name=uname or s.name)
        if session is None:
            self._store.save(updated)
        return updated

    async def start_login(self, region: str = "feishu") -> FeishuWebQRLoginSession:
        """Initiate a Feishu web QR sign-in flow (`POST /accounts/qrlogin/init`)."""
        norm_region = normalize_web_region(region)
        dom = "larksuite.com" if norm_region == "lark" else "feishu.cn"
        origin = f"https://accounts.{dom}"
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "X-App-Id": "1",
            "X-Api-Version": "1.0.8",
            "X-Device-Info": "platform=websdk",
            "X-Terminal-Type": "2",
            "Origin": origin,
            "Referer": f"{origin}/accounts/page/login?app_id=1",
        }
        resp = await self._do_request(
            "POST",
            f"{origin}/accounts/qrlogin/init",
            headers=headers,
            json_body={"redirect_uri": f"https://open-dev.{dom}/next/messenger"},
        )
        try:
            out = resp.json()
        except Exception as exc:
            raise FeishuWebSessionError("QR sign-in init returned non-JSON") from exc
        if not isinstance(out, Mapping) or _num(out.get("code")) != 0:
            code_val = out.get("code") if isinstance(out, Mapping) else resp.status_code
            raise FeishuWebSessionError(f"QR sign-in did not start (code {code_val})")
        flow = (resp.headers.get("x-flow-key") or "").strip()
        raw_data = out.get("data")
        data: Mapping[str, Any] = raw_data if isinstance(raw_data, Mapping) else {}
        raw_step = data.get("step_info")
        step: Mapping[str, Any] = raw_step if isinstance(raw_step, Mapping) else {}
        token = _str(step.get("token")).strip()
        if not token:
            raise FeishuWebSessionError("QR sign-in returned no token")
        qr_content = json.dumps({"qrlogin": {"token": token}}, separators=(",", ":"))
        matrix = encode_qr_matrix(qr_content)
        cookies = {str(k): str(v) for k, v in resp.cookies.items() if str(k) and v is not None}
        return FeishuWebQRLoginSession(
            login_id=f"qrl_{uuid.uuid4().hex[:16]}",
            region=norm_region,
            token=token,
            flow=flow,
            qr_content=qr_content,
            qr_matrix=matrix,
            qr_unicode=render_qr_unicode(matrix),
            qr_svg_data_url=render_qr_svg_data_url(matrix),
            expires_at=time.time() + 170.0,
            cookies=cookies,
        )

    async def poll_login(
        self, qr_login: FeishuWebQRLoginSession
    ) -> tuple[str, FeishuWebSession | None]:
        """Poll `/accounts/qrlogin/polling` once.

        Returns ``("waiting", None)``, ``("done", session)``, or raises
        ``FeishuWebQRLoginExpiredError`` when expired/cancelled.
        """
        if time.time() > qr_login.expires_at:
            raise FeishuWebQRLoginExpiredError("QR code expired or cancelled")
        dom = "larksuite.com" if qr_login.region == "lark" else "feishu.cn"
        origin = f"https://accounts.{dom}"
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/json",
            "X-App-Id": "1",
            "X-Api-Version": "1.0.8",
            "X-Device-Info": "platform=websdk",
            "X-Terminal-Type": "2",
            "Origin": origin,
            "Referer": f"{origin}/accounts/page/login?app_id=1",
        }
        if qr_login.flow:
            headers["X-Flow-Key"] = qr_login.flow
        if qr_login.cookies:
            headers["Cookie"] = cookie_header_from_map(qr_login.cookies)

        resp = await self._do_request(
            "POST",
            f"{origin}/accounts/qrlogin/polling",
            headers=headers,
            json_body={},
        )
        for ck_name, ck_val in resp.cookies.items():
            if ck_name and ck_val is not None:
                qr_login.cookies[str(ck_name)] = str(ck_val)
        if new_flow := (resp.headers.get("x-flow-key") or "").strip():
            qr_login.flow = new_flow

        try:
            out = resp.json()
        except Exception:
            out = {}
        data = out.get("data") if isinstance(out, Mapping) else None
        step = data.get("step_info") if isinstance(data, Mapping) else None
        if isinstance(step, Mapping):
            st = _num(step.get("status"))
            if st in {3, 4, 5}:
                raise FeishuWebQRLoginExpiredError("QR code expired or cancelled")
            # Also support cookies returned inside data/step_info if any
            raw_cks = step.get("cookies") or (
                data.get("cookies") if isinstance(data, Mapping) else None
            )
            if isinstance(raw_cks, Mapping):
                for k, v in raw_cks.items():
                    if k and v:
                        qr_login.cookies[str(k)] = str(v)

        if not qr_login.cookies.get("session"):
            return "waiting", None

        cookies = dict(qr_login.cookies)
        candidate = FeishuWebSession(
            region=qr_login.region,
            cookies=cookies,
            saved_at=float(int(time.time())),
            device_id=cookies.get("passport_web_did", ""),
            csrf_token=cookies.get("swp_csrf_token", ""),
            source="qr_login",
        )
        # Validate frontier ticket and load user profile
        await self.ticket(session=candidate)
        candidate = await self.profile(session=candidate)
        try:
            page_bytes, _ = await self.page(
                f"https://open-dev.{dom}/messenger/",
                session=candidate,
            )
            m_app = _TEA_APP_ID_RE.search(page_bytes)
            m_key = _APP_KEY_RE.search(page_bytes) or _APP_KEY_FALLBACK_RE.search(page_bytes)
            candidate = replace(
                candidate,
                web_app_id=m_app.group(1).decode("ascii") if m_app else candidate.web_app_id,
                app_key=(
                    m_key.group(1).decode("utf-8", errors="ignore") if m_key else candidate.app_key
                ),
            )
        except Exception:
            pass
        saved = self._store.save(candidate)
        return "done", (saved or candidate)

    async def call(
        self,
        name: str,
        body: Mapping[str, Any],
        *,
        is_mutation: bool = False,
        session: FeishuWebSession | None = None,
    ) -> dict[str, Any]:
        """Execute a protobuf gateway command on ``POST /im/gateway/`` and decode its response."""
        s = self._require_session(session)
        cmd = command_of(name)
        payload = encode(cmd.request, body)
        packet = encode(
            "Packet",
            {
                "payloadType": 1,
                "cmd": cmd.cmd,
                "cid": str(uuid.uuid4()),
                "payload": base64.b64encode(payload).decode("ascii"),
            },
        )
        hdrs = self.headers(s)
        hdrs.update(
            {
                "content-type": "application/x-protobuf",
                "x-command": str(cmd.cmd),
                "x-command-version": "5.7.0",
                "x-request-id": str(uuid.uuid4()),
            }
        )
        url = f"https://internal-api-lark-api.{s.domain()}/im/gateway/"
        try:
            resp = await self._do_request("POST", url, headers=hdrs, content=packet)
        except (
            httpx.TimeoutException,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.DecodingError,
        ) as exc:
            if is_mutation:
                raise FeishuDeliveryUncertainError(
                    f"Feishu web gateway {name} transport failed after submit ({type(exc).__name__}); "
                    "delivery may have occurred"
                ) from exc
            raise

        raw = resp.content[: 32 << 20]
        if resp.status_code in {401, 403}:
            raise FeishuWebSignedOutError(
                f"feishu web session rejected for {name} (HTTP {resp.status_code})"
            )
        if resp.status_code != 200:
            raise FeishuWebGatewayError(
                f"web gateway HTTP {resp.status_code} for {name}",
                status_code=resp.status_code,
            )
        try:
            p = decode("Packet", raw)
        except Exception as exc:
            if is_mutation:
                raise FeishuDeliveryUncertainError(
                    f"Feishu web gateway {name} returned HTTP 200 with unreadable Packet; "
                    "delivery may have occurred"
                ) from exc
            raise FeishuWebGatewayError(f"decode Packet for {name} failed: {exc}") from exc

        st = _num(p.get("status"))
        if st != 0:
            raise FeishuWebGatewayError(
                f"web gateway status {st} for {name}",
                status_code=200,
                gateway_status=st,
            )
        pl_b64 = _str(p.get("payload"))
        try:
            inner_bytes = base64.b64decode(pl_b64.encode("ascii")) if pl_b64 else b""
            return decode(cmd.response, inner_bytes)
        except Exception as exc:
            if is_mutation:
                raise FeishuDeliveryUncertainError(
                    f"Feishu web gateway {name} returned HTTP 200 with unreadable {cmd.response}; "
                    "delivery may have occurred"
                ) from exc
            raise FeishuWebGatewayError(f"decode {cmd.response} for {name} failed: {exc}") from exc

    async def p2p(self, user_id: str, *, create: bool = False) -> str:
        """Return the web chat ID of the 1v1 DM with ``user_id`` (creating it if ``create=True``)."""
        out = await self.call("CheckP2PChatsExistByUserRequest", {"chatterIds": [user_id]})
        m = out.get("chatterChatMap")
        if isinstance(m, Mapping):
            cid = _str(m.get(user_id))
            if cid and cid != "0":
                return cid
        if not create:
            raise FeishuWebSessionError(f"no direct chat with {user_id}")
        out = await self.call(
            "PutChatRequest",
            {"type": 1, "chatterIds": [user_id]},
            is_mutation=True,
        )
        chat = out.get("chat")
        if isinstance(chat, Mapping):
            cid = _str(chat.get("id"))
            if cid:
                return cid
        raise FeishuWebSessionError("could not open a direct chat")

    async def chats(self, chat_ids: Sequence[str]) -> dict[str, WebChat]:
        ids = [str(i).strip() for i in chat_ids if str(i).strip()]
        if not ids:
            return {}
        out = await self.call("PullChatsByIdsRequest", {"chatIds": ids})
        res: dict[str, WebChat] = {}
        m = out.get("chats")
        if isinstance(m, Mapping):
            for cid, v in m.items():
                if isinstance(v, Mapping):
                    res[str(cid)] = chat_of(v)
        return res

    async def chatters(self, chatter_ids: Sequence[str]) -> dict[str, WebChatter]:
        ids = [str(i).strip() for i in chatter_ids if str(i).strip()]
        if not ids:
            return {}
        out = await self.call("PullChattersByIdsRequest", {"chatterIds": ids})
        res: dict[str, WebChatter] = {}
        m = out.get("chatters")
        if isinstance(m, Mapping):
            for uid, v in m.items():
                if isinstance(v, Mapping):
                    res[str(uid)] = WebChatter(
                        id=_str(v.get("id")),
                        name=_str(v.get("name")),
                        type=_num(v.get("type")),
                    )
        return res

    async def chat_members(self, chat_id: str, limit: int = 100) -> dict[str, WebChatter]:
        out = await self.call(
            "PullChatChattersRequest",
            {"chatId": chat_id, "needChatters": True, "limit": limit},
        )
        res: dict[str, WebChatter] = {}
        m = out.get("chatters")
        if isinstance(m, Mapping):
            for uid, v in m.items():
                if isinstance(v, Mapping):
                    res[str(uid)] = WebChatter(
                        id=_str(v.get("id")),
                        name=_str(v.get("name")),
                        type=_num(v.get("type")),
                    )
        return res

    async def feed_cards(
        self,
        *,
        feed_type: int = FEED_INBOX,
        parent: str = "",
        cursor: int = 0,
        count: int = 30,
    ) -> FeedPage:
        body: dict[str, Any] = {
            "type": feed_type,
            "pullType": 1,
            "count": max(1, min(count, 100)),
            "withMessageEntity": True,
        }
        if cursor > 0:
            body["pullType"] = 2
            body["cursor"] = str(cursor)
        if parent:
            body["parentCardId"] = parent
        out = await self.call("PullFeedCardsRequest", body)
        return feed_page_of(out)

    async def messages_at(self, chat_id: str, positions: Sequence[int]) -> list[WebMessage]:
        pos_list = [int(p) for p in positions if int(p) >= 0]
        if not pos_list:
            return []
        out = await self.call(
            "PullMessagesByPositionsRequest",
            {"chatId": chat_id, "positions": pos_list},
        )
        res: list[WebMessage] = []
        m = out.get("messages")
        if isinstance(m, Mapping):
            for v in m.values():
                if isinstance(v, Mapping):
                    res.append(message_of(v))
        res.sort(key=lambda item: item.position)
        return res

    async def messages_by_id(self, message_ids: Sequence[str]) -> list[WebMessage]:
        ids = [str(i).strip() for i in message_ids if str(i).strip()]
        if not ids:
            return []
        out = await self.call("PullMessagesByIdsRequest", {"messageIds": ids})
        res: list[WebMessage] = []
        m = out.get("messages")
        if isinstance(m, Mapping):
            for v in m.values():
                if isinstance(v, Mapping):
                    res.append(message_of(v))
        return res

    async def topics(self, thread_ids: Sequence[str]) -> dict[str, WebTopic]:
        ids = [str(i).strip() for i in thread_ids if str(i).strip()]
        if not ids:
            return {}
        out = await self.call("PullThreadsByIDsRequest", {"threadIds": ids})
        res: dict[str, WebTopic] = {}
        ts = out.get("threads")
        if isinstance(ts, Mapping):
            for tid, v in ts.items():
                if isinstance(v, Mapping):
                    last = _num(v["lastMessagePosition"]) if "lastMessagePosition" in v else -1
                    res[str(tid)] = WebTopic(
                        id=str(tid),
                        chat=_str(v.get("channelId")),
                        last=last,
                        start=_num(v.get("messageStartPosition")),
                        replies=_num(v.get("replyCount")),
                        latest_reply_s=_i64(v.get("latestReplyTime")),
                    )
        return res

    async def topic_messages(
        self, thread_id: str, chat_id: str, positions: Sequence[int]
    ) -> list[WebMessage]:
        pos_list = [int(p) for p in positions if int(p) >= 0]
        if not pos_list:
            return []
        out = await self.call(
            "PullThreadMessagesByPositionsRequest",
            {
                "threadId": thread_id,
                "positions": pos_list,
                "channelId": chat_id,
                "channelType": 1,
            },
        )
        res: list[WebMessage] = []
        ms = out.get("messages")
        if isinstance(ms, Mapping):
            for v in ms.values():
                if isinstance(v, Mapping):
                    m = message_of(v)
                    m = replace(m, position=_num(v.get("threadPosition")))
                    res.append(m)
        res.sort(key=lambda item: item.position)
        return res

    async def react(self, message_id: str, reaction_type: str = REACT_ON_IT) -> None:
        await self.call(
            "PutReactionRequest",
            {"messageId": message_id, "reactionType": reaction_type},
        )

    async def unreact(self, message_id: str, reaction_type: str = REACT_ON_IT) -> None:
        await self.call(
            "DeleteReactionRequest",
            {"messageId": message_id, "reactionType": reaction_type},
        )

    async def send(self, out: WebOut) -> WebMessage:
        """Send ``out`` as the signed-in user via ``PutMessageRequest``.

        Raises ``FeishuDeliveryUncertainError`` if post-submit transport fails or
        if the gateway returns HTTP 200 without a non-empty message ID.
        """
        is_post = bool(out.title or len(out.rows) > 1 or len(out.blocks) > 0)
        if not is_post and not out.rows:
            raise ValueError("nothing to send")
        if out.blocks:
            rich, mentions = rich_blocks(out.blocks)
        else:
            rich, mentions = rich_text(out.rows, is_post)

        if is_post:
            typ = TYPE_POST
            content: dict[str, Any] = {"title": out.title, "richText": rich}
        else:
            typ = TYPE_TEXT
            content = {"richText": rich}

        cid = out.cid or str(uuid.uuid4())
        body: dict[str, Any] = {
            "type": typ,
            "chatId": out.chat,
            "cid": cid,
            "version": 1,
            "isNotified": True,
            "content": content,
        }
        if mentions:
            body["atUserIdList"] = mentions
        if out.reply_to is not None:
            root = out.reply_to.root_id or out.reply_to.id
            body["parentId"] = out.reply_to.id
            body["rootId"] = root
            body["isReplyInThread"] = bool(out.in_thread)

        res = await self.call("PutMessageRequest", body, is_mutation=True)
        raw_msg = res.get("message")
        if not isinstance(raw_msg, Mapping):
            raise FeishuDeliveryUncertainError(
                "Feishu web PutMessageRequest returned no message; delivery may have occurred"
            )
        msg = message_of(raw_msg)
        if not msg.id.strip():
            raise FeishuDeliveryUncertainError(
                "Feishu web PutMessageRequest message identity is unavailable; delivery may have occurred"
            )
        return msg


class FeishuWebFrontierListener:
    """Maintains the Feishu web client's ``msg-frontier`` WebSocket long connection (`push.go`)."""

    def __init__(
        self,
        client: FeishuWebClient,
        deliver: Callable[[PushedMessage], Awaitable[None] | None],
        *,
        on_state: Callable[[Exception | None], None] | None = None,
    ) -> None:
        self._client = client
        self._deliver = deliver
        self._on_state = on_state
        self._app_key = ""

    async def ws_url(self) -> str:
        s = self._client._require_session()
        dom = s.domain()
        key = self._app_key or s.app_key
        if not key:
            page_bytes, _ = await self._client.page(f"https://open-dev.{dom}/messenger/", session=s)
            m = _APP_KEY_RE.search(page_bytes) or _APP_KEY_FALLBACK_RE.search(page_bytes)
            if m is None:
                if not await self._client.valid(session=s):
                    raise FeishuWebSignedOutError(
                        "no appKey on the messenger page (session invalid)"
                    )
                raise FeishuWebSessionError("no appKey on the messenger page")
            key = m.group(1).decode("utf-8", errors="ignore")
            self._app_key = key

        ticket = await self._client.ticket(session=s)
        device = s.effective_device_id()
        access_key = compute_frontier_access_key(key, device)
        alphabet = string.ascii_lowercase + string.digits
        rid = "".join(random.choice(alphabet) for _ in range(10))
        params = {
            "accept_encoding": "gzip",
            "sdk_version": "7.72.8",
            "version_code": "7.72.8",
            "type": "sdk_main",
            "platform": "",
            "terminal_type": "2",
            "device_model": "",
            "lark_version": "7.72.0",
            "net_status": "5",
            "lark_env": "online",
            "request_id": rid,
            "aid": "1",
            "fpid": "2",
            "device_id": device,
            "access_key": access_key,
            "ticket": ticket,
        }
        return f"wss://msg-frontier.{dom}/ws/v2?{urlencode(params)}"

    async def handle_raw_frame(
        self, raw: bytes, send_binary: Callable[[bytes], Awaitable[None]]
    ) -> None:
        ack_bytes, _cmd, pushed_list = parse_frontier_frame(raw)
        if ack_bytes is not None:
            try:
                await send_binary(ack_bytes)
            except Exception:
                pass
        for pushed in pushed_list:
            res = self._deliver(pushed)
            if asyncio.iscoroutine(res):
                await res

    async def _once(self, stop_event: asyncio.Event) -> None:
        import websockets

        url = await self.ws_url()
        async with websockets.connect(
            url,
            additional_headers={"User-Agent": UA},
            open_timeout=20.0,
            max_size=64 * 1024 * 1024,
        ) as ws:
            if self._on_state is not None:
                self._on_state(None)

            async def _heartbeat() -> None:
                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=30.0)
                        return
                    except asyncio.TimeoutError:
                        hb = build_frontier_frame({"cmd": CMD_HEARTBEAT, "payloadType": 1})
                        await ws.send(hb)

            hb_task = asyncio.create_task(_heartbeat())
            try:
                while not stop_event.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=180.0)
                    if isinstance(raw, bytes):
                        await self.handle_raw_frame(raw, ws.send)
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        backoff = 2.0
        while not stop.is_set():
            started = time.monotonic()
            try:
                await self._once(stop)
            except asyncio.CancelledError:
                raise
            except FeishuWebSignedOutError as exc:
                if self._on_state is not None:
                    self._on_state(exc)
                raise
            except Exception as exc:
                if self._on_state is not None:
                    self._on_state(exc)
                if time.monotonic() - started > 60.0:
                    backoff = 2.0
                self._app_key = ""
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, 120.0)
