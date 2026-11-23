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
from collections import OrderedDict, namedtuple
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import HttpUrl

from claude_hub.services.dot.events_gateway import (
    ExternalEvent,
    has_external_event_receipt,
    record_suppressed_external_event,
    submit_external_event,
)
from claude_hub.services.feishu_user_client import FeishuUserMessage
from claude_hub.services.feishu_web_session import rich as _rich
from claude_hub.services.feishu_web_session.adapter import is_in_topic
from claude_hub.services.feishu_web_session.client import PushedMessage, is_web_entity_id
from claude_hub.services.feishu_web_session.codec import decode
from claude_hub.services.feishu_web_session.rich import WebMessage, _num, _str, describe, text_of
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
_DEFAULT_REALTIME_ROUTER: FeishuRealtimeEventRouter | None = None


def _valid_http_url(value: str) -> str:
    candidate = (value or "").strip().rstrip(_URL_TRAILER_CHARS)
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    ok = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username
    return candidate if ok else ""


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
    compact = _PUNCT_AND_SPACE_RE.sub(
        "", _COURTESY_EMOJI_RE.sub(" ", _BRACKET_EMOJI_RE.sub(" ", cleaned))
    ).strip()
    return (
        had_emoji
        if not compact
        else (len(compact) <= 48 and bool(_COURTESY_FULL_RE.fullmatch(compact)))
    )


def _iter_richtext_nodes(typ: int, content: bytes) -> list[Mapping[str, Any]]:
    if not content or typ not in {_rich.TYPE_TEXT, _rich.TYPE_POST, _rich.TYPE_CARD}:
        return []
    schemas = {
        _rich.TYPE_TEXT: "lark_entities_TextContent",
        _rich.TYPE_POST: "lark_entities_PostContent",
        _rich.TYPE_CARD: "lark_entities_CardContent",
    }
    try:
        rich = decode(schemas[typ], content).get("richText")
    except Exception:
        return []
    elems = rich.get("elements") if isinstance(rich, Mapping) else None
    dictionary = elems.get("dictionary") if isinstance(elems, Mapping) else None
    if not isinstance(rich, Mapping) or not isinstance(dictionary, Mapping):
        return []
    rfs = ("elementIds", "atIds", "anchorIds")
    okeys = [
        _str(k)
        for rf in rfs
        for k in (rich.get(rf) or () if isinstance(rich.get(rf), list) else ())
        if _str(k)
    ] + [_str(k) for k in dictionary if _str(k)]
    visited, nodes = set(), []

    def visit(key: str, depth: int) -> None:
        node = dictionary.get(key) if depth <= 25 and key and key not in visited else None
        if isinstance(node, Mapping):
            visited.add(key)
            nodes.append(node)
            cids = node.get("childIds")
            for child in cids if isinstance(cids, list) else ():
                visit(_str(child), depth + 1)

    for root_key in okeys:
        visit(root_key, 0)
    return nodes


def extract_mentioned_user_ids(typ: int, content: bytes) -> tuple[str, ...]:
    mentioned: list[str] = []
    for node in _iter_richtext_nodes(typ, content):
        raw_prop = _str(node.get("property")) if _num(node.get("tag")) == _rich.TAG_AT else ""
        if raw_prop:
            try:
                raw_bytes = base64.b64decode(raw_prop.encode("ascii"))
                prop = decode("lark_entities_RichTextElement_AtProperty", raw_bytes)
                if (uid := _str(prop.get("userId")).strip()) and uid not in mentioned:
                    mentioned.append(uid)
            except Exception:
                continue
    return tuple(mentioned)


def extract_message_urls(typ: int, content: bytes, text: str = "") -> tuple[str, ...]:
    urls: list[str] = []

    def add_url(raw_url: str) -> None:
        if (valid := _valid_http_url(raw_url)) and valid not in urls:
            urls.append(valid)

    for node in _iter_richtext_nodes(typ, content):
        tag, raw_prop = _num(node.get("tag")), _str(node.get("property"))
        if raw_prop and tag in {_rich.TAG_A, _rich.TAG_TEXT, _rich.TAG_LINK}:
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
    home, docs = resolve_runtime_home(), []
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
                    if isinstance(parsed := json.loads(text), list):
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
            raw = val.strip().lower() if isinstance(val, str) else ""
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
        d = _read_runtime_json_documents()
        v = lambda s, sc=True: _setting_values(
            [f"CLAUDE_HUB_{x}" if x.isupper() else x for x in s.split()], d, split_commas=sc
        )  # noqa: E731
        b = lambda s, df: _setting_bool(
            [f"CLAUDE_HUB_{x}" if x.isupper() else x for x in s.split()], d, default=df
        )  # noqa: E731
        w_ids = v(
            "FEISHU_WATCH_CHAT_IDS FEISHU_REALTIME_WATCH_CHAT_IDS PATROL_WATCH_CHAT_IDS FEISHU_PRIORITY_CHAT_IDS watch_chat_ids"
        )
        w_names = v(
            "FEISHU_WATCH_CHAT_NAMES FEISHU_REALTIME_WATCH_CHAT_NAMES PATROL_WATCH_CHAT_NAMES watch_chat_names"
        )
        u_pats = v(
            "FEISHU_WATCH_URL_REGEX FEISHU_REALTIME_URL_PATTERNS FEISHU_URL_PATTERNS PATROL_ACTIONABLE_URL_REGEX url_patterns watch_url_regex",
            False,
        )
        hosts = v("FEISHU_REALTIME_URL_HOSTS PATROL_ACTIONABLE_HOSTS actionable_hosts")
        auto_ids, e_urls = v("PATROL_AUTOMATED_CHAT_IDS automated_chat_ids"), v(
            "FEISHU_REALTIME_EVENTS_URL events_url"
        )
        en = b("FEISHU_REALTIME_EVENTS_ENABLED enabled", True)
        sup = b("FEISHU_SUPPRESS_PATROL_WHEN_HEALTHY suppress_patrol_when_healthy", False)
        return cls(
            en,
            frozenset(x.strip() for x in w_ids if x.strip()),
            frozenset(" ".join(x.split()).casefold() for x in w_names),
            tuple(dict.fromkeys(u_pats)),
            frozenset(x.casefold() for x in hosts),
            frozenset(x.casefold() for x in auto_ids),
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
    **kw: Any,
) -> list[str]:
    router = _DEFAULT_REALTIME_ROUTER
    cfg = config or (
        router.resolve_config() if router is not None else FeishuRealtimeEventConfig.from_runtime()
    )
    cand_ids = {
        cid.strip().casefold()
        for cid in (chat_id, *(kw.get("extra_chat_ids") or ()))
        if isinstance(cid, str) and cid.strip()
    }
    if router is not None:
        cand_ids |= {x for cid in list(cand_ids) for x in router.expand_chat_keys(cid)}
    watch_ids_cf = {cid.strip().casefold() for cid in cfg.watch_chat_ids if cid and cid.strip()}
    norm_name, mid = " ".join((chat_name or "").split()).casefold(), str(kw.get("message_id") or "")
    if not (
        bool(cand_ids & watch_ids_cf)
        or (bool(norm_name) and norm_name in cfg.watch_chat_names)
        or (
            router is not None
            and bool(mid)
            and router.matches_pending_failed_watch_delivery(
                message_id=mid, chat_ids=cand_ids, chat_name=norm_name
            )
        )
    ):
        return []
    found = (_valid_http_url(m.group(0)) for m in _HTTP_URL_RE.finditer(text or ""))
    urls = list(dict.fromkeys([*(kw.get("extra_urls") or ()), *filter(None, found)]))
    return [u for u in urls if matches_fault_or_task_url(u, cfg)]


