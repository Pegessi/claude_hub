"""Rich-text, Markdown block, message content, and feed parsing/formatting for Feishu web protocol.

Web-session protocol client
(``messages.go``, ``rich.go``, ``content.go``, ``feed.go``).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from claude_hub.services.feishu_web_session.codec import decode, encode

# Message types of the web protocol
TYPE_POST = 2
TYPE_FILE = 3
TYPE_TEXT = 4
TYPE_IMAGE = 5
TYPE_SYSTEM = 6
TYPE_AUDIO = 7
TYPE_SHARE_CHAT = 9
TYPE_STICKER = 10
TYPE_MERGE = 11
TYPE_CALENDAR = 12
TYPE_CLOUD_FILE = 13
TYPE_CARD = 14
TYPE_MEDIA = 15
TYPE_SHARE_EVENT = 16
TYPE_HONGBAO = 17
TYPE_CALENDAR_ALL = 18
TYPE_VIDEO_CHAT = 19
TYPE_LOCATION = 20
TYPE_HONGBAO_BIZ = 22
TYPE_SHARE_USER = 23
TYPE_TODO = 24
TYPE_FOLDER = 25
TYPE_VOTE = 27
TYPE_LINK = 28

MESSAGE_RECALLED = 2

# Chat types & modes
CHAT_P2P = 1
CHAT_GROUP = 2
CHAT_TOPIC_CHAT = 3
CHAT_MODE_THREAD = 2
CHAT_MODE_THREAD_V2 = 3

# Feed types & card types
FEED_INBOX = 1
FEED_DONE = 2
CARD_CHAT = 1
CARD_BOX = 5

# Rich-text element tags
TAG_TEXT = 1
TAG_P = 3
TAG_AT = 5
TAG_A = 6
TAG_LINK = 17
TAG_UL = 26
TAG_OL = 27
TAG_LI = 28
TAG_QUOTE = 29

# Block kinds
BLOCK_PARAGRAPH = "p"
BLOCK_BULLETS = "ul"
BLOCK_NUMBERS = "ol"
BLOCK_QUOTE = "quote"

# Standard reactions
REACT_ON_IT = "OnIt"
REACT_DONE = "DONE"

_SYSTEM_CALL_ENDED = 103
_SYSTEM_MEETING_ENDED = 902
_SYSTEM_MEETING_START = 911

_CARD_UPGRADE_HINTS = (
    "Upgrade to the latest app version",
    "请升级至最新版本客户端",
)

_MD_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_MD_NUMBER_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_MD_RULE_RE = re.compile(r"^\s*(-\s*){3,}$|^\s*(\*\s*){3,}$|^\s*(_\s*){3,}$")
_MD_INLINE_RE = re.compile(
    r"\*\*(.+?)\*\*|__(.+?)__|\[([^\]]+)\]\((https?://[^\s)]+)\)|`([^`]+)`|"
    r"(https?://[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]+)"
)
_MD_FORMAT_RE = re.compile(
    r"(?m)^\s*([-*+]|\d+[.)]|>|#{1,6})\s|\*\*.+?\*\*|\[[^\]]+\]\(https?://|`"
)
_URL_TRAILER = ".,;:!?。，；：！？、"
_BLANK_TPL_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")
_HTML_TAGS_RE = re.compile(r"<[^>]*>")


def _str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v))
    return str(v)


def _num(v: Any) -> int:
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return 0
    return 0


def _i64(v: Any) -> int:
    return _num(v)


def _zero(s: str) -> str:
    return "" if s == "0" else s


@dataclass
class Node:
    """One inline piece of rich text: text, a link, or a mention."""

    text: str = ""
    href: str = ""
    at: str = ""
    bold: bool = False


def T(s: str) -> Node:
    return Node(text=s)


def L(text: str, href: str) -> Node:
    return Node(text=text, href=href)


@dataclass
class Block:
    """One top-level block of a formatted message (`p`, `ul`, `ol`, `quote`)."""

    kind: str
    lines: list[list[Node]] = field(default_factory=list)


@dataclass(frozen=True)
class WebMessage:
    """A message as the Feishu web gateway returns it."""

    id: str = ""
    from_id: str = ""
    chat_id: str = ""
    root_id: str = ""
    parent_id: str = ""
    thread_id: str = ""
    type: int = TYPE_TEXT
    position: int = 0
    create_time_s: int = 0
    content: bytes = b""
    recalled: bool = False

    @property
    def create_time_ms(self) -> int:
        return (
            self.create_time_s * 1000 if self.create_time_s < 10_000_000_000 else self.create_time_s
        )


@dataclass(frozen=True)
class WebChat:
    """A chat as the Feishu web gateway returns it."""

    id: str = ""
    type: int = CHAT_GROUP
    name: str = ""
    open_chat_id: str = ""
    last_position: int = 0
    member_count: int = 0
    muted: bool = False
    dissolved: bool = False
    bot: bool = False
    has_last: bool = False
    thread: bool = False
    updated_s: int = 0
    last_message_id: str = ""


@dataclass(frozen=True)
class WebChatter:
    """A person or bot by web ID."""

    id: str = ""
    name: str = ""
    type: int = 1


@dataclass(frozen=True)
class FeedCard:
    """One row of the web client's chat feed list."""

    id: str = ""
    type: int = CARD_CHAT
    parent: str = ""
    updated_ms: int = 0


