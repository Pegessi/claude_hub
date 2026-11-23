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
            for k in raw_ids:
                ks = _str(k)
                if ks and ks not in ordered_keys:
                    ordered_keys.append(ks)
    for k in dictionary:
        ks = _str(k)
        if ks and ks not in ordered_keys:
            ordered_keys.append(ks)

    visited: set[str] = set()
    nodes: list[Mapping[str, Any]] = []

    def visit(key: str, depth: int) -> None:
        if depth > 25 or key in visited:
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
        if _num(node.get("tag")) != TAG_AT:
            continue
        raw_prop = _str(node.get("property"))
        if not raw_prop:
            continue
        try:
            prop_bytes = base64.b64decode(raw_prop.encode("ascii"))
            prop = decode("lark_entities_RichTextElement_AtProperty", prop_bytes)
        except Exception:
            continue
        uid = _str(prop.get("userId")).strip()
        if uid and uid not in mentioned:
            mentioned.append(uid)
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
        if not raw_prop:
            continue
        try:
            prop_bytes = base64.b64decode(raw_prop.encode("ascii"))
        except Exception:
            continue
        if tag == TAG_A:
            try:
                prop = decode("lark_entities_RichTextElement_AnchorProperty", prop_bytes)
                add_url(_str(prop.get("href")))
                for match in _HTTP_URL_RE.finditer(_str(prop.get("content"))):
                    add_url(match.group(0))
            except Exception:
                pass
        elif tag in {TAG_TEXT, TAG_LINK}:
            try:
                prop = decode("lark_entities_RichTextElement_TextProperty", prop_bytes)
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
        path = home / filename
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if isinstance(data, Mapping):
            docs.append(data)
    return docs


