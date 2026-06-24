# Agent Conversation Recovery on Startup (after machine reboot)

Date: 2026-06-23
Task: 服务agent恢复 (`70f4778e-1b3a-493a-bc87-f06dae3ba6a6`, autonomous)
Code: `backend/claude_hub/services/ttyd_manager.py`,
`backend/tests/test_ttyd_manager.py`

## System overview

Claude Hub persists every terminal tab's config to `~/.claude_hub/tabs.json`
(`STATE_FILE`). On backend startup, `lifespan()` in `main.py` calls
`ttyd_manager.start_all_tabs()`, which reconstructs a `TTYDProcess` per saved
tab via `_load_state()` and (re)launches its ttyd + tmux session.

Terminal persistence is provided by tmux: each tab owns a session named
`claude-hub-<tab_id[:8]>`. The key distinction this feature rests on:

- **Backend-only restart**: the machine stayed up, so the tmux session is still
  alive. Relaunch just reattaches (`tmux new-session -A`); the agent process and
  its conversation are exactly where they were. No resume needed — and resuming
  would be wrong (it would fork a second conversation).
- **Machine reboot**: tmux is gone. The old behavior relaunched each agent as a
  brand-new conversation, silently losing all prior context. This is the case
  the task targets: "机器要重启…启动时自动检测本地有没有之前的状态记录，把所有的
  terminal agent都恢复一下".

Every supported agent CLI already exposes a resume capability; this feature wires
that into startup so a reboot restores conversations instead of discarding them.

## Module design

All changes are in `TTYDProcess` (the per-tab object) plus its `_load_state`
reconstruction.

### Stable per-tab conversation id

`__init__` now accepts `agent_session_id` and `from_persisted_state`:

- For **claude** tabs, if no id is supplied a fresh `uuid4()` is generated and
  pinned at first launch via `claude --session-id <id>`. The id is written into
  `to_dict()` and so survives in `tabs.json`; on the next load it is read back.
- Non-claude agents get `agent_session_id = None` (their CLIs can't pin an id at
  launch).

Why a per-tab id rather than `claude --continue`? `--continue` is **cwd-scoped**
(most-recent conversation in the working directory). In a managed workspace ~8
agent tabs share one cwd (`/Users/bytedance/claude_hub`), so `--continue` would
resolve all of them to whichever conversation was touched last — a collision.
An explicit `--session-id`/`--resume <id>` keeps each tab's conversation
distinct. Verified empirically: a `--session-id` roundtrip resumes the exact
conversation; `claude --resume <unknown-id>` exits 1 (no fresh fallback), which
is why recovery uses `--resume <id> || <fresh-pinned>`.

### The recovery gate: `_should_recover(session_exists)`

Recovery fires only when **all** hold:

- `from_persisted_state` — this process was rebuilt from `tabs.json`, not freshly
  created by a user/API call (a brand-new tab has nothing to resume).
- `not session_exists` — the tmux session is absent (reboot), not a live
  reattach.
- `target == LOCAL` — remote tabs are out of scope.
- `agent_type ∈ {CLAUDE, CODEX, CURSOR}` — terminal tabs have no conversation.

`session_exists` is threaded from the two launch paths:
`ensure_tmux_session` (returns early if the session exists, so it passes
`session_exists=False`) and `_build_ttyd_command` (already computes
`session_exists`).

### Per-agent recovery commands (`_agent_start_command(recover)`)

- **claude**: `claude … --resume <id> || claude … --session-id <id>`. The
  fallback re-pins the same id so a legacy tab with no recorded session recovers
  cleanly on the following boot. Helpers `_claude_command()` /
  `_claude_session_arg()` were factored out so the fresh and resume forms share
  settings/model flags and solo-mode handling.
- **codex**: `codex resume --last || <fresh>` (codex cannot pin an id at launch,
  so "most recent" is the best available anchor). Solo codex recovers through the
  solo-mode launch branch; non-solo codex (which normally launches via the bare
  `self.shell` = `"codex"`) gets an explicit `recover` branch in both launch
  paths so it also routes through the agent command. `<fresh>` is the sandbox
  form when solo, plain `codex` otherwise.
- **cursor**: `agent --continue || agent`.
- **terminal**: unchanged (`${SHELL:-/bin/bash} -l`), never recovers.

