# Chat workflow integration: recovery boundaries

> Historical candidate only. The unpublished ChatWork API, UI, compatibility
> controls, and dedicated smoke harness were removed in the
> [2026-10-08 cleanup](2026-10-08-remove-chatwork.md). Commands and validation
> results below describe that earlier revision, not current functionality.

## Scope

This follow-up applies to the workflow candidate after integration with main
`c37b7f3`. It preserves existing Task, report, and ScheduledTask state models.
There is no new scheduler and no automatic replay of uncertain linked work.

## Confirmed failures and fixes

- One-shot Chat-linked tasks are ordinary reviewable tasks, not internal scheduled
  tasks. Both orphan-scan predicates previously omitted them. Include linked
  tasks in both predicates, retain the grace period, and mark a dead-worker
  execution failed with an explicit recovery request instead of redispatching it.
  Cleanup still checks the recorded owned session/tab identity and must not delete
  a shared or reassigned worker.
- Schedule updates replace stored objects. A fire call waiting behind another
  operation must reload the record after acquiring its lock; otherwise an older
  enabled object can launch work after Pause, Stop, or deletion.
- Reconciliation keeps a snapshot while cleanup awaits I/O. A later schedule or
  task can be deleted during that await. Reload each record before use, skip
  deleted records, and do not recreate a deleted TODO task from the old snapshot.
  Stop ignores only the exact missing-task KeyError after interruption, not
  unrelated failures.

The deletion explanation relies on actual suspension during cleanup/interruption,
not an assumed suspension between an unlocked lock check and acquisition.

## Validation

In `/home/tiger/claude_hub_worktree/chat-workflow-main-sync`, with existing real
Python dependencies and task-owned HOME/XDG/state/tmux settings:

- Before the production fix, all 19 new parameterized regressions failed on
  `3173f7d`. Failures included tasks remaining WORKING, extra dispatch after
  Pause/Stop/deletion, missing-record KeyErrors, and a deleted TODO reappearing.
- After the fix, `test_chat_work_recovery_boundaries.py`, `test_chat_work.py`, and
  `test_scheduled_tasks.py` passed: **122 tests**.
- Complete backend mypy passed: **118 source files**. Black/isort and diff checks
  also passed for the changed files.

Evidence: `/tmp/claude-hub-takeover-tests.KvXCMa/workflow-lifecycle-before/` and
`workflow-lifecycle-after/`. These are deterministic tests with mocked execution,
not real provider acceptance. The live smoke harness still needs safety work;
the production service and external Bot resources were not touched.

## Feedback evidence from production events

Independent review found that `redacted=True` means an event passed through the
redactor, even when its content was unchanged. Filtering that flag excluded all
normal persisted turns. Feedback now uses the persisted safe summary directly;
its hash and accepted quote never refer to the original secret text.

Three regressions use the actual `AgentStreamEvent -> redact_event ->
AgentStreamStore` path for legacy, Web, and Feishu origins. All three reproduced
empty source results before the fix. Afterward, 39 feedback tests and scoped mypy
passed; quoting a synthetic pre-redaction secret is rejected and the correction
file contains no such value. Evidence is in `feedback-redaction-before/` and
`feedback-redaction-after/` under the same task artifact root.

## Combined feature checkpoint

`6a1f693` combines the workflow fixes with Bot candidate `3ab6204`; the only text
conflicts were API router imports and CHANGELOG, and both sides were preserved.
Upstream main was independently rechecked at `c37b7f3`. Follow-up `af29874` retains
rebinding advice for a genuinely deleted binding target without confusing it
with a transient transport failure.

- Combined frontend: 629 unit tests, ESLint, TypeScript, and production build
  passed. Both fully mocked browser walkthroughs passed at desktop/mobile sizes.
  The workflow script now accepts an existing browser executable and blocks
  cross-origin requests, WebSockets, and service workers before navigation.
- Combined backend excluding `test_workspaces.py`: 612 tests passed across 20
  named suites. Full backend mypy passed for 122 source files.
- The broader first attempt was deliberately interrupted after one stale
  reviewer-prompt assertion and 342 passes. Fake terminal bootstrap also waited
  25 seconds per invocation. Neither result is presented as full-suite success;
  the workspace fixture and semantic assertion still need follow-up.
- Static review server port 33301 was released and its PID absent after testing.

Evidence directories: `joint-frontend`, `joint-browser`, `joint-backend`,
`joint-backend-scoped`, and `joint-binding-reason` under the artifact root above.
Browser results do not establish real Bot/provider execution. No main merge,
push, production restart, or external Bot changes were performed.

## Private process-cleanup guard

`backend/tests/smoke_runtime_guard.py` is only for a dedicated Linux smoke
controller. It is not part of the Hub server. The controller must stop creating
children and close third-party runtimes before final cleanup. The guarantee
covers ordinary fork/exec descendants while the controller remains alive; it
does not cover forcibly killing that controller with SIGKILL.

