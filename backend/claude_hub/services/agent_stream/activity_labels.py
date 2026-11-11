"""Human activity labels for an agent turn.

The tab-status payload used to describe a busy session as "native provider
turn is in flight", which is provider plumbing, not something a user can act
on. This module turns the tool call the provider is currently running into a
short, human line ("Running pytest -q", "Reading src/app.ts"), using only the
``tool_call_started`` payload the tailer already holds — no provider call, no
history read.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Activity/detail wording shared with the terminal-status payload. These are
# product strings: they describe what the user can see, not provider plumbing.
WORKING_ACTIVITY = "Responding"
IDLE_DETAIL = "Ready"
FAILED_TURN_DETAIL = "Last turn failed"

# Argv/command budgets keep a row's activity line readable next to a name.
_MAX_SUBJECT_CHARS = 56

# Tools whose interesting subject is an explicit payload key. The concrete
# command/path comes before a model-authored description: "Running "pytest -q""
# identifies the work better than "Running "Run the test suite"".
_SUBJECT_KEYS = (
    "command",
    "cmd",
    "file_path",
    "path",
    "notebook_path",
    "pattern",
    "query",
    "url",
    "description",
    "prompt",
)

# Prefix verbs per tool name (lower-case provider tool names). Unknown tools
# fall back to "Running <name>", which is still better than provider jargon.
_TOOL_VERBS: Dict[str, str] = {
    "bash": "Running",
    "bashoutput": "Checking",
    "shell": "Running",
    "run_command": "Running",
    "read": "Reading",
    "write": "Writing",
    "edit": "Editing",
    "multiedit": "Editing",
    "notebookedit": "Editing",
    "grep": "Searching",
    "glob": "Searching",
    "ls": "Listing",
    "webfetch": "Fetching",
    "websearch": "Searching the web for",
    "todowrite": "Planning",
    "task": "Delegating",
    "agent": "Delegating",
    "askuserquestion": "Asking you",
    "ask_user_question": "Asking you",
}

# Tools whose subject is not worth showing (the verb already says it all).
_NO_SUBJECT_TOOLS = frozenset(
    {"todowrite", "task", "agent", "askuserquestion", "ask_user_question", "webfetch"}
)


def _shorten(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    if len(collapsed) <= _MAX_SUBJECT_CHARS:
        return collapsed
    return collapsed[: _MAX_SUBJECT_CHARS - 1].rstrip() + "…"


def _subject(payload: Dict[str, Any], tool_key: str) -> Optional[str]:
    if tool_key in _NO_SUBJECT_TOOLS:
        return None
    args = payload.get("args")
    if not isinstance(args, dict):
        return None
    for key in _SUBJECT_KEYS:
        subject = _shorten(args.get(key))
        if subject:
            return subject
    return None


def tool_activity_label(payload: Any) -> Optional[str]:
    """Return a human label for one ``tool_call_started`` payload.

    ``payload`` is the persisted event payload (``{"name", "args", ...}``).
    Anything unexpected returns ``None`` so a weird provider record degrades
    to the generic in-turn label instead of a broken string.
    """
    if not isinstance(payload, dict):
        return None
    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        return None
    tool_key = raw_name.strip().lower()
    verb = _TOOL_VERBS.get(tool_key)
    subject = _subject(payload, tool_key)
    if verb is None:
        # MCP tools arrive as "mcp__server__tool"; keep the tail readable.
        verb = "Running"
        subject = subject or _shorten(raw_name)
    if subject is None:
        return verb
    return f'{verb} "{subject}"'
