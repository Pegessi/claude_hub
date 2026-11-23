"""Real-time Feishu WebSocket event triage, courtesy/self-reply suppression, and Dot event bridge.

Hooks into the personal web-session ``msg-frontier`` stream without interfering
with ``/ai`` command parsing:
1. Non-self ``@current_user`` mentions in any group or P2P chat trigger an
   immediate ``urgent`` event.
2. Non-self messages in configured priority watch groups that match configured
   fault/task URL patterns trigger an immediate ``urgent`` event even without ``@me``.
3. Courtesy-only acknowledgements and topics/chats where the current user has
   already replied after the trigger message are silently suppressed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import HttpUrl, JsonValue

from claude_hub.services.dot.events_gateway import (
    ExternalEvent,
    record_suppressed_external_event,
    submit_external_event,
)
from claude_hub.services.feishu_user_client import FeishuUserMessage
from claude_hub.services.feishu_web_session.adapter import is_in_topic
from claude_hub.services.feishu_web_session.client import PushedMessage
from claude_hub.services.feishu_web_session.codec import decode
from claude_hub.services.feishu_web_session.rich import (
    CHAT_P2P,
    TAG_A,
    TAG_AT,
    TAG_LINK,
    TAG_TEXT,
    TYPE_CARD,
    TYPE_POST,
    TYPE_SYSTEM,
    TYPE_TEXT,
    WebMessage,
    _num,
    _str,
    describe,
    text_of,
)
from claude_hub.services.runtime_isolation import resolve_runtime_home

logger = logging.getLogger(__name__)

_MAX_HANDLED_LEDGER = 1024
_MAX_REPLY_TRACKER = 1024
_CROSS_TRANSPORT_MAX_DELTA_MS = 180_000
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"')\]}]+", re.IGNORECASE)
_URL_TRAILER_CHARS = ".,;:!?。，；：！？、)】》>\"'"
_HTML_AT_TAG_RE = re.compile(r"<at\b[^>]*>.*?</at>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_AT_MENTION_RE = re.compile(
    r"@[\w\u4e00-\u9fff·._-]{1,30}"
    r"(?:\s+(?!(?:ok|okay|k+|got|roger|ack\w*|thanks?|thank|thx|tks|ty|np|no|done|"
    r"resolved|fixed|lgtm|oom|error\w*|crash\w*|hang\w*|fail\w*|bug\w*|issue\w*|"
    r"timeout\w*|why|how|help|check|investigate)\b)[A-Z][A-Za-z._-]{1,25}){0,2}"
)
_BRACKET_EMOJI_RE = re.compile(
    r"\[(?:赞|强|抱拳|玫瑰|感谢|比心|握手|OK|ok|好的|收到|加油|微笑|鼓掌|666|给力|salute|thumbsup|thanks)\]",
    re.IGNORECASE,
)
_COURTESY_EMOJI_RE = re.compile(r"[👍🙏👌🌹❤️🎉✅🤝👏💪🫡]+")
_PUNCT_AND_SPACE_RE = re.compile(r"[\s,.;:!?~，。；：！？、～·\"'‘’“”()（）【】\[\]\-_—/\\]+")

_SUBSTANTIVE_ISSUE_RE = re.compile(
    r"(?:报错|失败|挂了|卡住|超时|异常|不对|不正常|不行|不能|无法|求看|看看|看下|看一下|瞅瞅|"
    r"排查|检查|分析|定位|为什么|怎么|啥原因|什么原因|是不是|能否|能不能|请问|再看|"
    r"\b(?:oom|error\w*|crash\w*|hang\w*|fail\w*|bug\w*|issue\w*|timeout\w*|"
    r"why|how|help|check|investigate)\b)",
    re.IGNORECASE,
)

_COURTESY_ATOM = (
    r"(?:收到(?:了|啦)?|好的(?:呀|呢|啦)?|好滴|好嘞|好哒|好|明白(?:了)?|了解(?:了)?|"
    r"知道了|清楚了|晓得了|没问题(?:了)?|可以(?:的|了)?|行(?:的|了)?|中|"
    r"谢谢(?:你|您|啦|哈|老板|大佬|哥|姐|老师|大家)?|多谢(?:啦|哈|大佬|老师)?|"
    r"感谢(?:感谢|大佬|老师|各位|大家)?|谢啦|谢了|"
    r"辛苦(?:了|啦|你了|大佬|老师|各位|大家)?|麻烦(?:了|你了|大佬|老师)?|打扰了|抱歉打扰|"
    r"已(?:经)?(?:解决|恢复|修好|修复|搞定|处理(?:好)?|ok)(?:了|啦)?|"
    r"不用(?:看|查|管|处理|麻烦)(?:了|啦)?|没事(?:了|啦)?|恢复正常(?:了)?|"
    r"我(?:已(?:经)?|先)?(?:处理|修好|解决|搞定|恢复)(?:了|啦)?|"
    r"ok(?:ay)?|k+|got\s*it|roger|ack(?:nowledged)?|thanks?(?:\s+a\s+lot|\s+very\s+much)?|"
    r"thank\s*you|thx|tks|ty|np|no\s*problem|done|resolved|fixed|lgtm|\+?1)"
)
_COURTESY_FILLER = r"(?:了|啦|哈|呀|呢|哦|喔|噢|呗|先|那|我先|这边|目前|也|都)"
_COURTESY_FULL_RE = re.compile(
    rf"^(?:{_COURTESY_FILLER}*{_COURTESY_ATOM}{_COURTESY_FILLER}*){{1,5}}$",
    re.IGNORECASE,
)

RealtimeEventSink = Callable[[ExternalEvent], Awaitable[bool | Mapping[str, Any] | None]]


def _valid_http_url(value: str) -> str:
    candidate = (value or "").strip().rstrip(_URL_TRAILER_CHARS)
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return ""
    return candidate


def is_courtesy_or_ack_only(text: str) -> bool:
    """Return True when ``text`` is only a polite closing or simple confirmation."""
    raw = (text or "").strip()
    if not raw:
        return True
    # Any message carrying a valid HTTP(S) link is never a pure courtesy ack.
    for match in _HTTP_URL_RE.finditer(raw):
        if _valid_http_url(match.group(0)):
            return False

    cleaned = _HTML_AT_TAG_RE.sub(" ", raw)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = _INLINE_AT_MENTION_RE.sub(" ", cleaned)
    if _SUBSTANTIVE_ISSUE_RE.search(cleaned):
        return False

    had_courtesy_emoji = bool(
        _BRACKET_EMOJI_RE.search(cleaned) or _COURTESY_EMOJI_RE.search(cleaned)
    )
    cleaned = _BRACKET_EMOJI_RE.sub(" ", cleaned)
    cleaned = _COURTESY_EMOJI_RE.sub(" ", cleaned)
    compact = _PUNCT_AND_SPACE_RE.sub("", cleaned).strip()
    if not compact:
        return had_courtesy_emoji
    if len(compact) > 48:
        return False
    return bool(_COURTESY_FULL_RE.fullmatch(compact))


def _iter_richtext_nodes(typ: int, content: bytes) -> list[Mapping[str, Any]]:
    if not content or typ not in {TYPE_TEXT, TYPE_POST, TYPE_CARD}:
        return []
    schema = (
        "lark_entities_TextContent"
        if typ == TYPE_TEXT
        else "lark_entities_PostContent" if typ == TYPE_POST else "lark_entities_CardContent"
    )
    try:
        decoded = decode(schema, content)
    except Exception:
        return []
    rich = decoded.get("richText")
    if not isinstance(rich, Mapping):
        return []
    elements = rich.get("elements")
    dictionary = elements.get("dictionary") if isinstance(elements, Mapping) else None
    if not isinstance(dictionary, Mapping):
        return []

    ordered_keys: list[str] = []
    for root_field in ("elementIds", "atIds", "anchorIds"):
        raw_ids = rich.get(root_field)
        if isinstance(raw_ids, list):
            ordered_keys.extend(_str(k) for k in raw_ids if _str(k))
    ordered_keys.extend(_str(k) for k in dictionary if _str(k))

    visited: set[str] = set()
    nodes: list[Mapping[str, Any]] = []

    def visit(key: str, depth: int) -> None:
        if depth > 25 or not key or key in visited:
            return
        visited.add(key)
        node = dictionary.get(key)
        if not isinstance(node, Mapping):
            return
        nodes.append(node)
        kids = node.get("childIds")
        if isinstance(kids, list):
            for child in kids:
                visit(_str(child), depth + 1)

    for root_key in ordered_keys:
        visit(root_key, 0)
    return nodes


def extract_mentioned_user_ids(typ: int, content: bytes) -> tuple[str, ...]:
    """Extract mentioned user IDs from a Feishu web protocol message's richText."""
    mentioned: list[str] = []
    for node in _iter_richtext_nodes(typ, content):
        raw_prop = _str(node.get("property")) if _num(node.get("tag")) == TAG_AT else ""
        if not raw_prop:
            continue
        try:
            prop = decode(
                "lark_entities_RichTextElement_AtProperty",
                base64.b64decode(raw_prop.encode("ascii")),
            )
            uid = _str(prop.get("userId")).strip()
            if uid and uid not in mentioned:
                mentioned.append(uid)
        except Exception:
            continue
    return tuple(mentioned)