@dataclass(frozen=True)
class FeedPage:
    """One page of a feed list (`PullFeedCardsResponse`)."""

    cards: tuple[FeedCard, ...] = ()
    chats: Mapping[str, WebChat] = field(default_factory=dict)
    last: Mapping[str, WebMessage] = field(default_factory=dict)
    next_cursor: int = 0


@dataclass(frozen=True)
class WebOut:
    """A message to send as the signed-in user via ``PutMessageRequest``."""

    chat: str
    title: str = ""
    rows: Sequence[Sequence[Node]] = ()
    blocks: Sequence[Block] = ()
    reply_to: WebMessage | None = None
    in_thread: bool = False
    cid: str = ""


def message_of(x: Mapping[str, Any]) -> WebMessage:
    raw_b64 = _str(x.get("content"))
    try:
        content = base64.b64decode(raw_b64.encode("ascii")) if raw_b64 else b""
    except Exception:
        content = b""
    sec = _i64(x.get("createTime"))
    removed = bool(x.get("isRemoved"))
    return WebMessage(
        id=_str(x.get("id")),
        from_id=_str(x.get("fromId")),
        chat_id=_str(x.get("chatId")),
        root_id=_zero(_str(x.get("rootId"))),
        parent_id=_zero(_str(x.get("parentId"))),
        thread_id=_zero(_str(x.get("threadId"))),
        type=_num(x.get("type")),
        position=_num(x.get("position")),
        create_time_s=sec,
        content=content,
        recalled=(_num(x.get("status")) == MESSAGE_RECALLED) or removed,
    )


def chat_of(x: Mapping[str, Any]) -> WebChat:
    muted = False
    if "isRemind" in x and isinstance(x["isRemind"], bool):
        muted = not x["isRemind"]
    dissolved = bool(x.get("isDissolved"))
    last_msg_id = _zero(_str(x.get("lastMessageId")))
    has_last = bool(last_msg_id)
    mode = _num(x.get("chatMode"))
    chat_type = _num(x.get("type"))
    is_thread = mode in {CHAT_MODE_THREAD, CHAT_MODE_THREAD_V2} or chat_type == CHAT_TOPIC_CHAT
    is_bot = bool(x.get("isP2pAi")) or bool(x.get("isP2pOfficialBotAi"))
    return WebChat(
        id=_str(x.get("id")),
        type=chat_type,
        name=_str(x.get("name")),
        open_chat_id=_str(x.get("openChatId")),
        last_position=_num(x.get("lastMessagePosition")),
        member_count=_num(x.get("memberCount")),
        muted=muted,
        dissolved=dissolved,
        bot=is_bot,
        has_last=has_last,
        thread=is_thread,
        updated_s=_i64(x.get("updateTime")),
        last_message_id=last_msg_id,
    )


