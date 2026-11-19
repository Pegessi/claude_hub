"""Default terminal policies; runtime lookups use the compatibility bindings."""

import os
import re
from typing import Dict

from claude_hub.services.local_runtime_settings import local_setting

TMUX_SESSION_PREFIX = "claude-hub-"

_ORPHAN_TMUX_PRUNE_GRACE_SECONDS = 60.0

_TMUX_CONTROL_TIMEOUT_SECONDS = 5.0

_TAB_CREATE_STARTUP_TIMEOUT_SECONDS = 15.0

_MANAGED_TMUX_SESSION_RE = re.compile(rf"^{re.escape(TMUX_SESSION_PREFIX)}[0-9a-f]{{8}}$")

CURSOR_TRANSCRIPT_SCHEMA = "cli-transcript-v1"

SUPPORTED_CURSOR_TRANSCRIPT_VERSIONS: frozenset[str] = frozenset({"2026.08.25-3e8eec8"})

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]")

_BARE_SHELL_PROMPT_RE = re.compile(r"[❯>$#%»→λ]\s*$")

_ATTENTION_TAIL_PATTERNS = (
    "do you want to proceed",
    "do you want to continue",
    "enter to select",
    "tab/arrow keys to navigate",
    "arrow keys to navigate",
    "type something.",
    "esc to cancel",
    "(y/n)",
    "(y/n/a)",
    "[y/n]",
    "[y/n/a]",
    "press enter to continue",
    "press any key to continue",
)

_WORKING_TAIL_PATTERNS = (
    "esc to interrupt",
    "ctrl+c to interrupt",
    "ctrl-c to interrupt",
    # Cursor CLI shows "ctrl+c to stop" while the agent is actively running.
    "ctrl+c to stop",
    "ctrl-c to stop",
    "running…",
    "running...",
)

_CLAUDE_WORKING_STATUS_RE = re.compile(r"^[✻✢✶✳✷✸✹✺✽✦✧]\s+\S+…\s+\(", re.MULTILINE)

_CURSOR_WORKING_STATUS_RE = re.compile(r"\brunning\s+\d[\d,\.]*\s+tokens?\b", re.IGNORECASE)

_IDLE_TAIL_HINTS = (
    "? for shortcuts",
    "/ for commands",
)

_STATUS_CACHE_TTL_SECONDS = 0.75

_WORKING_FRAME_STALE_SECONDS = 45.0

_PORT_CHECK_TIMEOUT_SECONDS = 0.2

_MAX_TCP_PORT = 65535

_MAX_TTYD_BIND_ATTEMPTS = 3

_REMOTE_CAPTURE_TIMEOUT_SECONDS = 10.0

# The deployed model identifier is operator configuration, not a public default.
_CODING_PLAN_MODEL = local_setting("CLAUDE_HUB_CODING_PLAN_MODEL", "").strip()
_VOLCENGINE_CODING_PLAN_MODEL_ALIASES = (
    dict.fromkeys(
        (
            "ark/seed-code-0602",
            "ark/seed-code-0602[1m]",
            "ark/seed-code-6062",
            "ark/seed-code-6062[1m]",
        ),
        _CODING_PLAN_MODEL,
    )
    if _CODING_PLAN_MODEL
    else {}
)

_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

DEFAULT_CLAUDE_LAUNCH_ENV: Dict[str, str] = {
    "ANTHROPIC_BASE_URL": "https://ark.cn-beijing.volces.com/api/coding",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    **({key: _CODING_PLAN_MODEL for key in _MODEL_ENV_KEYS} if _CODING_PLAN_MODEL else {}),
}

_TMUX_LAUNCH_ENV_KEYS = (
    "HOME",
    "PATH",
    "SHELL",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)

_TMUX_STALE_ENV_KEYS = (
    "PYTEST_CURRENT_TEST",
    "PYTEST_VERSION",
)

_BACKFILL_MATCH_WINDOW_S = 90.0

_BACKFILL_ISOLATION_S = 600.0

_BACKFILL_MTIME_SLACK_S = 5.0

_CODEX_FENCE_SILENCE_S = 0.7

_CODEX_POLL_INTERVAL_S = 0.2

_CODEX_POLL_MAX_ATTEMPTS = 30

_CODEX_NEW_PIN_TS_EARLY_S = 2.0

_CODEX_NEW_PIN_TS_LATE_S = 8.0

_SINGLE_TAB_DISCOVERY_DELAY_S = 5.0


_CODEX_SESSIONS_CACHE_TTL = 30.0  # seconds

_TABS_STATE_VERSION = 2

_TAB_CLEANUP_AUTHORIZATION = "stream-discard-validated-v1"

_TAB_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