## Recovering tabs that were ALREADY running before this feature

Reviewer follow-up: *"现在已经在运行的这些terminal和agent能被恢复吗 如果重启了的话"* —
can the agents running right now be recovered on reboot?

Honest answer: **not automatically for most of them, by construction** — and a
loose guess would be unsafe. A tab created before this feature has
`agent_session_id = None` in `tabs.json`; on reboot it has no id to resume. The
live agent's id cannot be scraped from the running process (claude appends-and-
closes its `.jsonl`, and the id is not in argv/env). So the only place to learn
the id is Claude's conversation log, correlated by time.

Measured on this machine: all 26 live claude tabs share just 2 project dirs
(288 + 277 `.jsonl` files). A ±120s timestamp match gave only 10/26 confident,
16 ambiguous; a structurally-safe (sole-jsonl / sole-live-tab) match gave 0.
Cross-wiring a tab to the *wrong* conversation on reboot is worse than a clean
start, so guessing is unacceptable.

`TTYDManager._backfill_agent_session_ids()` (called at the top of
`start_all_tabs`, before any relaunch, while tmux sessions are still alive)
therefore pins an id **only on an unambiguous match** and logs every skip:

- Gate: `from_persisted_state ∧ CLAUDE ∧ LOCAL ∧ agent_session_id is None ∧ cwd`.
  Never overwrites an existing id; never touches cursor/terminal/remote tabs.
- Anchor: `tmux session_created` (survives a backend restart; `None` after a
  real reboot ⇒ skip, since correlation is then meaningless).
- Candidates: each `~/.claude/projects/<cwd-key>/<sid>.jsonl` start time (first
  timestamped line only).
- Confident-match rule (`_pick_backfill_session`, all required): best delta
  ≤ 90s; exactly one candidate or runner-up ≥ 600s away (temporal isolation);
  matched file `mtime ≥ session_created − 5s` (conversation lived during this
  session). Otherwise skip + log the reason.
- On any pin, `_save_state()` persists the id so the *next* reboot resumes.

Net: the unambiguous subset of the current fleet becomes reboot-recoverable now;
the rest start fresh (logged), and every tab created after deploy is fully
recoverable via its pinned id. This is a deliberate correctness-over-coverage
trade — better a clean start than a tab resuming someone else's conversation.

## Key issues / pitfalls

- **Do not resume a live tmux session.** The whole feature hinges on
  `session_exists` gating recovery. A backend restart with surviving tmux must
  reattach, not resume, or it forks a duplicate conversation. Tests
  `test_claude_live_session_reattaches_without_resume` and
  `test_should_recover_only_when_persisted_and_session_gone` lock this in.
- **Shared cwd ⇒ id pinning is mandatory, not optional.** `--continue` is unsafe
  here precisely because the managed workspace runs many agents in one directory.
- **`claude --resume <missing>` exits 1**, so the `|| <fresh>` fallback is load-
  bearing for legacy tabs persisted before this change (no `agent_session_id` in
  their `tabs.json`). For those, `agent_session_id` is regenerated on load and
  the fresh branch pins it.
- **mypy**: `self.agent_session_id` needs an explicit `Optional[str]`
  annotation before the if/elif/else, otherwise the `None` branch is inferred
  incompatible with the first-assigned `str`.
- **Remote / `_tmux_shell_command`**: left calling `_agent_start_command()` with
  the default `recover=False`. Remote recovery is out of scope and
  `_tmux_shell_command` has no live callers.

## Validation

- black, isort, mypy: clean.
- `tests/test_ttyd_manager.py`: 51 passed (11 recovery tests + 8 backfill tests:
  5 pure `_pick_backfill_session` cases and 3 manager-level skip/pin cases).
- Backfill safety re-review (adversarial): PASS, no wrong-id repro; cwd-key
  derivation verified against real on-disk `~/.claude/projects` dirs.
- Full suite: 24 unrelated failures (`test_workspaces.py`,
  `test_workspace_orchestrator_contract.py`, two `test_ensure_tab_running_*`)
  are pre-existing event-loop pollution under pytest-randomly ordering — they
  pass in isolation and in combination with the new file, and touch no code on
  this branch.
