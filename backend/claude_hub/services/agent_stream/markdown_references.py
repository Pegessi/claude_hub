"""Select actual Markdown links from a Chat's canonical assistant text."""

import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from markdown_it import MarkdownIt

from claude_hub.models import ManagedSession
from claude_hub.services.agent_stream.local_images import _temporary_file
from claude_hub.services.agent_stream.store import AgentStreamStore

# Bound incomplete streamed messages without dropping opening code fences.
_MAX_MESSAGE_CHARS = 1024 * 1024


class _LocalLinkParser(MarkdownIt):
    def validateLink(self, url: str) -> bool:
        # The UI converts local file URLs before sanitizing the resulting link.
        return bool(re.match(r"^file://(?:localhost)?/", url, re.I)) or super().validateLink(url)


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.excluded: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in {
            "script",
            "style",
            "iframe",
            "object",
            "template",
            "textarea",
            "title",
            "xmp",
            "plaintext",
            "noscript",
        }:
            self.excluded.append(tag)
        if tag == "a" and not self.excluded:
            # Browsers keep the first duplicate attribute, including an empty href.
            for name, value in attrs:
                if name == "href":
                    if value is not None and not value.lstrip().lower().startswith("file:"):
                        self.links.append(value)
                    break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # HTML non-void tags such as <template/> remain open in the browser.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        # Malformed crossed tags must not reopen access inside raw/inert HTML.
        if self.excluded and self.excluded[-1] == tag:
            self.excluded.pop()


def _references_path(text: str, path: Path, cwd: str) -> bool:
    if len(text) > _MAX_MESSAGE_CHARS:
        return False
    parser = _LocalLinkParser("commonmark")
    links: list[str] = []
    anchors = _AnchorCollector()
    for token in parser.parse(text):
        if token.type == "html_block":
            anchors.feed(token.content)
        for child in token.children or []:
            if child.type == "link_open" and not anchors.excluded:
                href = child.attrGet("href")
                if isinstance(href, str):
                    links.append(href)
            elif child.type == "html_inline":
                anchors.feed(child.content)
    for href in [*links, *anchors.links]:
        source = re.sub(r"^file://(?:localhost)?(?=/)", "", href.strip(), flags=re.I)
        if re.match(r"^[#?]|^[a-z][a-z\d+.-]*:|^//", source, re.I):
            continue
        source = unquote(source.split("#", 1)[0].split("?", 1)[0])
        source = re.sub(r"(\.(?:md|markdown|mdown|mkd)):\d+$", r"\1", source, flags=re.I)
        try:
            candidate = Path(source).expanduser()
            absolute = candidate if candidate.is_absolute() else Path(cwd).expanduser() / candidate
            if Path(os.path.abspath(absolute)) == path:
                return True
        except (OSError, ValueError, RuntimeError):
            continue
    return False


def referenced_temporary_markdown(session: ManagedSession, raw_path: str) -> Optional[Path]:
    """Authorize a live exact pathname, not an immutable content snapshot.

    Code spans, fences, images and arbitrary tool output never grant access.
    Oversized incomplete streams fail closed rather than losing parser context.
    """
    real = _temporary_file(raw_path)
    if real is None:
        return None
    buffers: dict[tuple[str, str, str], Optional[str]] = {}
    try:
        with AgentStreamStore(session.workspace_id, session.id).path.open() as events:
            for line in events:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict) or event.get("session_id") != session.id:
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict) or payload.get("role", "assistant") != "assistant":
                    continue
                message_id = str(event.get("message_id") or "")
                if message_id.endswith(":user"):
                    continue
                kind = event.get("type")
                if kind == "turn_completed":
                    text = payload.get("assistant_text")
                    if isinstance(text, str) and _references_path(
                        text, real, session.workspace_path
                    ):
                        return real
                elif kind == "text_delta":
                    text = payload.get("text")
                    if not isinstance(text, str):
                        continue
                    key = (str(event.get("run_epoch")), str(event.get("turn_id")), message_id)
                    previous = buffers.get(key, "")
                    if payload.get("snapshot") is not True:
                        if previous is None:
                            continue
                        text = previous + text
                    buffers[key] = text if len(text) <= _MAX_MESSAGE_CHARS else None
                    if len(buffers) > 64:
                        old = buffers.pop(next(iter(buffers)))
                        if old is not None and _references_path(old, real, session.workspace_path):
                            return real
    except (OSError, UnicodeError):
        return None
    return (
        real
        if any(
            text is not None and _references_path(text, real, session.workspace_path)
            for text in buffers.values()
        )
        else None
    )
