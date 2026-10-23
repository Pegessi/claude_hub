# Agent workflow and focused reference map

Read the root `AGENTS.md` first. This reference expands its mandatory workflow;
load only the section relevant to the task. Current code and persisted task
records take precedence over historical working logs.

## Git worktree and Hub workspace

A Git worktree isolates edits. One Hub Workspace represents the repository's
canonical Git common-dir: main and linked worktrees share that Hub Workspace.
`workspace ensure` canonicalizes its path to the primary checkout. The agent's
execution cwd is separate; explicitly pass the feature worktree.

1. `claude-hub workspace ensure --path …` reuses/creates the canonical Workspace.
2. `claude-hub agent status WORKSPACE_ID` checks for an existing compatible agent.
   Creation is best-effort reuse, not a concurrency/idempotency boundary;
   overlapping requests can each create a session. `--no-reuse-existing` is
   for deliberate parallel execution; `--ephemeral` is for a task-owned session.
3. From the feature worktree:
   `claude-hub agent create WORKSPACE_ID --agent-type claude --cwd . --env-preset NAME_OR_ID`.
   Any built-in preset or saved custom preset is accepted by name or id.
   `default_env_preset` may supply it; explicit `--env KEY=VALUE` overrides keys.
   `day1` is only an example. Bare creation without `--cwd` targets the primary
   checkout and must not be used to start a feature writer.
4. After the task reaches a terminal state, `claude-hub task cleanup TASK_ID`
   cleans task-owned ephemeral sessions. Never delete shared or persistent agents.

## Managed Chat execution

Workspace execution agents and reviewers may use a fixed Chat surface. Choose
**Chat** when adding the agent, or pass `--session-kind chat` to `agent create`
or `task run`. The default remains `terminal`; existing sessions are not migrated.
For example, from the intended feature worktree:

```bash
claude-hub agent create WORKSPACE_ID --agent-type claude --session-kind chat --cwd .
```

Local Claude, Codex and Cursor support both surfaces. MASO supports Chat only,
with a loopback service endpoint for managed execution; see [MASO](MASO.md).
SSH execution remains Terminal-only. Provider, model, environment preset and
execution directory retain their existing explicit selection rules.

Managed Chat stores no terminal process: its tab identifies navigation while
`/sessions/{managed_session_id}/stream/…` owns the conversation. Assignments and
manual input use the Workspace mailbox, and completion still requires structured
reports and the existing independent-review/human-acceptance gates. Native
`turn_started` proves display intent, not provider receipt; uncertain delivery
is never automatically replayed.

A blocking question answer targets its exact provider request and turn; it is
handled as an in-turn response, not arbitrary steering or a queued Task message.
Busy-turn user input is sent to the durable Workspace mailbox, and a retry after
an uncertain HTTP response must reuse its call ID. Use Workspace Agent controls
for managed lifecycle changes: direct-tab stream, edit/fork and destructive tab
routes reject managed Chat backing IDs. Managed execution mode remains Task-owned.
Model/reasoning changes apply to the actual ManagedSession while idle; changing
persistent Codex process environment requires a new session rather than a silent
partial hot-swap.

Stop pauses queued delivery as well as cancelling the current turn. An explicit
message or Task continuation resumes the session. A finished model turn without
a required Task report is shown as needing input, not automatically declared
complete or retried using terminal heuristics. Unrelated reviews rotate the
provider context without deleting display history; same-task review cycles keep
context. This feature does not change first-turn Hub instruction injection.

Agent deletion is fail-closed when native or Terminal resource retirement cannot
be confirmed: the session remains visible for inspection and retry instead of
releasing its identity while the old runtime may still exist. This tightens the
previous best-effort Terminal deletion failure behavior; successful Terminal
operations and default surface selection are unchanged.

## Delegation and evidence handoff

Use the smallest workflow that can validate the result. Simple tasks run directly;
complex tasks identify dependencies and use parallel work only where inputs and
ownership are independent. A role is a responsibility, not a quota of agents.
The Hub's independent evaluator remains the autonomous review gate.