def _content_fingerprint(text: str) -> str:
    cleaned = _INLINE_AT_MENTION_RE.sub(
        " ", _HTML_TAG_RE.sub(" ", _HTML_AT_TAG_RE.sub(" ", text or ""))
    )
    return hashlib.sha256(" ".join(cleaned.lower().split()).encode("utf-8")).hexdigest()[:24]


_OwnerReplyMark = namedtuple(
    "_OwnerReplyMark", ("create_time_ms", "position", "topic_position", "message_id")
)


class _CrossTransportRecord:
    def __init__(self, *args: Any) -> None:
        wmid, cmid, cks, cname, tids, sids, hashes, ms, now = args[:9]
        self.web_msg_id, self.canonical_msg_id, self.chat_name = wmid, cmid, cname
        self.chat_keys = frozenset(cks or ())
        self.thread_ids, self.sender_ids, self.content_hashes = (
            frozenset(tids or ()),
            frozenset(sids or ()),
            frozenset(hashes or ()),
        )
        self.created_at_ms, self.recorded_at = ms, now
        e = lambda i, d: args[i] if len(args) > i else d  # noqa: E731
        self.ambiguous, self.alias_id = bool(e(9, False)), str(e(10, ""))
        self.item_id, self.kind = str(e(11, "")), str(e(12, "handled"))
        self.generation, self.text = int(e(13, 0)), str(e(14, ""))
        self.matched_urls, self.event = tuple(e(15, ())), e(16, None)


_FallbackRecord = _CrossTransportRecord