def _split_regex_patterns_outside_braces(text: str) -> list[str]:
    """Split a string of regex patterns on newlines or top-level commas outside ``{...}``/``[...]``."""
    results: list[str] = []
    for line in text.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        buf: list[str] = []
        brace_depth = 0
        bracket_depth = 0
        escaped = False
        for ch in stripped_line:
            if escaped:
                buf.append(ch)
                escaped = False
                continue
            if ch == "\\":
                buf.append(ch)
                escaped = True
                continue
            if ch == "[" and brace_depth == 0:
                bracket_depth += 1
            elif ch == "]" and bracket_depth > 0:
                bracket_depth -= 1
            elif ch == "{" and bracket_depth == 0:
                brace_depth += 1
            elif ch == "}" and brace_depth > 0:
                brace_depth -= 1
            elif ch == "," and brace_depth == 0 and bracket_depth == 0:
                part = "".join(buf).strip()
                if part:
                    results.append(part)
                buf = []
                continue
            buf.append(ch)
        tail = "".join(buf).strip()
        if tail:
            results.append(tail)
    return results


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
            if split_commas:
                for part in re.split(r"[,\n]+", text):
                    cleaned = part.strip()
                    if cleaned:
                        values.append(cleaned)
            else:
                for part in _split_regex_patterns_outside_braces(text):
                    if part:
                        values.append(part)
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
    for name in names:
        if name in os.environ:
            raw = os.environ[name].strip().lower()
            if raw in {"1", "true", "yes", "on"}:
                return True
            if raw in {"0", "false", "no", "off"}:
                return False
    for doc in docs:
        for name in names:
            val = doc.get(name)
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
    suppress_patrol_when_healthy: bool = True

    @classmethod
    def from_runtime(cls) -> FeishuRealtimeEventConfig:
        docs = _read_runtime_json_documents()
        enabled = _setting_bool(
            ("CLAUDE_HUB_FEISHU_REALTIME_EVENTS_ENABLED", "enabled"),
            docs,
            default=True,
        )
        suppress_patrol = _setting_bool(
            (
                "CLAUDE_HUB_FEISHU_SUPPRESS_PATROL_WHEN_HEALTHY",
                "suppress_patrol_when_healthy",
            ),
            docs,
            default=True,
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
                ("CLAUDE_HUB_PATROL_AUTOMATED_CHAT_IDS", "automated_chat_ids"),
                docs,
            )
        )
        events_urls = _setting_values(
            ("CLAUDE_HUB_FEISHU_REALTIME_EVENTS_URL", "events_url"),
            docs,
        )
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
) -> list[str]:
    """Return matched fault/task URLs if the chat is a configured priority watch group."""
    cfg = config or FeishuRealtimeEventConfig.from_runtime()
    candidate_ids = {
        cid.strip().casefold()
        for cid in (chat_id, *extra_chat_ids)
        if isinstance(cid, str) and cid.strip()
    }
    normalized_name = " ".join((chat_name or "").split()).casefold()
    in_watch_group = bool(candidate_ids & cfg.watch_chat_ids) or (
        bool(normalized_name) and normalized_name in cfg.watch_chat_names
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


@dataclass(frozen=True)
class _CrossTransportRecord:
    web_msg_id: str
    canonical_msg_id: str
    chat_keys: frozenset[str]
    content_hash: str
    created_at_ms: int
    recorded_at: float


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


def _parse_created_ms(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num if num > 100_000_000_000 else num * 1000)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0
        if raw.replace(".", "", 1).isdigit():
            num = float(raw)
            return int(num if num > 100_000_000_000 else num * 1000)
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
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
        self._emitted_count = 0
        self._suppressed_count = 0
        self._suppressed_courtesy_count = 0
        self._suppressed_replied_count = 0
        self._delivery_failed_count = 0
        self._pending_fallback_count = 0
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
        return self._pending_fallback_count

    @property
    def last_delivery_error(self) -> str | None:
        return self._last_delivery_error

    @property
    def needs_patrol_fallback(self) -> bool:
        return self._pending_fallback_count > 0

    def acknowledge_patrol_fallback(self) -> None:
        """Clear pending fallback flag after a successful Feishu patrol run."""
        with self._lock:
            self._pending_fallback_count = 0
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
        return keys

    def _record_handled(
        self,
        message_id: str,
        *,
        alias_id: str | None = None,
        chat_keys: set[str] | None = None,
        text: str = "",
        created_at_ms: int = 0,
    ) -> None:
        now = time.time()
        clean_mid = (message_id or "").strip()
        clean_alias = (alias_id or "").strip()
        with self._lock:
            for mid in (clean_mid, clean_alias):
                if mid:
                    self._handled_msg_ids[mid] = now
                    self._handled_msg_ids.move_to_end(mid)
            while len(self._handled_msg_ids) > _MAX_HANDLED_LEDGER:
                self._handled_msg_ids.popitem(last=False)

            if clean_mid and text.strip():
                rec = _CrossTransportRecord(
                    web_msg_id=clean_mid,
                    canonical_msg_id=clean_alias or clean_mid,
                    chat_keys=frozenset(chat_keys or ()),
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
        text: str = "",
        created_at_ms: int = 0,
    ) -> bool:
        _ = thread_id
        clean_mid = (message_id or "").strip()
        with self._lock:
            if clean_mid and clean_mid in self._handled_msg_ids:
                return True
            # Cross-transport bridge (OpenAPI om_... vs Web numeric ID):
            # Never deduplicate two distinct message IDs in the same namespace by text!
            if clean_mid.startswith("om_") and text.strip():
                now_s = time.time()
                fp_hash = _content_fingerprint(text)
                ck = chat_id.strip().casefold() if chat_id else ""
                for rec in reversed(self._cross_transport_records.values()):
                    if rec.web_msg_id.startswith("om_"):
                        continue
                    if rec.content_hash != fp_hash:
                        continue
                    if ck and rec.chat_keys and ck not in rec.chat_keys:
                        continue
                    if created_at_ms > 0 and rec.created_at_ms > 0:
                        if abs(created_at_ms - rec.created_at_ms) > _CROSS_TRANSPORT_MAX_DELTA_MS:
                            continue
                    elif abs(now_s - rec.recorded_at) * 1000.0 > _CROSS_TRANSPORT_MAX_DELTA_MS:
                        continue
                    return True
        return False

    def should_suppress_openapi_mention(
        self,
        message: Mapping[str, Any],
        own_id: str,
    ) -> bool:
        """Shared suppression gate for OpenAPI mention pollers (`events_bridge.poll_mentions`)."""
        msg_id = str(message.get("message_id") or "").strip()
        chat_id = str(message.get("chat_id") or "").strip()
        thread_id = str(message.get("thread_id") or message.get("root_id") or "").strip()
        reply_to = str(message.get("reply_to") or message.get("parent_id") or "").strip()
        raw_content = message.get("content", message.get("text", ""))
        if isinstance(raw_content, Mapping):
            text = str(raw_content.get("text") or raw_content.get("content") or "")
        else:
            text = str(raw_content or "")
        created_ms = _parse_created_ms(message.get("create_time"))

        if is_courtesy_or_ack_only(text):
            return True
        if self.is_message_handled(
            msg_id,
            chat_id=chat_id,
            thread_id=thread_id,
            text=text,
            created_at_ms=created_ms,
        ):
            return True
        if self.has_owner_replied(
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
            message_id=msg_id,
            created_at_ms=created_ms,
            owner_ids={own_id.strip()} if own_id and own_id.strip() else None,
        ):
            return True
        return False

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
            create_time_ms=max(user_msg.create_time_ms, msg.create_time_ms),
            position=msg.position,
            topic_position=int(getattr(msg, "topic_position", None) or 0),
            message_id=user_msg.message_id,
        )
        with self._lock:
            for ck in chat_keys:
                if topic_id:
                    key = (ck, topic_id)
                    prev = self._owner_topic_replies.get(key)
                    if prev is None or _reply_mark_is_newer_or_equal(mark, prev):
                        self._owner_topic_replies[key] = mark
                        self._owner_topic_replies.move_to_end(key)
                else:
                    prev_chat = self._owner_chat_replies.get(ck)
                    if prev_chat is None or _reply_mark_is_newer_or_equal(mark, prev_chat):
                        self._owner_chat_replies[ck] = mark
                        self._owner_chat_replies.move_to_end(ck)
            for parent_ref in {
                msg.parent_id.strip(),
                msg.root_id.strip(),
                (user_msg.parent_id or "").strip(),
                (user_msg.root_id or "").strip(),
                topic_id,
            }:
                if parent_ref:
                    prev_parent = self._owner_parent_replies.get(parent_ref)
                    if prev_parent is None or _reply_mark_is_newer_or_equal(mark, prev_parent):
                        self._owner_parent_replies[parent_ref] = mark
                        self._owner_parent_replies.move_to_end(parent_ref)
                    if web_transport is not None and hasattr(web_transport, "get_message_alias"):
                        alias = web_transport.get_message_alias(parent_ref)
                        if alias:
                            prev_alias = self._owner_parent_replies.get(alias)
                            if prev_alias is None or _reply_mark_is_newer_or_equal(
                                mark, prev_alias
                            ):
                                self._owner_parent_replies[alias] = mark
                                self._owner_parent_replies.move_to_end(alias)

            while len(self._owner_topic_replies) > _MAX_REPLY_TRACKER:
                self._owner_topic_replies.popitem(last=False)
            while len(self._owner_chat_replies) > _MAX_REPLY_TRACKER:
                self._owner_chat_replies.popitem(last=False)
            while len(self._owner_parent_replies) > _MAX_REPLY_TRACKER:
                self._owner_parent_replies.popitem(last=False)

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
            if mark.create_time_ms > created_at_ms > 0:
                return True
            if mark.create_time_ms == created_at_ms and created_at_ms > 0:
                if position > 0 and mark.position > 0:
                    return mark.position > position
                if topic_position > 0 and mark.topic_position > 0:
                    return mark.topic_position > topic_position
                return direct_anchor
            return False

        with self._lock:
            # Direct reply to message_id itself is a true direct anchor
            if clean_mid and clean_mid in self._owner_parent_replies:
                if _mark_is_newer(self._owner_parent_replies[clean_mid], direct_anchor=True):
                    return True
            # Sibling reply to the same parent is a thread/parent peer, not a direct child of message_id
            if clean_reply_to and clean_reply_to in self._owner_parent_replies:
                if _mark_is_newer(self._owner_parent_replies[clean_reply_to], direct_anchor=False):
                    return True
            for ck in chat_keys:
                if clean_tid:
                    mark = self._owner_topic_replies.get((ck, clean_tid))
                    if mark is not None and _mark_is_newer(mark, direct_anchor=False):
                        return True
                else:
                    mark = self._owner_chat_replies.get(ck)
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
                    cached_chat_keys = self._chat_keys(cached.chat_id, web_transport)
                    if not (chat_keys & cached_chat_keys):
                        continue
                    direct_reply = cached.parent_id == clean_mid or cached.root_id == clean_mid
                    same_parent = bool(clean_reply_to) and cached.parent_id == clean_reply_to
                    cached_topic = (
                        cached.thread_id or cached.root_id if is_in_topic(cached) else ""
                    ).strip()
                    same_topic = bool(clean_tid) and cached_topic == clean_tid
                    same_main_chat = not clean_tid and not cached_topic
                    if not (direct_reply or same_parent or same_topic or same_main_chat):
                        continue
                    c_ms = cached.create_time_ms
                    if c_ms > created_at_ms > 0:
                        return True
                    if c_ms == created_at_ms and created_at_ms > 0:
                        if position > 0 and cached.position > 0:
                            if cached.position > position:
                                return True
                            continue
                        cached_tpos = int(getattr(cached, "topic_position", None) or 0)
                        if topic_position > 0 and cached_tpos > 0:
                            if cached_tpos > topic_position:
                                return True
                            continue
                        if direct_reply:
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
        owner_ids: set[str],
    ) -> bool:
        """Best-effort check of latest thread/chat messages; returns True if owner already replied."""
        if user_client is None or not hasattr(user_client, "list_messages_page"):
            return False
        container_type = "thread" if thread_id else "chat"
        container_id = thread_id if thread_id else chat_id
        if not container_id:
            return False
        try:
            async with asyncio.timeout(2.5):
                page = await user_client.list_messages_page(
                    container_id_type=container_type,
                    container_id=container_id,
                    sort_type="ByCreateTimeDesc",
                    page_size=20,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

        seen_target = False
        for item in getattr(page, "items", ()):
            if not isinstance(item, FeishuUserMessage):
                continue
            if item.message_id == message_id:
                seen_target = True
                continue
            if item.sender_open_id not in owner_ids:
                continue
            direct_reply = item.parent_id == message_id or item.root_id == message_id
            same_scope = (
                (bool(thread_id) and (item.thread_id == thread_id or item.root_id == thread_id))
                or (not thread_id and not item.thread_id)
                or direct_reply
            )
            if not same_scope:
                continue
            if item.create_time_ms > created_at_ms > 0:
                return True
            if direct_reply and item.create_time_ms >= created_at_ms:
                return True
            # In ByCreateTimeDesc order, items appearing before message_id with equal timestamp are newer
            if not seen_target and item.create_time_ms == created_at_ms and created_at_ms > 0:
                return True
        return False

    async def _ensure_chat_metadata(
        self,
        web_chat_id: str,
        web_transport: Any,
        cfg: FeishuRealtimeEventConfig,
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

        needs_lookup = (
            cached is None
            and (
                any(cid.startswith("oc_") for cid in cfg.watch_chat_ids)
                or bool(cfg.watch_chat_names)
            )
            and web_chat_id.strip().casefold() not in cfg.watch_chat_ids
        )
        if needs_lookup:
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

    async def _deliver_event(
        self,
        event: ExternalEvent,
        cfg: FeishuRealtimeEventConfig,
    ) -> bool:
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
        if not cfg.enabled:
            return False

        msg = pushed.message
        web_chat_id = (msg.chat_id or user_msg.chat_id).strip()
        if not web_chat_id:
            return False

        # Automated sender prefix check
        if user_msg.sender_open_id.startswith(("cli_", "app_", "bot_")) or msg.from_id.startswith(
            ("cli_", "app_", "bot_")
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

        matched_urls: list[str] = []
        if urls and (cfg.url_patterns or cfg.actionable_hosts):
            matched_urls = matches_watch_group_url(
                web_chat_id,
                chat_name,
                full_text,
                cfg,
                extra_urls=urls,
                extra_chat_ids=[open_chat_id] if open_chat_id else (),
            )
        is_watch_group_url = bool(matched_urls)

        topic_id = (
            user_msg.thread_id or (msg.thread_id or msg.root_id if is_in_topic(msg) else "") or ""
        ).strip()

        if not is_at_me and not is_watch_group_url:
            # Preserve slow patrol fallback when an uncovered P2P or thread message is actionable
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
                    with self._lock:
                        self._pending_fallback_count += 1
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

        if self.is_message_handled(
            canonical_msg_id,
            chat_id=open_chat_id or web_chat_id,
            thread_id=topic_id,
            text=full_text,
            created_at_ms=user_msg.create_time_ms,
        ) or self.is_message_handled(
            user_msg.message_id,
            chat_id=web_chat_id,
            thread_id=topic_id,
            text=full_text,
            created_at_ms=user_msg.create_time_ms,
        ):
            return False

        # Gate 1: Courtesy / simple confirmation closing suppression
        if not is_watch_group_url and (
            is_courtesy_or_ack_only(user_msg.text) or is_courtesy_or_ack_only(full_text)
        ):
            self._suppressed_count += 1
            self._suppressed_courtesy_count += 1
            self._record_handled(
                user_msg.message_id,
                alias_id=canonical_msg_id,
                chat_keys=chat_keys,
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

        # Gate 2: Owner already replied in the same chat/topic after this message
        # Only consider an owner's own `/ai ...` command as a reply when it is a direct reply to this message;
        # an unrelated `/ai` command in the main chat is a Hub command, not a reply to a colleague's mention.
        already_replied = self.has_owner_replied(
            chat_id=web_chat_id,
            thread_id=topic_id,
            reply_to=msg.parent_id or msg.root_id,
            message_id=user_msg.message_id,
            created_at_ms=user_msg.create_time_ms,
            position=msg.position,
            topic_position=int(getattr(msg, "topic_position", None) or 0),
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
                owner_ids=eff_owner_ids,
            )
        if already_replied:
            self._suppressed_count += 1
            self._suppressed_replied_count += 1
            self._record_handled(
                user_msg.message_id,
                alias_id=canonical_msg_id,
                chat_keys=chat_keys,
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
            part
            for part in (
                chat_label[:120],
                user_msg.sender_open_id[:80],
                full_text[:600],
            )
            if part
        ]
        payload: dict[str, JsonValue] = {
            "chat_id": open_chat_id or web_chat_id,
            "web_chat_id": web_chat_id,
            "chat_name": chat_name[:200],
            "chat_type": "p2p" if pushed.chat_type == CHAT_P2P else "group",
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
        try:
            delivered = await self._deliver_event(event, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with self._lock:
                self._delivery_failed_count += 1
                self._pending_fallback_count += 1
                self._last_delivery_error = str(exc) or type(exc).__name__
            logger.warning(
                "Feishu real-time event delivery failed for %s",
                canonical_msg_id,
                exc_info=True,
            )
            return False

        if not delivered:
            with self._lock:
                self._delivery_failed_count += 1
                self._pending_fallback_count += 1
                self._last_delivery_error = "rejected"
            return False

        self._emitted_count += 1
        self._record_handled(
            user_msg.message_id,
            alias_id=canonical_msg_id,
            chat_keys=chat_keys,
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
