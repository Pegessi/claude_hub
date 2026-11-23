"""Real-time Feishu WebSocket event triage, courtesy/self-reply suppression, and Dot event bridge."""

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
from claude_hub.services.feishu_web_session import rich as _rich
from claude_hub.services.feishu_web_session.adapter import is_in_topic
from claude_hub.services.feishu_web_session.client import PushedMessage, is_web_entity_id
from claude_hub.services.feishu_web_session.codec import decode
from claude_hub.services.feishu_web_session.rich import (
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
    rf"^(?:{_COURTESY_FILLER}*{_COURTESY_ATOM}{_COURTESY_FILLER}*){{1,5}}$", re.IGNORECASE
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
    raw = (text or "").strip()
    if not raw:
        return True
    if any(_valid_http_url(m.group(0)) for m in _HTTP_URL_RE.finditer(raw)):
        return False
    cleaned = _INLINE_AT_MENTION_RE.sub(" ", _HTML_TAG_RE.sub(" ", _HTML_AT_TAG_RE.sub(" ", raw)))
    if _SUBSTANTIVE_ISSUE_RE.search(cleaned):
        return False
    had_emoji = bool(_BRACKET_EMOJI_RE.search(cleaned) or _COURTESY_EMOJI_RE.search(cleaned))
    cleaned = _COURTESY_EMOJI_RE.sub(" ", _BRACKET_EMOJI_RE.sub(" ", cleaned))
    compact = _PUNCT_AND_SPACE_RE.sub("", cleaned).strip()
    return (
        had_emoji
        if not compact
        else (len(compact) <= 48 and bool(_COURTESY_FULL_RE.fullmatch(compact)))
    )


def _iter_richtext_nodes(typ: int, content: bytes) -> list[Mapping[str, Any]]:
    if not content or typ not in {_rich.TYPE_TEXT, _rich.TYPE_POST, _rich.TYPE_CARD}:
        return []
    schema = (
        "lark_entities_TextContent"
        if typ == _rich.TYPE_TEXT
        else "lark_entities_PostContent" if typ == _rich.TYPE_POST else "lark_entities_CardContent"
    )
    try:
        decoded = decode(schema, content)
    except Exception:
        return []
    rich = decoded.get("richText")
    elements = rich.get("elements") if isinstance(rich, Mapping) else None
    dictionary = elements.get("dictionary") if isinstance(elements, Mapping) else None
    if not isinstance(rich, Mapping) or not isinstance(dictionary, Mapping):
        return []
    ordered_keys = [
        _str(k)
        for rf in ("elementIds", "atIds", "anchorIds")
        for k in (rich.get(rf) or () if isinstance(rich.get(rf), list) else ())
        if _str(k)
    ] + [_str(k) for k in dictionary if _str(k)]
    visited: set[str] = set()
    nodes: list[Mapping[str, Any]] = []

    def visit(key: str, depth: int) -> None:
        node = dictionary.get(key) if depth <= 25 and key and key not in visited else None
        if isinstance(node, Mapping):
            visited.add(key)
            nodes.append(node)
            kids = node.get("childIds")
            for child in kids if isinstance(kids, list) else ():
                visit(_str(child), depth + 1)

    for root_key in ordered_keys:
        visit(root_key, 0)
    return nodes


def extract_mentioned_user_ids(typ: int, content: bytes) -> tuple[str, ...]:
    mentioned: list[str] = []
    for node in _iter_richtext_nodes(typ, content):
        raw_prop = _str(node.get("property")) if _num(node.get("tag")) == _rich.TAG_AT else ""
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
    urls: list[str] = []

    def add_url(raw_url: str) -> None:
        valid = _valid_http_url(raw_url)
        if valid and valid not in urls:
            urls.append(valid)

    for node in _iter_richtext_nodes(typ, content):
        tag, raw_prop = _num(node.get("tag")), _str(node.get("property"))
        if not raw_prop or tag not in {_rich.TAG_A, _rich.TAG_TEXT, _rich.TAG_LINK}:
            continue
        schema = (
            "lark_entities_RichTextElement_AnchorProperty"
            if tag == _rich.TAG_A
            else "lark_entities_RichTextElement_TextProperty"
        )
        try:
            prop = decode(schema, base64.b64decode(raw_prop.encode("ascii")))
            if tag == _rich.TAG_A:
                add_url(_str(prop.get("href")))
            for match in _HTTP_URL_RE.finditer(_str(prop.get("content"))):
                add_url(match.group(0))
        except Exception:
            pass
    for match in _HTTP_URL_RE.finditer(
        "\n".join(filter(None, (text, text_of(typ, content) if content else "")))
    ):
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
    names: Sequence[str], docs: Sequence[Mapping[str, Any]], *, split_commas: bool = True
) -> list[str]:
    values: list[str] = []

    def extend_raw(raw: Any, from_list_item: bool = False) -> None:
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return
            if not from_list_item and text.startswith("[") and text.endswith("]"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, list):
                        for item in parsed:
                            extend_raw(item, True)
                        return
                except ValueError:
                    pass
            parts = (
                [text]
                if from_list_item
                else (re.split(r"[,\n]+", text) if split_commas else text.splitlines())
            )
            values.extend(p.strip() for p in parts if p.strip())
        elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
            for item in raw:
                extend_raw(item, True)

    for src in (os.environ, *docs):
        for name in names:
            if name in src:
                extend_raw(src[name])
    return values


def _setting_bool(
    names: Sequence[str], docs: Sequence[Mapping[str, Any]], *, default: bool
) -> bool:
    for src in (os.environ, *docs):
        for n in names:
            val = src.get(n)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                raw = val.strip().lower()
                if raw in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                    return raw in {"1", "true", "yes", "on"}
    return default