Every delegation supplies: task/owner, one concrete objective and acceptance
criteria, input references and base/head SHA, dependencies, allowed paths and
read/write boundary, worktree/resources, available tools, budget, stop condition,
and output format. Research and review are read-only. Two writers must not own
the same file. Worktrees do not isolate ports, databases, tmux, caches or cloud
resources: name those resources per task and serialize shared mutations.

For a one-off answer from another provider, use a Hub subagent instead of a
Task: `claude-hub agent run --provider X --prompt-file -` blocks and returns the
final answer. It creates no Task, review or acceptance. Use `--keep` with
`agent send` for follow-ups. Archiving the calling Chat closes its subagents.
Use Workspace Tasks only for tracked, reviewed work. See
[agent run subagents](working-logs/2026-10-24-agent-run-subagents.md).

A delegate returns result, changed files/artifacts and head SHA, reproducible
commands with cwd/results, unverified criteria, risks and next action. The owner
checks the evidence and records accepted/rejected/retried only for actual work.
Respect user-selected models; otherwise use configured defaults. Record actual
model evidence when exposed and state runtime limitations honestly.

On recovery, verify checkout/status/base/head, current task/report records,
remaining acceptance items and the next bounded action. Snapshots and conversation
summaries are navigation caches. Reload source records if they conflict or omit
needed details; do not edit generated state or infer acceptance from prose.

## Choosing a change boundary

Before implementation, identify the existing owner of the behavior, its callers,
and the externally observable contract. For stateful work, also identify who owns
the data, mutation lock, request identity and cleanup. Prefer an existing owner
and explicit inputs over another store or orchestration layer. A new owner needs
a reason the existing one cannot serve, not just a convenient place for new code.

Keep each change independently understandable and testable. Separate substantial
moves/renames from behavior changes when possible, retaining a working application
at each step. Coordinate overlapping ownership and integrate reviewed slices
promptly when authorized; pulling main into a long-lived branch alone does not
make that work available to other developers. Do not rewrite delivered baselines.

