# Chat-owned durable work and bounded feedback

## Outcome and baseline

Chat remains the user's main interface. Small work stays in the current agent;
useful short collaboration uses available native subagents. Independently
continuing work has a durable link to the originating Chat. Hub delegation is
available when a different provider, configured model, environment or independent
quota is useful. Task persistence and executor selection are separate decisions.

The implementation starts at `codex/agent-os-integration` / `87df43d`, combining
the four existing context, tasks, verification and hygiene branches. Before
creating the new worktrees, a fresh remote main read returned `683e1f5`, equal to
local main and an ancestor of this integration. The original candidates remain
unchanged. This work does not merge main or deploy the shared service.

## Ownership and integration

Native Codex subagents implement independent scopes in dedicated worktrees under
`~/claude_hub_worktree`:

| Branch | Scope |
| --- | --- |
| `codex/chat-workflow-bridge` | Existing scheduler/Task linkage, API/CLI, execution and result lifecycle |
| `codex/chat-workflow-feedback` | Automatic bounded feedback, exact Chat correction evidence, relevant retrieval |
| `codex/chat-workflow-ui` | Chat work cards, state/result presentation and controls |
| `codex/chat-workflow-integration` | Shared execution guidance, integration, cross-scope validation and delivery |

One writer owns each surface. The bridge owner coordinates shared schema/router/
CLI registration and the scheduler's feedback tick hook. Feedback implementation
stays in its existing service/mixin plus focused boundary modules. The controller
owns native runtime guidance and shared task guidance. No Hub-managed agents are
created to implement this change.

## Behavior contracts

- Reuse `ScheduledTask` and `WorkspaceTask`; do not introduce a second scheduler
  or an alternative task lifecycle. A stable work card groups monitor executions.
- The source Chat and creation request key identify retried requests. Persist
  execution ownership before asynchronous launch. Re-read state after awaits;
  prevent overlapping monitor checks and preserve stop/pause decisions.
- Preserve actual provider/cwd/model/environment selection at launch. Reject
  unsupported settings instead of silently discarding them. Do not infer quotas.
- Waiting monitors do not hold a model session. Routine unchanged results stay
  out of the Chat transcript and provider context. Results and evidence remain
  inspectable from the linked work card and CLI.
- Ordinary reviewed tasks retain their acceptance requirements. Monitor outcome
  reports do not grant arbitrary workers authority over unrelated tasks.
- Feedback processes fresh bounded evidence only, coalesces work, excludes its
  own internal runs, and skips model work when there is nothing new. Explicit
  Chat corrections cite verified original user messages. Unrelated lessons are
  not injected as a fallback.
- Automation controls and lifecycle outcomes must be visible and truthful.
  Pause/stop language distinguishes future checks from already-running work.

## Acceptance evidence

Record final commit SHAs, independent review findings, targeted test commands,
isolated HTTP/CLI persistence checks, and browser scenarios here after integration.
Mocked transport checks and real provider execution must be identified separately.
All servers, tmux sockets and runtime homes used in validation are task-owned;
the main Hub on ports 5173/8173 is not restarted or mutated.

Initial shared-policy validation: all 114 native transport tests and 30
orchestrator/prompt measurement tests passed. A real installed Codex app-server
using `CodexNativeSession` from this checkout completed a tool-free turn with
the expected `HUB_WORKFLOW_SMOKE_OK` response; the process was stopped in
`finally`. It used an isolated runtime home/socket and a non-service Hub URL.
This verifies the actual provider transport with the new policy, not the full
work dispatch/report/cleanup path. The ephemeral evidence file is
`/var/folders/sg/n3v76wfd73gc06sq76ntb5rr0000gn/T/hub-chat-workflow-native-kloyw6bo/result.json`.