def extract_message_urls(typ: int, content: bytes, text: str = "") -> tuple[str, ...]:
    """Extract all valid HTTP(S) URLs from richText anchors and message text."""
    urls: list[str] = []

    def add_url(raw_url: str) -> None:
        valid = _valid_http_url(raw_url)
        if valid and valid not in urls:
            urls.append(valid)

    for node in _iter_richtext_nodes(typ, content):
        tag = _num(node.get("tag"))
        raw_prop = _str(node.get("property"))
        if not raw_prop or tag not in {TAG_A, TAG_TEXT, TAG_LINK}:
            continue
        schema = (
            "lark_entities_RichTextElement_AnchorProperty"
            if tag == TAG_A
            else "lark_entities_RichTextElement_TextProperty"
        )
        try:
            prop = decode(schema, base64.b64decode(raw_prop.encode("ascii")))
            if tag == TAG_A:
                add_url(_str(prop.get("href")))
            for match in _HTTP_URL_RE.finditer(_str(prop.get("content"))):
                add_url(match.group(0))
        except Exception:
            pass

    combined_text = "\n".join(filter(None, (text, text_of(typ, content) if content else "")))
    for match in _HTTP_URL_RE.finditer(combined_text):
        add_url(match.group(0))
    return tuple(urls)


def _read_runtime_json_documents() -> list[Mapping[str, Any]]:
    home = resolve_runtime_home()
    docs: list[Mapping[str, Any]] = []
    for filename in ("feishu_realtime_events.json", "local_runtime_settings.json"):
        try:
            data = json.loads((home / filename).read_text(encoding="utf-8"))
            if isinstance(data, Mapping):
                docs.append(data)
        except (OSError, UnicodeError, ValueError):
            continue
    return docs


def _setting_values(
    names: Sequence[str],
    docs: Sequence[Mapping[str, Any]],
    *,
    split_commas: bool = True,
) -> list[str]:
    values: list[str] = []

    def extend_raw(raw: Any, *, from_list_item: bool = False) -> None:
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return
            if not from_list_item and text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    parsed = None
                if isinstance(parsed, list):
                    for item in parsed:
                        extend_raw(item, from_list_item=True)
                    return
            if from_list_item:
                values.append(text)
                return
            parts = re.split(r"[,\n]+", text) if split_commas else text.splitlines()
            values.extend(p.strip() for p in parts if p.strip())
        elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
            for item in raw:
                extend_raw(item, from_list_item=True)

    for name in names:
        if name in os.environ:
            extend_raw(os.environ[name])
    for doc in docs:
        for name in names:
            if name in doc:
                extend_raw(doc[name])
    return values


def _setting_bool(
    names: Sequence[str], docs: Sequence[Mapping[str, Any]], *, default: bool
) -> bool:
    sources: list[Any] = [os.environ[n] for n in names if n in os.environ]
    sources.extend(doc[n] for doc in docs for n in names if n in doc)
    for val in sources:
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            raw = val.strip().lower()
            if raw in {"1", "true", "yes", "on"}:
                return True
            if raw in {"0", "false", "no", "off"}:
                return False
    return default


