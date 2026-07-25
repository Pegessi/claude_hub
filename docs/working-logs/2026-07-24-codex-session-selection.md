# Codex Local Session Selection

Date: 2026-07-24
Scope: backend (`ttyd_manager.py`, `api/codex.py`, `api/__init__.py`, schemas),
frontend (`CodexSessionSelector.vue`, `TabBar.vue`, `types/index.ts`)

## Problem

Users accumulate many Codex CLI sessions across different working directories on
their local machine. When creating a new Codex terminal tab in the Hub, the only
options were (a) start a fresh session or (b) resume the single `--last` session
per cwd. There was no way to browse the full set of local sessions and pick a
specific conversation to continue. The user asked for a session-selection UI in
the "Create New Terminal" dialog that lists existing local Codex sessions
(grouped by working directory, with title and timestamp) and resumes the chosen
one via `codex resume <session-id>`.

Remote-workspace session connection was explicitly deferred to a follow-up
(out of scope for this change).

## System Overview

```
New-tab dialog (TabBar.vue)
  └─ CodexSessionSelector.vue  (local Codex only)
        ├─ GET /api/codex/sessions   →  grouped session list
        └─ v-model:session-id        →  selected session_id ('' = fresh)
              │
              ▼
  createTab({ agent_session_id })   (schemas: TerminalTabCreate)
              │
              ▼
  TTYDManager.create_tab → TTYDProcess(agent_session_id=...)
              │
              ▼
  _has_explicit_session_id → _should_recover() → codex resume <id> || codex
```

## Module Design

### Backend

- **`ttyd_manager.py`**
  - `list_codex_sessions()` — module-level function that reuses the existing
    `_codex_iter_rollouts()` walker (which covers both active
    `~/.codex/sessions/.../rollout-*.jsonl` and archived
    `~/.codex/archived_sessions/...`). It dedupes by stable `session_id`,
    keeping the rollout with the most recent `start_epoch`; groups the
    surviving sessions by `cwd`; sorts groups by their latest session
    descending and sessions within each group descending. Each session is
    returned as `{session_id, cwd, start_time (ISO), title}`. The internal
    `start_epoch` is popped before returning.
  - `_codex_session_title(path)` — reads a rollout and finds the first
    `response_item` with `role=user` whose content is a real prompt rather
    than boilerplate. A message is treated as preamble (skipped) if **any**
    content item starts with one of `_CODEX_SKIP_TITLE_PREFIXES`
    (`<environment_context>`, `<permissions instructions>`,
    `<recommended_plugins>`, `# AGENTS.md instructions`, ...). The chosen
    text is whitespace-collapsed and truncated to `_CODEX_TITLE_MAX_LEN=80`
    with an ellipsis.
  - `TTYDProcess.__init__` — after the `agent_session_id` assignment block,
    sets `self._has_explicit_session_id = bool(agent_session_id)`. This flag
    distinguishes an explicitly-requested resume id from the uuid4 placeholder
    generated for conversation pinning on fresh tabs.
  - `_should_recover(session_exists)` — returns `True` on reboot recovery
    (`from_persisted_state`) **or** an explicit session id
    (`_has_explicit_session_id`); otherwise `False` for fresh tabs. This
    drives `_codex_launch_command(recover)` to build
    `codex resume <id>{flags} || codex{flags}` (solo flags on both branches)
    for an explicit id, while a fresh tab still launches plain `codex`.

- **`api/codex.py`** (new) — `GET /api/codex/sessions`, auth-gated via
  `get_current_user`, returns the grouped list from `list_codex_sessions()`.
  The function is imported from the **module** directly
  (`from ..services.ttyd_manager import list_codex_sessions as _list_codex_sessions`)
  because `from ..services import ttyd_manager` resolves to the manager
  *instance* (the name is rebound in `services/__init__.py`), which does not
  expose the module-level function.

- **`api/__init__.py`** — includes `codex_router`.

