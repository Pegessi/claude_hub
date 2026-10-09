# Quality and maintainability program

## Delivery boundary

The user authorized development of three reviewable stages, followed by separate
merge decisions. None of these stages authorizes a main merge, push, production
restart, real Provider/Bot/OAuth test, or changes to existing user state.

Stage 1 starts from upstream `ed82d107757652facae5b6427bbcc7a1cf932d4b` in
`~/claude_hub_worktree/quality-stage1-foundation` (`feat/quality-stage1`). This
includes the independent Feishu UI polish and restart dependency-sync fixes.
The primary checkout and its local IPv6 configuration remain untouched.

## Stage outcomes

| Stage | Scope | Acceptance |
| --- | --- | --- |
| 1 — trustworthy verification | Shared local/CI commands and tool versions; independent check results; full typing fixes; rejected Task edits preserve files; contradictory instructions corrected | Clean-checkout checks are reproducible, none is silently bypassed, and failed edits preserve authoritative data |
| 2 — user-facing acceptance | Reuse existing component tests and isolated browser fixtures; current UI style/terminology/accessibility rules; backend/frontend contract checks | Representative bad changes fail the right check; browser fixtures are explicitly mocked rather than reported as live integration |
| 3 — bounded structural improvements | Explicit workspace commit boundary and Mailbox state ownership; extract independent frontend flows; update current architecture and lifecycle guidance | Existing behavior tests remain green; a follow-up change can be implemented without understanding unrelated flows |

Stages 2 and 3 will use separate branches based on their validated prerequisites.
Implementation and review evidence will distinguish each stage's own changes from
inherited work. Stable interfaces and behavioral tests precede structural changes.

## Stage 1 investigation

At the base revision, full `mypy .` reproduces 1,430 diagnostics. Most missing
constructor arguments come from Pydantic field defaults not understood by the
plain dataclass transform. The official Pydantic plugin with `init_typed` and
`init_forbid_extra` enabled reduces the set to 435 without excluding tests or
weakening constructor checks. Remaining fixture/import/type errors were
corrected by owned file groups, with behavior assertions preserved.

The base CI runs Node 20 while several tests import TypeScript directly. Local
Node 24.21.0 runs those tests. Tool declarations and a shared verification entry
remove this difference. Type checks and pytest report independently.

The attachment bug is a real failure-ordering defect: an invalid Task edit can
unlink the old file before rejecting the request. Its repair must also preserve
old/new files correctly on pre-commit failure, post-commit failure, and ambiguous
storage outcomes. Existing operation reservations and lock order remain required.

## Stage 1 implementation

- `scripts/verify.sh` is shared by local checks and CI. Backend format/types/tests
  and frontend lint/types/tests/build report independently. Pinned declarations:
  Python 3.11.16, Node 24.21.0, uv 0.12.15 and pnpm 9.15.9; the Python build
  backend is hatchling 1.32.4. Installation remains explicit and lockfile-based.
  `.python-test-version` and `.node-test-version` avoid tool auto-discovery:
  verification must not change production restart interpreter selection or a
  directory-triggered Node version switch.
- Full `mypy .` includes tests and the imported Task Graph helpers. The Pydantic
  plugin understands model defaults while retaining typed constructors and
  forbidden extra fields. Existing unannotated-function settings are unchanged;
  checking every file does not mean every unannotated body has type coverage.
- Task edits prepare and validate inputs before writing. Records, descendant
  updates and session/report mappings share the workspace commit. Confirmed
  pre-commit failures restore memory and remove only newly owned files; confirmed
  commits run external actions and old-file cleanup afterward. Unknown outcomes
  retain both file sets. A readback failure cannot replace a cancellation or
  interrupt with an ordinary I/O error.
- New entry-point tests execute the real Bash script against recording fake tools
  and a synthetic environment, never the caller's credentials. Task transaction
  tests exercise the real API function and storage failure paths.
- Current contribution, cleanup and native Chat guidance now matches the code.
  `AGENTS.md` and `CLAUDE.md` remain identical. History is not the current contract.

### Environment preparation

Tools and dependency downloads went directly to a task-owned HDFS directory.
The mount rejected uv's editable distribution-cache write with `EBUSY`; that
attempt stopped. Downloads remained there. Only pure-code cache copies and build
outputs used a task-owned local directory, and the final editable build ran
**offline** after its build dependencies had been cached. No model weights or
production runtime were involved. Failed setup logs were retained.

### Verification

Executed through the shared entry point with the declared tools:

| Check | Result |
| --- | --- |
| Backend Black/isort | Pass |
| Full-directory mypy | Pass, 231 files |
| Default backend pytest scope | 2,704 pass, 7 skip, 1 rerun; 1,413.62 seconds |
| Final terminal-manager module | 185 pass after the final test-only edits |
| Frontend Node tests | 700 pass |
| Frontend lint / typecheck / build | Pass; existing bundle-size warning remains |
| Guide equality / documentation checks | Pass |

