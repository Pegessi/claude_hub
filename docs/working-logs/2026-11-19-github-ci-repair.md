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

## 失败诊断补齐

702661b0 hosted attempt1 仅 HTML 外链 popup30s失败，attempt2该场景通过但 composer移动端Local按钮定位30s失败。两次失败均保留，不继续重跑。同源码本地完整 image/composer 加只读输入探针通过，尚未证明生产缺陷，不凭猜测改变安全策略、UI或期限。

连续Playwright trace在本地使原本通过的inbox超过120秒，实测不合适，未推送。改用轻量输入ring：各frame仅记录最近60次pointerdown/up/click的目标tag/id/class/button或link短标签、坐标、modal前后数量与viewport，不采集输入value。成功不截图或读取DOM；普通失败仅在总计2秒内采集最多2page×4frame的ring/modal按钮几何与一张截图，写现有runner artifact目录。诊断异常不替代原结果；signal/cancel优先原清理路径。Goal额外child context尚未注入该ring，报告明确覆盖边界。测试覆盖成功/失败与诊断异常/超时，以及record/capture中取消仍完成context/browser/driver清理；真实Chromium probe证明ring上限60、目标记录、截图与原始异常均保留。所有原门禁与断言保留。