def feed_page_of(out: Mapping[str, Any]) -> FeedPage:
    cards: list[FeedCard] = []
    raw_cards = out.get("cards")
    if isinstance(raw_cards, list):
        for v in raw_cards:
            if not isinstance(v, Mapping):
                continue
            parent = _zero(_str(v.get("parentCardId")))
            ms = _i64(v.get("updateTimeMs"))
            if ms <= 0:
                sec = _i64(v.get("updateTime"))
                ms = sec * 1000 if sec > 0 else 0
            cards.append(
                FeedCard(
                    id=_str(v.get("id")),
                    type=_num(v.get("type")),
                    parent=parent,
                    updated_ms=ms,
                )
            )
    chats: dict[str, WebChat] = {}
    raw_chats = out.get("chats")
    if isinstance(raw_chats, Mapping):
        for cid, v in raw_chats.items():
            if isinstance(v, Mapping):
                chats[str(cid)] = chat_of(v)

    last_msgs: dict[str, WebMessage] = {}
    raw_msgs = out.get("messages")
    if isinstance(raw_msgs, Mapping):
        for v in raw_msgs.values():
            if isinstance(v, Mapping):
                m = message_of(v)
                if m.chat_id:
                    last_msgs[m.chat_id] = m

    return FeedPage(
        cards=tuple(cards),
        chats=chats,
        last=last_msgs,
        next_cursor=_i64(out.get("nextCursor")),
    )


def rich_text(rows: Sequence[Sequence[Node]], paragraphs: bool) -> tuple[dict[str, Any], list[str]]:
    dictionary: dict[str, Any] = {}
    ids: list[str] = []
    at_ids: list[str] = []
    anchor_ids: list[str] = []
    mentions: list[str] = []
    inner_parts: list[str] = []

    for i, row in enumerate(rows):
        keys: list[str] = []
        for n in row:
            key = str(len(dictionary))
            if n.at:
                tag = TAG_AT
                text = n.text if n.text.startswith("@") else f"@{n.text}"
                prop = encode(
                    "lark_entities_RichTextElement_AtProperty",
                    {"userId": n.at, "content": text},
                )
                at_ids.append(key)
                if n.at != "all":
                    mentions.append(n.at)
                inner_parts.append(text)
            elif n.href:
                tag = TAG_A
                text = n.text or n.href
                prop = encode(
                    "lark_entities_RichTextElement_AnchorProperty",
                    {"href": n.href, "content": text},
                )
                anchor_ids.append(key)
                inner_parts.append(text)
            else:
                tag = TAG_TEXT
                prop = encode(
                    "lark_entities_RichTextElement_TextProperty",
                    {"content": n.text},
                )
                inner_parts.append(n.text)
            dictionary[key] = {
                "tag": tag,
                "property": base64.b64encode(prop).decode("ascii"),
            }
            keys.append(key)

        if paragraphs:
            key = str(len(dictionary))
            prop = encode("lark_entities_RichTextElement_TextProperty", {})
            dictionary[key] = {
                "tag": TAG_P,
                "property": base64.b64encode(prop).decode("ascii"),
                "childIds": keys,
            }
            ids.append(key)
            if i < len(rows) - 1:
                inner_parts.append("\n")
        else:
            ids.extend(keys)

    rich: dict[str, Any] = {
        "elementIds": ids,
        "innerText": "".join(inner_parts),
        "elements": {"dictionary": dictionary},
        "atIds": at_ids,
        "anchorIds": anchor_ids,
        "version": 1,
    }
    return rich, mentions


def has_markdown(text: str) -> bool:
    return bool(_MD_FORMAT_RE.search(text or ""))


def inline_nodes(s: str) -> list[Node]:
    out: list[Node] = []

    def add_text(t: str) -> None:
        if not t:
            return
        if out and not out[-1].href and not out[-1].at and not out[-1].bold:
            out[-1].text += t
            return
        out.append(T(t))

    last = 0
    for m in _MD_INLINE_RE.finditer(s):
        add_text(s[last : m.start()])
        last = m.end()
        g1, g2, g3, g4, g5, g6 = m.groups()
        if g1 or g2:
            for n in inline_nodes((g1 or "") + (g2 or "")):
                n.bold = True
                out.append(n)
        elif g3:
            out.append(L(g3, g4 or ""))
        elif g5:
            add_text(g5)
        elif g6:
            u = g6.rstrip(_URL_TRAILER)
            out.append(L(u, u))
            add_text(g6[len(u) :])
    add_text(s[last:])
    return out