- **schemas** — `TerminalTabCreate` / `TerminalTab` gained an optional
  `agent_session_id` field.

### Frontend

- **`CodexSessionSelector.vue`** (new) — fetches `/api/codex/sessions` on
  mount (with refresh button and loading/error/empty states). Renders a radio
  list: "Start a fresh session" (`''`) plus each session (title, cwd,
  formatted time). Uses the `v-model:session-id` pattern
  (`defineProps<{sessionId: string}>` + `defineEmits<{'update:sessionId': [id: string]}>`)
  with `$emit` in the template; a `watch` syncs the local selection if the
  parent resets the value on modal close.
- **`TabBar.vue`** — imports the selector, adds `agent_session_id: ''` to the
  create-tab `form`, and shows the selector only for
  `form.agent_type === 'codex' && form.target === 'local'`. `handleCreateTab`
  forwards the selected id (only for local Codex, else `undefined`) and resets
  it after submit; the value is also cleared on modal close and when
  switching away from the codex agent type.

## Key Issues / Pitfalls

1. **Generated uuid4 vs. explicit session id.** `TTYDProcess.__init__` always
   generates a `uuid.uuid4()` placeholder stored in `self.agent_session_id`
   for conversation pinning on fresh claude/codex tabs. A naive
   `if self.agent_session_id: return True` in `_should_recover` made **every**
   fresh Codex tab launch `codex resume --last || codex` instead of `codex`,
   breaking fresh-tab behavior and 4 existing tests. Fix: track
   `_has_explicit_session_id = bool(agent_session_id)` at init time and check
   that flag instead of the attribute. This preserves both the new resume
   feature and the existing fresh-tab / pinning behavior.

2. **Title extraction picks up boilerplate.** Real rollouts begin with several
   user-role messages that are not the actual prompt
   (`<environment_context>`, `<permissions instructions>`,
   `<recommended_plugins>`, `# AGENTS.md instructions`). The first pass
   returned these as the title. The fix treats a message as preamble if **any**
   content item starts with a boilerplate prefix (a single message can carry
   multiple content items) and keeps scanning for the first real user message.

3. **`from ..services import ttyd_manager` returns the instance.**
   `services/__init__.py` binds the `ttyd_manager` name to the manager
   instance, shadowing the submodule. Tests and the API must reach the
   *module* (for the `_codex_iter_rollouts` monkeypatch and the
   `list_codex_sessions` function). Use `importlib.import_module(...)` in
   tests and import the function directly from the module path in the API.

4. **Dedup / sort stability.** A session can have multiple rollout files
   (active + archived, or resumed). `list_codex_sessions` dedupes by
   `session_id` keeping the most recent `start_epoch`, then sorts groups and
   within-group descending so the picker shows recent work first.

## Acceptance Criteria (reviewer-checkable)

- AC1: `GET /api/codex/sessions` returns sessions grouped by cwd, sorted
  most-recent-first, each exposing `session_id`, `cwd`, `start_time` (ISO),
  `title`.
- AC2: title extraction skips boilerplate env/permissions/plugins/AGENTS.md
  blocks and surfaces the first real user message.
- AC3: duplicate session ids collapse to the most recent rollout.
- AC4: an explicit `agent_session_id` produces `codex resume <id> || codex`
  (solo flags on both branches); a fresh tab still launches plain `codex`.
- AC5: the session picker only appears for local Codex tabs.
- AC6: `codex resume <uuid>` works for both active and archived sessions.

## Tests

- `backend/tests/test_codex_sessions.py` (new): endpoint grouping/sort +
  required fields, boilerplate-skipping title, dedup-by-session-id.
- `backend/tests/test_ttyd_manager.py`: added
  `test_fresh_tab_with_explicit_session_id_recovers` and
  `test_generated_session_id_does_not_recover` to lock down the
  `_has_explicit_session_id` distinction.

## Out of Scope / Follow-up

- Remote-workspace Codex session selection — deferred.
- Auto-refresh / live updates of the session list — manual refresh only.
