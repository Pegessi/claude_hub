# Demand and Feishu triage patrol

## Scope and ownership

Candidate branch: `feat/demand-triage-patrol`, based on `7c211de`.
All work stays in `~/claude_hub_worktree/demand-triage-patrol`.
No primary Hub service, shared tmux, live Feishu delivery, or credential-file
contents are used for development or verification. The parallel Task-recipe
scope, frontend, `AGENTS.md` / `CLAUDE.md`, and `CHANGELOG.md` are untouched;
the integration owner will add the changelog entry.

## Entry points and design

- Feishu websocket ingress accepts merged forwards; the existing dispatch path
  expands them into bounded, explicitly untrusted context, retaining child image
  ownership and the existing aggregate attachment limits. A bare P2P forward
  carries the user-approved default triage intent; accompanying instructions win.
- `services/demand_patrol/` owns validated configuration, deterministic gates,
  source adapters, and bounded run evidence. SQLite `local_storage` stores the
  logical `demand_patrol_state.json` document under the resolved runtime home.
- Feishu checks first authenticate the user identity, then inspect a bounded
  overlap window. Quiet runs never invoke a model, InfoWall mutation, or Dot.
  Actionable self-relevant unanswered messages may receive bounded read-only
  diagnosis, stable InfoWall deduplication, and a private reply-draft notice.
- The daily digest has no model dependency. It joins InfoWall demands, canonical
  Codebase MR observations and the existing Hub lifecycle projection. This
  branch's API is `workspace_manager.task_lifecycles` / `derive_task_lifecycle`,
  not the `compute_task_lifecycle` name in the task brief.
- `DemandPatrolService` serializes manual and scheduled runs. Daily windows are
  `[11:00, 12:30)` and `[17:30, 19:00)` in Asia/Shanghai by default. A scheduled
  notification claims its date/slot durably **before** delivery to avoid duplicate
  pushes after an ambiguous crash. Failed or uncertain delivery retains its claim
  and is observable in recent runs; use a manual daily run for explicit retry.
- API: authenticated `GET /api/patrol/status`, partial `PUT /api/patrol/config`,
  and `POST /api/patrol/run`. CLI: `claude-hub patrol status|config|run`.
  CLI registration follows the current `cli/main.py` registrar (the package
  `__init__.py` is only a reexport). `--dry-run` does not update watermarks,
  snapshots, delivery claims or run history, invoke models, or send notifications.
  `--no-notify` suppresses delivery but allows a real run's processing writes.
- Lifespan stops the patrol before draining Subagent and Dot transports.
  `CLAUDE_HUB_DISABLE_PATROL_LOOP=1` disables the resident loop; pytest also
  defaults to disabled, including child backends. Explicit tests use fake sources.

## Validation plan and results

The required checks cover formatting, the complete backend type graph, document
and import/size invariants, mocked Feishu parsing/dispatch, patrol scheduling and
restart deduplication, public REST/CLI controls, and bounded command execution.
The current repository uses `message_event_from_sdk`, not `_parse_message_event`.
The requested `tests/test_feishu_bot.py` does not exist at the base revision;
its actual split suites are reported separately rather than fabricating a pass.

Final source validation (all run in the owned worktree):

| Command | Result / evidence |
| --- | --- |
| `./scripts/verify.sh backend-format` | Passed: 666 files unchanged; `/tmp/claude-hub-verify.vipvlfe3` |
| `./scripts/verify.sh backend-types` | Passed: 641 source files; `/tmp/claude-hub-verify.hek2t4fa` |
| `./scripts/verify.sh docs` | Passed, including identical agent guides and structural gates |
| Focused command below | 207 passed in 4.11s; `/tmp/claude-hub-verify.m0sq4pd_` |
| `PY_COLORS=0 ./scripts/verify.sh backend-tests` | Default suite: 6,713 passed, 10 skipped, 17 warnings; 459.05s, 86% coverage; `/tmp/claude-hub-verify.vxkwgm9s` |
| `PY_COLORS=0 ./scripts/verify.sh backend-tests -- tests/test_ci_health.py -q` | 11 passed; confirms color-sensitive baseline failure diagnosis |

```bash
./scripts/verify.sh backend-tests -- \
  tests/test_feishu_merge_forward.py \
  tests/test_demand_patrol.py \
  tests/test_demand_patrol_digest.py \
  tests/test_demand_patrol_triage.py \
  tests/test_feishu_bot_http.py \
  tests/test_feishu_bot_websocket.py \
  tests/test_feishu_channel_dispatch.py \
  tests/test_feishu_protocol_client_bridge.py \
  tests/test_structure_model_cli_domains.py -q
```

The exact requested command including `tests/test_feishu_bot.py` was attempted:
pytest exited 4 before collection because that file is absent
(`/tmp/claude-hub-verify.qpjf2o7u`). The command above substitutes its existing
split suites and adds Channel A's focused tests; it is not a fabricated pass for
the absent path.

`./scripts/verify.sh all` was also attempted. Docs, format and types passed;
the default backend run stopped at an existing `test_ci_health.py` helper that
expects stderr lines to begin `ERROR: ` but receives an ANSI color prefix in this
environment (1,354 passed before the failure). No unrelated test code was changed:
the color-neutral default-suite rerun above passed. All four frontend targets
were blocked by absent `frontend/node_modules`; frontend validation therefore
remains incomplete, and `all` is **not** reported as passed. Existing backend
skips/warnings are retained as gaps, not counted as passes.

Independent integration review identified and closed two issues before the final
checks: status/config shared a long-held diagnosis lock, and killing only the CLI
wrapper left descendants holding its stdout pipe. Separate run/state locks keep
controls responsive and preserve concurrent configuration changes; POSIX command
sessions now have owned process-group cleanup with bounded reaping. Both regression
tests passed independently (2 passed, `/tmp/claude-hub-verify.ooruw3b9`) and in the
final 207-test suite. Disabling patrol prevents future runs; it does not forcibly
cancel an already-running diagnosis or erase uncertain delivery evidence.

Index safety and committed-HEAD checkout safety are checked at commit delivery.

## Evidence boundaries

No live Feishu, InfoWall write, Codebase MR query, or model run is used to claim
end-to-end delivery. Mocked transport tests prove parsing, gating, payload and
budget contracts; installed CLI specifications inform the adapters. Provider
availability, actual enterprise permissions, and real notification delivery
remain deployment smoke checks. Read-only triage is a prompt-level restriction
on the existing solo Subagent execution contract, not a new filesystem sandbox.