def markdown_blocks(text: str) -> list[Block]:
    blocks: list[Block] = []

    def add(kind: str, line_nodes: list[Node]) -> None:
        if blocks and kind != BLOCK_PARAGRAPH and blocks[-1].kind == kind:
            blocks[-1].lines.append(line_nodes)
            return
        blocks.append(Block(kind=kind, lines=[line_nodes]))

    def blank() -> None:
        if blocks and not (blocks[-1].kind == BLOCK_PARAGRAPH and not blocks[-1].lines[0]):
            blocks.append(Block(kind=BLOCK_PARAGRAPH, lines=[[]]))

    fenced = False
    normalized = (text or "").strip().replace("\r\n", "\n")
    for raw in normalized.split("\n"):
        line = raw.rstrip(" \t")
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            add(BLOCK_PARAGRAPH, [T(line)])
        elif not line.strip():
            blank()
        elif line.strip().startswith(">"):
            q = line.strip()[1:]
            if q.startswith(" "):
                q = q[1:]
            add(BLOCK_QUOTE, inline_nodes(q))
        elif _MD_RULE_RE.match(line):
            blank()
        elif m_bul := _MD_BULLET_RE.match(line):
            add(BLOCK_BULLETS, inline_nodes(m_bul.group(1)))
        elif m_num := _MD_NUMBER_RE.match(line):
            add(BLOCK_NUMBERS, inline_nodes(m_num.group(1)))
        elif m_head := _MD_HEADING_RE.match(line):
            ns = inline_nodes(m_head.group(1))
            for n in ns:
                n.bold = True
            add(BLOCK_PARAGRAPH, ns)
        else:
            add(BLOCK_PARAGRAPH, inline_nodes(line))

    while blocks and blocks[-1].kind == BLOCK_PARAGRAPH and not blocks[-1].lines[0]:
        blocks.pop()
    return blocks


def rich_blocks(blocks: Sequence[Block]) -> tuple[dict[str, Any], list[str]]:
    dictionary: dict[str, Any] = {}
    roots: list[str] = []
    at_ids: list[str] = []
    anchor_ids: list[str] = []
    mentions: list[str] = []
    inner_parts: list[str] = []

    def put(
        tag: int,
        prop: bytes,
        style: Mapping[str, str] | None,
        kids: Sequence[str] | None,
    ) -> str:
        key = str(len(dictionary))
        entry: dict[str, Any] = {
            "tag": tag,
            "property": base64.b64encode(prop).decode("ascii"),
        }
        if kids:
            entry["childIds"] = list(kids)
        if style:
            entry["style"] = dict(style)
        dictionary[key] = entry
        return key

    def encode_inline(ns: Sequence[Node]) -> list[str]:
        keys: list[str] = []
        for n in ns:
            style = {"fontWeight": "bold"} if n.bold else None
            if n.at:
                t = n.text if n.text.startswith("@") else f"@{n.text}"
                prop = encode(
                    "lark_entities_RichTextElement_AtProperty",
                    {"userId": n.at, "content": t},
                )
                k = put(TAG_AT, prop, None, None)
                at_ids.append(k)
                keys.append(k)
                if n.at != "all":
                    mentions.append(n.at)
                inner_parts.append(t)
            elif n.href:
                t = n.text or n.href
                prop = encode(
                    "lark_entities_RichTextElement_AnchorProperty",
                    {"href": n.href, "content": t},
                )
                k = put(TAG_A, prop, style, None)
                anchor_ids.append(k)
                keys.append(k)
                inner_parts.append(t)
            else:
                prop = encode(
                    "lark_entities_RichTextElement_TextProperty",
                    {"content": n.text},
                )
                keys.append(put(TAG_TEXT, prop, style, None))
                inner_parts.append(n.text)
        return keys

    empty_prop = encode("lark_entities_RichTextElement_TextProperty", {})
    for bi, b in enumerate(blocks):
        if bi > 0:
            inner_parts.append("\n")
        if b.kind in {BLOCK_BULLETS, BLOCK_NUMBERS}:
            items: list[str] = []
            for i, line_nodes in enumerate(b.Lines if hasattr(b, "Lines") else b.lines):
                if i > 0:
                    inner_parts.append("\n")
                kids = encode_inline(line_nodes)
                items.append(put(TAG_LI, empty_prop, None, kids))
            if b.kind == BLOCK_NUMBERS:
                tag, prop = TAG_OL, b"\x08\x00\x10\x01"
            else:
                tag, prop = TAG_UL, b"\x08\x00"
            roots.append(put(tag, prop, None, items))
        elif b.kind == BLOCK_QUOTE:
            paras: list[str] = []
            for i, line_nodes in enumerate(b.lines):
                if i > 0:
                    inner_parts.append("\n")
                kids = encode_inline(line_nodes)
                paras.append(put(TAG_P, empty_prop, None, kids))
            roots.append(put(TAG_QUOTE, empty_prop, None, paras))
        else:
            first_line = b.lines[0] if b.lines else []
            kids = encode_inline(first_line)
            roots.append(put(TAG_P, empty_prop, None, kids))

    rich: dict[str, Any] = {
        "elementIds": roots,
        "innerText": "".join(inner_parts),
        "elements": {"dictionary": dictionary},
        "atIds": at_ids,
        "anchorIds": anchor_ids,
        "version": 1,
    }
    return rich, mentions


