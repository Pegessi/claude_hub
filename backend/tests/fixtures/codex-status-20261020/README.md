# Codex terminal status regression captures

`busy.ansi` is the unmodified byte output of `tmux capture-pane -e -p -t
claude-hub-13656409` (agent-4, task 7ee3b02b) at 2026-10-20 12:04 UTC
(host clock). `idle.ansi` is the unmodified `-S -200` capture of
claude-hub-93962adf (agent-3). See capture-manifest.json for exact timestamps,
commands and SHA-256. ANSI SGR styling is retained, not reconstructed.

`busy-frame-0.ansi` through `busy-frame-5.ansi` are six later unmodified captures
of agent-4 approximately 220ms apart; see rapid-manifest.json. They exercise
natural spinner/frame repainting without injecting terminal input. Capture
commands only read panes; they do not resize or control sessions.

The host UTC clock reads eight hours behind Hub's naive timestamp strings
(e.g. capture 12:04 UTC versus API sampled_at 20:04). Both are retained as
observed; no timestamp was silently converted.

`busy-subagent.ansi` is a later unmodified busy frame with live subagent
interaction, timestamped and hashed in capture-manifest.json.

`native-statuses.json` is a projection of real captured tab status JSON for
Claude, Cursor and MASO (fields retained: provider, status, detail, sampled_at).
These native-chat statuses bypass pane classification. Full source status and
log evidence is retained with the task's FINDINGS.md, not treated as terminal
screens for these providers.

Tests explicitly derive elapsed-time, braille-spinner and tall/resized-chrome
variants from the busy bytes. Those variants are not claimed as independent
live captures. No shared session was resized. Review round 3 adds actual installed-Codex
private-TUI resize captures in `resized/`; see its README and manifest.
Captured busy screens already contain real ANSI-heavy tool history; captured
idle retains Ran/Worked history.
