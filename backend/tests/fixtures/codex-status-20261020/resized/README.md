# Actual Codex 0.163.0 resize captures

These are unmodified ANSI captures of the installed Codex TUI, **not text-wrap
transformations**. A task-owned private tmux socket (`codex-status-resize-task5`)
and private CODEX_HOME ran the CLI with a loopback-only mock Responses endpoint
at 127.0.0.1:18843. The mock emits response.created and keeps the response open;
no model, external provider, tool or other agent is contacted. Command:

```
env CODEX_HOME=<task-artifacts>/resize-probe/codex-home codex \
  --sandbox read-only --ask-for-approval never \
  'Resize screen status probe; wait for the mock response.'
```

CLI version `codex-cli 0.163.0`; initial private tmux window 120x40. All startup
and server code/config remain in the task handoff's resize-probe/probe.py and
resize-probe/codex-home/config.toml. `manifest.json` records each exact
resize-window command, capture-pane -e -p -S -120 command, UTC timestamp,
width/height and byte SHA-256. Resize order is 120x40, 80x24, 40x24, 30x16,
24x12, 20x12, 80x8, 120x12, 200x60. Busy captures follow a 400ms repaint wait.

Only this private probe received resize commands. An Escape key then cancelled
its pending mock turn; after 500ms, idle screens were captured at the same sizes
with 200ms waits. Idle is **cancelled mock idle**, not completed live model work.
The private probe auto-cleaned its tmux server and HTTP server after 120s.
The CLI left a managed daemon under its private CODEX_HOME; its environment
and executable path were verified before SIGTERM to that task-owned daemon.
It did not exit gracefully; a subsequent ownership-checked SIGKILL removed
that private daemon. No shared process was signalled.
Cleanup evidence is in the handoff resize-probe/cleanup.json. Shared agent-3/agent-4 and default tmux server were untouched.

Real narrow rendering ellipsizes the status line rather than word-wrapping:

- 40 columns: `• Working (20s • esc to interrupt)`
- 30 columns: `• Working (21s • esc to inter…`
- 24 columns: `• Working (21s • esc to…`
- 20 columns: `• Working (21s • es…`

This reproduces an actual status bug in the initial e254dc4 candidate at the
three narrow sizes (3 failed, 6 passed). The reviewer soft-wrap probe is tested
separately as **derived** input; it is not claimed as this CLI's native rendering.
Provider connectivity, tool execution and deployed Hub UI are not established
by this isolated rendering experiment.
