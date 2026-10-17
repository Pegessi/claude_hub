"""Pure Codex session parsing and catalog projection for terminal discovery.

Filesystem discovery and the listing cache stay in ``ttyd_manager``. Callback
arguments preserve its existing function and constant monkeypatch boundaries.
"""

import json
import re
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Pattern, Protocol, Tuple


class SessionScanEntry(Protocol):
    """Read-only fields consumed from the manager-owned ScanEntry records."""

    @property
    def path(self) -> str: ...

    @property
    def mtime_ns(self) -> int: ...

    @property
    def size(self) -> int: ...

    @property
    def cwd(self) -> str: ...

    @property
    def ts(self) -> Optional[float]: ...

    @property
    def title(self) -> str: ...

    @property
    def thread_source(self) -> str: ...


# Prefixes that mark a user message as boilerplate context (env / permissions /
# AGENTS.md / recommended plugins) rather than the user's actual first prompt.
# These are injected by codex (or the AGENTS.md preamble) at the start of every
# session and should not be used as the session title. A message counts as
# boilerplate if ANY of its content items starts with one of these prefixes.
_CODEX_SKIP_TITLE_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<system-reminder>",
    "<recommended_plugins>",
    "# AGENTS.md instructions",
    "<codex_internal_context",
)

# Prefixes for workspace-manager-injected bootstrap/role prompts. These are
# idle-session preamble (waiting instructions, reviewer guidelines, dispatcher
# setup, resident-agent directives) and do not contain a task title. They are
# skipped in the same way as codex's own boilerplate so that later real
# messages (task assignment, review, or human input) can surface as the title.
_CODEX_WORKSPACE_BOOTSTRAP_PREFIXES = (
    "You are a resident",
    "You are the dispatcher agent for this workspace.",
    "You are an independent reviewer agent for this workspace.",
    "You are this workspace's RESIDENT",
)

# Prefixes for workspace-manager task-delivery prompts. These ARE meaningful
# first messages but start with a system line rather than a human-authored
# title. For these we extract the "Task title:" field (or inline variant) and
# use it as the session title.
_CODEX_WORKSPACE_TASK_PREFIXES = (
    "New workspace task assigned.",
    "Review workspace task.",
    "Continue workspace task from review.",
    "Dispatch decision needed.",
)

# Prefixes for workspace-manager hard-recovery prompts. These start with a
# warning emoji paragraph but contain a "Task title:" line further down.
_CODEX_WORKSPACE_RECOVERY_PREFIXES = (
    "⚠️  Your previous context was automatically cleared",
    "⚠️  Context refreshed after error.",
)

# Regex to extract "Task title: <title>" from a task-delivery prompt body.
_CODEX_TASK_TITLE_RE = re.compile(r"^\s*Task title:\s*(.+?)\s*$", re.MULTILINE)

# Regex to extract the inline "Task: <id> (<title>)" variant used in the
# revision-resume prompt.
_CODEX_TASK_INLINE_RE = re.compile(
    r"^\s*Task:\s*[0-9a-fA-F-]+\s+\((.+?)\)\s*(?:mode=|complexity=|iteration=|$)",
    re.MULTILINE,
)

# Regex to extract "Workspace: <name>" and "Session: <id>" from bootstrap
# prompts so we can fall back to a role label like "Reviewer (my-ws)".
_CODEX_WORKSPACE_LINE_RE = re.compile(r"^\s*Workspace:\s*(.+?)\s*$", re.MULTILINE)
_CODEX_SESSION_LINE_RE = re.compile(r"^\s*Session:\s*(.+?)\s*$", re.MULTILINE)

# Maximum length of the extracted session title (truncated with ellipsis).
_CODEX_TITLE_MAX_LEN = 80
# The first meaningful user message (and any task-delivery prompt) always
# appears near the top of the rollout — within the first few dozen lines.
# Scanning the whole file (which can be 10+ MB) for sessions that never
# contain a task-title prompt is wasteful, so cap the scan.
_CODEX_TITLE_SCAN_MAX_LINES = 100


