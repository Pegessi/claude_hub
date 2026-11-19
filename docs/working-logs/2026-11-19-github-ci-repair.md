# GitHub CI repair (2026-11-19)

## Failure and scope

The main CI run 44522377715 failed the backend recovery regression because a
runner without Cursor CLI did not advertise Plan mode. The fake ACP process
supports it, but the test accidentally consulted host CLI help. A narrowly scoped
fixture supplies that capability only for this transport regression; every
recovery, model, child-thread and cleanup assertion remains unchanged. The fixture
lives in conftest because the existing test module exceeds the source-size budget.

All three browser jobs also failed installing locked Playwright 1.58 on the
Ubuntu 26.04 runner selected by ubuntu-latest. Pin just those jobs to supported
Ubuntu 24.04; retain every install, test, timeout, assertion and required job.

## Reproduction and validation

The isolated clone reproduces the Cursor failure with a PATH containing declared
tools but no agent. The same focused verify target must pass after the fixture.
Playwright with PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu26.04-x64 rejects browser
installation before downloads. This override is a reproduction tool only and is
not applied to CI or verification. Final checks include verify.sh all, the exact
three browser job targets, all-history private-pattern audit and the hosted CI
run. Full result receipts and final SHAs are delivered in the external task report.

The reviewed Feishu fallback commit is integrated without real chat sends. No
live service restart or primary checkout history rewrite is part of this repair.

## Hosted follow-up findings

The first hosted candidate reached a different 0.2-second stream delivery check
and timed out. The same test passed unchanged locally, and one diagnostic rerun
passed it remotely; no deadline, durability or assertion was changed. That rerun
then exposed another unmocked host capability: the Patrol fallback regression
assumed Codex was installed. Model availability only for that mocked case,
leaving provider-absence tests untouched; reproduce with no provider CLIs on PATH.

The mocked Goal UI runner also exhausted its unchanged 120-second scenario
budget after the wide-theme matrix. Run its independent behavior and appearance
phases concurrently in isolated contexts with separate localStorage/mock state.
The extra context is an explicitly closed child of the same BrowserRun-owned
browser; the lifecycle owner's single managed context/browser/driver and failure
handling remain unchanged. TaskGroup propagates either failure and cancellation;
focused tests cover both phase results and context cleanup on success/failure/
cancellation. Retain every original assertion, awaited oracle and screenshot,
plus the same scenario and suite deadlines. No lifecycle/verifier gate is relaxed.
