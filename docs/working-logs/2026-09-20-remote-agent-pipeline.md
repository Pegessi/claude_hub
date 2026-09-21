# Remote Chat / Terminal / workspace agent pipeline

Date: 2026-09-20 · Branch: `fix/remote-agent-pipeline` (worktree `~/claude_hub_worktree/remote-agent-audit`)

## System overview

A Hub "remote tab" is still a **local** ttyd + tmux seat. For Terminal sessions
the pane command is `ssh -tt` into the chosen SSH profile, which then starts a
**remote** tmux session named `claude-hub-{tab_id[:8]}` and the agent CLI.
Reverse SSH (`-R 127.0.0.1:{forward}:127.0.0.1:{hub_port}`) allocates the
remote listen port as `settings.port + 10000` (live 8173 → 18173; isolated
18273 → 28273) so a remote agent can call back into this Hub without
colliding with another Hub on the same SSH host.

Structured Chat is a different surface: the pane is an inert local shell, and
the model runs in a local native `ProviderSession`. Workspace-managed agents
(orchestrator / reviewer / dispatcher) are **Terminal-only**.

Probed on an isolated worktree backend (`127.0.0.1:18273`, Vite `5276`, tmux
socket `ch-remote-agent-audit`, runtime home
`~/.claude_hub/worktrees/remote-agent-audit`). Live Hub `5173`/`8173` was not
touched.

## Probe results (before this branch)

| Path | Result |
| --- | --- |
| `GET /api/remote/profiles` | 20 aliases, including fake `ASCII` from an SSH `Host` comment |
| Remote fs list `mac_mini` | 200, `/Users/Apple` |
| Remote fs list `merlin_dev` | 502 `Remote returned invalid directory data` (stdout is not JSON) |
| Chat + Remote create | **201**, `target=remote` persisted, pane is local zsh (no SSH) |
| Terminal + Remote create | 201, pane `Apple@Mac-mini ~ %`, process is `ssh` |
| Agent on remote workspace, omitted target | 201, **`target=local`** (intentional CLI/backend default) |
| Explicit remote agent / reviewer | 201, SSH + remote Codex start; Codex "Update available" TUI ate bootstrap; reviewer ran `npm install -g @openai/codex` |

## Module design

- **Fail-closed Chat + Remote.** `TerminalTabCreate._reject_chat_remote`,
  `TTYDManager.create_tab` / `update_tab`, TabBar Remote disabled for Chat, and
  `handleCreateTab` all reject the combination. Native Chat cannot SSH; storing
  `target=remote` was a lie.
- **SSH Host comments.** `_discover_ssh_config_profiles` strips `#…` before
  tokenizing `Host` so `Host merlin_persistent  # 自定义，必须为 ASCII 字符`
  does not invent a profile named `ASCII`.
- **Reviewer placement.** Auto-review follows the **worker's**
  `target` / `remote_profile_id` / `remote_cwd`, not `workspace.target`. Idle
  reviewer reuse must match that placement. A local workspace with a remote
  worker no longer silently gets a local reviewer.
- **Add-Agent default.** The workspace UI now preselects `workspace.target`.
  Backend omitted-target remains LOCAL (`_effective_agent_target`) so CLI
  scripts that never pass `--target` stay local. The UI hint explains the
  mismatch.

## Key issues / pitfalls

1. **Chat + Remote was a UI/API lie.** Chat launch is an `if session_kind ==
   CHAT` branch *before* the remote SSH launcher. The pane never left the Hub
   host. Do not "fix" this by SSHing Chat; keep Chat local and refuse Remote.
2. **Omitted agent target is LOCAL even on a remote workspace.** Covered by
   `test_ensure_workspace_agent_omitted_target_on_remote_workspace_creates_local`.
   UI defaults are a convenience, not a backend policy change.
3. **Codex update TUI vs bootstrap.** `_wait_for_agent_prompt` now runs
   before bootstrap. An "Update available" + "Skip" pane is dismissed with
   Down then Enter (not a raw Enter on option 1). If the dialog is still
   showing when the wait expires, create rolls back instead of installing.
4. **merlin_dev directory listing.** Listing SSH is `-T` / BatchMode /
   RequestTTY=no. `parse_remote_listing_stdout` takes the last JSON object
   that looks like a directory payload so MOTD can precede it. Merlin
   workspace / ssh-candy aliases (`merlin_dev`, `merlin_dev_2`,
   `merlin_dev_evo`, merlin-ssh-proxy, `*.seedjob.*` on workspace hosts)
   swallow `ssh host 'cmd'` (rc 0, empty stdout) and instead run a line
   from stdin. `list_remote_directory` therefore sends a **single-line**
   base64-wrapped python listing command on stdin for those profiles only —
   not a blanket `merlin_*` match (`merlin_persistent` stays argv). A
   multiline `python3 -c` heredoc would be truncated by a one-line stdin
   shell. Do not use `-tt` for listing; it hangs on the Trial TTY.
   Interactive Terminal/agent create on these aliases is fail-closed
   (`STDIN_SHELL_REMOTE_UNSUPPORTED`); profiles are marked `stdin_shell`
   for the UI.
5. **Claude `--settings` is Hub-local.** `_write_launch_settings_file`
   already no-ops for `target=remote`; remote Claude launchers must not
   embed `--settings`. Locked by test.
6. **Reverse-forward.** `-R` maps local `settings.port` to a remote listen
   port of `settings.port + REMOTE_FORWARD_PORT_OFFSET` (10000). Isolated
   preview 18273 therefore listens remotely on 28273 instead of colliding
   with live Hub's 18173. `REMOTE_FORWARD_PORT_BASE` remains 18173 as the
   default-Hub constant for docs/tests that assume PORT=8173.

## Isolation

Worktree backends must not write `~/.claude_hub/workspaces`, live `tabs.json`,
or the default tmux server. This probe used
`~/.claude_hub/worktrees/remote-agent-audit` and `tmux -L ch-remote-agent-audit`.
Remote leftover tmux names to delete after verification: only sessions this
probe created. Do not kill older `claude-hub-*` sessions on `mac_mini`.

## Probe results (after this branch)

Isolated Hub `127.0.0.1:18273` / Vite `5276`, runtime
`~/.claude_hub/worktrees/remote-agent-audit`, tmux `-L ch-remote-agent-audit`.

| Path | Result |
| --- | --- |
| Remote fs list `mac_mini` | 200, `/Users/Apple` |
| Remote fs list `merlin_dev` | 200, `/home/tiger` (stdin-shell + one-line base64 listing) |
| Chat + Remote create | **422** `CHAT_REMOTE_UNSUPPORTED` |
| Terminal + Remote `mac_mini` | 201, local pane `ssh -tt mac_mini …` |
| Remote workspace agent (`terminal`, `mac_mini`) | 201, `-R 127.0.0.1:28273:127.0.0.1:18273` |
| `merlin_dev` as interactive Terminal/agent | **400** `STDIN_SHELL_REMOTE_UNSUPPORTED`; listing still 200 |