@dataclass(frozen=True)
class FeishuRealtimeEventConfig:
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
        p = "CLAUDE_HUB_"
        w_ids = _setting_values(
            (
                f"{p}FEISHU_WATCH_CHAT_IDS",
                f"{p}FEISHU_REALTIME_WATCH_CHAT_IDS",
                f"{p}PATROL_WATCH_CHAT_IDS",
                f"{p}FEISHU_PRIORITY_CHAT_IDS",
                "watch_chat_ids",
            ),
            docs,
        )
        w_names = _setting_values(
            (
                f"{p}FEISHU_WATCH_CHAT_NAMES",
                f"{p}FEISHU_REALTIME_WATCH_CHAT_NAMES",
                f"{p}PATROL_WATCH_CHAT_NAMES",
                "watch_chat_names",
            ),
            docs,
        )
        u_pats = _setting_values(
            (
                f"{p}FEISHU_WATCH_URL_REGEX",
                f"{p}FEISHU_REALTIME_URL_PATTERNS",
                f"{p}FEISHU_URL_PATTERNS",
                f"{p}PATROL_ACTIONABLE_URL_REGEX",
                "url_patterns",
                "watch_url_regex",
            ),
            docs,
            split_commas=False,
        )
        hosts = _setting_values(
            (
                f"{p}FEISHU_REALTIME_URL_HOSTS",
                f"{p}PATROL_ACTIONABLE_HOSTS",
                "actionable_hosts",
            ),
            docs,
        )
        auto_ids = _setting_values((f"{p}PATROL_AUTOMATED_CHAT_IDS", "automated_chat_ids"), docs)
        e_urls = _setting_values((f"{p}FEISHU_REALTIME_EVENTS_URL", "events_url"), docs)
        en = _setting_bool((f"{p}FEISHU_REALTIME_EVENTS_ENABLED", "enabled"), docs, default=True)
        sup = _setting_bool(
            (f"{p}FEISHU_SUPPRESS_PATROL_WHEN_HEALTHY", "suppress_patrol_when_healthy"),
            docs,
            default=False,
        )
        return cls(
            en,
            frozenset(v.strip() for v in w_ids if v.strip()),
            frozenset(" ".join(v.split()).casefold() for v in w_names),
            tuple(dict.fromkeys(u_pats)),
            frozenset(v.casefold() for v in hosts),
            frozenset(v.casefold() for v in auto_ids),
            e_urls[0] if e_urls else "",
            sup,
        )


def matches_fault_or_task_url(url: str, config: FeishuRealtimeEventConfig | None = None) -> bool:
    valid = _valid_http_url(url)
    if not valid:
        return False
    cfg = config or FeishuRealtimeEventConfig.from_runtime()
    host = (urlsplit(valid).hostname or "").casefold()
    for clean_pat in (p.strip() for p in cfg.url_patterns if p.strip()):
        try:
            if re.search(clean_pat, valid, re.IGNORECASE):
                return True
        except re.error:
            if clean_pat.casefold() in valid.casefold():
                return True
    return any(
        h and (host == h or host.endswith("." + h) or h in valid.casefold())
        for h in cfg.actionable_hosts
    )


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
    router = _DEFAULT_REALTIME_ROUTER
    cfg = config or (
        router.resolve_config() if router is not None else FeishuRealtimeEventConfig.from_runtime()
    )
    cand_ids = {
        cid.strip().casefold()
        for cid in (chat_id, *extra_chat_ids)
        if isinstance(cid, str) and cid.strip()
    }
    if router is not None:
        cand_ids |= {x for cid in list(cand_ids) for x in router.expand_chat_keys(cid)}
    watch_ids_cf = {cid.strip().casefold() for cid in cfg.watch_chat_ids if cid and cid.strip()}
    norm_name = " ".join((chat_name or "").split()).casefold()
    in_watch = (
        bool(cand_ids & watch_ids_cf)
        or (bool(norm_name) and norm_name in cfg.watch_chat_names)
        or (
            router is not None
            and bool(message_id)
            and router.matches_pending_failed_watch_delivery(
                message_id=message_id, chat_ids=cand_ids, chat_name=norm_name
            )
        )
    )
    if not in_watch:
        return []
    found = (_valid_http_url(m.group(0)) for m in _HTTP_URL_RE.finditer(text or ""))
    urls = list(dict.fromkeys([*extra_urls, *filter(None, found)]))
    return [u for u in urls if matches_fault_or_task_url(u, cfg)]


def _content_fingerprint(text: str) -> str:
    cleaned = _INLINE_AT_MENTION_RE.sub(
        " ", _HTML_TAG_RE.sub(" ", _HTML_AT_TAG_RE.sub(" ", text or ""))
    )
    return hashlib.sha256(" ".join(cleaned.lower().split()).encode("utf-8")).hexdigest()[:24]


def _ids_compatible_across_ns(
    cand: str, known: frozenset[str], *, require_same_presence: bool = False
) -> bool:
    c, r = (cand or "").strip().casefold(), {x.strip().casefold() for x in known if x and x.strip()}
    if require_same_presence and bool(c) != bool(r):
        return False
    if not c or not r or c in r:
        return True
    return not any(is_web_entity_id(x) == is_web_entity_id(c) for x in r)


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
    chat_name: str
    thread_ids: frozenset[str]
    sender_ids: frozenset[str]
    content_hashes: frozenset[str]
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
    thread_ids: frozenset[str]
    sender_ids: frozenset[str]
    text: str
    content_hashes: frozenset[str]
    created_at_ms: int
    matched_urls: tuple[str, ...] = ()
    event: ExternalEvent | None = None


