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

Final commit SHAs, independent review findings, targeted checks and live smoke
results are recorded below. Mocked transport checks and real provider execution
are identified separately.
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

UI commits `b9d28bd` and `01288b7` passed 602 unit tests, ESLint, TypeScript and
production build in the combined checkout. The full-app mocked API browser
walkthrough covered desktop/mobile, source-tab changes, preserved composer
drafts, reload and controls with no page errors. Evidence is under
`/tmp/claude-hub-chat-work-ui/`. Polling also stops while the app is showing
Workspace mode, even though its Chat pane remains mounted.

Feedback commit `1023e77` adds fresh-evidence automation and exact correction
capture. Its focused checks passed 38 automation/store tests, 17 evidence
tests and five manual lifecycle cases. Input budgets are record/character/read
and frequency limits; they are not billing or output-token caps. See the
[feedback delivery log](2026-10-05-chat-feedback-automation.md).

Final production candidate `7dba387` includes bridge `075b50f`, feedback
`1023e77`, UI `01288b7`, shared policy and the integrated acceptance harness.
An independent native Codex reviewer examined the combined lifecycle and final
incremental changes; no remaining confirmed introduced defect was found in
that scope. Review drove fixes for report/stop races, pause presentation,
cleanup recovery, report terminality and explicit delegated acceptance guidance.

Combined backend acceptance passed 223 tests covering linked work, feedback,
scheduling, report atomicity, abort, mailbox, public API and runtime contracts.
The final shared-policy change passed all 144 native transport, orchestrator
contract and prompt-measurement tests. The mailbox rollback fixture now uses
its actual workspace cwd: the previous `/tmp` value violated existing assignment
validation, independently reproduced on the original integration baseline.
Changed backend files pass Black and isort using `backend/pyproject.toml`;
`AGENTS.md` and `CLAUDE.md` remain identical. Targeted mypy still reports the
existing duplicate `sub_thread` declaration in `codex_jsonl.py:684`; this is
not an all-backend or all-CI-green claim.

The real one-shot Codex smoke on `7dba387` passed creation retry deduplication,
progress reporting, completion reporting, explicit controller acceptance,
caller-owned worker cleanup, cold backend restart, and the built frontend
against the actual API with no browser page errors. The controller accepted
only after checking the requested evidence. This proves the acceptance path;
it does not measure whether a free-form Chat parent independently makes the
right delegation or acceptance decision. The screenshot shows a completed
linked card and result notification. Evidence files are `result.json`,
`work.json`, CLI outputs, server logs and `browser.png` under:

`/var/folders/sg/n3v76wfd73gc06sq76ntb5rr0000gn/T/hub-chat-workflow-live-tkzy_z5t/`

Two earlier harness attempts informed this final run: one incorrectly expected
direct work to bypass acceptance; another imported the installed main CLI after
the worker shell reset `PYTHONPATH`. The harness now preserves acceptance and
explicitly loads the candidate CLI. All test backends and their isolated tmux
servers were stopped. Recurring monitor behavior is covered by deterministic
manager/API tests, not a live recurring schedule. No token-cost or latency
improvement benchmark has been performed.

## User-facing boundaries

- Cards update and notify in the active or reopened Chat. There is no new
  application-wide subscription or push notification for a hidden Chat.
- Chat-linked launch currently supports local source Chats. Remote launches
  are rejected rather than redirected to the local machine. Existing remote
  Hub features are unchanged.
- Inherited environment settings depend on the source tab record still being
  available; a saved preset can be selected for independently configured work.
- Explicit provider/model selection is supported where the runtime honors it.
  No automatic quota discovery or unverified cross-provider failover is added.
- Direct Chat corrections are selected deliberately and matched against a
  bounded recent user-message tail. The backend verifies provenance, not the
  semantic truth of the lesson subsequently generated.

The opt-in `backend/tests/manual_chat_workflow_smoke.py --with-provider` starts
an isolated backend, exercises real Codex through a one-shot linked task, then
checks report history, cleanup, restart recovery and the built Chat UI. It never
targets an existing Hub or creates a recurring user schedule.
