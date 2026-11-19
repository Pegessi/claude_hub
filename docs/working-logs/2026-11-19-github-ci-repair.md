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
