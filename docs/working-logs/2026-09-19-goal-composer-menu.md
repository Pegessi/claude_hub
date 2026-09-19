# Goal composer simplification

## System overview

The explicit Goal remains a Hub-managed persistent objective, separate from
Agent/Plan mode. This iteration removes configuration friction: one plus menu
for attachments and Goal creation, one objective field, and concise tips. There
is no Goal token budget, default turn limit, or hard turn ceiling. Completion,
needs-input/blocked, manual pause, restart recovery, and protocol/error handling
remain unchanged. Provider quotas and costs are not bypassed.

## Module design

- `ComposerAddMenu.vue` owns popup focus, keyboard navigation, outside dismissal,
  and responsive touch targets. `StructuredPane.vue` supplies capabilities and
  admission/attachment lock reasons and coordinates the existing mode/model menus.
- `GoalSetupDialog.vue` submits only an objective; `GoalStatusBar.vue` retains
  status, checkpoint details, lifecycle actions, and uncertain-stop recovery.
- The backend removes budget fields and scheduler stop checks. Token usage and
  completed-turn count are accounting only. Recent completion IDs retain the last
  100 items; current-turn identity still rejects older callbacks after eviction.
  Checkpoint history retains ten versions. These are storage bounds, not run limits.
- Snapshot v2 accepts v1 records, discards obsolete limit fields and create-input
  fingerprint keys, and maps `budget_limited` to paused with an explicit resume
  message. Loading never auto-starts work. The retired budget PATCH responds 410
  with a refresh instruction for older open clients. Older create payloads can
  replay using their objective and request ID; obsolete fields are ignored.

## Key issues / pitfalls

- Do not merely hide numeric inputs while leaving default/hard caps in execution.
- Restore focus to the stable plus trigger before the Goal dialog opens; menu
  items disappear from the DOM and cannot serve as a dialog return target.
- Starting/resuming in Plan mode remains prohibited. The menu explains why.
- Keep the new menu and teleported dialog closed when a cached pane deactivates.
- Native file selection must remain within a synchronous user click chain.
- Old records retain uncertain dispatch identity and existing cancellation guards.
  Removing limits does not authorize a second scheduler or broader actions.

## Validation

- Controller/API tests include 125 consecutive completions, bounded recent IDs,
  stale callback eviction, legacy snapshot/retry migration, and the retired API.
- Frontend unit coverage checks objective-only requests and existing lifecycle
  reconciliation. Real Vue browser checks mount the full StructuredPane against
  stubbed APIs in an isolated Vite server, never the live Hub or a paid provider.
- Browser coverage includes keyboard open/navigation/Tab/Escape, native attachment
  picker, outside dismissal, Plan guard, focus trap/return, objective-only create,
  active locks, pause/resume, and uncertain clear/retry at 1280px and 375px, including
  dark/light themes.
- Verified: 426 backend Goal/Chat transport/attachment regressions, 450 frontend
  unit tests, and five Chromium UI scenarios passed. Frontend lint/typecheck/build
  and backend mypy (101 source files), Black/isort checks passed. The UI screenshots
  were inspected at desktop and mobile widths. Live paid-provider runs and the
  unrelated full backend suite were not part of this focused iteration.