def rows_of(text: str) -> list[list[Node]]:
    return [[T(line)] for line in (text or "").split("\n")]


def formatted_out(out: WebOut, text: str) -> WebOut:
    if has_markdown(text):
        blocks = markdown_blocks(text)
        if blocks:
            return replace(out, blocks=blocks, rows=())
    return replace(out, rows=rows_of(text), blocks=())


def extract_text_and_title_from_openapi_content(
    msg_type: str, content: Mapping[str, Any] | str
) -> tuple[str, str]:
    """Convert an OpenAPI ``text`` or ``post`` payload into ``(markdown_text, title)``."""
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, Mapping):
                content = parsed
            else:
                return str(content), ""
        except Exception:
            return str(content), ""

    if (
        msg_type == "text"
        or "text" in content
        and "zh_cn" not in content
        and "content" not in content
    ):
        return _str(content.get("text")), ""

    locale_block = content.get("zh_cn") or content.get("en_us") or content
    if not isinstance(locale_block, Mapping):
        return _str(content), ""
    title = _str(locale_block.get("title")).strip()
    raw_rows = locale_block.get("content")
    if not isinstance(raw_rows, list):
        return _str(locale_block.get("text")), title

    lines: list[str] = []
    for row in raw_rows:
        if not isinstance(row, list):
            continue
        parts: list[str] = []
        for elem in row:
            if not isinstance(elem, Mapping):
                continue
            tag = _str(elem.get("tag")).lower()
            if tag == "a":
                href = _str(elem.get("href")).strip()
                label = _str(elem.get("text")).strip() or href
                if href and label != href:
                    parts.append(f"[{label}]({href})")
                else:
                    parts.append(href or label)
            elif tag == "at":
                name = _str(
                    elem.get("user_name") or elem.get("name") or elem.get("user_id")
                ).strip()
                if name:
                    parts.append(name if name.startswith("@") else f"@{name}")
            else:
                parts.append(_str(elem.get("text")))
        lines.append("".join(parts))
    return "\n".join(lines), title


def _rich_plain(v: Any, skip_user: str = "") -> str:
    if not isinstance(v, Mapping):
        return ""
    els = v.get("elements")
    dictionary = els.get("dictionary") if isinstance(els, Mapping) else None
    if not isinstance(dictionary, Mapping):
        return _str(v.get("innerText"))

    buf: list[str] = []

    def visit(key: str, depth: int) -> None:
        if depth > 20:
            return
        e = dictionary.get(key)
        if not isinstance(e, Mapping):
            return
        raw_prop = _str(e.get("property"))
        try:
            prop = base64.b64decode(raw_prop.encode("ascii")) if raw_prop else b""
        except Exception:
            prop = b""
        tag = _num(e.get("tag"))
        if tag in {TAG_TEXT, TAG_LINK}:
            try:
                p = decode("lark_entities_RichTextElement_TextProperty", prop)
                buf.append(_str(p.get("content")))
            except Exception:
                pass
        elif tag == TAG_A:
            try:
                p = decode("lark_entities_RichTextElement_AnchorProperty", prop)
                t = _str(p.get("content")) or _str(p.get("href"))
                buf.append(t)
            except Exception:
                pass
        elif tag == TAG_AT:
            try:
                p = decode("lark_entities_RichTextElement_AtProperty", prop)
                uid = _str(p.get("userId"))
                if not (skip_user and uid == skip_user):
                    t = _str(p.get("content"))
                    if not t.startswith("@"):
                        t = "@" + t
                    buf.append(t)
            except Exception:
                pass

        kids = e.get("childIds")
        if isinstance(kids, list):
            for k in kids:
                visit(_str(k), depth + 1)
        if tag in {TAG_P, TAG_LI}:
            buf.append("\n")

    roots = v.get("elementIds")
    if isinstance(roots, list):
        for k in roots:
            visit(_str(k), 0)

    joined = "".join(buf).strip()
    if joined:
        return joined
    return _str(v.get("innerText"))