@dataclass(frozen=True)
class FeishuRealtimeEventConfig:
    """Runtime-configurable watch groups and fault/task URL rules (zero hardcoded IDs/domains)."""

    enabled: bool = True
    watch_chat_ids: frozenset[str] = frozenset()
    watch_chat_names: frozenset[str] = frozenset()
    url_patterns: tuple[str, ...] = ()
    actionable_hosts: frozenset[str] = frozenset()
    automated_chat_ids: frozenset[str] = frozenset()
    events_url: str = ""
    suppress_patrol_when_healthy: bool = False

    @classmethod
    def from_runtime(cls) -> FeishuRealtimeEventConfig:
        docs = _read_runtime_json_documents()
        enabled = _setting_bool(
            ("CLAUDE_HUB_FEISHU_REALTIME_EVENTS_ENABLED", "enabled"), docs, default=True
        )
        suppress_patrol = _setting_bool(
            ("CLAUDE_HUB_FEISHU_SUPPRESS_PATROL_WHEN_HEALTHY", "suppress_patrol_when_healthy"),
            docs,
            default=False,
        )
        watch_ids = frozenset(
            item.casefold()
            for item in _setting_values(
                (
                    "CLAUDE_HUB_FEISHU_WATCH_CHAT_IDS",
                    "CLAUDE_HUB_FEISHU_REALTIME_WATCH_CHAT_IDS",
                    "CLAUDE_HUB_PATROL_WATCH_CHAT_IDS",
                    "CLAUDE_HUB_FEISHU_PRIORITY_CHAT_IDS",
                    "watch_chat_ids",
                ),
                docs,
            )
        )
        watch_names = frozenset(
            " ".join(item.split()).casefold()
            for item in _setting_values(
                (
                    "CLAUDE_HUB_FEISHU_WATCH_CHAT_NAMES",
                    "CLAUDE_HUB_FEISHU_REALTIME_WATCH_CHAT_NAMES",
                    "CLAUDE_HUB_PATROL_WATCH_CHAT_NAMES",
                    "watch_chat_names",
                ),
                docs,
            )
        )
        url_patterns = tuple(
            dict.fromkeys(
                _setting_values(
                    (
                        "CLAUDE_HUB_FEISHU_WATCH_URL_REGEX",
                        "CLAUDE_HUB_FEISHU_REALTIME_URL_PATTERNS",
                        "CLAUDE_HUB_FEISHU_URL_PATTERNS",
                        "CLAUDE_HUB_PATROL_ACTIONABLE_URL_REGEX",
                        "url_patterns",
                        "watch_url_regex",
                    ),
                    docs,
                    split_commas=False,
                )
            )
        )
        actionable_hosts = frozenset(
            item.casefold()
            for item in _setting_values(
                (
                    "CLAUDE_HUB_FEISHU_REALTIME_URL_HOSTS",
                    "CLAUDE_HUB_PATROL_ACTIONABLE_HOSTS",
                    "actionable_hosts",
                ),
                docs,
            )
        )
        automated_chat_ids = frozenset(
            item.casefold()
            for item in _setting_values(
                ("CLAUDE_HUB_PATROL_AUTOMATED_CHAT_IDS", "automated_chat_ids"), docs
            )
        )
        events_urls = _setting_values(("CLAUDE_HUB_FEISHU_REALTIME_EVENTS_URL", "events_url"), docs)
        return cls(
            enabled=enabled,
            watch_chat_ids=watch_ids,
            watch_chat_names=watch_names,
            url_patterns=url_patterns,
            actionable_hosts=actionable_hosts,
            automated_chat_ids=automated_chat_ids,
            events_url=events_urls[0] if events_urls else "",
            suppress_patrol_when_healthy=suppress_patrol,
        )


def matches_fault_or_task_url(
    url: str,
    config: FeishuRealtimeEventConfig | None = None,
) -> bool:
    """Return True when ``url`` matches configured fault/task URL regexes or hosts."""
    valid = _valid_http_url(url)
    if not valid:
        return False
    cfg = config or FeishuRealtimeEventConfig.from_runtime()
    parsed = urlsplit(valid)
    host = (parsed.hostname or "").casefold()

    for pattern in cfg.url_patterns:
        clean_pat = pattern.strip()
        if not clean_pat:
            continue
        try:
            if re.search(clean_pat, valid, re.IGNORECASE):
                return True
        except re.error:
            if clean_pat.casefold() in valid.casefold():
                return True

    for allowed_host in cfg.actionable_hosts:
        if not allowed_host:
            continue
        if (
            host == allowed_host
            or host.endswith("." + allowed_host)
            or allowed_host in valid.casefold()
        ):
            return True
    return False


def matches_watch_group_url(
    chat_id: str,
    chat_name: str,
    text: str,
    config: FeishuRealtimeEventConfig | None = None,
    *,
    extra_urls: Sequence[str] = (),
    extra_chat_ids: Sequence[str] = (),
    message_id: str = "",
) -> list[str]:
    """Return matched fault/task URLs if the chat is a configured priority watch group."""
    router = _DEFAULT_REALTIME_ROUTER
    cfg = config or (
        router.resolve_config() if router is not None else FeishuRealtimeEventConfig.from_runtime()
    )
    candidate_ids = {
        cid.strip().casefold()
        for cid in (chat_id, *extra_chat_ids)
        if isinstance(cid, str) and cid.strip()
    }
    if router is not None:
        for cid in list(candidate_ids):
            candidate_ids.update(router.expand_chat_keys(cid))
    normalized_name = " ".join((chat_name or "").split()).casefold()
    in_watch_group = bool(candidate_ids & cfg.watch_chat_ids) or (
        bool(normalized_name) and normalized_name in cfg.watch_chat_names
    )
    if not in_watch_group and router is not None:
        in_watch_group = router.matches_pending_failed_watch_delivery(
            message_id=message_id,
            chat_ids=candidate_ids,
            chat_name=normalized_name,
            text=text,
        )
    if not in_watch_group:
        return []

    urls: list[str] = list(extra_urls)
    for match in _HTTP_URL_RE.finditer(text or ""):
        valid = _valid_http_url(match.group(0))
        if valid and valid not in urls:
            urls.append(valid)

    return [u for u in urls if matches_fault_or_task_url(u, cfg)]


