"""Feishu realtime message parsing and trigger matching."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from claude_hub.services.feishu_web_session import rich as _rich
from claude_hub.services.feishu_web_session.codec import decode
from claude_hub.services.feishu_web_session.rich import _num, _str, text_of
from claude_hub.services.runtime_isolation import resolve_runtime_home

from .feishu_realtime_settings import ENABLED_ENV, SETTINGS_FILE, parse_switch

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
    for filename in (SETTINGS_FILE, "local_runtime_settings.json"):
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
            if (value := parse_switch(src.get(n))) is not None:
                return value
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
    suppress_patrol_when_healthy: bool = True
    provider: str = "maso"
    model: str = ""
    reasoning_effort: str = "high"
    workspace_id: str = ""
    p2p_debounce_seconds: float = 75.0
    p2p_active_seconds: float = 900.0

    @classmethod
    def from_runtime(cls) -> FeishuRealtimeEventConfig:
        d = _read_runtime_json_documents()

        def v(s: str, sc: bool = True) -> list[str]:
            return _setting_values(
                [f"CLAUDE_HUB_{x}" if x.isupper() else x for x in s.split()], d, split_commas=sc
            )

        def b(s: str, df: bool) -> bool:
            return _setting_bool(
                [f"CLAUDE_HUB_{x}" if x.isupper() else x for x in s.split()], d, default=df
            )

        def scalar(name: str, default: str) -> str:
            env_name = f"CLAUDE_HUB_FEISHU_REALTIME_{name.upper()}"
            if env_name in os.environ:
                return os.environ[env_name].strip()
            for doc in d:
                for key in (env_name, name):
                    if isinstance(doc.get(key), str):
                        return str(doc[key]).strip()
            return default

        def seconds(name: str, default: float) -> float:
            env_name = f"CLAUDE_HUB_FEISHU_REALTIME_{name.upper()}"
            for src in (os.environ, *d):
                for key in (env_name, name):
                    raw = src.get(key)
                    if raw is None or isinstance(raw, bool):
                        continue
                    try:
                        value = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(value) and 0 <= value <= 86400:
                        return value
            return default

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
        en = _setting_bool([ENABLED_ENV, "enabled"], d, default=True)
        sup = b("FEISHU_SUPPRESS_PATROL_WHEN_HEALTHY suppress_patrol_when_healthy", True)
        return cls(
            en,
            frozenset(x.strip() for x in w_ids if x.strip()),
            frozenset(" ".join(x.split()).casefold() for x in w_names),
            tuple(dict.fromkeys(u_pats)),
            frozenset(x.casefold() for x in hosts),
            frozenset(x.casefold() for x in auto_ids),
            e_urls[0] if e_urls else "",
            sup,
            scalar("provider", "maso"),
            scalar("model", ""),
            scalar("reasoning_effort", "high"),
            scalar("workspace_id", ""),
            seconds("p2p_debounce_seconds", 75.0),
            seconds("p2p_active_seconds", 900.0),
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


def _content_fingerprint(text: str) -> str:
    cleaned = _INLINE_AT_MENTION_RE.sub(
        " ", _HTML_TAG_RE.sub(" ", _HTML_AT_TAG_RE.sub(" ", text or ""))
    )
    return hashlib.sha256(" ".join(cleaned.lower().split()).encode("utf-8")).hexdigest()[:24]


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