def parse_session_meta(
    first_line: str, *, datetime_type: type[datetime] = datetime
) -> Tuple[Optional[str], Optional[float], Optional[str], str]:
    """Parse a codex rollout first line into (sid, ts_epoch, cwd, thread_source).

    Returns ``(None, None, None, "")`` for unparseable lines. Canonical sid is
    ``payload.id`` (V0 verified: matches the filename uuid and is what codex
    resume keys off when the exact uuid is passed); ``payload.session_id`` is
    the stable parent thread id and differs on forks, so we treat it as a
    secondary alias only for back-compat with older legacy-flat rollouts.
    Defensive against legacy format (no ``payload`` wrapper) which stores
    ``session_id``/``id``/``cwd``/``timestamp`` at the top level.

    ``thread_source`` is one of ``"user"``, ``"subagent"``, ``"automation"``,
    or ``""`` for legacy rollouts that don't carry the field.
    """
    try:
        record = json.loads(first_line)
    except (json.JSONDecodeError, TypeError):
        return None, None, None, ""
    if not isinstance(record, dict):
        return None, None, None, ""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        payload = record  # legacy flat format
    sid = payload.get("id") or payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None, None, None, ""
    ts_raw = payload.get("timestamp") or record.get("timestamp")
    ts_epoch: Optional[float] = None
    if isinstance(ts_raw, str):
        try:
            ts_epoch = datetime_type.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts_epoch = None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str):
        cwd = None
    thread_source = payload.get("thread_source")
    if not isinstance(thread_source, str):
        thread_source = ""
    return sid, ts_epoch, cwd, thread_source


def diff_scans(
    pre: Mapping[str, SessionScanEntry], post: Mapping[str, SessionScanEntry]
) -> Dict[str, str]:
    """Compute per-sid change between a pre and post snapshot.

    Returns a dict sid -> change_type, where change_type is one of:
    * ``'new'``       — sid not in pre; a fresh rollout file was created.
    * ``'appended'``  — file existed pre and both size AND mtime grew.
    * ``'attr_changed'`` — existed pre but mtime changed without size growth
                        (e.g. fs touch, codex metadata rewrite); NOT treated
                        as session activity for attribution purposes.
    Sids present in pre but absent in post (archival / deletion) are omitted.
    """
    out: Dict[str, str] = {}
    for sid, post_entry in post.items():
        pre_entry = pre.get(sid)
        if pre_entry is None:
            out[sid] = "new"
            continue
        if post_entry.path != pre_entry.path:
            # sid relocated (e.g. moved to archived_sessions) — treat as attr
            out[sid] = "attr_changed"
            continue
        if post_entry.size > pre_entry.size and post_entry.mtime_ns > pre_entry.mtime_ns:
            out[sid] = "appended"
            continue
        if post_entry.mtime_ns != pre_entry.mtime_ns:
            out[sid] = "attr_changed"
    return out


def extract_task_title_from_text(
    text: str, *, task_title_re: Pattern[str], task_inline_re: Pattern[str]
) -> str | None:
    """Try to pull a task title from a workspace task/recovery prompt body.

    Checks for both ``Task title: <x>`` and the inline ``Task: <id> (<x>)``
    variants. Returns the first match stripped, or None if neither is found.
    """
    m = task_title_re.search(text)
    if m:
        return m.group(1).strip()
    m = task_inline_re.search(text)
    if m:
        return m.group(1).strip()
    return None


def bootstrap_role_label(text: str, *, workspace_line_re: Pattern[str]) -> str | None:
    """Derive a short role label from a workspace bootstrap/idle prompt.

    Uses the role-identifying first line plus workspace name when available,
    e.g. "Reviewer (my-workspace)". Returns None if the text does not look like
    a bootstrap prompt.
    """
    first_line = text.strip().split("\n", 1)[0]
    role_map = {
        "You are a resident": "Agent",
        "You are the dispatcher agent for this workspace.": "Dispatcher",
        "You are an independent reviewer agent for this workspace.": "Reviewer",
        "You are this workspace's RESIDENT": "Resident",
    }
    role = None
    for prefix, label in role_map.items():
        if first_line.startswith(prefix):
            role = label
            break
    if role is None:
        return None
    ws_match = workspace_line_re.search(text)
    if ws_match:
        return f"{role} ({ws_match.group(1).strip()})"
    return role