Extract a responsibility when changes repeatedly cross unrelated concerns, callers
reach into another owner's private state, or one workflow cannot be tested without
setting up unrelated workflows. File length is a clue, not an acceptance threshold.
Move the responsibility with its tests; separating a template from the state that
must outlive it can change behavior. The current
[Task state/UI boundaries](../ARCHITECTURE.md#task-state-and-ui-ownership) are one
concrete example, not a mandatory architecture for every module.

Before delivery, review the call path as well as the diff: is there one authority
for each state transition, are obsolete paths removed, and can the requirement be
checked without depending on the new implementation's layout? Give stable,
mechanically testable boundaries executable checks; leave design tradeoffs to
review. A regression test should fail on the defect it claims to prevent. Keep
current contracts in the architecture map or focused guide and implementation
history in dated logs, rather than copying history into the entry instructions.

When a failure recurs, first determine whether the missing protection belongs in
a behavioral test, a boundary check, tooling, or a short contract clarification.
If repair exceeds the current scope, record the affected owner, evidence and next
action in the existing work record. Revisit it when that area changes; do not let
an unowned TODO substitute for a decision. Remove superseded guidance when its
replacement is adopted, and reassess checks that repeatedly produce false alarms.

## Validation and review

Run checks appropriate to the change: syntax/lint/type/target tests first, then
contract/integration or real user paths where needed. A UI, concurrency or live
service claim needs evidence from that path; unit tests alone cannot prove it.
Use an independent reviewer for the required gate. Reviewer work starts from
requirements and the actual candidate diff/call paths, then tests worker claims.
Record cwd/base/head, commands/results and evidence paths. Findings state file/line,
severity, confidence, causal counterexample and the smallest necessary fix.
Explicitly list checks not run and remaining risks. A missing test result is unknown.

Commands (run in the task worktree):

- Use `./scripts/verify.sh all` for final validation and `--help` for focused
  targets. The backend type check includes tests; each target reports separately.
- For UI changes, additionally run the explicit browser gates and check the
  affected states/layouts in [UI acceptance](UI_ACCEPTANCE.md). `all` does not
  start browsers or prove live provider/OAuth/Bot behavior.
- When a change alters what a user can observe or do, extend the scenario that
  owns that surface, or add one, while writing the change rather than after.
  [UI acceptance](UI_ACCEPTANCE.md#adding-a-gate-for-new-work) covers when a
  gate is warranted, when it is not, and why a gate must be seen to fail once
  before it is trusted.
- Use the declared tool versions and locked setup from
  [CONTRIBUTING](../CONTRIBUTING.md#5-run-validation). Verification does not
  install dependencies or silently select a different runtime.
- Backend tests use private HOME/XDG/Hub paths and retain evidence. Existing
  fixtures own helper-process cleanup. Inspect interrupted runs before deleting
  their directories; do not target the main service.
- Development servers require dedicated ports and isolated `CLAUDE_HUB_HOME`,
  `CLAUDE_HUB_STATE_ROOT`, and `CLAUDE_HUB_TMUX_SOCKET` before imports.
- Prompt measurement remains `python scripts/measure_prompts.py` with a
  backend-capable interpreter; token estimates state their tokenizer/fallback.

CI invokes the same checks. Required job results, browser/manual acceptance and
live-provider evidence are distinct. A check not run is unknown; a worker report
or historical pass does not prove the final candidate. Keep current contracts in
current guides and use dated working logs only as historical evidence.

## Directory hygiene

Start with `git worktree list --porcelain`, the exact checkout's Git status, and
its top-level entries. Do not recursively search home or scan unrelated projects.
A branch being merged or old does not prove its worktree is disposable. Before
moving/removing any candidate, preserve dirty/untracked/ignored artifacts and
verify no process, tmux session, dev server or browser test still uses it. Unknown
ownership or use means keep it. Use `git worktree move` to relocate; never plain
`mv`. Only a proven disposable checkout may use `git worktree remove`.

Store new task evidence under a task-named directory in the assigned worktree or
an explicitly owned runtime directory, then link it from the report. Do not move
historical logs, probes, `tasks/`, `.cursor/`, or `tmp_remote_media/` as incidental
cleanup. Cleanup is a separate bounded change with a reviewed inventory.

## Task Navigation

Use this table before reading broad context. Load only the docs relevant to the
task.

| Task shape | Read first |
| --- | --- |
| Task Graph / TaskMailbox (agent use) | `docs/TASK_GRAPH.md` (primary: `claude-hub task`) |
| Task ownership UI / shared Bot pool and pairing | `docs/working-logs/2026-10-07-bot-pool-and-task-ui.md` |
| Architecture / data flow | `ARCHITECTURE.md` |
| Recent shipped behavior | `CHANGELOG.md` |
| Bug symptom history | `WORKLOG.md` |
| Workspace task lifecycle, reports, Goal Packet | `docs/working-logs/2026-05-23-workspace-goal-packet-v1.md` |
| Workspace state / review routing policy | `docs/working-logs/2026-05-23-state-machine-assessment.md` |
| Autonomous mode and evaluator loop | `docs/working-logs/2026-05-26-autonomous-mode-v1.md` |
| Review profiles and reviewer evidence | `docs/working-logs/2026-05-26-review-profiles-v1.md` |
| Auto Mode sub-agent orchestration | `docs/working-logs/2026-06-01-auto-mode-cli-subagent-orchestration.md` |
| Long-running autonomous timing / heartbeat | `docs/working-logs/2026-06-04-auto-mode-observability.md` |
| Agent API error hard-context recovery | `docs/working-logs/2026-07-11-agent-error-hard-recovery.md` |
| Dispatch chain recovery (GP review / continue stalls) | `docs/working-logs/2026-07-15-dispatch-chain-recovery.md` |
| Resident agent: lifecycle, run-now, periodic tasks, next-run | `docs/working-logs/2026-06-25-resident-agent-three-state-lifecycle.md`, `docs/working-logs/2026-07-01-resident-behavior-optimization.md` |
| Feedback harness / lesson retrieval plan | `docs/working-logs/2026-06-06-feedback-harness-plan.md` |
| Active lessons / workspace feedback | `docs/working-logs/lessons-catalog.md` |
| Terminal replay, ttyd, tmux, Playwright terminal debug | `docs/terminal-debugging.md` |
| Deployment | `docs/DEPLOYMENT.md` |
| CLI (`claude-hub`) | `docs/working-logs/2026-06-15-claude-hub-cli.md` |
| CLI workspace/agent reuse lifecycle | `docs/working-logs/2026-08-26-cli-reuse-lifecycle-policy.md` |
| Scheduled tasks (cron / interval / one-off) | `docs/working-logs/2026-09-08-scheduled-tasks.md` |
| Orphan reviewer tabs / tab-session reconciliation | `docs/working-logs/2026-06-19-orphan-reviewer-tab-reconcile.md` |
| Subagent mode / worktree runtime isolation / `/clear` seat check | `docs/working-logs/2026-08-31-subagent-mode-and-session-seat.md` |
| Claude/Cursor/Codex approval cards (AskUserQuestion / AskQuestion / requestUserInput) | `docs/working-logs/2026-09-05-claude-ask-user-question-approval.md`, `docs/working-logs/2026-09-02-chat-composer-ux-and-ask-question.md`, `docs/working-logs/2026-09-06-codex-question-approval.md` |
| Add a new agent type / TraeX (Codex fork) terminal+Chat wiring | `docs/working-logs/2026-09-16-traex-agent.md` |
| Structured Chat Goal mode | `docs/working-logs/2026-09-17-chat-goal-mode.md` |
| Remote tabs / remote workspace agents and reviewers | `docs/working-logs/2026-09-20-remote-agent-pipeline.md` |
| Long Chat turns / stream watchdog timeouts | `docs/working-logs/2026-09-20-chat-long-turn-watchdog.md` |
| Slow Chat history / switching sessions / lazy execution details | `docs/working-logs/2026-10-18-chat-history-lazy-load.md`; `frontend/src/composables/useAgentStream.ts`; `backend/claude_hub/services/agent_stream/store.py`; `frontend/src/features/chat/controllers/useChatTimeline.ts` |
| Scheduled Chat run wedge / stale reaper / backlog supersede | `docs/working-logs/2026-09-22-scheduled-chat-stale-run.md` |

## Common Edit Areas

| Task | Key files |
| --- | --- |
| Add API endpoint | `backend/claude_hub/api/*.py`, `backend/claude_hub/models/schemas.py`, `backend/claude_hub/api/__init__.py` |
| Change terminal rendering | `backend/claude_hub/api/terminal.py`, `backend/claude_hub/services/ttyd_manager.py`, `docs/terminal-debugging.md` |
| Change auth | `backend/claude_hub/auth/dependencies.py`, `backend/claude_hub/auth/session.py`, `backend/claude_hub/api/auth.py`, `backend/claude_hub/config.py` |
| Add frontend component | `frontend/src/components/*.vue`, parent component, `frontend/src/types/index.ts`, relevant store |
| Change layout/pane system | `frontend/src/stores/terminalStore.ts`, `frontend/src/components/LayoutSelector.vue`, `frontend/src/components/TerminalGridView.vue` |
| Change workspace orchestration | `backend/claude_hub/services/workspace_manager/`, `backend/claude_hub/services/workspace_state_policy.py`, `backend/claude_hub/api/workspaces.py`, `frontend/src/stores/workspaceStore.ts` |

## Runtime pitfalls

- Pinia state/computed getters need `storeToRefs()`; actions may be destructured.
- Backend import in `terminal.py` clears proxy environment variables. For loopback
  probes use `curl --noproxy '*'` when a system proxy interferes.
- ttyd WebSockets require subprotocol `tty` at both accept/connect ends.
- httpx decompresses responses; strip `content-encoding` before forwarding.
- Vite WebSocket proxy entries need `ws: true`.
- Parse WebSocket cookies from `websocket.headers["cookie"]`; FastAPI `Cookie`
  injection is unreliable there.
- Keep tmux mouse mode off so xterm text selection works.
- `/clear` must fail closed if the stored session name differs from
  `claude-hub-{tab_id[:8]}`. Never target another seat to recover a task.

## Working logs

Meaningful development adds a focused `docs/working-logs/YYYY-MM-DD-topic.md`
covering the system, changed module responsibilities and pitfalls. Put stable
lessons in the narrowest useful place: a regression test, tool, policy, `REVIEW.md`,
or linked runbook. Root instructions remain a small navigation map.