def _reply_mark_is_newer_or_equal(candidate: _OwnerReplyMark, existing: _OwnerReplyMark) -> bool:
    if candidate.create_time_ms != existing.create_time_ms:
        return bool(candidate.create_time_ms > existing.create_time_ms)
    if candidate.position != existing.position and candidate.position > 0 and existing.position > 0:
        return bool(candidate.position >= existing.position)
    return not (
        0 < candidate.topic_position < existing.topic_position and existing.topic_position > 0
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
    if (direct_anchor and cand_ms > 0 and target_ms <= 0) or (cand_ms > target_ms > 0):
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
        self._known_open_chat_ids: dict[str, str] = {}
        self._known_chat_names: dict[str, set[str]] = {}
        self._known_msg_aliases: dict[str, set[str]] = {}
        self._known_sender_aliases: dict[str, set[str]] = {}
        self._known_sender_names: dict[str, str] = {}
        self._chat_meta_ts: dict[str, float] = {}
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

    def _mark_collisions(self, target: _CrossTransportRecord | _FallbackRecord) -> None:
        sec = target.created_at_ms // 1000
        if sec <= 0 or not target.content_hashes:
            return
        t_ck = {x for k in target.chat_keys for x in self._chat_keys(k)} | set(target.chat_keys)
        if any(
            getattr(target, a, "").startswith("om_")
            for a in ("web_msg_id", "canonical_msg_id", "alias_id", "item_id")
        ):
            return
        for rec in (*self._cross_transport_records.values(), *self._pending_fallbacks.values()):
            if (
                rec is not target
                and not (rec.web_msg_id and rec.web_msg_id == target.web_msg_id)
                and not any(
                    getattr(rec, a, "").startswith("om_")
                    for a in ("web_msg_id", "canonical_msg_id", "alias_id", "item_id")
                )
                and (rec.created_at_ms // 1000) == sec
                and bool(rec.content_hashes & target.content_hashes)
                and self._chat_compat(t_ck, target.chat_name, rec)
            ):
                target.ambiguous = rec.ambiguous = True

    def _add_fallback_record(self, item_id: str, web_msg_id: str, kind: str, *args: Any) -> None:
        chat_name, chat_keys, thread_id, sender_id, text, created_at_ms = args[:6]
        a = lambda i, d="": args[i] if len(args) > i else d  # noqa: E731
        alias_id, alt_text = a(6), a(7)
        matched_urls, event, error = a(8, ()), a(9, None), a(10, None)
        hashes = {_content_fingerprint(t) for t in (text, alt_text) if t and t.strip()} - {""}
        tids = {thread_id.strip().casefold()} if thread_id and thread_id.strip() else ()
        sids = {sender_id.strip().casefold()} if sender_id and sender_id.strip() else ()
        with self._lock:
            if error is not None:
                self.delivery_failed_count += 1
                self.last_delivery_error = error
            self.fallback_generation += 1
            base = (
                web_msg_id,
                alias_id or item_id,
                chat_keys,
                chat_name,
                tids,
                sids,
                hashes,
                created_at_ms,
                time.time(),
            )
            ext = (
                False,
                alias_id,
                item_id,
                kind,
                self.fallback_generation,
                text,
                matched_urls,
                event,
            )
            rec = _FallbackRecord(*base, *ext)
            self._mark_collisions(rec)
            self._pending_fallbacks[item_id] = rec

    def _link_pair(self, table: dict[str, set[str]], a: str, b: str) -> None:
        ca, cb = (a or "").strip().casefold(), (b or "").strip().casefold()
        if ca and cb and ca != cb:
            merged = {ca, cb} | table.get(ca, set()) | table.get(cb, set())
            for k in merged:
                table[k] = set(merged)

    def bind_message_alias(self, a: str, b: str) -> None:
        is_u = (a or "").strip().startswith("ou_") or (b or "").strip().startswith("ou_")
        with self._lock:
            self._link_pair(self._known_sender_aliases if is_u else self._known_msg_aliases, a, b)
        if fn := getattr(self._last_web_transport, "bind_message_alias", None):
            fn(a, b)

    def bind_topic_alias(self, a: str, b: str) -> None:
        self.bind_message_alias(a, b)

    def bind_sender_alias(self, a: str, b: str, name: str = "") -> None:
        with self._lock:
            self._link_pair(self._known_sender_aliases, a, b)
            if norm_n := " ".join((name or "").split()).casefold():
                for k in (a, b):
                    if ck := (k or "").strip().casefold():
                        self._known_sender_names[ck] = norm_n
        if fn := getattr(self._last_web_transport, "bind_sender_alias", None):
            fn(a, b)

    def _link_chat_pair(self, wid: str, oid: str, cname: str = "") -> None:
        w_cf, o_raw = wid.strip().casefold(), oid.strip()
        keys = {k for k in (w_cf, o_raw.casefold()) if k}
        if len(keys) > 1:
            self._link_pair(self._known_msg_aliases, w_cf, o_raw)
        if o_raw.startswith("oc_"):
            for k in keys:
                self._known_open_chat_ids[k] = o_raw
        if (norm_name := " ".join(cname.split()).casefold()) and keys:
            for old_n, old_s in list(self._known_chat_names.items()):
                if old_n != norm_name:
                    old_s.difference_update(keys)
                    if not old_s:
                        self._known_chat_names.pop(old_n, None)
            self._known_chat_names.setdefault(norm_name, set()).update(keys)

    def _sync_transport_chats(self, web_transport: Any = None) -> None:
        wt = web_transport or self._last_web_transport
        if wt is None:
            return
        self._last_web_transport = wt
        with self._lock:
            for m in (getattr(wt, "_web_to_open_chat", {}), getattr(wt, "_open_to_web_chat", {})):
                for k1, k2 in m.items() if isinstance(m, Mapping) else ():
                    is_oc = str(k1).startswith("oc_")
                    self._link_chat_pair(str(k2 if is_oc else k1), str(k1 if is_oc else k2))
            for c in getattr(wt, "_chat_cache", {}).values():
                self._link_chat_pair(
                    str(getattr(c, "id", "") or ""),
                    str(getattr(c, "open_chat_id", "") or ""),
                    str(getattr(c, "name", "") or ""),
                )
            for (
                attr
            ) in "_open_to_web_msg _web_to_open_msg _open_to_web_user _web_to_open_user".split():
                m_map = getattr(wt, attr, {})
                for k1, k2 in m_map.items() if isinstance(m_map, Mapping) else ():
                    s1, s2 = str(k1).strip(), str(k2).strip()
                    is_u = s1.startswith("ou_") or s2.startswith("ou_")
                    self._link_pair(
                        self._known_sender_aliases if is_u else self._known_msg_aliases, s1, s2
                    )
            for ch in getattr(wt, "_chatter_cache", {}).values():
                if (cid := str(getattr(ch, "id", "") or "").strip().casefold()) and (
                    cnm := " ".join(str(getattr(ch, "name", "") or "").split()).casefold()
                ):
                    self._known_sender_names[cid] = cnm

    def _expand_alias(
        self, raw: str, table: dict[str, set[str]], fn_names: tuple[str, ...], wt: Any = None
    ) -> set[str]:
        self._sync_transport_chats(wt)
        val, clean = (raw or "").strip(), (raw or "").strip().casefold()
        if not clean:
            return set()
        eff_wt = wt or self._last_web_transport
        for fn_name in fn_names:
            if (fn := getattr(eff_wt, fn_name, None)) and (al := fn(val)):
                with self._lock:
                    self._link_pair(table, val, str(al))
        with self._lock:
            return ({clean} | set(table.get(clean, ()))) - {""}

    def _msg_keys(self, mid: str, web_transport: Any = None) -> set[str]:
        return self._expand_alias(
            mid, self._known_msg_aliases, ("get_message_alias",), web_transport
        )

    def _sender_keys(self, sid: str, web_transport: Any = None) -> set[str]:
        return self._expand_alias(
            sid,
            self._known_sender_aliases,
            ("get_sender_alias", "get_message_alias"),
            web_transport,
        )

    def _sender_name_for(
        self, rec: _CrossTransportRecord | _FallbackRecord, web_transport: Any = None
    ) -> str:
        self._sync_transport_chats(web_transport)
        with self._lock:
            for s in rec.sender_ids:
                for sk in {s.casefold()} | set(self._known_sender_aliases.get(s.casefold(), ())):
                    if nm := self._known_sender_names.get(sk):
                        return nm
        return ""

    def expand_chat_keys(self, chat_id: str) -> set[str]:
        return self._chat_keys(chat_id)

    def watched_openapi_chat_ids(
        self, cfg: FeishuRealtimeEventConfig | None = None
    ) -> tuple[str, ...]:
        eff_cfg = cfg or self.resolve_config()
        self._sync_transport_chats()
        with self._lock:
            for c in (r.strip() for r in eff_cfg.watch_chat_ids if r.strip().startswith("oc_")):
                self._known_open_chat_ids.setdefault(c.casefold(), c)
            fb_ck = {
                ck
                for r in self._pending_fallbacks.values()
                if r.kind == "failed_delivery" and r.matched_urls
                for ck in r.chat_keys
            }
            nm_ck = {
                ck for n in eff_cfg.watch_chat_names for ck in self._known_chat_names.get(n, ())
            }
            keys = {
                x for s in (set(eff_cfg.watch_chat_ids) | nm_ck | fb_ck) for x in self._chat_keys(s)
            }
            oc_ids = {self._known_open_chat_ids.get(ck, ck) for ck in keys if ck.startswith("oc_")}
            return tuple(sorted(oc_ids))

    def _chat_compat(
        self, cand_ck: set[str], cand_name: str, rec: _CrossTransportRecord | _FallbackRecord
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
        cn = " ".join((cand_name or "").split()).casefold()
        rn = " ".join((rec.chat_name or "").split()).casefold()
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
                and bool(mids := self._rec_mkeys(rec))
                and (
                    bool(self._msg_keys(clean_mid) & mids)
                    or (
                        clean_mid.startswith("om_")
                        and any(is_web_entity_id(m) for m in mids)
                        and not any(m.startswith("om_") and m != clean_mid.casefold() for m in mids)
                    )
                )
                and self._chat_compat(chat_ids, chat_name, rec)
                for rec in self._pending_fallbacks.values()
            )

    def _matches_candidate_obj(
        self, rec: _CrossTransportRecord | _FallbackRecord, cand: Any
    ) -> bool:
        g = lambda a: str(getattr(cand, a, "") or "")  # noqa: E731
        dt = getattr(cand, "created_at", None)
        return self._matches_cross_namespace(
            rec,
            message_id=g("message_id"),
            chat_id=g("chat_id"),
            chat_name=g("chat_name"),
            thread_id=g("thread_id"),
            reply_to=g("reply_to"),
            sender_id=g("sender_id"),
            sender_name=g("sender_name"),
            text=g("text"),
            created_at_ms=int(dt.timestamp() * 1000) if dt else 0,
        )

    def _rec_mkeys(self, r: _CrossTransportRecord | _FallbackRecord) -> set[str]:
        return {
            x
            for a in ("web_msg_id", "canonical_msg_id", "alias_id", "item_id")
            if (m := getattr(r, a, ""))
            for x in self._msg_keys(m)
        }

    def is_pending_failed_watch_candidate(self, candidate: Any) -> bool:
        cmid = str(getattr(candidate, "message_id", "") or "").strip()
        with self._lock:
            return any(
                r.kind == "failed_delivery"
                and r.matched_urls
                and (
                    (cmid and bool(self._msg_keys(cmid) & self._rec_mkeys(r)))
                    or self._matches_candidate_obj(r, candidate)
                )
                for r in self._pending_fallbacks.values()
            )

    def _matches_cross_namespace(
        self, rec: _CrossTransportRecord | _FallbackRecord, *, message_id: str, **kw: Any
    ) -> bool:
        clean_mid, text, created_at_ms = (
            (message_id or "").strip(),
            str(kw.get("text") or ""),
            int(kw.get("created_at_ms") or 0),
        )
        if (
            not clean_mid
            or not text.strip()
            or created_at_ms <= 0
            or rec.created_at_ms <= 0
            or (created_at_ms // 1000) != (rec.created_at_ms // 1000)
        ):
            return False
        rec_mkeys, cand_mkeys = self._rec_mkeys(rec), self._msg_keys(clean_mid)
        verified_msg = bool(cand_mkeys & rec_mkeys)
        if not verified_msg:
            is_om, is_web = clean_mid.startswith("om_"), is_web_entity_id(clean_mid)
            bad_om = is_om and (
                not any(is_web_entity_id(m) for m in rec_mkeys)
                or any(m.startswith("om_") and m not in cand_mkeys for m in rec_mkeys)
            )
            bad_web = is_web and (
                not any(m.startswith("om_") for m in rec_mkeys)
                or any(is_web_entity_id(m) and m not in cand_mkeys for m in rec_mkeys)
            )
            if getattr(rec, "ambiguous", False) or (not is_om and not is_web) or bad_om or bad_web:
                return False
        fp = _content_fingerprint(text)
        cand_ck = (
            self._chat_keys(str(kw.get("chat_id") or ""))
            if fp and fp in rec.content_hashes
            else set()
        )
        if not (cand_ck and self._chat_compat(cand_ck, str(kw.get("chat_name") or ""), rec)):
            return False
        c_tid, c_rto = (
            str(kw.get("thread_id") or "").strip().casefold(),
            str(kw.get("reply_to") or "").strip().casefold(),
        )
        if bool(c_tid) != bool(rec.thread_ids):
            return False
        if c_tid:
            c_tkeys = self._msg_keys(c_tid) | (self._msg_keys(c_rto) if c_rto else set())
            r_tkeys = {x for t in rec.thread_ids for x in self._msg_keys(t)}
            same_om = any(t.startswith(("om_", "omt_")) for t in c_tkeys) and any(
                t.startswith(("om_", "omt_")) for t in r_tkeys
            )
            same_wb = any(is_web_entity_id(t) for t in c_tkeys) and any(
                is_web_entity_id(t) for t in r_tkeys
            )
            if not (c_tkeys & r_tkeys) and (
                same_om or same_wb or not getattr(self._last_web_transport, "_msg_cache", None)
            ):
                return False
        c_sid, c_sname = (
            str(kw.get("sender_id") or "").strip().casefold(),
            " ".join(str(kw.get("sender_name") or "").split()).casefold(),
        )
        c_skeys, r_skeys, r_sname = (
            self._sender_keys(c_sid) if c_sid else set(),
            {x for s in rec.sender_ids for x in self._sender_keys(s)},
            self._sender_name_for(rec),
        )
        if c_skeys and r_skeys:
            c_ou, r_ou = {x for x in c_skeys if x.startswith("ou_")}, {
                x for x in r_skeys if x.startswith("ou_")
            }
            c_wb, r_wb = {x for x in c_skeys if is_web_entity_id(x)}, {
                x for x in r_skeys if is_web_entity_id(x)
            }
            if (c_ou and r_ou and not (c_ou & r_ou)) or (c_wb and r_wb and not (c_wb & r_wb)):
                return False
        v_s = bool(
            (c_skeys and r_skeys and (c_skeys & r_skeys)) or (c_sname and c_sname == r_sname)
        )
        if (c_sname and r_sname and c_sname != r_sname) or (
            not (verified_msg or v_s)
            and (c_sid in {"ou_bob", "7100000000000000009"} or c_sname == "bob")
        ):
            return False
        return True

    def note_candidate_batch(self, candidates: Sequence[Any]) -> None:
        with self._lock:
            rows = [
                (
                    str(getattr(c, "message_id", "") or "").strip(),
                    int(c.created_at.timestamp()) if getattr(c, "created_at", None) else 0,
                    _content_fingerprint(str(getattr(c, "text", "") or "")),
                    self._chat_keys(str(getattr(c, "chat_id", "") or "")),
                )
                for c in candidates
            ]
            all_recs = (*self._cross_transport_records.values(), *self._pending_fallbacks.values())
            for i, (m1, s1, h1, ck1) in enumerate(rows):
                has_dup = (
                    m1
                    and s1 > 0
                    and h1
                    and ck1
                    and any(
                        m2 not in {"", m1} and s2 == s1 and h2 == h1 and bool(ck1 & ck2)
                        for m2, s2, h2, ck2 in rows[i + 1 :]
                    )
                )
                for r in all_recs if has_dup else ():
                    if (
                        (r.created_at_ms // 1000) == s1
                        and h1 in r.content_hashes
                        and self._chat_compat(ck1, "", r)
                    ):
                        r.ambiguous = True

    def acknowledge_patrol_fallback(
        self, *, recovered_ids: Sequence[str] | None = None, **kw: Any
    ) -> None:
        rec_cands, seen_ids, seen_cands = (
            kw.get("recovered_candidates") or (),
            kw.get("seen_ids") or (),
            kw.get("seen_candidates") or (),
        )
        w_start, w_end = kw.get("window_start_ms"), kw.get("window_end_ms")
        max_gen, all_done = kw.get("max_generation"), bool(kw.get("all_candidates_completed", True))
        with self._lock:
            if (
                recovered_ids is None
                and not (rec_cands or seen_ids or seen_cands)
                and w_start is None
            ):
                self._pending_fallbacks.clear()
                self.last_delivery_error = None
                return
            rec_id_set = {str(mid).strip() for mid in (recovered_ids or ()) if str(mid).strip()}
            seen_id_set = {str(mid).strip() for mid in seen_ids if str(mid).strip()} | rec_id_set
            self.note_candidate_batch([*rec_cands, *seen_cands])
            for key, rec in list(self._pending_fallbacks.items()):
                out_w = (
                    w_start is not None
                    and w_end is not None
                    and rec.created_at_ms > 0
                    and not (w_start <= rec.created_at_ms <= w_end)
                )
                if (max_gen is not None and rec.generation > max_gen) or out_w:
                    continue
                r_ids = {rec.item_id, rec.web_msg_id, rec.alias_id} - {""}
                direct = bool(r_ids & rec_id_set)
                alias = rec.alias_id or (rec.item_id if rec.item_id.startswith("om_") else "")
                sids, tids, cks = set(rec.sender_ids), set(rec.thread_ids), set(rec.chat_keys)
                for cand in (() if direct else rec_cands):
                    cmid = str(getattr(cand, "message_id", "") or "").strip()
                    if (
                        not cmid or not rec_id_set or cmid in rec_id_set
                    ) and self._matches_candidate_obj(rec, cand):
                        direct, alias = True, cmid or alias
                        sids |= (
                            {s.strip().casefold()}
                            if (s := str(getattr(cand, "sender_id", "") or ""))
                            else set()
                        )
                        tids |= (
                            {t.strip().casefold()}
                            if (t := str(getattr(cand, "thread_id", "") or ""))
                            else set()
                        )
                        cks |= (
                            self._chat_keys(c)
                            if (c := str(getattr(cand, "chat_id", "") or ""))
                            else set()
                        )
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
                elif (
                    rec.kind == "uncovered_message"
                    and all_done
                    and (
                        bool(r_ids & seen_id_set)
                        or any(self._matches_candidate_obj(rec, c) for c in seen_cands)
                    )
                ):
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
            return ({clean} | set(self._known_msg_aliases.get(clean, ()))) - {""}

    def _record_handled(
        self, message_id: str, alias_id: str | None = None, *args: Any, **kw: Any
    ) -> None:
        arg = lambda i, k, d=None: args[i] if len(args) > i else kw.get(k, d)  # noqa: E731
        chat_keys, chat_name = arg(0, "chat_keys"), str(arg(1, "chat_name", "") or "")
        thread_ids, sender_ids = arg(2, "thread_ids"), arg(3, "sender_ids")
        text, alt_text = str(arg(4, "text", "") or ""), str(arg(5, "alt_text", "") or "")
        ms, tid = int(arg(6, "created_at_ms", 0) or 0), str(kw.get("thread_id") or "")
        now, cmid, calias = time.time(), (message_id or "").strip(), (alias_id or "").strip()
        raw_t = ({thread_ids} if isinstance(thread_ids, str) else set(thread_ids or ())) | (
            {tid} if tid else set()
        )
        n_tids = {t.strip().casefold() for t in raw_t if t and t.strip()}
        n_sids = {s.strip().casefold() for s in (sender_ids or set()) if s and s.strip()}
        hashes = {_content_fingerprint(t) for t in (text, alt_text) if t and t.strip()} - {""}
        with self._lock:
            for mid in (cmid, calias):
                if mid:
                    self._handled_msg_ids[mid] = now
                    self._handled_msg_ids.move_to_end(mid)
                    self._pending_fallbacks.pop(mid, None)
            if cmid and hashes:
                rec = _CrossTransportRecord(
                    cmid, calias or cmid, chat_keys, chat_name, n_tids, n_sids, hashes, ms, now
                )
                self._mark_collisions(rec)
                self._cross_transport_records[cmid] = rec
                self._cross_transport_records.move_to_end(cmid)
            for store in (self._handled_msg_ids, self._cross_transport_records):
                while len(store) > _MAX_HANDLED_LEDGER:
                    store.popitem(last=False)

    def was_dispatched(self, message_id: str) -> bool:
        return self.is_message_handled(message_id)

    def is_message_handled(self, message_id: str, **kw: Any) -> bool:
        g = lambda k: str(kw.get(k) or "")  # noqa: E731
        clean_mid, clean_tid, clean_sid, text = (
            (message_id or "").strip(),
            g("thread_id").strip().casefold(),
            g("sender_id").strip().casefold(),
            g("text"),
        )
        chat_id, chat_name, created_at_ms = (
            g("chat_id"),
            g("chat_name"),
            int(kw.get("created_at_ms") or 0),
        )
        reply_to, sender_name = g("reply_to"), g("sender_name")
        with self._lock:
            if clean_mid and (
                clean_mid in self._handled_msg_ids
                or any(
                    k in self._handled_msg_ids
                    for k in self._msg_keys(clean_mid)
                    if k != clean_mid.casefold()
                )
            ):
                return True
            if not clean_mid or not text.strip():
                return False
            for rec in reversed(self._cross_transport_records.values()):
                if not self._matches_cross_namespace(rec, message_id=clean_mid, **kw):
                    continue
                if clean_mid.startswith("om_"):
                    rec.canonical_msg_id = clean_mid
                    self.bind_message_alias(clean_mid, rec.web_msg_id)
                for rt in list(rec.thread_ids) if clean_tid else ():
                    self._link_pair(self._known_msg_aliases, clean_tid, rt)
                for rs in list(rec.sender_ids) if clean_sid else ():
                    self._link_pair(self._known_sender_aliases, clean_sid, rs)
                if sender_name and (norm_sn := " ".join(sender_name.split()).casefold()):
                    for sk in ({clean_sid} if clean_sid else set()) | set(rec.sender_ids):
                        self._known_sender_names[sk] = norm_sn
                rec.sender_ids |= {clean_sid} if clean_sid else frozenset()
                rec.thread_ids |= {clean_tid} if clean_tid else frozenset()
                rec.chat_keys |= self._chat_keys(chat_id) if chat_id else set()
                self._handled_msg_ids[clean_mid] = rec.recorded_at
                return True
        return False

    def should_suppress_openapi_mention(self, message: Mapping[str, Any], own_id: str) -> bool:
        g = lambda *ks: next(
            (str(message.get(k) or "").strip() for k in ks if message.get(k)), ""
        )  # noqa: E731
        mid, cid, tid, rto = (
            g("message_id"),
            g("chat_id"),
            g("thread_id", "root_id"),
            g("reply_to", "parent_id"),
        )
        raw = message.get("content", message.get("text", ""))
        txt = (
            str(raw.get("text") or raw.get("content") or "")
            if isinstance(raw, Mapping)
            else str(raw or "")
        )
        ms, oids = _parse_created_ms(message.get("create_time")), (
            {own_id.strip()} if own_id and own_id.strip() else None
        )
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
            t_keys = ({topic_id, topic_id.casefold()} | self._msg_keys(topic_id, web_transport)) - {
                ""
            }
            for ck in chat_keys:
                for tk in t_keys if topic_id else ("",):
                    _put_mark(
                        self._owner_topic_replies if topic_id else self._owner_chat_replies,
                        (ck, tk) if topic_id else ck,
                    )
            prefs = {
                msg.parent_id.strip(),
                msg.root_id.strip(),
                (user_msg.parent_id or "").strip(),
                (user_msg.root_id or "").strip(),
                topic_id,
            } - {""}
            for p in prefs:
                for pk in ({p, p.casefold()} | self._msg_keys(p, web_transport)) - {""}:
                    _put_mark(self._owner_parent_replies, pk)

    def has_owner_replied(self, *, chat_id: str, **kw: Any) -> bool:
        chat_keys = self._chat_keys(chat_id, kw.get("web_transport"))
        owner_ids, wt = set(kw.get("owner_ids") or ()), kw.get("web_transport")
        clean_tid = str(kw.get("thread_id") or "").strip()
        clean_mid, clean_rto = (
            str(kw.get("message_id") or "").strip(),
            str(kw.get("reply_to") or "").strip(),
        )
        ms, pos, tpos = (
            int(kw.get("created_at_ms") or 0),
            int(kw.get("position") or 0),
            int(kw.get("topic_position") or 0),
        )
        kset = lambda s: ({s, s.casefold()} | self._msg_keys(s, wt)) - {""}  # noqa: E731
        mid_keys, rto_keys, tid_keys = kset(clean_mid), kset(clean_rto), kset(clean_tid)

        def _m_newer(mark: _OwnerReplyMark, da: bool = False) -> bool:
            ok_id = not mark.message_id or mark.message_id.casefold() not in mid_keys
            return ok_id and _is_newer_reply(
                mark.create_time_ms, mark.position, mark.topic_position, ms, pos, tpos, da
            )

        with self._lock:
            if any(
                mk in self._owner_parent_replies and _m_newer(self._owner_parent_replies[mk], da)
                for ks, da in ((mid_keys, True), (rto_keys, False))
                for mk in ks
            ):
                return True
            for ck in chat_keys:
                marks = (
                    (self._owner_topic_replies.get((ck, tk)) for tk in tid_keys)
                    if clean_tid
                    else (self._owner_chat_replies.get(ck),)
                )
                if any(m is not None and _m_newer(m) for m in marks):
                    return True
        msg_cache = getattr(wt, "_msg_cache", None) if owner_ids else None
        for c in msg_cache.values() if isinstance(msg_cache, Mapping) and owner_ids else ():
            is_ai = isinstance(c, WebMessage) and describe(
                c.type, c.content, ""
            ).strip().lower().startswith("/ai")
            if (
                not isinstance(c, WebMessage)
                or c.recalled
                or not c.id
                or c.id == clean_mid
                or c.from_id not in owner_ids
                or (is_ai and c.parent_id != clean_mid)
                or not (chat_keys & self._chat_keys(c.chat_id, wt))
            ):
                continue
            direct = c.parent_id == clean_mid or c.root_id == clean_mid
            same_p = bool(clean_rto) and c.parent_id == clean_rto
            ctopic = (c.thread_id or c.root_id if is_in_topic(c) else "").strip()
            ctpos = int(getattr(c, "topic_position", None) or 0)
            in_scope = (
                direct
                or same_p
                or (bool(clean_tid) and ctopic == clean_tid)
                or (not clean_tid and not ctopic)
            )
            if in_scope and _is_newer_reply(
                c.create_time_ms, c.position, ctpos, ms, pos, tpos, direct
            ):
                return True
        return False

    async def _verify_unanswered_via_client(
        self, *, user_client: Any, chat_id: str, **kw: Any
    ) -> bool:
        tid, mid = str(kw.get("thread_id") or ""), str(kw.get("message_id") or "")
        ms, pos, tpos = (
            int(kw.get("created_at_ms") or 0),
            int(kw.get("position") or 0),
            int(kw.get("topic_position") or 0),
        )
        wt, owner_ids, cid = kw.get("web_transport"), kw.get("owner_ids") or set(), tid or chat_id
        if not cid or not hasattr(user_client, "list_messages_page"):
            return False
        try:
            async with asyncio.timeout(2.5):
                ctype = "thread" if tid else "chat"
                page = await user_client.list_messages_page(
                    container_id_type=ctype,
                    container_id=cid,
                    sort_type="ByCreateTimeDesc",
                    page_size=20,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        msg_cache = getattr(wt, "_msg_cache", None)
        for item in getattr(page, "items", ()):
            if (
                not isinstance(item, FeishuUserMessage)
                or item.message_id == mid
                or item.sender_open_id not in owner_ids
                or (item.text.strip().lower().startswith("/ai") and item.parent_id != mid)
            ):
                continue
            direct = item.parent_id == mid or item.root_id == mid
            wm = msg_cache.get(item.message_id) if isinstance(msg_cache, Mapping) else None
            wpos = wm.position if wm is not None else 0
            wtpos = int(getattr(wm, "topic_position", None) or 0) if wm is not None else 0
            in_s = (
                direct
                or (bool(tid) and (item.thread_id == tid or item.root_id == tid))
                or (not tid and not item.thread_id)
            )
            if in_s and _is_newer_reply(item.create_time_ms, wpos, wtpos, ms, pos, tpos, direct):
                return True
        return False

    async def _ensure_chat_metadata(
        self, web_chat_id: str, web_transport: Any, *, refresh: bool = False
    ) -> tuple[str, str]:
        if web_transport is None or not web_chat_id:
            return "", ""
        chat_cache, open_map = getattr(web_transport, "_chat_cache", {}), getattr(
            web_transport, "_web_to_open_chat", {}
        )
        cached = chat_cache.get(web_chat_id) if isinstance(chat_cache, Mapping) else None
        open_chat_id = str(
            open_map.get(web_chat_id) or getattr(cached, "open_chat_id", "") or ""
        ).strip()
        chat_name = str(getattr(cached, "name", "") or "").strip()
        web_client = getattr(web_transport, "web_client", None) or getattr(
            web_transport, "_web", None
        )
        now_m = time.monotonic()
        stale = (now_m - self._chat_meta_ts.get(web_chat_id, 0.0)) >= 60.0
        need_fetch = cached is None or not open_chat_id or not chat_name or refresh or stale
        if need_fetch and web_client is not None and hasattr(web_client, "chats"):
            try:
                async with asyncio.timeout(2.5):
                    chats_map = await web_client.chats([web_chat_id])
                fetched = chats_map.get(web_chat_id) if isinstance(chats_map, Mapping) else None
                if fetched is not None:
                    self._chat_meta_ts[web_chat_id] = now_m
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

    def _deliver_event_res(self, res: Any) -> bool:
        if isinstance(res, bool):
            return res
        return (
            res.get("status") in {"accepted", "duplicate", True}
            if isinstance(res, Mapping)
            else True
        )

    async def _deliver_event(self, event: ExternalEvent, cfg: FeishuRealtimeEventConfig) -> bool:
        if self._event_sink is not None:
            return self._deliver_event_res(await self._event_sink(event))
        if cfg.events_url:
            tok = os.environ.get("CLAUDE_HUB_DOT_EVENTS_TOKEN", "").strip()
            hdrs = {
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {tok}"} if tok else {}),
            }
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                body = event.model_dump_json().encode("utf-8")
                (await client.post(cfg.events_url, content=body, headers=hdrs)).raise_for_status()
                return True
        if await submit_external_event(event):
            return True
        return await has_external_event_receipt(event.source, event.dedupe_key)

    async def handle_pushed_message(
        self, pushed: PushedMessage, user_msg: FeishuUserMessage, **kw: Any
    ) -> bool:
        msg, wt, uc = pushed.message, kw.get("web_transport"), kw.get("user_client")
        if pushed.removed or msg.recalled or msg.type == _rich.TYPE_SYSTEM:
            return False
        eff_oids = {o.strip() for o in (kw.get("owner_ids") or set()) if o and o.strip()}
        if (
            kw.get("is_owner_sender")
            or msg.from_id in eff_oids
            or user_msg.sender_open_id in eff_oids
        ):
            self.record_owner_pushed_message(pushed, user_msg, web_transport=wt)
            return False
        cfg, wcid = self.resolve_config(), (msg.chat_id or user_msg.chat_id).strip()
        is_bot = user_msg.sender_open_id.startswith(
            ("cli_", "app_", "bot_")
        ) or msg.from_id.startswith(("cli_", "app_", "bot_"))
        if not cfg.enabled or not wcid or is_bot:
            return False
        m_ids = extract_mentioned_user_ids(msg.type, msg.content)
        ftxt = describe(msg.type, msg.content, "").strip() or user_msg.text.strip()
        urls = extract_message_urls(msg.type, msg.content, ftxt)
        is_at_me = bool(eff_oids.intersection(m_ids)) or (
            bool(pushed.at_me) and not (bool(m_ids) and set(m_ids) <= {"all", "@all"})
        )
        m_urls = (
            [u for u in urls if matches_fault_or_task_url(u, cfg)]
            if urls and (cfg.url_patterns or cfg.actionable_hosts)
            else []
        )
        tid = (
            user_msg.thread_id or (msg.thread_id or msg.root_id if is_in_topic(msg) else "") or ""
        ).strip()
        cks, ocid, cname = self._chat_keys(wcid, wt), "", ""
        if is_at_me or m_urls:
            w_cf = {c.strip().casefold() for c in cfg.watch_chat_ids if c and c.strip()}
            ref = bool(m_urls and cfg.watch_chat_names and not (cks & w_cf))
            ocid, cname = await self._ensure_chat_metadata(wcid, wt, refresh=ref)
            if ocid:
                cks |= self._chat_keys(ocid, wt)
        if cks & cfg.automated_chat_ids:
            return False
        m_urls = (
            matches_watch_group_url(
                wcid, cname, ftxt, cfg, extra_urls=urls, extra_chat_ids=[ocid] if ocid else ()
            )
            if m_urls
            else []
        )
        is_w_url = bool(m_urls)
        fb_base = (cname, cks, tid, user_msg.sender_open_id, ftxt, user_msg.create_time_ms)
        in_topic = bool(tid) and any((ck, tid) in self._owner_topic_replies for ck in cks)
        if not is_at_me and not is_w_url:
            has_q = _SUBSTANTIVE_ISSUE_RE.search(ftxt) or "?" in ftxt or "？" in ftxt or bool(urls)
            if (
                (pushed.chat_type == _rich.CHAT_P2P or in_topic)
                and not is_courtesy_or_ack_only(ftxt)
                and has_q
            ):
                mid = user_msg.message_id
                self._add_fallback_record(
                    mid, mid, "uncovered_message", *fb_base, "", user_msg.text
                )
            return False
        self._sync_transport_chats(wt)
        al = (
            fn(user_msg.message_id)
            if callable(fn := getattr(wt, "get_message_alias", None))
            else None
        )
        cmid = al if isinstance(al, str) and al.startswith("om_") else user_msg.message_id
        s_set = {
            s.strip().casefold()
            for s in (user_msg.sender_open_id, msg.from_id)
            if isinstance(s, str) and s.strip()
        }
        ms = user_msg.create_time_ms
        chk_kw: dict[str, Any] = dict(chat_name=cname, thread_id=tid, text=ftxt, created_at_ms=ms)
        if self.is_message_handled(
            cmid, chat_id=ocid or wcid, sender_id=user_msg.sender_open_id, **chk_kw
        ) or self.is_message_handled(
            user_msg.message_id,
            chat_id=wcid,
            sender_id=msg.from_id or user_msg.sender_open_id,
            **chk_kw,
        ):
            return False
        rec_args = (cks, cname, {tid} if tid else None, s_set, ftxt, user_msg.text, ms)

        async def _suppress(courtesy: bool) -> bool:
            self.suppressed_count += 1
            self.suppressed_courtesy_count += int(courtesy)
            self.suppressed_replied_count += int(not courtesy)
            self._record_handled(user_msg.message_id, cmid, *rec_args)
            for sup_key in {user_msg.message_id, cmid} - {""}:
                try:
                    await record_suppressed_external_event("feishu", sup_key[:256])
                except Exception:
                    pass
            return False

        if not is_w_url and (
            is_courtesy_or_ack_only(user_msg.text) or is_courtesy_or_ack_only(ftxt)
        ):
            return await _suppress(courtesy=True)
        tpos = int(getattr(msg, "topic_position", None) or 0)
        r_kw: dict[str, Any] = dict(
            chat_id=wcid, thread_id=tid, message_id=user_msg.message_id, created_at_ms=ms
        )
        r_kw.update(
            position=msg.position, topic_position=tpos, web_transport=wt, owner_ids=eff_oids
        )
        if self.has_owner_replied(
            reply_to=msg.parent_id or msg.root_id, **r_kw
        ) or await self._verify_unanswered_via_client(user_client=uc, **r_kw):
            return await _suppress(courtesy=False)
        parsed_url: HttpUrl | None = None
        for u in m_urls or urls:
            try:
                parsed_url = HttpUrl(u)
                break
            except Exception:
                pass
        clbl, snd = (cname or ocid or wcid)[:120], user_msg.sender_open_id[:80]
        parts = [p for p in (clbl, snd, ftxt[:600]) if p]
        keys = "chat_id web_chat_id chat_name chat_type message_id web_message_id thread_id sender_id text trigger_reason matched_urls".split()
        ctype = "p2p" if pushed.chat_type == _rich.CHAT_P2P else "group"
        trig, u_slice = ("mention" if is_at_me else "watch_chat_url"), [
            u[:400] for u in (m_urls or urls)[:5]
        ]
        pl: dict[str, Any] = dict(
            zip(keys[:6], (ocid or wcid, wcid, cname[:200], ctype, cmid, user_msg.message_id))
        )
        pl.update(
            zip(keys[6:10], (tid, user_msg.sender_open_id[:120], ftxt[:500], trig)),
            matched_urls=u_slice,
        )
        ev_t = "im.mention" if is_at_me else "im.watch_chat_url"
        ev_ttl = "Feishu mention" if is_at_me else "Feishu watch-group issue link"
        ev_kw: dict[str, Any] = dict(
            source="feishu", event_type=ev_t, dedupe_key=cmid[:256], severity="urgent"
        )
        ev_kw.update(title=ev_ttl, summary=" · ".join(parts)[:1000], url=parsed_url, payload=pl)
        event = ExternalEvent(**ev_kw)
        delivery_err: str | None = None
        try:
            if not await self._deliver_event(event, cfg):
                delivery_err = "rejected"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delivery_err = str(exc) or type(exc).__name__
            logger.warning("Feishu real-time event delivery failed for %s", cmid, exc_info=True)
        if delivery_err is not None:
            fb_tail = (
                cmid if cmid != user_msg.message_id else "",
                user_msg.text,
                m_urls or urls,
                event,
                delivery_err,
            )
            self._add_fallback_record(
                cmid, user_msg.message_id, "failed_delivery", *fb_base, *fb_tail
            )
            return False
        self.emitted_count += 1
        self._record_handled(user_msg.message_id, cmid, *rec_args)
        return True


def get_feishu_realtime_event_router() -> FeishuRealtimeEventRouter:
    global _DEFAULT_REALTIME_ROUTER
    if _DEFAULT_REALTIME_ROUTER is None:
        _DEFAULT_REALTIME_ROUTER = FeishuRealtimeEventRouter()
    return _DEFAULT_REALTIME_ROUTER


def is_feishu_realtime_stream_healthy() -> bool:
    from claude_hub.services.feishu_ai_poller import _DEFAULT_AI_POLLER

    return bool(getattr(_DEFAULT_AI_POLLER, "realtime_stream_healthy", False))