def text_without(typ: int, content: bytes, user: str = "") -> str:
    if not content:
        return ""
    if typ == TYPE_TEXT:
        try:
            m = decode("lark_entities_TextContent", content)
        except Exception:
            return ""
        t = _rich_plain(m.get("richText"), user)
        return t if t else _str(m.get("text"))
    if typ == TYPE_POST:
        try:
            m = decode("lark_entities_PostContent", content)
        except Exception:
            return ""
        body = _rich_plain(m.get("richText"), user) or _str(m.get("text"))
        title = _str(m.get("title"))
        if title:
            return f"{title}\n{body}".strip()
        return body
    return ""


def text_of(typ: int, content: bytes) -> str:
    return text_without(typ, content, "")


def _upgrade_hint(s: str) -> bool:
    return any(h in s for h in _CARD_UPGRADE_HINTS)


def _span_seconds(total_seconds: int) -> str:
    total_seconds = max(0, int(round(total_seconds)))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    if h > 0:
        return f"{h} 小时 {m} 分 {s} 秒"
    if m > 0:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def _spaced(n: str) -> str:
    parts: list[str] = []
    while len(n) > 3:
        parts.append(n[:3])
        n = n[3:]
    if n:
        parts.append(n)
    return " ".join(parts)


def meeting_text(
    content: bytes,
    names: Callable[[list[str]], Mapping[str, str]] | None = None,
    me: str = "",
) -> tuple[str, bool]:
    if not content:
        return "[视频会议]", False
    try:
        x = decode("lark_entities_VideoChatContent", content)
    except Exception:
        return "[视频会议]", False
    card = x.get("meetingCard")
    if not isinstance(card, Mapping):
        card = {}
    sponsor = _str(card.get("sponsorId"))
    ids: list[str] = []
    raw_list = card.get("participants")
    if isinstance(raw_list, list):
        for v in raw_list:
            if isinstance(v, Mapping):
                uid = _str(v.get("userId"))
                if uid and uid not in ids:
                    ids.append(uid)
    joined = bool(me and (sponsor == me or me in ids))
    known: Mapping[str, str] = names([sponsor, *ids]) if names is not None else {}
    parts: list[str] = []
    if num_str := _spaced(_str(card.get("meetNumber"))):
        parts.append("会议号 " + num_str)
    if sponsor_name := known.get(sponsor, ""):
        parts.append("发起人 " + sponsor_name)
    who = [known[uid] for uid in ids if known.get(uid)]
    if who:
        parts.append("参会 " + "、".join(who))
    elif ids:
        parts.append(f"{len(ids)} 人参会")
    start_ms = _i64(card.get("startTimeMs"))
    end_ms = _i64(card.get("endTimeMs"))
    if start_ms > 0 and end_ms > start_ms:
        parts.append("时长 " + _span_seconds(round((end_ms - start_ms) / 1000.0)))
    text = ("[视频会议] " + _str(card.get("topic"))).strip()
    if parts:
        text += "（" + "，".join(parts) + "）"
    return text, joined


def system_text(
    content: bytes,
    names: Callable[[list[str]], Mapping[str, str]] | None = None,
) -> str:
    if not content:
        return ""
    try:
        x = decode("lark_entities_SystemContent", content)
    except Exception:
        return ""
    raw_contents = x.get("contents")
    contents: Mapping[str, Any] = raw_contents if isinstance(raw_contents, Mapping) else {}
    raw_values = x.get("contentValues")
    values: Mapping[str, Any] = raw_values if isinstance(raw_values, Mapping) else {}

    def hms(hk: str, mk: str, sk: str) -> str:
        secs = _num(contents.get(hk)) * 3600 + _num(contents.get(mk)) * 60 + _num(contents.get(sk))
        return _span_seconds(secs)

    type_num = _num(x.get("typeNum"))
    if type_num == _SYSTEM_MEETING_ENDED:
        return "[系统] 会议结束，时长 " + hms("numHour", "numMin", "numSec")
    if type_num == _SYSTEM_MEETING_START:
        return "[系统] 会议已开始"
    if type_num == _SYSTEM_CALL_ENDED:
        return "[系统] 通话结束，时长 " + hms(
            "duration_time_hour", "duration_time_min", "duration_time_sec"
        )

    tpl = _str(x.get("messageTemplate")).strip()
    if not tpl:
        return ""
    missing = False

    def repl(m: re.Match[str]) -> str:
        nonlocal missing
        key = m.group(1)
        v = values.get(key)
        if isinstance(v, Mapping):
            if t := _str(v.get("text")):
                return t
            id_list = v.get("ids")
            if isinstance(id_list, list) and id_list and names is not None:
                str_ids = [_str(i) for i in id_list]
                known = names(str_ids)
                who = [known[i] for i in str_ids if known.get(i)]
                if who:
                    return "、".join(who)
        missing = True
        return m.group(0)

    out = _BLANK_TPL_RE.sub(repl, tpl)
    if missing:
        return ""
    return "[系统] " + out