These are local results, not a new GitHub Actions run. Evidence root:
`/tmp/claude-hub-quality-stage1.gRf9C0/`. The complete post-product-repair run is
`unified-all-release/checks.log`; backend evidence is
`/tmp/claude-hub-verify.YsfZe6` (pytest exit 0). Its aggregate exit was 1 because
mypy caught two incompatible task-set variable reuses in the new toy test. The
subsequent edits changed only that test's variable names and readiness condition,
not product code. Full-directory mypy and the complete 185-case terminal module
were rerun successfully (`release-types-final`, `release-terminal-final`). No
successful aggregate exit is invented for the earlier invocation.

The seven skips were the dirty-tree Git provenance check, one optional sidebar
browser case and five optional Goal browser cases. The existing marked
`test_real_cold_restart_7tab_bijection` used one rerun; this was not a first-attempt
pass. The inherited default scope excludes replay/performance suites. The
separate browser signal and fixed-control-language requirements belong to stage
2. Mocked agents do not prove real Provider, Feishu Bot or OAuth compatibility.

Independent static review covered the attachment transaction, per-tab lifecycle
locking and its caller/observer ordering, selected typing fixes, verification
entry point and CI. Execution results are recorded separately. There is no claim
of exhaustive review of every pre-existing test or universal deadlock freedom.

### Test synchronization and resource ownership

An earlier full run ended with 1,579 passes, one failure and one skip after a
controlled interruption. The queue fake considered a replacement process's
existence proof that initialization and thread resume had finished. Recovery
could outlive a failed assertion and its monkeypatch. Tests now wait for the
actual recovery completion, close owned asynchronous work before patch teardown,
and permanently use nonexistent private fake command paths. Unexpected commands
remain failures even when a caller catches the immediate exception. Six focused
regressions and all 33 shared wedge tests passed; these overlapping counts are
not added to the full-suite count.

The shared entry point also accepts explicit focused runs:
`./scripts/verify.sh backend-tests -- tests/test_verify_entrypoint.py -q`.
It records full/focused scope and each pytest argument, clears inherited pytest
selection/plugin overrides, and keeps raw coverage data in its private evidence
directory. Focused results do not refresh or validate an older coverage XML file.
All 15 entry-point tests passed. CI and `all` retain the default full scope.

After the complete run, 13 ttyd processes from the isolated HMR browser fixtures
remained. Their private HOME, tmux socket and process identities tied them to that
run. Each was reclaimed through a pidfd after checking its recorded start time
and exact HOME; no production process was targeted. Inventory and cleanup records
are in `unified-all-final/process-inventory.json` and `process-cleanup.json`.

The logs show successful backend lifespan cleanup but also a terminal listener
starting after DELETE had removed its tab record. Source review confirms a
pre-existing delete/recovery race: `delete_tab` does not take the per-tab start
lock, while `ensure_tab_running` captures its process before acquiring that lock.
A late proxy request can therefore restart an unregistered process. The repair
uses the existing per-tab lock for seven lifecycle entry points and re-reads the
current owner after acquiring it. It introduces no deletion flag, second lock
registry or process-sweeping framework. Failed or cancelled teardown retains the
registered owner. Archived configuration updates do not restart the terminal;
existing environment/tunnel preparation is unchanged.

Ten deterministic lifecycle cases produced seven failures and three passes on
the old product, then ten passes after the repair. The real isolated HMR suite
also passed all 12 cases in 184.09 seconds. A subsequent process inventory found
no remaining ttyd with the candidate backend cwd, unlike the 13 before repair.
These browser tests use private Hub/Vite/tmux instances and fake agent commands,
not real Provider execution. Evidence: `terminal-lifecycle-before-all`,
`terminal-lifecycle-after`, and `terminal-hmr-lifecycle-after` under the evidence
root above. Full-directory mypy remains green.

Archive holds the tab lock through native stream shutdown to serialize restore.
The lock review depends on the current mapping: Workspace-managed sessions are
Terminal tabs; ordinary native Chat tabs are not those managed session records.
Terminal tailer shutdown cancels its polling task and flushes text, without the
native terminal-edge activity callback. Native Chat activity can await a
workspace lock, but managed Task renaming targets a different tab. Supporting
managed Chat later requires revisiting this lock ordering. Existing ttyd kill
and tmux waits are not all bounded; this repair makes no new timeout guarantee.

The expanded terminal module run also exposed an independent test timing issue:
a Python toy launcher was given only one second to become ready, and failure
skipped its normal cleanup. It now uses an atomic ready marker and a release
handshake, exact single-launch command ownership, and bounded task/process cleanup
that preserves the primary failure. No production stop timeout was changed to
accommodate the test. Final module and typing checks passed after the test-only
corrections noted above. Main integration and deployment remain unauthorized.