The helper checks actual pidfd support and subreaper state. It inspects direct
children across controller threads, then rechecks current parent PID and start
time after opening each pidfd. It never authorizes a signal from a multi-level
PPID snapshot. TERM and KILL have separate finite budgets. Read, signal, identity,
and budget failures remain recorded even if later observations are empty.
Known Popen objects collect their own exit statuses before adopted children are
reaped; success also requires ECHILD and a complete empty direct-child scan.

Validation:

- 27 pure fault-injection tests passed without real OS signals, descriptors,
  process creation, or /proc reads. The helper passed mypy.
- The process test is skipped by default. After static review, explicit opt-in
  passed on this host using only bounded Python test processes. It verified
  double-fork/setsid cleanup, the SIGKILL escalation, adopted-daemon reaping,
  retained starter exit status, readable pidfd completion, and an unaffected
  sibling. The starter is explicitly waited before cleanup; live-Popen polling
  transitions are covered by the pure mock test, not this process test.
- Test daemons have an independent lifetime bound and parent-death protection;
  the parent establishes a verified pidfd before permitting cleanup.

Evidence: `process-guard-mock/` and `process-guard-owned-processes/` under the same
artifact root. These results do not authorize or validate any provider run.

## Workspace regression fixtures

The workspace fixtures now isolate `settings.port` from the parent environment
and represent already-ready fake terminals without bypassing the product readiness
check. Complexity and lesson assertions follow the integrated execution policy.
The complete workspace/subagent suites and two dedicated readiness cases passed:
158 tests, with Black/isort checks passing. Evidence is in
`workspace-final-port-isolation/`; the test-only change is commit `446f035`.

## Manual smoke isolation candidate

The guard is now wired into `backend/tests/manual_chat_workflow_smoke.py`.
The manual scenario still requires a separate, explicit authorization before
using a real provider account. Run the controller with `python -B`; required
arguments are:

- `--with-provider`
- `--runtime-root` and `--artifact-dir`: existing parents for newly created,
  private, task-owned directories.
- `--codex-auth-file`: an explicitly selected owner-only regular file.
- `--browser-executable`: an existing Chromium executable; no browser download
  or installation is performed.
- `--codex-model`: an actual Codex model ID, not an assumed orchestration alias.
- `--network-mode auto|direct|proxy`: `auto` and `proxy` also require explicit
  `--inherit-proxy-env`; `direct` does not inherit proxy variables.

Source Chat and worker execution keep the canonical feature-worktree cwd.
Backend and report CLI processes use the private runtime cwd, with `PYTHONPATH`
pinning imports to the candidate backend. Isolated `-I` helper processes disable
bytecode writes explicitly. The parent retains its prebound loopback socket
across the backend restart and closes it during final cleanup.

An authorized run exercises the real work API, reports, caller acceptance,
owned-worker cleanup, and cold recovery. Browser inspection mocks the source
Chat stream and Feishu binding; startup and persisted-work APIs remain real. This
is not a fully real Chat-stream or Bot acceptance test.

Raw runtime logs and command outputs remain private, are never auto-uploaded,
and are not certified sanitized. Failed runs retain their runtime. Cleanup makes
best-effort removal of known sensitive copies, but only certifies that removal
when writer shutdown and path absence have both been verified. Controller
SIGKILL remains outside the cleanup guarantee.

Current deterministic validation:

- 42 harness boundary tests, 9 signal/cleanup regressions, and the existing 27
  pure guard tests passed together (78 tests). The signal tests call a mocked
  handler directly; no OS signal is sent. No provider, browser, real child-process
  creation, or live proc inspection was performed by these tests.
- Manual harness mypy passed; all three harness/test files passed Black/isort.
  After adding an explicit AST node-type assertion, the 9 signal cases passed
  again.
- The standalone `--help` entrypoint parsed successfully without entering the
  scenario.

Independent review identified a cleanup-time interruption gap. The follow-up
installs one handler before runtime allocation, briefly masking SIGINT/SIGTERM
until both handlers are ready. The first interrupt enables deferral before
raising; later signals update only two fixed boolean fields. Normal completion,
exception handling, and final cleanup enter deferral before teardown; result
output remains inside the finalization block. Cleanup health uses only new
cleanup errors, while overall acceptance still requires no scenario errors.

An interruption during early directory allocation may leave an empty private
directory. At that point no provider credential has been copied and no child has
started. This residual-directory case does not involve a running provider or a
retained credential copy. Controller SIGKILL remains outside the guarantee.

Evidence: `manual-harness-typed/`, `manual-harness-signals/`, and
`manual-harness-signals-final/` under the same artifact root. Independent static
follow-up review found the reported interruption gap fixed, with no remaining
blocker in the reviewed changes. This review did not run the scenario or send OS
signals. A fresh fetch confirmed that the candidate contains `origin/main` at
`c37b7f37645a0159e8ee721e5dd11f97659a2238`.

**No real-provider run has been performed for this integrated candidate. A
separate authorized run is still required; real OAuth/Bot round trips need their
own independent test authorization.**