def _content_fingerprint(text: str) -> str:
    cleaned = _HTML_AT_TAG_RE.sub(" ", text or "")
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = _INLINE_AT_MENTION_RE.sub(" ", cleaned)
    normalized = " ".join(cleaned.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class _OwnerReplyMark:
    create_time_ms: int
    position: int
    topic_position: int
    message_id: str


@dataclass
class _CrossTransportRecord:
    web_msg_id: str
    canonical_msg_id: str
    chat_keys: frozenset[str]
    thread_id: str
    sender_ids: frozenset[str]
    content_hash: str
    created_at_ms: int
    recorded_at: float


@dataclass
class _FallbackRecord:
    item_id: str
    web_msg_id: str
    alias_id: str
    kind: str  # "failed_delivery" | "uncovered_message"
    generation: int
    chat_keys: frozenset[str]
    chat_name: str
    thread_id: str
    sender_id: str
    text: str
    content_hash: str
    created_at_ms: int
    matched_urls: tuple[str, ...] = ()
    event: ExternalEvent | None = None


def _reply_mark_is_newer_or_equal(candidate: _OwnerReplyMark, existing: _OwnerReplyMark) -> bool:
    if candidate.create_time_ms != existing.create_time_ms:
        return candidate.create_time_ms > existing.create_time_ms
    if candidate.position != existing.position and candidate.position > 0 and existing.position > 0:
        return candidate.position >= existing.position
    if (
        candidate.topic_position != existing.topic_position
        and candidate.topic_position > 0
        and existing.topic_position > 0
    ):
        return candidate.topic_position >= existing.topic_position
    return True


def _is_newer_reply(
    cand_ms: int,
    cand_pos: int,
    cand_tpos: int,
    *,
    target_ms: int,
    target_pos: int,
    target_tpos: int,
    direct_anchor: bool = False,
) -> bool:
    if cand_ms > target_ms > 0:
        return True
    if cand_ms == target_ms and target_ms > 0:
        if target_pos > 0 and cand_pos > 0:
            return cand_pos > target_pos
        if target_tpos > 0 and cand_tpos > 0:
            return cand_tpos > target_tpos
        return direct_anchor
    return False


def _parse_created_ms(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num if num > 100_000_000_000 else num * 1000)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.replace(".", "", 1).isdigit():
            num = float(raw)
            return int(num if num > 100_000_000_000 else num * 1000)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


class FeishuRealtimeEventRouter:
    """Evaluates real-time WebSocket messages and routes urgent events to Dot."""

    def __init__(
        self,
        *,
        config: FeishuRealtimeEventConfig | None = None,
        event_sink: RealtimeEventSink | None = None,
    ) -> None:
        self._explicit_config = config
        self._event_sink = event_sink
        self._lock = threading.RLock()
        self._handled_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._cross_transport_records: OrderedDict[str, _CrossTransportRecord] = OrderedDict()
        self._owner_topic_replies: OrderedDict[tuple[str, str], _OwnerReplyMark] = OrderedDict()
        self._owner_chat_replies: OrderedDict[str, _OwnerReplyMark] = OrderedDict()
        self._owner_parent_replies: OrderedDict[str, _OwnerReplyMark] = OrderedDict()
        self._known_chat_aliases: dict[str, set[str]] = {}
        self._pending_fallbacks: OrderedDict[str, _FallbackRecord] = OrderedDict()
        self._fallback_generation = 0
        self._emitted_count = 0
        self._suppressed_count = 0
        self._suppressed_courtesy_count = 0
        self._suppressed_replied_count = 0
        self._delivery_failed_count = 0
        self._last_delivery_error: str | None = None

    @property
    def emitted_count(self) -> int:
        return self._emitted_count

    @property
    def suppressed_count(self) -> int:
        return self._suppressed_count

    @property
    def suppressed_courtesy_count(self) -> int:
        return self._suppressed_courtesy_count

    @property
    def suppressed_replied_count(self) -> int:
        return self._suppressed_replied_count

    @property
    def delivery_failed_count(self) -> int:
        return self._delivery_failed_count

    @property
    def pending_fallback_count(self) -> int:
        return len(self._pending_fallbacks)

    @property
    def fallback_generation(self) -> int:
        return self._fallback_generation

    @property
    def last_delivery_error(self) -> str | None:
        return self._last_delivery_error

    @property
    def needs_patrol_fallback(self) -> bool:
        return bool(self._pending_fallbacks)

    def _add_fallback_record(
        self,
        *,
        item_id: str,
        web_msg_id: str,
        alias_id: str = "",
        kind: str,
        chat_name: str,
        chat_keys: set[str],
        thread_id: str,
        sender_id: str,
        text: str,
        created_at_ms: int,
        matched_urls: Sequence[str] = (),
        event: ExternalEvent | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            if error is not None:
                self._delivery_failed_count += 1
                self._last_delivery_error = error
            self._fallback_generation += 1
            self._pending_fallbacks[item_id] = _FallbackRecord(
                item_id,
                web_msg_id,
                alias_id,
                kind,
                self._fallback_generation,
                frozenset(chat_keys),
                chat_name,
                thread_id,
                sender_id,
                text,
                _content_fingerprint(text),
                created_at_ms,
                tuple(matched_urls),
                event,
            )

    def expand_chat_keys(self, chat_id: str) -> set[str]:
        clean = (chat_id or "").strip().casefold()
        with self._lock:
            return ({clean} | set(self._known_chat_aliases.get(clean, ()))) if clean else set()

    def matches_pending_failed_watch_delivery(
        self,
        *,
        message_id: str = "",
        chat_ids: set[str] | None = None,
        chat_name: str = "",
        text: str = "",
    ) -> bool:
        clean_mid = (message_id or "").strip()
        fp_hash = _content_fingerprint(text) if text.strip() else ""
        with self._lock:
            return any(
                rec.kind == "failed_delivery"
                and rec.matched_urls
                and (
                    (clean_mid and clean_mid in {rec.item_id, rec.web_msg_id, rec.alias_id})
                    or (chat_ids and (chat_ids & rec.chat_keys))
                    or (chat_name and rec.chat_name and chat_name == rec.chat_name.casefold())
                    or (fp_hash and rec.content_hash == fp_hash)
                )
                for rec in self._pending_fallbacks.values()
            )

    def acknowledge_patrol_fallback(
        self,
        *,
        recovered_ids: Sequence[str] | None = None,
        recovered_candidates: Sequence[Any] = (),
        seen_ids: Sequence[str] = (),
        window_start_ms: int | None = None,
        window_end_ms: int | None = None,
        max_generation: int | None = None,
        all_candidates_completed: bool = True,
    ) -> None:
        """Clear only recovered/covered fallback items within the scanned generation/window."""
        with self._lock:
            if (
                recovered_ids is None
                and not recovered_candidates
                and not seen_ids
                and window_start_ms is None
                and window_end_ms is None
                and max_generation is None
            ):
                self._pending_fallbacks.clear()
                self._last_delivery_error = None
                return
            rec_id_set = {str(mid).strip() for mid in (recovered_ids or ()) if str(mid).strip()}
            seen_id_set = {str(mid).strip() for mid in seen_ids if str(mid).strip()} | rec_id_set
            for key, rec in list(self._pending_fallbacks.items()):
                if max_generation is not None and rec.generation > max_generation:
                    continue
                if (
                    window_start_ms is not None
                    and window_end_ms is not None
                    and rec.created_at_ms > 0
                    and not (window_start_ms <= rec.created_at_ms <= window_end_ms)
                ):
                    continue
                direct = bool({rec.item_id, rec.web_msg_id, rec.alias_id} & rec_id_set - {""})
                matched_alias = rec.alias_id or rec.item_id
                if not direct:
                    for cand in recovered_candidates:
                        cand_dt = getattr(cand, "created_at", None)
                        cand_ms = int(cand_dt.timestamp() * 1000) if cand_dt is not None else 0
                        if (
                            str(getattr(cand, "thread_id", "") or "").strip() == rec.thread_id
                            and _content_fingerprint(str(getattr(cand, "text", "") or ""))
                            == rec.content_hash
                            and (
                                rec.created_at_ms <= 0
                                or cand_ms <= 0
                                or abs(cand_ms - rec.created_at_ms) <= _CROSS_TRANSPORT_MAX_DELTA_MS
                            )
                        ):
                            direct = True
                            matched_alias = (
                                str(getattr(cand, "message_id", "") or "").strip() or matched_alias
                            )
                            break
                if direct:
                    self._record_handled(
                        rec.web_msg_id or rec.item_id,
                        alias_id=matched_alias,
                        chat_keys=set(rec.chat_keys),
                        thread_id=rec.thread_id,
                        sender_ids={rec.sender_id.casefold()} if rec.sender_id else None,
                        text=rec.text,
                        created_at_ms=rec.created_at_ms,
                    )
                    self._pending_fallbacks.pop(key, None)
                elif rec.kind == "uncovered_message" and all_candidates_completed:
                    if not seen_id_set or (
                        {rec.item_id, rec.web_msg_id, rec.alias_id} & seen_id_set - {""}
                    ):
                        self._pending_fallbacks.pop(key, None)
            if not self._pending_fallbacks:
                self._last_delivery_error = None

    def resolve_config(self) -> FeishuRealtimeEventConfig:
        if self._explicit_config is not None:
            return self._explicit_config
        return FeishuRealtimeEventConfig.from_runtime()

    def _chat_keys(self, chat_id: str, web_transport: Any = None) -> set[str]:
        keys = {chat_id.strip().casefold()} if chat_id and chat_id.strip() else set()
        if web_transport is not None and chat_id:
            open_map = getattr(web_transport, "_web_to_open_chat", {})
            web_map = getattr(web_transport, "_open_to_web_chat", {})
            chat_cache = getattr(web_transport, "_chat_cache", {})
            if isinstance(open_map, Mapping) and chat_id in open_map:
                keys.add(str(open_map[chat_id]).strip().casefold())
            if isinstance(web_map, Mapping) and chat_id in web_map:
                keys.add(str(web_map[chat_id]).strip().casefold())
            if isinstance(chat_cache, Mapping) and chat_id in chat_cache:
                cached = chat_cache[chat_id]
                open_cid = getattr(cached, "open_chat_id", "")
                if open_cid:
                    keys.add(str(open_cid).strip().casefold())
        keys.discard("")
        if len(keys) > 1:
            with self._lock:
                for k in keys:
                    self._known_chat_aliases.setdefault(k, set()).update(keys)
        return keys

    def _record_handled(
        self,
        message_id: str,
        *,
        alias_id: str | None = None,
        chat_keys: set[str] | None = None,
        thread_id: str = "",
        sender_ids: set[str] | None = None,
        text: str = "",
        created_at_ms: int = 0,
    ) -> None:
        now = time.time()
        clean_mid = (message_id or "").strip()
        clean_alias = (alias_id or "").strip()
        clean_tid = (thread_id or "").strip()
        norm_senders = frozenset(
            s.strip().casefold() for s in (sender_ids or set()) if s and s.strip()
        )
        with self._lock:
            for mid in (clean_mid, clean_alias):
                if mid:
                    self._handled_msg_ids[mid] = now
                    self._handled_msg_ids.move_to_end(mid)
                    self._pending_fallbacks.pop(mid, None)
            while len(self._handled_msg_ids) > _MAX_HANDLED_LEDGER:
                self._handled_msg_ids.popitem(last=False)

            if clean_mid and text.strip():
                rec = _CrossTransportRecord(
                    web_msg_id=clean_mid,
                    canonical_msg_id=clean_alias or clean_mid,
                    chat_keys=frozenset(chat_keys or ()),
                    thread_id=clean_tid,
                    sender_ids=norm_senders,
                    content_hash=_content_fingerprint(text),
                    created_at_ms=created_at_ms,
                    recorded_at=now,
                )
                self._cross_transport_records[clean_mid] = rec
                self._cross_transport_records.move_to_end(clean_mid)
                while len(self._cross_transport_records) > _MAX_HANDLED_LEDGER:
                    self._cross_transport_records.popitem(last=False)

    def was_dispatched(self, message_id: str) -> bool:
        """Return True when ``message_id`` (or its known alias) was already handled."""
        return self.is_message_handled(message_id)

    def is_message_handled(
        self,
        message_id: str,
        *,
        chat_id: str = "",
        thread_id: str = "",
        sender_id: str = "",
        text: str = "",
        created_at_ms: int = 0,
    ) -> bool:
        clean_mid = (message_id or "").strip()
        clean_tid = (thread_id or "").strip()
        clean_sid = (sender_id or "").strip().casefold()
        with self._lock:
            if clean_mid and clean_mid in self._handled_msg_ids:
                return True
            # Cross-transport bridge (OpenAPI om_... vs Web numeric ID):
            # Require non-conflicting canonical alias, non-empty chat match, identical thread/topic,
            # matching sender (when known), and identical second-precision creation timestamp.
            if clean_mid.startswith("om_") and text.strip():
                ck = chat_id.strip().casefold() if chat_id else ""
                if not ck or created_at_ms <= 0:
                    return False
                fp_hash = _content_fingerprint(text)
                for rec in reversed(self._cross_transport_records.values()):
                    if rec.web_msg_id.startswith("om_"):
                        continue
                    if rec.canonical_msg_id.startswith("om_") and rec.canonical_msg_id != clean_mid:
                        continue
                    if rec.content_hash != fp_hash:
                        continue
                    if not rec.chat_keys or ck not in rec.chat_keys:
                        continue
                    if rec.thread_id != clean_tid:
                        continue
                    if clean_sid and rec.sender_ids and clean_sid not in rec.sender_ids:
                        continue
                    if rec.created_at_ms <= 0 or (created_at_ms // 1000) != (
                        rec.created_at_ms // 1000
                    ):
                        continue
                    rec.canonical_msg_id = clean_mid
                    self._handled_msg_ids[clean_mid] = rec.recorded_at
                    return True
        return False

    def should_suppress_openapi_mention(self, message: Mapping[str, Any], own_id: str) -> bool:
        """Shared suppression gate for OpenAPI mention pollers (`events_bridge.poll_mentions`)."""
        msg_id = str(message.get("message_id") or "").strip()
        chat_id = str(message.get("chat_id") or "").strip()
        thread_id = str(message.get("thread_id") or message.get("root_id") or "").strip()
        reply_to = str(message.get("reply_to") or message.get("parent_id") or "").strip()
        raw_content = message.get("content", message.get("text", ""))
        text = (
            str(raw_content.get("text") or raw_content.get("content") or "")
            if isinstance(raw_content, Mapping)
            else str(raw_content or "")
        )
        created_ms = _parse_created_ms(message.get("create_time"))
        return bool(
            is_courtesy_or_ack_only(text)
            or self.is_message_handled(
                msg_id, chat_id=chat_id, thread_id=thread_id, text=text, created_at_ms=created_ms
            )
            or self.has_owner_replied(
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to=reply_to,
                message_id=msg_id,
                created_at_ms=created_ms,
                owner_ids={own_id.strip()} if own_id and own_id.strip() else None,
            )
        )

    def record_owner_pushed_message(
        self,
        pushed: PushedMessage,
        user_msg: FeishuUserMessage,
        *,
        web_transport: Any = None,
    ) -> None:
        """Record an outgoing/pushed message from the current user to suppress earlier questions."""
        msg = pushed.message
        chat_keys = self._chat_keys(msg.chat_id or user_msg.chat_id, web_transport)
        topic_id = (
            user_msg.thread_id or (msg.thread_id or msg.root_id if is_in_topic(msg) else "") or ""
        ).strip()
        mark = _OwnerReplyMark(
            max(user_msg.create_time_ms, msg.create_time_ms),
            msg.position,
            int(getattr(msg, "topic_position", None) or 0),
            user_msg.message_id,
        )

        def _put_mark(store: OrderedDict[Any, _OwnerReplyMark], k: Any) -> None:
            prev = store.get(k)
            if prev is None or _reply_mark_is_newer_or_equal(mark, prev):
                store[k] = mark
                store.move_to_end(k)
                while len(store) > _MAX_REPLY_TRACKER:
                    store.popitem(last=False)

        with self._lock:
            for ck in chat_keys:
                if topic_id:
                    _put_mark(self._owner_topic_replies, (ck, topic_id))
                else:
                    _put_mark(self._owner_chat_replies, ck)
            for parent_ref in {
                msg.parent_id.strip(),
                msg.root_id.strip(),
                (user_msg.parent_id or "").strip(),
                (user_msg.root_id or "").strip(),
                topic_id,
            }:
                if parent_ref:
                    _put_mark(self._owner_parent_replies, parent_ref)
                    if web_transport is not None and hasattr(web_transport, "get_message_alias"):
                        alias = web_transport.get_message_alias(parent_ref)
                        if alias:
                            _put_mark(self._owner_parent_replies, alias)

    def has_owner_replied(
        self,
        *,
        chat_id: str,
        thread_id: str = "",
        reply_to: str = "",
        message_id: str = "",
        created_at_ms: int = 0,
        position: int = 0,
        topic_position: int = 0,
        web_transport: Any = None,
        owner_ids: set[str] | None = None,
    ) -> bool:
        """Check in-memory reply marks and web transport cache for a newer owner reply."""
        chat_keys = self._chat_keys(chat_id, web_transport)
        clean_tid = (thread_id or "").strip()
        clean_mid = (message_id or "").strip()
        clean_reply_to = (reply_to or "").strip()

        def _mark_is_newer(mark: _OwnerReplyMark, *, direct_anchor: bool = False) -> bool:
            if mark.message_id and mark.message_id == clean_mid:
                return False
            return _is_newer_reply(
                mark.create_time_ms,
                mark.position,
                mark.topic_position,
                target_ms=created_at_ms,
                target_pos=position,
                target_tpos=topic_position,
                direct_anchor=direct_anchor,
            )

        with self._lock:
            if clean_mid and clean_mid in self._owner_parent_replies:
                if _mark_is_newer(self._owner_parent_replies[clean_mid], direct_anchor=True):
                    return True
            if clean_reply_to and clean_reply_to in self._owner_parent_replies:
                if _mark_is_newer(self._owner_parent_replies[clean_reply_to], direct_anchor=False):
                    return True
            for ck in chat_keys:
                mark = (
                    self._owner_topic_replies.get((ck, clean_tid))
                    if clean_tid
                    else self._owner_chat_replies.get(ck)
                )
                if mark is not None and _mark_is_newer(mark, direct_anchor=False):
                    return True

        if web_transport is not None and owner_ids:
            msg_cache = getattr(web_transport, "_msg_cache", None)
            if isinstance(msg_cache, Mapping):
                for cached in msg_cache.values():
                    if not isinstance(cached, WebMessage) or cached.recalled or not cached.id:
                        continue
                    if cached.id == clean_mid or cached.from_id not in owner_ids:
                        continue
                    cached_text = describe(cached.type, cached.content, "").strip()
                    if cached_text.lower().startswith("/ai") and cached.parent_id != clean_mid:
                        continue
                    if not (chat_keys & self._chat_keys(cached.chat_id, web_transport)):
                        continue
                    direct_reply = cached.parent_id == clean_mid or cached.root_id == clean_mid
                    same_parent = bool(clean_reply_to) and cached.parent_id == clean_reply_to
                    cached_topic = (
                        cached.thread_id or cached.root_id if is_in_topic(cached) else ""
                    ).strip()
                    if not (
                        direct_reply
                        or same_parent
                        or (bool(clean_tid) and cached_topic == clean_tid)
                        or (not clean_tid and not cached_topic)
                    ):
                        continue
                    if _is_newer_reply(
                        cached.create_time_ms,
                        cached.position,
                        int(getattr(cached, "topic_position", None) or 0),
                        target_ms=created_at_ms,
                        target_pos=position,
                        target_tpos=topic_position,
                        direct_anchor=direct_reply,
                    ):
                        return True
        return False

    async def _verify_unanswered_via_client(
        self,
        *,
        user_client: Any,
        chat_id: str,
        thread_id: str,
        message_id: str,
        created_at_ms: int,
        position: int = 0,
        topic_position: int = 0,
        web_transport: Any = None,
        owner_ids: set[str],
    ) -> bool:
        """Best-effort check of latest thread/chat messages; returns True if owner already replied."""
        if user_client is None or not hasattr(user_client, "list_messages_page"):
            return False
        container_id = thread_id if thread_id else chat_id
        if not container_id:
            return False
        try:
            async with asyncio.timeout(2.5):
                page = await user_client.list_messages_page(
                    container_id_type="thread" if thread_id else "chat",
                    container_id=container_id,
                    sort_type="ByCreateTimeDesc",
                    page_size=20,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

        msg_cache = (
            getattr(web_transport, "_msg_cache", None) if web_transport is not None else None
        )
        for item in getattr(page, "items", ()):
            if (
                not isinstance(item, FeishuUserMessage)
                or item.message_id == message_id
                or item.sender_open_id not in owner_ids
            ):
                continue
            if item.text.strip().lower().startswith("/ai") and item.parent_id != message_id:
                continue
            direct_reply = item.parent_id == message_id or item.root_id == message_id
            if not (
                direct_reply
                or (bool(thread_id) and (item.thread_id == thread_id or item.root_id == thread_id))
                or (not thread_id and not item.thread_id)
            ):
                continue
            wm = msg_cache.get(item.message_id) if isinstance(msg_cache, Mapping) else None
            if _is_newer_reply(
                item.create_time_ms,
                wm.position if wm is not None else 0,
                int(getattr(wm, "topic_position", None) or 0) if wm is not None else 0,
                target_ms=created_at_ms,
                target_pos=position,
                target_tpos=topic_position,
                direct_anchor=direct_reply,
            ):
                return True
        return False

    async def _ensure_chat_metadata(
        self, web_chat_id: str, web_transport: Any, cfg: FeishuRealtimeEventConfig
    ) -> tuple[str, str]:
        """Resolve (open_chat_id, chat_name) from web_transport cache or gateway when needed."""
        if web_transport is None or not web_chat_id:
            return "", ""
        chat_cache = getattr(web_transport, "_chat_cache", {})
        open_map = getattr(web_transport, "_web_to_open_chat", {})
        cached = chat_cache.get(web_chat_id) if isinstance(chat_cache, Mapping) else None
        open_chat_id = str(
            open_map.get(web_chat_id) or getattr(cached, "open_chat_id", "") or ""
        ).strip()
        chat_name = str(getattr(cached, "name", "") or "").strip()
        if (
            cached is None
            and (
                any(cid.startswith("oc_") for cid in cfg.watch_chat_ids)
                or bool(cfg.watch_chat_names)
            )
            and web_chat_id.strip().casefold() not in cfg.watch_chat_ids
        ):
            web_client = getattr(web_transport, "web_client", None) or getattr(
                web_transport, "_web", None
            )
            if web_client is not None and hasattr(web_client, "chats"):
                try:
                    async with asyncio.timeout(2.5):
                        chats_map = await web_client.chats([web_chat_id])
                    fetched = chats_map.get(web_chat_id) if isinstance(chats_map, Mapping) else None
                    if fetched is not None:
                        if hasattr(web_transport, "_remember_chat"):
                            web_transport._remember_chat(fetched)
                        open_chat_id = str(getattr(fetched, "open_chat_id", "") or open_chat_id)
                        chat_name = str(getattr(fetched, "name", "") or chat_name)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
        return open_chat_id, chat_name

    async def _deliver_event(self, event: ExternalEvent, cfg: FeishuRealtimeEventConfig) -> bool:
        if self._event_sink is not None:
            res = await self._event_sink(event)
            if isinstance(res, bool):
                return res
            if isinstance(res, Mapping):
                return res.get("status") in {"accepted", "duplicate", True}
            return True
        if cfg.events_url:
            headers = {"Content-Type": "application/json"}
            token = os.environ.get("CLAUDE_HUB_DOT_EVENTS_TOKEN", "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                resp = await client.post(
                    cfg.events_url,
                    content=event.model_dump_json().encode("utf-8"),
                    headers=headers,
                )
                resp.raise_for_status()
                return True
        return await submit_external_event(event)

    async def handle_pushed_message(
        self,
        pushed: PushedMessage,
        user_msg: FeishuUserMessage,
        *,
        web_transport: Any = None,
        user_client: Any = None,
        owner_ids: set[str] | None = None,
        is_owner_sender: bool = False,
    ) -> bool:
        """Inspect one pushed web message and emit an urgent Dot event when matched and unanswered."""
        if pushed.removed or pushed.message.recalled or pushed.message.type == TYPE_SYSTEM:
            return False
        eff_owner_ids = {oid.strip() for oid in (owner_ids or set()) if oid and oid.strip()}
        if (
            is_owner_sender
            or pushed.message.from_id in eff_owner_ids
            or user_msg.sender_open_id in eff_owner_ids
        ):
            self.record_owner_pushed_message(pushed, user_msg, web_transport=web_transport)
            return False
        cfg = self.resolve_config()
        msg = pushed.message
        web_chat_id = (msg.chat_id or user_msg.chat_id).strip()
        if (
            not cfg.enabled
            or not web_chat_id
            or user_msg.sender_open_id.startswith(("cli_", "app_", "bot_"))
            or msg.from_id.startswith(("cli_", "app_", "bot_"))
        ):
            return False
        mentioned_ids = extract_mentioned_user_ids(msg.type, msg.content)
        full_text = describe(msg.type, msg.content, "").strip() or user_msg.text.strip()
        urls = extract_message_urls(msg.type, msg.content, full_text)
        mentioned_owner = bool(eff_owner_ids.intersection(mentioned_ids))
        only_at_all = bool(mentioned_ids) and set(mentioned_ids) <= {"all", "@all"}
        is_at_me = mentioned_owner or (bool(pushed.at_me) and not only_at_all)
        open_chat_id, chat_name = await self._ensure_chat_metadata(web_chat_id, web_transport, cfg)
        chat_keys = self._chat_keys(web_chat_id, web_transport)
        if open_chat_id:
            chat_keys.add(open_chat_id.casefold())
        if chat_keys & cfg.automated_chat_ids:
            return False
        matched_urls = (
            matches_watch_group_url(
                web_chat_id,
                chat_name,
                full_text,
                cfg,
                extra_urls=urls,
                extra_chat_ids=[open_chat_id] if open_chat_id else (),
            )
            if urls and (cfg.url_patterns or cfg.actionable_hosts)
            else []
        )
        is_watch_group_url = bool(matched_urls)
        topic_id = (
            user_msg.thread_id or (msg.thread_id or msg.root_id if is_in_topic(msg) else "") or ""
        ).strip()

        if not is_at_me and not is_watch_group_url:
            is_p2p = pushed.chat_type == CHAT_P2P
            in_participated_topic = bool(
                topic_id and any((ck, topic_id) in self._owner_topic_replies for ck in chat_keys)
            )
            if (is_p2p or in_participated_topic) and not is_courtesy_or_ack_only(full_text):
                if (
                    _SUBSTANTIVE_ISSUE_RE.search(full_text)
                    or "?" in full_text
                    or "？" in full_text
                    or bool(urls)
                ):
                    self._add_fallback_record(
                        item_id=user_msg.message_id,
                        web_msg_id=user_msg.message_id,
                        kind="uncovered_message",
                        chat_name=chat_name,
                        chat_keys=chat_keys,
                        thread_id=topic_id,
                        sender_id=user_msg.sender_open_id,
                        text=full_text,
                        created_at_ms=user_msg.create_time_ms,
                    )
            return False

        alias_id = (
            web_transport.get_message_alias(user_msg.message_id)
            if web_transport is not None and hasattr(web_transport, "get_message_alias")
            else None
        )
        canonical_msg_id = (
            alias_id
            if isinstance(alias_id, str) and alias_id.startswith("om_")
            else user_msg.message_id
        )
        sender_id_set = {
            s.strip().casefold()
            for s in (user_msg.sender_open_id, msg.from_id)
            if isinstance(s, str) and s.strip()
        }

        if self.is_message_handled(
            canonical_msg_id,
            chat_id=open_chat_id or web_chat_id,
            thread_id=topic_id,
            sender_id=user_msg.sender_open_id,
            text=full_text,
            created_at_ms=user_msg.create_time_ms,
        ) or self.is_message_handled(
            user_msg.message_id,
            chat_id=web_chat_id,
            thread_id=topic_id,
            sender_id=msg.from_id or user_msg.sender_open_id,
            text=full_text,
            created_at_ms=user_msg.create_time_ms,
        ):
            return False

        async def _suppress(courtesy: bool) -> bool:
            self._suppressed_count += 1
            if courtesy:
                self._suppressed_courtesy_count += 1
            else:
                self._suppressed_replied_count += 1
            self._record_handled(
                user_msg.message_id,
                alias_id=canonical_msg_id,
                chat_keys=chat_keys,
                thread_id=topic_id,
                sender_ids=sender_id_set,
                text=full_text,
                created_at_ms=user_msg.create_time_ms,
            )
            for sup_key in {user_msg.message_id, canonical_msg_id}:
                if sup_key:
                    try:
                        await record_suppressed_external_event("feishu", sup_key[:256])
                    except Exception:
                        pass
            return False

        # Gate 1: Courtesy / simple confirmation closing suppression
        if not is_watch_group_url and (
            is_courtesy_or_ack_only(user_msg.text) or is_courtesy_or_ack_only(full_text)
        ):
            return await _suppress(courtesy=True)

        # Gate 2: Owner already replied in the same chat/topic after this message
        msg_tpos = int(getattr(msg, "topic_position", None) or 0)
        already_replied = self.has_owner_replied(
            chat_id=web_chat_id,
            thread_id=topic_id,
            reply_to=msg.parent_id or msg.root_id,
            message_id=user_msg.message_id,
            created_at_ms=user_msg.create_time_ms,
            position=msg.position,
            topic_position=msg_tpos,
            web_transport=web_transport,
            owner_ids=eff_owner_ids,
        )
        if not already_replied:
            already_replied = await self._verify_unanswered_via_client(
                user_client=user_client,
                chat_id=web_chat_id,
                thread_id=topic_id,
                message_id=user_msg.message_id,
                created_at_ms=user_msg.create_time_ms,
                position=msg.position,
                topic_position=msg_tpos,
                web_transport=web_transport,
                owner_ids=eff_owner_ids,
            )
        if already_replied:
            return await _suppress(courtesy=False)

        primary_url_str = matched_urls[0] if matched_urls else (urls[0] if urls else "")
        parsed_url: HttpUrl | None = None
        if primary_url_str:
            try:
                parsed_url = HttpUrl(primary_url_str)
            except Exception:
                parsed_url = None

        event_type = "im.mention" if is_at_me else "im.watch_chat_url"
        title = "Feishu mention" if is_at_me else "Feishu watch-group issue link"
        chat_label = chat_name or open_chat_id or web_chat_id
        summary_parts = [
            p for p in (chat_label[:120], user_msg.sender_open_id[:80], full_text[:600]) if p
        ]
        chat_type_str = "p2p" if pushed.chat_type == CHAT_P2P else "group"
        payload: dict[str, JsonValue] = {
            "chat_id": open_chat_id or web_chat_id,
            "web_chat_id": web_chat_id,
            "chat_name": chat_name[:200],
            "chat_type": chat_type_str,
            "message_id": canonical_msg_id,
            "web_message_id": user_msg.message_id,
            "thread_id": topic_id,
            "sender_id": user_msg.sender_open_id[:120],
            "text": full_text[:500],
            "trigger_reason": "mention" if is_at_me else "watch_chat_url",
            "matched_urls": [u[:400] for u in (matched_urls or urls)[:5]],
        }
        event = ExternalEvent(
            source="feishu",
            event_type=event_type,
            dedupe_key=canonical_msg_id[:256],
            severity="urgent",
            title=title,
            summary=" · ".join(summary_parts)[:1000],
            url=parsed_url,
            payload=payload,
        )
        delivery_err: str | None = None
        try:
            delivered = await self._deliver_event(event, cfg)
            if not delivered:
                delivery_err = "rejected"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delivery_err = str(exc) or type(exc).__name__
            logger.warning(
                "Feishu real-time event delivery failed for %s",
                canonical_msg_id,
                exc_info=True,
            )

        if delivery_err is not None:
            self._add_fallback_record(
                item_id=canonical_msg_id,
                web_msg_id=user_msg.message_id,
                alias_id=canonical_msg_id if canonical_msg_id != user_msg.message_id else "",
                kind="failed_delivery",
                chat_name=chat_name,
                chat_keys=chat_keys,
                thread_id=topic_id,
                sender_id=user_msg.sender_open_id,
                text=full_text,
                created_at_ms=user_msg.create_time_ms,
                matched_urls=matched_urls or urls,
                event=event,
                error=delivery_err,
            )
            return False

        self._emitted_count += 1
        self._record_handled(
            user_msg.message_id,
            alias_id=canonical_msg_id,
            chat_keys=chat_keys,
            thread_id=topic_id,
            sender_ids=sender_id_set,
            text=full_text,
            created_at_ms=user_msg.create_time_ms,
        )
        return True


_DEFAULT_REALTIME_ROUTER: FeishuRealtimeEventRouter | None = None


def get_feishu_realtime_event_router() -> FeishuRealtimeEventRouter:
    global _DEFAULT_REALTIME_ROUTER
    if _DEFAULT_REALTIME_ROUTER is None:
        _DEFAULT_REALTIME_ROUTER = FeishuRealtimeEventRouter()
    return _DEFAULT_REALTIME_ROUTER


def is_feishu_realtime_stream_healthy() -> bool:
    """Return True when the Feishu WebSocket frontier listener is connected and healthy."""
    from claude_hub.services.feishu_ai_poller import _DEFAULT_AI_POLLER

    if _DEFAULT_AI_POLLER is None:
        return False
    return bool(getattr(_DEFAULT_AI_POLLER, "realtime_stream_healthy", False))
