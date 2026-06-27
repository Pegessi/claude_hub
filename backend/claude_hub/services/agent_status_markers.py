"""Agent-type-specific terminal status markers.

Each agent CLI paints its "actively working" indicator differently and in a
different part of the pane. Claude Code and Cursor keep their indicator on (or
near) the bottom line, so a small bottom-of-frame scan classifies them. Codex
(GPT-5.5) is the exception on two counts:

1. Its working indicator uses formats that match none of the Claude/Cursor
   markers — a braille spinner + token count ("⠞ Working  4.03k tokens") or a
   bullet + elapsed + interrupt hint ("• Working (3s • esc to interrupt)").
2. It renders that indicator ABOVE a tall persistent bottom chrome: the
   ``›``/``❯`` input composer, a "Queued follow-up inputs" panel that grows one
   line per queued item, and a ``gpt-5.5 … · ~/path`` footer. A bottom-10/-12
   line scan therefore falls entirely inside the chrome and misses the
   indicator, so a busy codex frame is misread as idle.

This module is the single authoritative home for the codex working-marker set
so that both the runtime-status classifier (``ttyd_manager``) and the
auto-continue busy-check (``workspace_state_policy``) agree by construction —
they call the same detector. Keeping one explicit marker set per agent type
(rather than loosening a single shared regex list) follows the lesson learned
when the cursor agent type was added: every consumer that parsed session output
kept using Claude-CLI-specific markers and silently returned wrong results for
the new agent type.

This is a dependency-free leaf module (only ``re``) so it can be imported from
anywhere without pulling in the terminal manager or its import-time side
effects.
"""

import re

# Two codex working-indicator formats observed verbatim in backend captures:
#   " ⠀⠞ Working  4.03k tokens"          braille spinner glyph(s) + token count
#   "• Working (3s • esc to interrupt)"  bullet + elapsed + interrupt hint
#
# Both place the word "Working" at the start of the line, optionally preceded by
# decoration: a run of braille spinner glyphs (U+2800–U+28FF), a bullet
# (U+2022), and/or whitespace. Anchoring to the line start plus a codex-specific
# suffix ("<n>[k|m] tokens", or "(… esc to interrupt") keeps prose that merely
# contains the word "working" from matching, and keeps Claude/Cursor frames
# (whose status lines start with "✻"/"Running", never "Working") from matching.
CODEX_WORKING_STATUS_RE = re.compile(
    r"^[\s•⠀-⣿]*working\s+\d[\d.,]*[km]?\s+tokens?\b"
    r"|^[\s•⠀-⣿]*working\s*\([^)]*esc to interrupt",
    re.IGNORECASE | re.MULTILINE,
)

# Codex runs as a full-screen (alternate-screen) TUI, so a tmux capture returns
# the current frame with no accumulated scrollback — the marker is present iff
# codex is showing it right now. We still bound the search to the recent frame
# as defense-in-depth (in case a capture ever includes history), set well above
# the tallest realistic codex chrome — composer + a sizable "Queued follow-up
# inputs" panel + footer — so the indicator is never scanned out from the top.
_CODEX_SCAN_LINES = 60


def codex_output_is_working(output: str) -> bool:
    """Return True if a codex pane shows a live working indicator.

    Codex places the indicator above its persistent bottom chrome, so callers
    must pass the whole captured frame (not just the bottom lines). A genuinely
    idle codex frame repaints the indicator away, so its absence is a reliable
    idle signal.
    """

    recent = "\n".join(output.splitlines()[-_CODEX_SCAN_LINES:])
    return bool(CODEX_WORKING_STATUS_RE.search(recent))