def _reply_mark_is_newer_or_equal(candidate: _OwnerReplyMark, existing: _OwnerReplyMark) -> bool:
    if candidate.create_time_ms != existing.create_time_ms:
        return candidate.create_time_ms > existing.create_time_ms
    if candidate.position != existing.position and candidate.position > 0 and existing.position > 0:
        return candidate.position >= existing.position
    return (
        candidate.topic_position >= existing.topic_position
        if candidate.topic_position != existing.topic_position
        and candidate.topic_position > 0
        and existing.topic_position > 0
        else True
    )


def _is_newer_reply(
    cand_ms: int,
    cand_pos: int,
    cand_tpos: int,
    target_ms: int,
    target_pos: int = 0,
    target_tpos: int = 0,
    direct_anchor: bool = False,
) -> bool:
    if cand_ms > target_ms > 0:
        return True
    if cand_ms != target_ms or target_ms <= 0:
        return False
    return (
        cand_pos > target_pos
        if target_pos > 0 and cand_pos > 0
        else (cand_tpos > target_tpos if target_tpos > 0 and cand_tpos > 0 else direct_anchor)
    )


def _parse_created_ms(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value if value > 100_000_000_000 else value * 1000)
    raw = value.strip() if isinstance(value, str) else ""
    if raw.replace(".", "", 1).isdigit():
        num = float(raw)
        return int(num if num > 100_000_000_000 else num * 1000)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    except ValueError:
        return 0