def _card_own_text(content: bytes, user: str = "") -> str:
    if not content:
        return "[卡片]"
    try:
        x = decode("lark_entities_CardContent", content)
    except Exception:
        return "[卡片]"
    raw_h = x.get("cardHeader")
    h: Mapping[str, Any] = raw_h if isinstance(raw_h, Mapping) else {}
    title = _str(h.get("title")).strip() or _str(h.get("mainTitle")).strip()
    body = _rich_plain(x.get("richText"), user)
    if _upgrade_hint(body):
        body = ""
    body = body.strip()
    if body.startswith("[图片]"):
        body = body[len("[图片]") :].strip()
    combined = f"{title}\n{body}".strip()
    return f"[卡片] {combined}".strip()


def forwarded(content: bytes) -> tuple[str, list[WebMessage]]:
    if not content:
        return "", []
    try:
        x = decode("lark_entities_MergeForwardContent", content)
    except Exception:
        return "", []
    title = _str(x.get("groupChatName"))
    a, b = _str(x.get("p2pCreatorName")), _str(x.get("p2pPartnerName"))
    if not title and a and b:
        title = f"{a} 和 {b}"
    msgs: list[WebMessage] = []
    raw_list = x.get("messages")
    if isinstance(raw_list, list):
        for v in raw_list:
            if isinstance(v, Mapping):
                msgs.append(message_of(v))
    return title, msgs


def describe_with(
    typ: int,
    content: bytes | None,
    user: str = "",
    names: Callable[[list[str]], Mapping[str, str]] | None = None,
) -> str:
    raw = content or b""

    def tag(label: str, name: str) -> str:
        return f"{label} {name.strip()}".strip()

    def field_val(schema: str, *path: str) -> str:
        if not raw:
            return ""
        try:
            v: Any = decode(schema, raw)
        except Exception:
            return ""
        for p in path:
            if not isinstance(v, Mapping):
                return ""
            v = v.get(p)
        return _str(v)

    if typ in {TYPE_TEXT, TYPE_POST}:
        return text_without(typ, raw, user)
    if typ == TYPE_IMAGE:
        return "[图片]"
    if typ == TYPE_FILE:
        return tag("[文件]", field_val("lark_entities_FileContent", "name"))
    if typ == TYPE_FOLDER:
        return tag("[文件夹]", field_val("lark_entities_FileContent", "name"))
    if typ == TYPE_MEDIA:
        return tag("[视频]", field_val("lark_entities_MediaContent", "name"))
    if typ == TYPE_AUDIO:
        return tag(
            "[语音]",
            field_val("lark_entities_AudioContent", "audioRecognition", "recognitionResult"),
        )
    if typ == TYPE_SHARE_USER:
        return tag("[名片]", field_val("lark_entities_ShareUserCardContent", "name"))
    if typ == TYPE_SHARE_CHAT:
        return tag("[群名片]", field_val("lark_entities_ShareGroupChatContent", "chat", "name"))
    if typ == TYPE_SHARE_EVENT:
        return tag(
            "[日程]",
            field_val("lark_entities_ShareCalendarEventContent", "calendarEvent", "summary"),
        )
    if typ == TYPE_LOCATION:
        return tag(
            "[位置]",
            field_val("lark_entities_LocationContent", "locationDescription", "name"),
        )
    if typ == TYPE_CARD:
        return _card_own_text(raw, user)
    if typ == TYPE_MERGE:
        return "[合并转发]"
    if typ == TYPE_STICKER:
        return "[表情]"
    if typ in {TYPE_CALENDAR, TYPE_CALENDAR_ALL}:
        return "[日程]"
    if typ == TYPE_CLOUD_FILE:
        return "[云文档]"
    if typ in {TYPE_HONGBAO, TYPE_HONGBAO_BIZ}:
        return "[红包]"
    if typ == TYPE_VIDEO_CHAT:
        t, _ = meeting_text(raw, names, "")
        return t
    if typ == TYPE_SYSTEM:
        return system_text(raw, names)
    if typ == TYPE_TODO:
        return "[任务]"
    if typ == TYPE_VOTE:
        return "[投票]"
    if typ == TYPE_LINK:
        return "[链接]"
    return ""


