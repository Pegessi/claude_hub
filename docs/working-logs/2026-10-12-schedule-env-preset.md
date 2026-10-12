# 2026-10-12 — Scheduled session tasks can launch workers with a chosen env preset

## Context

The QFO4ALL workspace runs a recurring `kind=hub_task` schedule ("512890 ETF
每日择时管线与 InfoWall 推送", `0 17 * * 1-5`). Investigation after cleaning up
12 stuck `review` runs found two layered problems:

1. The scheduled hub_task → worker binding bug fixed on 2026-09-23
   (`b5cbf70`) had prevented any completion/auto-cleanup; local `main` predated
   that fix until a pull on 2026-10-12.
2. After updating and test-firing the schedule, the freshly spawned worker
   failed on **every** provider request:
   `API Error: 400 Your account (2104798963) does not have a valid CodingPlan
   subscription, or your subscription has expired.`

Root cause of (2): scheduled session-creating kinds had no way to specify a
launch environment. `_fire_hub_task` / `_fire_new_session` built
`EnsureWorkspaceAgentRequest` without `env_preset` and without explicit env, so
`create_tab` fell back to `DEFAULT_CLAUDE_LAUNCH_ENV`
(`ark.cn-beijing.volces.com/api/coding` + `doubao-seed-2.0-code`). The
workspace's healthy `day1` custom preset (super-relay, `ark/seed-evolving`)
could only be used by manually created agents, never by the scheduler.

## Change

- `ScheduledTask` / `ScheduledTaskCreate` / `ScheduledTaskUpdate` gain
  `env_preset: Optional[str]`; persisted in `scheduled_tasks.json` and surfaced
  in `ScheduledTaskView`. Old records without the key load fine (default
  `None`).
- `_validate_scheduled_task_fields`:
  - rejects `env_preset` for `chat_turn` / `tab_message` (no session is
    created; preset is meaningless);
  - resolves the preset via `resolve_env_preset` at create/update time so an
    unknown id or malformed text returns 400 immediately instead of failing
    every fire.
- `_fire_new_session` / `_fire_hub_task` pass `env_preset=task.env_preset`
  into `EnsureWorkspaceAgentRequest`; `None` preserves the previous default
  behavior.
- Fire-time revalidation (`_resolve_fire_env_preset`): a custom preset deleted
  or corrupted after the schedule was saved makes the fire record a clear
  `last_error` ("references env preset … no longer available") instead of
  every run silently falling back to the hardcoded default env. For
  `hub_task` the check runs before the internal task is created, so a missing
  preset leaves no orphaned task or worker behind.
- Orphan convergence: when a scheduled hub task's worker dies and the task is
  redispatched to a replacement orchestrator, the new worker inherits the
  schedule's `env_preset`.
- Create/update normalize whitespace and an empty string to `None`.
- If a persistence file hand-edits a preset onto a non-session kind (the API
  can never produce that state), the next update strips the stale value
  instead of locking the schedule out of every edit/disable.
- CLI: `claude-hub schedule create|update --env-preset NAME_OR_ID`.
- Frontend: Scheduled Tasks panel shows an Env preset select (built-in +
  saved custom, excluding `none` which is represented by the "Agent default"
  empty option) for the two session kinds; on edit, choosing "Agent default"
  emits an explicit `null` so a stored preset can be cleared. CLI-supplied
  preset names (not just ids) are mapped to the option id, and the mapping is
  re-applied reactively once presets finish loading; a stored preset that no
  longer resolves is shown disabled as "… (no longer available)". TS types
  updated.

## Tests

- New backend tests: preset flows through to the worker request for both
  `hub_task` and `new_session`; rejected for `chat_turn`; unknown preset
  rejected on create/update; update trims and clears the field.
- Review-followup tests: whitespace normalization on create; preset rejected
  for `tab_message`; a preset deleted after save records `last_status=error`
  on fire (both kinds) without creating a hub task or spawning a worker.
- `test_task_storage_contract` fixture gained the new persisted key.
- Full backend suite: 2852 passed, 7 failed; all 7 reproduce on clean `main`
  in the same environment (5 feishu bot-pool `ExternalDispatchRetired`, 1
  real cold-restart timing budget, 1 chromium terminal-HMR e2e) and are
  pre-existing/environmental, not caused by this change.
- Frontend: typecheck, eslint (changed files), 747 unit tests, build pass.

## Independent adversarial review

A separate reviewer traced every `EnsureWorkspaceAgentRequest` fire/redispatch
site, persistence round-trip, validation path and frontend edge; verdict was
SHIP with no blocker/major. Two worthwhile findings were fixed before merge:
a stale preset on a non-session kind could lock a schedule out of edits
(backend), and a CLI-supplied preset name could be mislabeled
"(no longer available)" in the edit modal until reload (frontend reactive
name→id mapping). Deferred nits: CLI has no dedicated clear flag (use
`--payload-json '{"env_preset": null}'`) and `schedule list` omits the
preset column; a hidden built-in still shows as missing in the editor though
it fires fine; orphan redispatch can only recover the preset from the dead
session row (stamping the schedule id on the internal task would let it
re-derive the preset); the two preset lookups around tab spawn leave a tiny
benign TOCTOU window.

## Operational note

Recreating the QFO4ALL schedule with `--env-preset day1` plus an end-to-end
manual fire (worker completes → auto-DONE → ephemeral session auto-deleted) is
tracked as follow-up work and requires a backend restart on the new code.