class FeishuRealtimeEventRouter:
    def __init__(
        self,
        *,
        config: FeishuRealtimeEventConfig | None = None,
        event_sink: RealtimeEventSink | None = None,
    ) -> None:
        self._explicit_config, self._event_sink, self._lock = config, event_sink, threading.RLock()
        self._handled_msg_ids: OrderedDict[str, float] = OrderedDict()
        self._cross_transport_records: OrderedDict[str, _CrossTransportRecord] = OrderedDict()
        self._owner_topic_replies: OrderedDict[tuple[str, str], _OwnerReplyMark] = OrderedDict()
        self._owner_chat_replies: OrderedDict[str, _OwnerReplyMark] = OrderedDict()
        self._owner_parent_replies: OrderedDict[str, _OwnerReplyMark] = OrderedDict()
        self._known_chat_aliases: dict[str, set[str]] = {}
        self._known_open_chat_ids: dict[str, str] = {}
        self._known_chat_names: dict[str, set[str]] = {}
        self._last_web_transport: Any = None
        self._pending_fallbacks: OrderedDict[str, _FallbackRecord] = OrderedDict()
        self.fallback_generation = self.emitted_count = self.suppressed_count = 0
        self.suppressed_courtesy_count = self.suppressed_replied_count = 0
        self.delivery_failed_count = 0
        self.last_delivery_error: str | None = None

    @property
    def pending_fallback_count(self) -> int:
        return len(self._pending_fallbacks)

    @property
    def needs_patrol_fallback(self) -> bool:
        return bool(self._pending_fallbacks)

    def _add_fallback_record(
        self,
        item_id: str,
        web_msg_id: str,
        kind: str,
        chat_name: str,
        chat_keys: set[str],
        thread_id: str,
        sender_id: str,
        text: str,
        created_at_ms: int,
        alias_id: str = "",
        alt_text: str = "",
        matched_urls: Sequence[str] = (),
        event: ExternalEvent | None = None,
        error: str | None = None,
    ) -> None:
        hashes = {_content_fingerprint(t) for t in (text, alt_text) if t and t.strip()} - {""}
        tids = frozenset({thread_id.strip().casefold()} if thread_id and thread_id.strip() else ())
        sids = frozenset({sender_id.strip().casefold()} if sender_id and sender_id.strip() else ())
        with self._lock:
            if error is not None:
                self.delivery_failed_count += 1
                self.last_delivery_error = error
            self.fallback_generation += 1
            self._pending_fallbacks[item_id] = _FallbackRecord(
                item_id,
                web_msg_id,
                alias_id,
                kind,
                self.fallback_generation,
                frozenset(chat_keys),
                chat_name,
                tids,
                sids,
                text,
                frozenset(hashes),
                created_at_ms,
                tuple(matched_urls),
                event,
            )

    def _link_chat_pair(self, wid: str, oid: str, cname: str = "") -> None:
        w_cf, o_raw = wid.strip().casefold(), oid.strip()
        keys = {k for k in (w_cf, o_raw.casefold()) if k}
        if len(keys) > 1:
            for k in keys:
                self._known_chat_aliases.setdefault(k, set()).update(keys)
        if o_raw.startswith("oc_"):
            for k in keys:
                self._known_open_chat_ids[k] = o_raw
        norm_name = " ".join(cname.split()).casefold()
        if norm_name and keys:
            self._known_chat_names.setdefault(norm_name, set()).update(keys)

    def _sync_transport_chats(self, web_transport: Any = None) -> None:
        wt = web_transport or self._last_web_transport
        if wt is None:
            return
        self._last_web_transport = wt
        open_map, web_map, chat_cache = (
            getattr(wt, "_web_to_open_chat", {}),
            getattr(wt, "_open_to_web_chat", {}),
            getattr(wt, "_chat_cache", {}),
        )
        with self._lock:
            for wid, oid in open_map.items() if isinstance(open_map, Mapping) else ():
                self._link_chat_pair(str(wid), str(oid))
            for oid, wid in web_map.items() if isinstance(web_map, Mapping) else ():
                self._link_chat_pair(str(wid), str(oid))
            for c in chat_cache.values() if isinstance(chat_cache, Mapping) else ():
                self._link_chat_pair(
                    str(getattr(c, "id", "") or ""),
                    str(getattr(c, "open_chat_id", "") or ""),
                    str(getattr(c, "name", "") or ""),
                )

    def expand_chat_keys(self, chat_id: str) -> set[str]:
        return self._chat_keys(chat_id)

    def watched_openapi_chat_ids(
        self, cfg: FeishuRealtimeEventConfig | None = None
    ) -> tuple[str, ...]:
        eff_cfg = cfg or self.resolve_config()
        self._sync_transport_chats()
        with self._lock:
            for raw_cid in eff_cfg.watch_chat_ids:
                if raw_cid.strip().startswith("oc_"):
                    self._known_open_chat_ids.setdefault(
                        raw_cid.strip().casefold(), raw_cid.strip()
                    )
            seed = (
                set(eff_cfg.watch_chat_ids)
                | {
                    ck
                    for cname in eff_cfg.watch_chat_names
                    for ck in self._known_chat_names.get(cname, ())
                }
                | {
                    ck
                    for rec in self._pending_fallbacks.values()
                    if rec.kind == "failed_delivery" and rec.matched_urls
                    for ck in rec.chat_keys
                }
            )
            keys = {x for s in seed for x in self._chat_keys(s)}
            return tuple(
                sorted(
                    {self._known_open_chat_ids.get(ck, ck) for ck in keys if ck.startswith("oc_")}
                )
            )

    def _chat_compat(
        self,
        cand_ck: set[str],
        cand_name: str,
        rec: _CrossTransportRecord | _FallbackRecord,
    ) -> bool:
        rec_ck = {x for k in rec.chat_keys for x in self._chat_keys(k)} | set(rec.chat_keys)
        if cand_ck & rec_ck:
            return True
        if (
            any(k.startswith("oc_") for k in cand_ck) and any(k.startswith("oc_") for k in rec_ck)
        ) or (
            any(is_web_entity_id(k) for k in cand_ck) and any(is_web_entity_id(k) for k in rec_ck)
        ):
            return False
        cn, rn = (
            " ".join((cand_name or "").split()).casefold(),
            " ".join((rec.chat_name or "").split()).casefold(),
        )
        if cn and rn:
            return cn == rn
        if getattr(rec, "kind", "") == "failed_delivery" and getattr(rec, "matched_urls", ()):
            cfg = self.resolve_config()
            w_ids = {c.strip().casefold() for c in cfg.watch_chat_ids if c and c.strip()}
            return bool((cand_ck & w_ids) or (cn and cn in cfg.watch_chat_names))
        return False

    def matches_pending_failed_watch_delivery(
        self, *, message_id: str = "", chat_ids: set[str] | None = None, chat_name: str = ""
    ) -> bool:
        clean_mid = (message_id or "").strip()
        if not clean_mid or not chat_ids:
            return False
        with self._lock:
            return any(
                rec.kind == "failed_delivery"
                and rec.matched_urls
                and (
                    clean_mid in ({rec.item_id, rec.web_msg_id, rec.alias_id} - {""})
                    or (
                        clean_mid.startswith("om_")
                        and any(
                            is_web_entity_id(m) for m in (rec.item_id, rec.web_msg_id, rec.alias_id)
                        )
                        and not any(
                            m.startswith("om_") and m != clean_mid
                            for m in (rec.item_id, rec.web_msg_id, rec.alias_id)
                        )
                    )
                )
                and self._chat_compat(chat_ids, chat_name, rec)
                for rec in self._pending_fallbacks.values()
            )

    def _matches_candidate_obj(
        self, rec: _CrossTransportRecord | _FallbackRecord, cand: Any
    ) -> bool:
        cand_dt = getattr(cand, "created_at", None)
        return self._matches_cross_namespace(
            rec,
            message_id=str(getattr(cand, "message_id", "") or ""),
            chat_id=str(getattr(cand, "chat_id", "") or ""),
            chat_name=str(getattr(cand, "chat_name", "") or ""),
            thread_id=str(getattr(cand, "thread_id", "") or ""),
            sender_id=str(getattr(cand, "sender_id", "") or ""),
            text=str(getattr(cand, "text", "") or ""),
            created_at_ms=int(cand_dt.timestamp() * 1000) if cand_dt is not None else 0,
        )

    def is_pending_failed_watch_candidate(self, candidate: Any) -> bool:
        cand_mid = str(getattr(candidate, "message_id", "") or "").strip()
        with self._lock:
            return any(
                rec.kind == "failed_delivery"
                and rec.matched_urls
                and (
                    (cand_mid and cand_mid in ({rec.item_id, rec.web_msg_id, rec.alias_id} - {""}))
                    or self._matches_candidate_obj(rec, candidate)
                )
                for rec in self._pending_fallbacks.values()
            )

    def _matches_cross_namespace(
        self,
        rec: _CrossTransportRecord | _FallbackRecord,
        *,
        message_id: str,
        chat_id: str,
        chat_name: str = "",
        thread_id: str = "",
        sender_id: str = "",
        text: str = "",
        created_at_ms: int = 0,
    ) -> bool:
        clean_mid = (message_id or "").strip()
        if (
            not clean_mid
            or not text.strip()
            or created_at_ms <= 0
            or rec.created_at_ms <= 0
            or (created_at_ms // 1000) != (rec.created_at_ms // 1000)
        ):
            return False
        known_mids = {
            getattr(rec, a, "")
            for a in ("web_msg_id", "canonical_msg_id", "alias_id", "item_id")
            if getattr(rec, a, "")
        }
        if clean_mid not in known_mids:
            is_om, is_web = clean_mid.startswith("om_"), is_web_entity_id(clean_mid)
            if (
                (not is_om and not is_web)
                or (
                    is_om
                    and (
                        not any(is_web_entity_id(m) for m in known_mids)
                        or any(m.startswith("om_") and m != clean_mid for m in known_mids)
                    )
                )
                or (
                    is_web
                    and (
                        not any(m.startswith("om_") for m in known_mids)
                        or any(is_web_entity_id(m) and m != clean_mid for m in known_mids)
                    )
                )
            ):
                return False
        fp_hash = _content_fingerprint(text)
        cand_ck = self._chat_keys(chat_id) if fp_hash and fp_hash in rec.content_hashes else set()
        return (
            bool(cand_ck and self._chat_compat(cand_ck, chat_name, rec))
            and _ids_compatible_across_ns(thread_id, rec.thread_ids, require_same_presence=True)
            and _ids_compatible_across_ns(sender_id, rec.sender_ids, require_same_presence=False)
        )

    def acknowledge_patrol_fallback(
        self,
        *,
        recovered_ids: Sequence[str] | None = None,
        recovered_candidates: Sequence[Any] = (),
        seen_ids: Sequence[str] = (),
        seen_candidates: Sequence[Any] = (),
        window_start_ms: int | None = None,
        window_end_ms: int | None = None,
        max_generation: int | None = None,
        all_candidates_completed: bool = True,
    ) -> None:
        with self._lock:
            if (
                recovered_ids is None
                and not recovered_candidates
                and not seen_ids
                and not seen_candidates
                and window_start_ms is None
            ):
                self._pending_fallbacks.clear()
                self.last_delivery_error = None
                return
            rec_id_set = {str(mid).strip() for mid in (recovered_ids or ()) if str(mid).strip()}
            seen_id_set = {str(mid).strip() for mid in seen_ids if str(mid).strip()} | rec_id_set
            for key, rec in list(self._pending_fallbacks.items()):
                if (max_generation is not None and rec.generation > max_generation) or (
                    window_start_ms is not None
                    and window_end_ms is not None
                    and rec.created_at_ms > 0
                    and not (window_start_ms <= rec.created_at_ms <= window_end_ms)
                ):
                    continue
                direct = bool({rec.item_id, rec.web_msg_id, rec.alias_id} & rec_id_set - {""})
                alias = rec.alias_id or (rec.item_id if rec.item_id.startswith("om_") else "")
                sids, tids, cks = set(rec.sender_ids), set(rec.thread_ids), set(rec.chat_keys)
                if not direct:
                    for cand in recovered_candidates:
                        cmid = str(getattr(cand, "message_id", "") or "").strip()
                        if (
                            not cmid or not rec_id_set or cmid in rec_id_set
                        ) and self._matches_candidate_obj(rec, cand):
                            direct, alias = True, cmid or alias
                            if getattr(cand, "sender_id", ""):
                                sids.add(str(cand.sender_id).strip().casefold())
                            if getattr(cand, "thread_id", ""):
                                tids.add(str(cand.thread_id).strip().casefold())
                            if getattr(cand, "chat_id", ""):
                                cks.update(self._chat_keys(str(cand.chat_id)))
                            break
                if direct:
                    self._record_handled(
                        rec.web_msg_id or rec.item_id,
                        alias,
                        cks,
                        rec.chat_name,
                        tids,
                        sids,
                        rec.text,
                        "",
                        rec.created_at_ms,
                    )
                    self._pending_fallbacks.pop(key, None)
                elif rec.kind == "uncovered_message" and all_candidates_completed:
                    if bool(
                        {rec.item_id, rec.web_msg_id, rec.alias_id} & seen_id_set - {""}
                    ) or any(self._matches_candidate_obj(rec, c) for c in seen_candidates):
                        self._pending_fallbacks.pop(key, None)
            if not self._pending_fallbacks:
                self.last_delivery_error = None

    def resolve_config(self) -> FeishuRealtimeEventConfig:
        return self._explicit_config or FeishuRealtimeEventConfig.from_runtime()

    def _chat_keys(self, chat_id: str, web_transport: Any = None) -> set[str]:
        self._sync_transport_chats(web_transport)
        raw = (chat_id or "").strip()
        clean = raw.casefold()
        if not clean:
            return set()
        with self._lock:
            if raw.startswith("oc_"):
                self._known_open_chat_ids.setdefault(clean, raw)
            keys = ({clean} | set(self._known_chat_aliases.get(clean, ()))) - {""}
            if len(keys) > 1:
                for k in keys:
                    self._known_chat_aliases.setdefault(k, set()).update(keys)
            return keys

    def _record_handled(
        self,
        message_id: str,
        alias_id: str | None = None,
        chat_keys: set[str] | None = None,
        chat_name: str = "",
        thread_ids: set[str] | frozenset[str] | str | None = None,
        sender_ids: set[str] | frozenset[str] | None = None,
        text: str = "",
        alt_text: str = "",
        created_at_ms: int = 0,
        thread_id: str = "",
    ) -> None:
        now = time.time()
        clean_mid, clean_alias = (message_id or "").strip(), (alias_id or "").strip()
        raw_tids = ({thread_ids} if isinstance(thread_ids, str) else set(thread_ids or ())) | (
            {thread_id} if thread_id else set()
        )
        norm_tids = frozenset(t.strip().casefold() for t in raw_tids if t and t.strip())
        norm_senders = frozenset(
            s.strip().casefold() for s in (sender_ids or set()) if s and s.strip()
        )
        hashes = frozenset(
            {_content_fingerprint(t) for t in (text, alt_text) if t and t.strip()} - {""}
        )
        with self._lock:
            for mid in (clean_mid, clean_alias):
                if mid:
                    self._handled_msg_ids[mid] = now
                    self._handled_msg_ids.move_to_end(mid)
                    self._pending_fallbacks.pop(mid, None)
            if clean_mid and hashes:
                self._cross_transport_records[clean_mid] = _CrossTransportRecord(
                    clean_mid,
                    clean_alias or clean_mid,
                    frozenset(chat_keys or ()),
                    chat_name,
                    norm_tids,
                    norm_senders,
                    hashes,
                    created_at_ms,
                    now,
                )
                self._cross_transport_records.move_to_end(clean_mid)
            for store in (self._handled_msg_ids, self._cross_transport_records):
                while len(store) > _MAX_HANDLED_LEDGER:
                    store.popitem(last=False)

    def was_dispatched(self, message_id: str) -> bool:
        return self.is_message_handled(message_id)

    def is_message_handled(
        self,
        message_id: str,
        *,
        chat_id: str = "",
        chat_name: str = "",
        thread_id: str = "",
        sender_id: str = "",
        text: str = "",
        created_at_ms: int = 0,
    ) -> bool:
        clean_mid = (message_id or "").strip()
        clean_tid = (thread_id or "").strip().casefold()
        clean_sid = (sender_id or "").strip().casefold()
        with self._lock:
            if clean_mid and clean_mid in self._handled_msg_ids:
                return True
            if not clean_mid or not text.strip():
                return False
            for rec in reversed(self._cross_transport_records.values()):
                if not self._matches_cross_namespace(
                    rec,
                    message_id=clean_mid,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    thread_id=clean_tid,
                    sender_id=clean_sid,
                    text=text,
                    created_at_ms=created_at_ms,
                ):
                    continue
                if clean_mid.startswith("om_"):
                    rec.canonical_msg_id = clean_mid
                    if hasattr(self._last_web_transport, "bind_message_alias"):
                        self._last_web_transport.bind_message_alias(clean_mid, rec.web_msg_id)
                rec.sender_ids = rec.sender_ids | ({clean_sid} if clean_sid else frozenset())
                rec.thread_ids = rec.thread_ids | ({clean_tid} if clean_tid else frozenset())
                rec.chat_keys = rec.chat_keys | (self._chat_keys(chat_id) if chat_id else set())
                self._handled_msg_ids[clean_mid] = rec.recorded_at
                return True
        return False

    def should_suppress_openapi_mention(self, message: Mapping[str, Any], own_id: str) -> bool:
        mid = str(message.get("message_id") or "").strip()
        cid = str(message.get("chat_id") or "").strip()
        tid = str(message.get("thread_id") or message.get("root_id") or "").strip()
        rto = str(message.get("reply_to") or message.get("parent_id") or "").strip()
        raw = message.get("content", message.get("text", ""))
        txt = (
            str(raw.get("text") or raw.get("content") or "")
            if isinstance(raw, Mapping)
            else str(raw or "")
        )
        ms = _parse_created_ms(message.get("create_time"))
        oids = {own_id.strip()} if own_id and own_id.strip() else None
        return bool(
            is_courtesy_or_ack_only(txt)
            or self.is_message_handled(mid, chat_id=cid, thread_id=tid, text=txt, created_at_ms=ms)
            or self.has_owner_replied(
                chat_id=cid,
                thread_id=tid,
                reply_to=rto,
                message_id=mid,
                created_at_ms=ms,
                owner_ids=oids,
            )
        )

    def record_owner_pushed_message(
        self, pushed: PushedMessage, user_msg: FeishuUserMessage, *, web_transport: Any = None
    ) -> None:
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
            if store.get(k) is None or _reply_mark_is_newer_or_equal(mark, store[k]):
                store[k] = mark
                store.move_to_end(k)
                while len(store) > _MAX_REPLY_TRACKER:
                    store.popitem(last=False)

        with self._lock:
            for ck in chat_keys:
                _put_mark(
                    self._owner_topic_replies if topic_id else self._owner_chat_replies,
                    (ck, topic_id) if topic_id else ck,
                )
            for parent_ref in {
                msg.parent_id.strip(),
                msg.root_id.strip(),
                (user_msg.parent_id or "").strip(),
                (user_msg.root_id or "").strip(),
                topic_id,
            } - {""}:
                _put_mark(self._owner_parent_replies, parent_ref)
                alias = (
                    web_transport.get_message_alias(parent_ref)
                    if hasattr(web_transport, "get_message_alias")
                    else None
                )
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
        chat_keys = self._chat_keys(chat_id, web_transport)
        clean_tid, clean_mid, clean_reply_to = (
            (thread_id or "").strip(),
            (message_id or "").strip(),
            (reply_to or "").strip(),
        )

        def _mark_is_newer(mark: _OwnerReplyMark, da: bool = False) -> bool:
            return bool(not mark.message_id or mark.message_id != clean_mid) and _is_newer_reply(
                mark.create_time_ms,
                mark.position,
                mark.topic_position,
                created_at_ms,
                position,
                topic_position,
                da,
            )

        with self._lock:
            for mid, da in ((clean_mid, True), (clean_reply_to, False)):
                if (
                    mid
                    and mid in self._owner_parent_replies
                    and _mark_is_newer(self._owner_parent_replies[mid], da)
                ):
                    return True
            for ck in chat_keys:
                mark = (
                    self._owner_topic_replies.get((ck, clean_tid))
                    if clean_tid
                    else self._owner_chat_replies.get(ck)
                )
                if mark is not None and _mark_is_newer(mark):
                    return True

        msg_cache = getattr(web_transport, "_msg_cache", None) if owner_ids else None
        if isinstance(msg_cache, Mapping) and owner_ids:
            for cached in msg_cache.values():
                if (
                    not isinstance(cached, WebMessage)
                    or cached.recalled
                    or not cached.id
                    or cached.id == clean_mid
                    or cached.from_id not in owner_ids
                    or (
                        describe(cached.type, cached.content, "").strip().lower().startswith("/ai")
                        and cached.parent_id != clean_mid
                    )
                    or not (chat_keys & self._chat_keys(cached.chat_id, web_transport))
                ):
                    continue
                direct = cached.parent_id == clean_mid or cached.root_id == clean_mid
                same_p = bool(clean_reply_to) and cached.parent_id == clean_reply_to
                ctopic = (cached.thread_id or cached.root_id if is_in_topic(cached) else "").strip()
                tpos = int(getattr(cached, "topic_position", None) or 0)
                if (
                    direct
                    or same_p
                    or (bool(clean_tid) and ctopic == clean_tid)
                    or (not clean_tid and not ctopic)
                ) and _is_newer_reply(
                    cached.create_time_ms,
                    cached.position,
                    tpos,
                    created_at_ms,
                    position,
                    topic_position,
                    direct,
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
        cid = thread_id or chat_id
        if not cid or not hasattr(user_client, "list_messages_page"):
            return False
        try:
            async with asyncio.timeout(2.5):
                page = await user_client.list_messages_page(
                    container_id_type="thread" if thread_id else "chat",
                    container_id=cid,
                    sort_type="ByCreateTimeDesc",
                    page_size=20,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        msg_cache = getattr(web_transport, "_msg_cache", None)
        for item in getattr(page, "items", ()):
            if (
                not isinstance(item, FeishuUserMessage)
                or item.message_id == message_id
                or item.sender_open_id not in owner_ids
                or (item.text.strip().lower().startswith("/ai") and item.parent_id != message_id)
            ):
                continue
            direct = item.parent_id == message_id or item.root_id == message_id
            wm = msg_cache.get(item.message_id) if isinstance(msg_cache, Mapping) else None
            wpos = wm.position if wm is not None else 0
            wtpos = int(getattr(wm, "topic_position", None) or 0) if wm is not None else 0
            if (
                direct
                or (bool(thread_id) and (item.thread_id == thread_id or item.root_id == thread_id))
                or (not thread_id and not item.thread_id)
            ) and _is_newer_reply(
                item.create_time_ms, wpos, wtpos, created_at_ms, position, topic_position, direct
            ):
                return True
        return False

    async def _ensure_chat_metadata(self, web_chat_id: str, web_transport: Any) -> tuple[str, str]:
        if web_transport is None or not web_chat_id:
            return "", ""
        chat_cache = getattr(web_transport, "_chat_cache", {})
        open_map = getattr(web_transport, "_web_to_open_chat", {})
        cached = chat_cache.get(web_chat_id) if isinstance(chat_cache, Mapping) else None
        open_chat_id = str(
            open_map.get(web_chat_id) or getattr(cached, "open_chat_id", "") or ""
        ).strip()
        chat_name = str(getattr(cached, "name", "") or "").strip()
        web_client = getattr(web_transport, "web_client", None) or getattr(
            web_transport, "_web", None
        )
        if (
            cached is None
            and (not open_chat_id or not chat_name)
            and web_client is not None
            and hasattr(web_client, "chats")
        ):
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
        if open_chat_id or chat_name:
            with self._lock:
                self._link_chat_pair(web_chat_id, open_chat_id, chat_name)
        return open_chat_id, chat_name

    async def _deliver_event(self, event: ExternalEvent, cfg: FeishuRealtimeEventConfig) -> bool:
        if self._event_sink is not None:
            res = await self._event_sink(event)
            return (
                res
                if isinstance(res, bool)
                else (
                    res.get("status") in {"accepted", "duplicate", True}
                    if isinstance(res, Mapping)
                    else True
                )
            )
        if cfg.events_url:
            hdrs = {"Content-Type": "application/json"}
            if tok := os.environ.get("CLAUDE_HUB_DOT_EVENTS_TOKEN", "").strip():
                hdrs["Authorization"] = f"Bearer {tok}"
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                resp = await client.post(
                    cfg.events_url, content=event.model_dump_json().encode("utf-8"), headers=hdrs
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
        msg = pushed.message
        if pushed.removed or msg.recalled or msg.type == _rich.TYPE_SYSTEM:
            return False
        eff_owner_ids = {oid.strip() for oid in (owner_ids or set()) if oid and oid.strip()}
        if (
            is_owner_sender
            or msg.from_id in eff_owner_ids
            or user_msg.sender_open_id in eff_owner_ids
        ):
            self.record_owner_pushed_message(pushed, user_msg, web_transport=web_transport)
            return False
        cfg = self.resolve_config()
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
        only_at_all = bool(mentioned_ids) and set(mentioned_ids) <= {"all", "@all"}
        is_at_me = bool(eff_owner_ids.intersection(mentioned_ids)) or (
            bool(pushed.at_me) and not only_at_all
        )
        open_chat_id, chat_name = await self._ensure_chat_metadata(web_chat_id, web_transport)
        chat_keys = self._chat_keys(web_chat_id, web_transport) | (
            self._chat_keys(open_chat_id, web_transport) if open_chat_id else set()
        )
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
            in_topic = bool(
                topic_id and any((ck, topic_id) in self._owner_topic_replies for ck in chat_keys)
            )
            if (
                (pushed.chat_type == _rich.CHAT_P2P or in_topic)
                and not is_courtesy_or_ack_only(full_text)
                and (
                    _SUBSTANTIVE_ISSUE_RE.search(full_text)
                    or "?" in full_text
                    or "？" in full_text
                    or bool(urls)
                )
            ):
                self._add_fallback_record(
                    user_msg.message_id,
                    user_msg.message_id,
                    "uncovered_message",
                    chat_name,
                    chat_keys,
                    topic_id,
                    user_msg.sender_open_id,
                    full_text,
                    user_msg.create_time_ms,
                    "",
                    user_msg.text,
                )
            return False

        alias_id = (
            web_transport.get_message_alias(user_msg.message_id)
            if hasattr(web_transport, "get_message_alias")
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
        chk_kw: dict[str, Any] = {
            "chat_name": chat_name,
            "thread_id": topic_id,
            "text": full_text,
            "created_at_ms": user_msg.create_time_ms,
        }
        if self.is_message_handled(
            canonical_msg_id,
            chat_id=open_chat_id or web_chat_id,
            sender_id=user_msg.sender_open_id,
            **chk_kw,
        ) or self.is_message_handled(
            user_msg.message_id,
            chat_id=web_chat_id,
            sender_id=msg.from_id or user_msg.sender_open_id,
            **chk_kw,
        ):
            return False

        async def _suppress(courtesy: bool) -> bool:
            self.suppressed_count += 1
            self.suppressed_courtesy_count += int(courtesy)
            self.suppressed_replied_count += int(not courtesy)
            self._record_handled(
                user_msg.message_id,
                canonical_msg_id,
                chat_keys,
                chat_name,
                {topic_id} if topic_id else None,
                sender_id_set,
                full_text,
                user_msg.text,
                user_msg.create_time_ms,
            )
            for sup_key in {user_msg.message_id, canonical_msg_id} - {""}:
                try:
                    await record_suppressed_external_event("feishu", sup_key[:256])
                except Exception:
                    pass
            return False

        if not is_watch_group_url and (
            is_courtesy_or_ack_only(user_msg.text) or is_courtesy_or_ack_only(full_text)
        ):
            return await _suppress(courtesy=True)

        reply_kw: dict[str, Any] = {
            "chat_id": web_chat_id,
            "thread_id": topic_id,
            "message_id": user_msg.message_id,
            "created_at_ms": user_msg.create_time_ms,
            "position": msg.position,
            "topic_position": int(getattr(msg, "topic_position", None) or 0),
            "web_transport": web_transport,
            "owner_ids": eff_owner_ids,
        }
        if self.has_owner_replied(
            reply_to=msg.parent_id or msg.root_id, **reply_kw
        ) or await self._verify_unanswered_via_client(user_client=user_client, **reply_kw):
            return await _suppress(courtesy=False)

        parsed_url: HttpUrl | None = None
        if primary_url_str := (matched_urls[0] if matched_urls else (urls[0] if urls else "")):
            try:
                parsed_url = HttpUrl(primary_url_str)
            except Exception:
                pass
        clabel = (chat_name or open_chat_id or web_chat_id)[:120]
        parts = [p for p in (clabel, user_msg.sender_open_id[:80], full_text[:600]) if p]
        payload: dict[str, JsonValue] = {
            "chat_id": open_chat_id or web_chat_id,
            "web_chat_id": web_chat_id,
            "chat_name": chat_name[:200],
            "chat_type": "p2p" if pushed.chat_type == _rich.CHAT_P2P else "group",
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
            event_type="im.mention" if is_at_me else "im.watch_chat_url",
            dedupe_key=canonical_msg_id[:256],
            severity="urgent",
            title="Feishu mention" if is_at_me else "Feishu watch-group issue link",
            summary=" · ".join(parts)[:1000],
            url=parsed_url,
            payload=payload,
        )
        delivery_err: str | None = None
        try:
            if not await self._deliver_event(event, cfg):
                delivery_err = "rejected"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delivery_err = str(exc) or type(exc).__name__
            logger.warning(
                "Feishu real-time event delivery failed for %s", canonical_msg_id, exc_info=True
            )
        if delivery_err is not None:
            self._add_fallback_record(
                canonical_msg_id,
                user_msg.message_id,
                "failed_delivery",
                chat_name,
                chat_keys,
                topic_id,
                user_msg.sender_open_id,
                full_text,
                user_msg.create_time_ms,
                canonical_msg_id if canonical_msg_id != user_msg.message_id else "",
                user_msg.text,
                matched_urls or urls,
                event,
                delivery_err,
            )
            return False
        self.emitted_count += 1
        self._record_handled(
            user_msg.message_id,
            canonical_msg_id,
            chat_keys,
            chat_name,
            {topic_id} if topic_id else None,
            sender_id_set,
            full_text,
            user_msg.text,
            user_msg.create_time_ms,
        )
        return True


_DEFAULT_REALTIME_ROUTER: FeishuRealtimeEventRouter | None = None


def get_feishu_realtime_event_router() -> FeishuRealtimeEventRouter:
    global _DEFAULT_REALTIME_ROUTER
    if _DEFAULT_REALTIME_ROUTER is None:
        _DEFAULT_REALTIME_ROUTER = FeishuRealtimeEventRouter()
    return _DEFAULT_REALTIME_ROUTER


def is_feishu_realtime_stream_healthy() -> bool:
    from claude_hub.services.feishu_ai_poller import _DEFAULT_AI_POLLER

    return bool(getattr(_DEFAULT_AI_POLLER, "realtime_stream_healthy", False))