def describe(typ: int, content: bytes | None, user: str = "") -> str:
    return describe_with(typ, content, user, None)


def _i18n_text(x: Mapping[str, Any]) -> str:
    if s := _str(x.get("content")):
        return s
    m = x.get("i18n_content")
    if isinstance(m, Mapping):
        for k in ("zh_cn", "en_us"):
            if s := _str(m.get(k)):
                return s
    return ""


def _cell_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, list):
        return ", ".join(_cell_text(e) for e in v)
    if isinstance(v, Mapping):
        return _str(v.get("text")) or _str(v.get("name")) or _str(v.get("content"))
    return ""


def _card_table(x: Mapping[str, Any]) -> str:
    cols = x.get("columns")
    names: list[str] = []
    keys: list[str] = []
    if isinstance(cols, list):
        for c in cols:
            if isinstance(c, Mapping):
                keys.append(_str(c.get("name")))
                names.append(_str(c.get("display_name")) or _str(c.get("name")))
    out = [" | ".join(names)]
    rows = x.get("rows")
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, Mapping):
                cells = [_cell_text(r.get(k)) for k in keys]
                out.append(" | ".join(cells))
    return "\n".join(out)


def card_text(raw: str) -> str:
    try:
        card = json.loads(raw)
    except Exception:
        return ""
    lines: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        t = "".join(buf).strip()
        if t and not _upgrade_hint(t):
            lines.append(t)
        buf.clear()

    def walk(v: Any, depth: int) -> None:
        if depth > 40:
            return
        if isinstance(v, list):
            for e in v:
                walk(e, depth + 1)
            return
        if not isinstance(v, Mapping):
            return
        raw_p = v.get("property")
        p: Mapping[str, Any] = raw_p if isinstance(raw_p, Mapping) else v
        tag = _str(v.get("tag"))
        if tag in {"plain_text", "lark_md", "text"}:
            buf.append(_i18n_text(p))
            return
        if tag == "br":
            buf.append("\n")
            return
        if tag in {"a", "link"}:
            buf.append(_i18n_text(p) or _str(p.get("url")) or _str(p.get("href")))
            return
        if tag == "markdown":
            s = _i18n_text(p)
            if s:
                flush()
                buf.append(s)
                flush()
                return
        if tag in {"img", "image", "img_combination"}:
            flush()
            lines.append("[图片]")
            return
        if tag == "table":
            flush()
            if t := _card_table(p):
                lines.append(t)
            return
        if tag == "collapsible_panel":
            flush()
            walk(p.get("header"), depth + 1)
            flush()
            if bool(p.get("expanded")):
                walk(p.get("elements"), depth + 1)
                flush()
            return
        if tag in {
            "button",
            "overflow",
            "select_static",
            "select_person",
            "multi_select_static",
            "multi_select_person",
            "date_picker",
            "picker_time",
            "picker_datetime",
            "input",
            "checker",
            "hr",
            "action",
            "select_img",
        }:
            return

        for k in (
            "header",
            "title",
            "subtitle",
            "text",
            "content",
            "fields",
            "extra",
            "newBody",
            "body",
            "elements",
            "columns",
        ):
            if k == "body" and p.get("newBody") is not None:
                continue
            if k in p and not isinstance(p[k], str):
                walk(p[k], depth + 1)
        i18n_els = p.get("i18n_elements")
        if isinstance(i18n_els, Mapping) and p.get("elements") is None:
            target = i18n_els.get("zh_cn")
            if target is None:
                target = i18n_els.get("en_us")
            walk(target, depth + 1)
        if tag:
            flush()

    walk(card, 0)
    flush()
    return "\n".join(lines)
