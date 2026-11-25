"""Full-context recognition of the reserved trailing Hub Goal envelope.

Only protocol-only Markdown fences are wrappers. Inline quotations, blockquotes,
prose inside a code example, and text after the signal are not control input.
This does not infer completion from prose or from a checkpoint.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.rules_block import StateBlock, html_block

GOAL_TAIL_SCAN_CHARS = 64 * 1024 + 4096
MISSING_GOAL_SIGNAL_MESSAGE = "assistant response omitted a valid trailing goal-status signal"
GOAL_STATUS_RE = re.compile(
    r'<goal-status\s+state=["\'](continue|complete|blocked|needs_input)["\']\s*>'
    r"([^<]*(?:<(?!/?goal-status\b)[^<]*)*)</goal-status>\s*$",
    re.IGNORECASE | re.DOTALL,
)
_TAG = re.compile(r"<(/?)goal-(plan|checkpoint|status)\b[^>]*>", re.IGNORECASE)
_TAG_PREFIX = re.compile(r"</?goal-(plan|checkpoint|status)\b", re.IGNORECASE)
_BLOCK = re.compile(
    r"<goal-(plan|checkpoint|status)\b[^>]*>.*?</goal-\1>\s*", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class GoalSignal:
    state: str
    message: str | None
    protocol_text: str


def _control_blocks(text: str) -> Iterator[tuple[int, int]]:
    """Yield only non-nested blocks whose entire line prefix is control input."""
    stack: list[str] = []
    start = previous_end = 0
    nested = False
    for tag in _TAG.finditer(text):
        name = tag.group(2).lower()
        if not tag.group(1):
            if not stack:
                line_start = text.rfind("\n", 0, tag.start()) + 1
                prefix = text[line_start : tag.start()]
                # Adjacency requires a validated root block on this line,
                # not a quoted checkpoint or an arbitrary matching closer.
                adjacent = (
                    previous_end > line_start and not text[previous_end : tag.start()].strip()
                )
                start = tag.start()
                # An invalid outer opener is still an outer opener. Dropping
                # it would promote a subsequent nested tag to root authority.
                nested = not adjacent and re.fullmatch(r" {0,3}", prefix) is None
            else:
                nested = True
            stack.append(name)
            continue
        if not stack or stack[-1] != name:
            nested = True
            continue
        stack.pop()
        if stack or nested:
            continue
        end = tag.end()
        if _BLOCK.fullmatch(text[start:end]) is not None:
            yield start, end
            previous_end = end


def _protocol_only(text: str) -> bool:
    end = 0
    for start, block_end in _control_blocks(text):
        if text[end:start].strip():
            return False
        end = block_end
    return end > 0 and not text[end:].strip()


def _html_block_without_goal_tags(
    state: StateBlock, start_line: int, end_line: int, silent: bool
) -> bool:
    # Goal tags are protocol syntax, not Type-7 HTML blocks (which consume
    # everything until a blank line, hiding following fences and quotes).
    pos = state.bMarks[start_line] + state.tShift[start_line]
    if _TAG_PREFIX.match(state.src, pos) is not None:
        return False
    return html_block(state, start_line, end_line, silent)


def _normalize_fences(text: str) -> str:
    # Use the existing Markdown lexer for container/indentation boundaries,
    # including lazy blockquote continuation. A tail substring cannot supply
    # that context. Preserve ordinary source indentation for _control_blocks.
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    offsets[-1] = len(text)
    parser = MarkdownIt("commonmark")
    parser.block.ruler.at(
        "html_block",
        _html_block_without_goal_tags,
        {"alt": ["paragraph", "reference", "blockquote"]},
    )
    tokens = parser.parse(text)
    # A paragraph mixing literal protocol code spans with controls is ambiguous.
    # Do not let metadata consumers re-search its quoted multi-line contents.
    quoted_paragraphs = {
        token.map[0]
        for token in tokens
        if token.type == "inline"
        and token.map is not None
        and any(
            child.type == "code_inline" and _TAG.search(child.content)
            for child in token.children or []
        )
    }
    parts: list[str] = []
    cursor = 0
    for token in tokens:
        if token.level != 0 or token.map is None or token.nesting == -1:
            continue
        first, last = token.map
        start, end = offsets[first], offsets[last]
        parts.append(text[cursor:start])
        source = text[start:end]
        if token.type == "fence":
            closer = re.fullmatch(
                r" {0,3}" + re.escape(token.markup[0]) + "{" + str(len(token.markup)) + r",}[ \t]*",
                lines[last - 1].rstrip("\r\n"),
            )
            if (
                last > first + 1
                and closer is not None
                and token.info.strip().lower() in {"", "xml", "html", "json", "text"}
                and _protocol_only(token.content)
            ):
                parts.append(token.content.strip() + "\n")
            else:
                parts.append("[quoted Markdown block]\n")
        elif token.type == "paragraph_open" and first not in quoted_paragraphs:
            parts.append(source)
        else:
            parts.append("[quoted Markdown block]\n")
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def parse_goal_signal(text: str) -> GoalSignal | None:
    # Bound returned protocol data, not lexical context. Cropping the input can
    # turn an unclosed fence, outer tag or quoted long line into a root signal.
    # The cheap suffix check avoids scanning a long answer on ordinary deltas.
    text = text.rstrip()
    if "</goal-status>" not in text[-GOAL_TAIL_SCAN_CHARS:].lower():
        return None
    normalized = _normalize_fences(text.replace("\r\n", "\n").replace("\r", "\n")).rstrip()
    blocks = [
        (start, end)
        for start, end in _control_blocks(normalized)
        if start >= len(normalized) - GOAL_TAIL_SCAN_CHARS
    ]
    if not blocks or blocks[-1][1] != len(normalized):
        return None
    start, end = blocks[-1]
    match = GOAL_STATUS_RE.fullmatch(normalized[start:end])
    if match is None or len(match.group(2)) > 4096:
        return None
    # Only the whitespace-contiguous terminal envelope owns metadata. Earlier
    # examples separated by prose or quotations cannot supply a checkpoint/plan.
    for earlier_start, earlier_end in reversed(blocks[:-1]):
        if normalized[earlier_end:start].strip():
            break
        start = earlier_start
    protocol_text = normalized[start:end]
    return GoalSignal(match.group(1).lower(), match.group(2).strip() or None, protocol_text)
