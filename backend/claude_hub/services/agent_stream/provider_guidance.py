"""Pure prompt guidance formatting shared by native provider transports.

This module owns no session state. The transport decides when guidance is
injected and passes its compatibility constants into the formatting helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence, Tuple

from ..agent_execution_policy import EXECUTION_POLICY

# Interactive question protocol guidance (chat "no timeout").
#
# Claude and Cursor run each chat turn as a one-shot ``--print`` subprocess.
# In non-interactive mode the CLI cannot render an interactive prompt, so when
# the agent calls AskUserQuestion/AskQuestion the CLI immediately auto-declines
# it with a placeholder tool_result ("Answer questions?" for Claude; a
# "skipped" notice for Cursor). The agent reads that placeholder as "the user
# declined" and ends the turn — the perceived "timeout". This guidance teaches
# the agent to interpret the placeholder as "still pending in the UI — wait",
# so the card stays live and the agent continues when the user's follow-up
# answer arrives.
#
# Claude receives it via ``--append-system-prompt`` (clean, not part of the
# user message). Cursor's CLI has no system-prompt flag, so the guidance is
# prepended to the prompt wrapped in sentinel markers; the adapter strips the
# sentinel block when normalizing user messages so it never reaches the
# persisted transcript or the UI.
QUESTION_PROTOCOL_GUIDANCE = (
    "INTERACTIVE QUESTION PROTOCOL (Hub UI): When you call an interactive "
    "question tool (AskUserQuestion or AskQuestion), the question is rendered "
    "as an interactive card in the user's Hub interface and the user will "
    "answer by clicking it. In this non-interactive transport the tool may "
    "return a placeholder result (such as 'Answer questions?' or a 'skipped' "
    "notice) instead of the real answer. This placeholder means the question "
    "is STILL PENDING in the UI — it does NOT mean the user declined, "
    "dismissed, or skipped it. When you see this placeholder: (1) do NOT say "
    "the user declined, dismissed, ignored, or skipped the question; (2) do "
    "NOT re-ask, choose a default, or proceed without the answer; (3) end "
    "your turn with one short neutral line such as 'I've made my selection "
    "above — waiting for your choice.' and stop. The user's real answer will "
    "arrive as a follow-up message; continue from it then."
)

_HUB_QUESTION_PROTOCOL_START = "<<<HUB_QUESTION_PROTOCOL_V1>>>"
_HUB_QUESTION_PROTOCOL_END = "<<<END_HUB_QUESTION_PROTOCOL_V1>>>"


_HUB_IMAGE_ATTACHMENT_START = "<<<HUB_IMAGE_ATTACHMENT_V1>>>"
_HUB_IMAGE_ATTACHMENT_END = "<<<END_HUB_IMAGE_ATTACHMENT_V1>>>"

_IMAGE_ATTACHMENT_GUIDANCE = (
    "The user attached the image(s) listed below. Use your Read tool to read "
    "each image file before answering the user's message that follows. The "
    "image contents are part of the user's request; do not ask the user to "
    "re-attach them."
)


# Hub Chat runtime / execution guidance.
#
# A native Chat agent runs with ``CLAUDE_HUB_TAB_ID`` in its provider
# subprocess env (overlaid in :meth:`ProviderSession._build_env`) and the
# ``claude-hub`` CLI on PATH, and ``schedule create --kind chat_turn`` can
# enqueue a turn for the current conversation. Linked work can instead run in
# its own context. Keep this capability map short and load command help only
# for the selected path; it must not force a delegation on every user turn.
#
# Injected once, on the first user turn of a transport session (see
# :meth:`ProviderSession.send_message`), as a sentinel-wrapped prompt prefix.
# The ``$CLAUDE_HUB_TAB_ID`` reference is deliberately literal: the model
# expands it from its own process env; the backend never substitutes the id.
# Every transcript normalizer strips the block (see
# :func:`strip_hub_runtime_guidance`) so it reaches neither the persisted
# timeline nor the Chat UI; the authoritative Hub echo persists the clean
# user text before the transport ever wraps it.
HUB_RUNTIME_GUIDANCE = (
    "You are running inside Claude Hub. The `claude-hub` CLI is available on PATH. "
    "Read this Chat's ID from $CLAUDE_HUB_TAB_ID; never hardcode it.\n"
    + EXECUTION_POLICY
    + "Task tools: use `claude-hub task context` to inspect current context; consult only the selected "
    "command's help. `task register` creates an initiator-controlled record without dispatch; "
    "`task progress` records initiator progress; `task handoff` preserves the Task ID and changes control "
    "without starting work; `task dispatch` starts Workspace execution. Check context before replacing work.\n"
    "A reporter key permits progress, whole-Task completion, and release, not other Task management. "
    "A tracking role does not narrow that permission. Keep keys in protected files, never chat, logs, "
    "reports, or command output; authorize subagent access explicitly. Omit key-file options so "
    "register/handoff store the per-Task default file that `task progress` finds by Task ID and "
    "`task context` reports. Do not assume inherited context "
    "or permissions, or fall back to manual progress if a key is lost. A child's completed substep "
    "does not complete the whole Task.\n"
    "Lessons: for unfamiliar or repeated problems, use `claude-hub feedback context <workspace-id> "
    "--query <topic>`, then `claude-hub lessons get` for relevant details, not on every turn. For an "
    "explicit reusable user correction, get message IDs with `claude-hub feedback sources` and follow "
    "`feedback capture --help` to cite the exact quote. Do not invent corrections. Automatic controls: "
    "`claude-hub feedback status` / `feedback configure`.\n"
    "Schedules: create one only when the user requests a scheduled follow-up or recurring check. "
    "Use `schedule create --kind hub_task --help` for independent Workspace work. To continue this Chat:\n"
    '  claude-hub schedule create --name "<name>" --kind chat_turn '
    '--tab-id "$CLAUDE_HUB_TAB_ID" --interval <seconds> --message "<follow-up>"\n'
    "Use `claude-hub schedule list` / `schedule delete <id>` to inspect/remove schedules. Due Chat "
    "turns queue behind active turns or Goals; they do not interrupt them. `--kind tab_message` is "
    "for plain Terminal sessions, not this Chat."
)


_HUB_RUNTIME_START = "<claude_hub_instructions>"
_HUB_RUNTIME_END = "</claude_hub_instructions>"
# Provider transcripts can outlive the version that injected their first turn.
_HUB_RUNTIME_MARKERS = (
    (_HUB_RUNTIME_START, _HUB_RUNTIME_END),
    ("<<<HUB_RUNTIME_V1>>>", "<<<END_HUB_RUNTIME_V1>>>"),
)


def wrap_guidance_block(text: str, *, body: str, opening: str, closing: str) -> str:
    """Prefix a hidden block without modifying the user message."""
    return f"{opening}\n{body}\n{closing}\n\n{text}"


def wrap_image_attachment_guidance(
    text: str,
    image_paths: Sequence[Path],
    *,
    guidance: str,
    opening: str,
    closing: str,
) -> str:
    """Reference staged Cursor images without reading or owning their files."""
    if not image_paths:
        return text
    paths = "\n".join(f"- {path}" for path in image_paths)
    return wrap_guidance_block(text, body=f"{guidance}\n{paths}", opening=opening, closing=closing)


def wrap_hub_runtime_guidance(
    text: str,
    *,
    guidance: Optional[str],
    context: str,
    opening: str,
    closing: str,
    markers: Sequence[Tuple[str, str]],
) -> str:
    """Hide runtime advice and context together, escaping quoted close markers.

    Context may quote untrusted reports or memory cards. Break any closing
    marker in those quotes so the rest cannot leak into the visible message.
    """
    for _opening, marker_close in markers:
        context = context.replace(marker_close, marker_close[:2] + " " + marker_close[2:])
    body = "\n\n".join(part for part in (guidance, context) if part)
    return wrap_guidance_block(text, body=body, opening=opening, closing=closing)


def strip_guidance_block(text: str, *, markers: Sequence[Tuple[str, str]]) -> str:
    """Remove only the earliest matching block, preserving malformed user text.

    Current and historical marker pairs must never be mixed. If the earliest
    opening has no matching close, leave the whole message unchanged even if
    a later block is complete. Strip only separator newlines after the block.
    """
    if not text:
        return text
    start = len(text)
    selected: Optional[Tuple[str, str]] = None
    for opening, closing in markers:
        candidate = text.find(opening)
        if 0 <= candidate < start:
            start = candidate
            selected = (opening, closing)
    if selected is None:
        return text
    opening, closing = selected
    end = text.find(closing, start + len(opening))
    if end == -1:
        return text
    end += len(closing)
    return text[:start] + text[end:].lstrip("\n")