def classify_and_extract(
    text: str,
    *,
    bootstrap_prefixes: tuple[str, ...],
    task_prefixes: tuple[str, ...],
    recovery_prefixes: tuple[str, ...],
    role_label: Callable[[str], str | None],
    task_title: Callable[[str], str | None],
) -> tuple[str, str | None]:
    """Classify a user message and return (action, extracted_title).

    Returns one of:
      - ("skip", None)   — boilerplate / bootstrap preamble, keep scanning
      - ("title", t)     — use ``t`` as the session title
      - ("fallback", t)  — use ``t`` (truncated first-line / collapsed text)
                           when no task title or role label applied
    """
    stripped = text.strip()
    if not stripped:
        return ("skip", None)

    # Workspace bootstrap/idle prompts: these come FIRST in a managed session
    # (before any task is assigned), so we do NOT return the role label
    # immediately — we remember it as a fallback and keep scanning for a later
    # task message. If the session is truly idle the caller will pick up the
    # remembered fallback at end-of-rollout.
    if any(stripped.startswith(p) for p in bootstrap_prefixes):
        label = role_label(stripped)
        if label:
            return ("fallback", label)
        return ("skip", None)

    # Task-delivery prompts (assignment / review / continue / dispatch):
    # extract the embedded Task title.
    if any(stripped.startswith(p) for p in task_prefixes):
        t = task_title(stripped)
        if t:
            return ("title", t)
        return ("fallback", stripped)

    # Hard-recovery / revision-resume prompts: same extraction, just prefixed
    # with the warning emoji.
    if any(stripped.startswith(p) for p in recovery_prefixes):
        t = task_title(stripped)
        if t:
            return ("title", t)
        return ("fallback", stripped)

    return ("fallback", stripped)


def title_from_lines(
    lines: Iterable[str],
    *,
    skip_prefixes: tuple[str, ...],
    max_length: int,
    classify: Callable[[str], tuple[str, str | None]],
) -> str:
    """Extract a session title from an iterable of rollout JSONL lines.

    See :func:`_codex_session_title` for the extraction rules. This variant
    operates on already-read lines so callers can combine session_meta parsing
    and title extraction in a single file open.
    """
    fallback_title = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload") or {}
        if payload.get("role") != "user":
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        texts: List[str] = []
        is_preamble = False
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "input_text":
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if any(text.startswith(p) for p in skip_prefixes):
                is_preamble = True
                break
            texts.append(text)
        if is_preamble:
            continue
        if not texts:
            continue

        joined = "\n\n".join(texts)
        action, extracted = classify(joined)
        if action == "skip":
            continue
        if action == "title" and extracted:
            title = " ".join(extracted.split())
            if len(title) > max_length:
                title = title[: max_length - 1] + "…"
            return title
        if action == "fallback" and extracted:
            collapsed = " ".join(extracted.split())
            if len(collapsed) > max_length:
                collapsed = collapsed[: max_length - 1] + "…"
            fallback_title = collapsed
    return fallback_title


def parse_session_index(lines: Iterable[str]) -> Dict[str, str]:
    """Read the latest nonempty thread name for each session from index lines."""
    out: Dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = record.get("id")
        name = record.get("thread_name")
        if isinstance(sid, str) and sid and isinstance(name, str) and name:
            out[sid] = name
    return out


def group_sessions(
    scan: Mapping[str, SessionScanEntry],
    thread_names: Mapping[str, str],
    *,
    datetime_type: type[datetime] = datetime,
) -> List[Dict[str, Any]]:
    """Project user sessions into cwd groups without mutating scan or index data."""
    raw: Dict[str, Dict[str, Any]] = {}
    for sid, entry in scan.items():
        # Filter out subagent and automation sessions. Only user-initiated
        # sessions (thread_source == "user") and legacy rollouts (empty
        # thread_source) are shown in the picker.
        if entry.thread_source in ("subagent", "automation"):
            continue
        start_epoch = entry.ts or 0.0
        existing = raw.get(sid)
        if existing and existing["start_epoch"] >= start_epoch:
            continue
        title = thread_names.get(sid) or entry.title
        raw[sid] = {
            "session_id": sid,
            "cwd": entry.cwd or "",
            "start_epoch": start_epoch,
            "start_time": (
                datetime_type.fromtimestamp(start_epoch).isoformat() if start_epoch else ""
            ),
            "title": title,
        }

    # Group by cwd.
    by_cwd: Dict[str, List[Dict[str, Any]]] = {}
    for sess in raw.values():
        by_cwd.setdefault(sess["cwd"], []).append(sess)

    # Sort within each group (most recent first) and order groups by latest.
    result: List[Dict[str, Any]] = []
    for cwd, sessions in sorted(
        by_cwd.items(),
        key=lambda kv: max(s["start_epoch"] for s in kv[1]),
        reverse=True,
    ):
        sessions.sort(key=lambda s: s["start_epoch"], reverse=True)
        for s in sessions:
            s.pop("start_epoch", None)  # internal-only, not for the API
        result.append(
            {
                "cwd": cwd,
                "sessions": sessions,
            }
        )

    return result
