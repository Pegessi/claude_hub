"""Convert assistant text into bounded Feishu message payloads."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Mapping, TypedDict

FEISHU_MESSAGE_MAX_CHARS = 20_000
FEISHU_EMPTY_RESPONSE_TEXT = "Claude Hub completed without a text response."
_FEISHU_AT_TAG_RE = re.compile(r"<\s*/?\s*at\b[^>]*>", re.IGNORECASE)
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^( {0,3})(#{1,6})([ \t]+.*)$")
_BIDI_CONTROL_CHARACTERS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e" "\u2066\u2067\u2068\u2069"
)


@dataclass(frozen=True)
class FeishuMessagePayload:
    """A Feishu message type and its JSON-compatible content mapping."""

    msg_type: Literal["post", "text"]
    content: Mapping[str, object]


class FeishuMarkdownElement(TypedDict):
    tag: Literal["md"]
    text: str


class FeishuPostLocale(TypedDict):
    content: list[list[FeishuMarkdownElement]]


class FeishuPostContent(TypedDict):
    zh_cn: FeishuPostLocale


def build_feishu_post_content(markdown: str) -> FeishuPostContent:
    """Return bounded Feishu rich-text content or reject unsafe Markdown."""

    prepared = _prepare_post_markdown(markdown)
    return {"zh_cn": {"content": [[{"tag": "md", "text": prepared}]]}}


def build_feishu_message_payload(text: str) -> FeishuMessagePayload:
    """Prefer a rich-text post and safely fall back to a plain text payload."""

    normalized = text.strip() or FEISHU_EMPTY_RESPONSE_TEXT
    try:
        return FeishuMessagePayload(
            msg_type="post",
            content=build_feishu_post_content(normalized),
        )
    except ValueError:
        return FeishuMessagePayload(
            msg_type="text",
            content={"text": _bound_plain_text(_sanitize_plain_text(normalized))},
        )


def _prepare_post_markdown(markdown: str) -> str:
    if _has_unsafe_control_character(markdown):
        raise ValueError("Feishu Markdown contains an unsafe control character")
    neutralized = _neutralize_mentions(_normalize_markdown_for_feishu(markdown))
    if _open_fence(neutralized) is not None:
        raise ValueError("Feishu Markdown contains an unpaired fenced code block")
    return _bound_markdown(neutralized)


def _normalize_markdown_for_feishu(markdown: str) -> str:
    """Apply the heading subset used by Feishu post ``md`` elements.

    Feishu's Markdown bridge renders H1 as H4 and H2/H3 as H5. Existing
    H4-H6 headings remain unchanged. Fenced code is byte-for-byte content and
    must never be interpreted as headings.
    """

    normalized: list[str] = []
    open_fence: tuple[str, int] | None = None
    previous_was_heading = False
    for line in markdown.splitlines():
        fence = _FENCE_RE.match(line)
        if open_fence is not None:
            normalized.append(line)
            if (
                fence is not None
                and fence.group(1)[0] == open_fence[0]
                and len(fence.group(1)) >= open_fence[1]
                and not fence.group(2).strip()
            ):
                open_fence = None
            previous_was_heading = False
            continue
        if fence is not None:
            marker, suffix = fence.groups()
            if marker[0] != "`" or "`" not in suffix:
                open_fence = (marker[0], len(marker))
            normalized.append(line)
            previous_was_heading = False
            continue

        heading = _HEADING_RE.match(line)
        if heading is None:
            normalized.append(line)
            previous_was_heading = False
            continue
        if previous_was_heading:
            normalized.append("")
        indent, marker, suffix = heading.groups()
        target_level = 4 if len(marker) == 1 else 5 if len(marker) <= 3 else len(marker)
        normalized.append(f"{indent}{'#' * target_level}{suffix}")
        previous_was_heading = True
    return "\n".join(normalized)


def _has_unsafe_control_character(text: str) -> bool:
    return any(
        (character not in "\n\t" and unicodedata.category(character) == "Cc")
        or character in _BIDI_CONTROL_CHARACTERS
        for character in text
    )


def _sanitize_plain_text(text: str) -> str:
    without_controls = "".join(
        (
            character
            if (
                character in "\n\t"
                or (
                    unicodedata.category(character) != "Cc"
                    and character not in _BIDI_CONTROL_CHARACTERS
                )
            )
            else "\ufffd"
        )
        for character in text
    )
    return _neutralize_mentions(without_controls)


def _neutralize_mentions(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        return f"＜{tag[1:-1]}＞"

    return _FEISHU_AT_TAG_RE.sub(replace, text)


def _open_fence(markdown: str) -> tuple[str, int] | None:
    open_fence: tuple[str, int] | None = None
    for line in markdown.splitlines():
        match = _FENCE_RE.match(line)
        if match is None:
            continue
        marker = match.group(1)
        suffix = match.group(2)
        if open_fence is None:
            if marker[0] == "`" and "`" in suffix:
                continue
            open_fence = (marker[0], len(marker))
        elif marker[0] == open_fence[0] and len(marker) >= open_fence[1] and not suffix.strip():
            open_fence = None
    return open_fence


def _bound_plain_text(text: str) -> str:
    if len(text) <= FEISHU_MESSAGE_MAX_CHARS:
        return text
    return text[: FEISHU_MESSAGE_MAX_CHARS - 1] + "…"


def _bound_markdown(markdown: str) -> str:
    if len(markdown) <= FEISHU_MESSAGE_MAX_CHARS:
        return markdown

    # Keep the truncation marker on its own line. Appending it directly to a
    # natural closing fence (````` -> `````…``) would turn that line back into
    # code and leave the bounded result with an open fence.
    ellipsis = "\n…"
    prefix_limit = FEISHU_MESSAGE_MAX_CHARS - len(ellipsis)
    while prefix_limit >= 0:
        prefix = markdown[:prefix_limit]
        open_fence = _open_fence(prefix)
        if open_fence is None:
            return prefix + ellipsis

        closing_fence = open_fence[0] * open_fence[1]
        suffix = f"\n…\n{closing_fence}"
        next_prefix_limit = FEISHU_MESSAGE_MAX_CHARS - len(suffix)
        if prefix_limit <= next_prefix_limit:
            candidate = prefix + suffix
            if _open_fence(candidate) is None:
                return candidate
            raise ValueError("Cannot safely close truncated Feishu Markdown fence")
        # Shortening the prefix can cross another opening or closing fence.
        # Recompute from that exact prefix instead of reusing stale fence state.
        prefix_limit = next_prefix_limit

    raise ValueError("Cannot safely bound Feishu Markdown")


__all__ = [
    "FEISHU_EMPTY_RESPONSE_TEXT",
    "FEISHU_MESSAGE_MAX_CHARS",
    "FeishuMessagePayload",
    "build_feishu_message_payload",
    "build_feishu_post_content",
]
