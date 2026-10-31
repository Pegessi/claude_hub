# 后台监控按 scope 隔离与错误状态剪枝（224d0bc 跟进）

## 背景与范围

基线候选 `224d0bc`（fix(workspace): isolate background monitor step failures）已批准，
留两项排队改进。本次只在分支 `codex/rv-monitor-isolation` 完成这两项，不合并、不推送、
不重启共享服务：

1. 会话状态刷新、resident agent tick、scheduled task tick 原来只有“整轮一个超时预算”，
   一个挂死的会话/工作区/定时任务会耗尽预算并跳过本轮后续全部条目。
2. 失败去重状态只有“成功后清除”，持续失败但 scope 实体随后消失（session/workspace/task
   id 轮换）时，rate-limit bucket 会随监控任务长期驻留，缺少上界。

## 改动

### 按 scope 的超时/异常预算（`monitoring/refresh.py`、`_workspaces.py`）

- `_refresh_session_statuses` 被拆为三个可独立守护的阶段，非监控调用方（board、
  dispatch start/queue）行为不变：
  - `_monitor_refresh_sessions(workspace_id)`：本轮覆盖的未归档会话，稳定顺序。
  - `_collect_session_agent_statuses(sessions)`：共享的批量 ttyd 状态 I/O。
  - `_refresh_one_session_status(session, statuses, run_auto_continue=...)`：单会话主体，
    由原循环体逐条搬入（`continue` 变为 `return changed`），返回该会话是否产生落盘变更。
  - `_mark_runtime_task_failures(workspace_id, now)`：原刷新末尾的任务失败扫描，改为返回
    `changed`，保存动作上移到调用方，保存点与原来一致。
- 监控循环中：批量状态获取保留全局 guard（失败时本轮跳过下游，等价于原方法内部抛错）；
  每个会话单独 guard；失败扫描、保存分别 guard。`refresh_changed` 仍按
  `item or changed` 聚合。
- resident tick 按 `_tick_resident_agents` 原样内联（同一个 `now`、相同 due 过滤与参数），
  每个 due 工作区独立预算。
- scheduled tick 按 `_tick_scheduled_tasks` 逐阶段内联：recovery/disable/reap/converge/drain
  保留全局 guard，due 定时任务逐个 guard；`_scheduled_chat_recovery_pending` 仍只在
  recovery 成功返回后清除（guard 拦下失败时保留下轮重试，语义与原异常路径一致）。
- 聚合方法 `_tick_resident_agents` / `_tick_scheduled_tasks` 保留（后者仍被
  `tests/test_scheduled_tasks.py` 直接调用），监控不再走聚合入口。

### 错误日志状态剪枝（`_workspaces.py`）

- 原闭包内的 dict 逻辑提取为 `_StepErrorLogState`：`record_failure` 返回
  `(emit_log, suppressed_count)`，`clear_scope` 在成功后驱逐该 `(step, scope)` 全部失败类型
  bucket，`prune_idle` 按保留期清理。
- 每轮开始执行一次 `prune_idle`，保留期为
  `WORKSPACE_MONITOR_ERROR_LOG_INTERVAL_SECONDS × 8`（默认 480s）。持续失败会每个窗口
  flush 一次从而保持新鲜；失败流停止（实体被删）的 bucket 到期消失，map 规模受“近期仍在
  失败的 scope 数量”约束。
- 首次失败基线改为 `-inf`，避免单调时钟接近 0 的首次失败被误判为窗口内重复。

### 不变量

- `asyncio.CancelledError` 一律继续向上抛出，关闭路径不变；外层 backstop 保留。
- 步骤顺序、参数、`_save_state` 保存点、5s 轮询间隔均不变。
- 行为差异只有：某阶段被 guard 拦下后，本轮后续阶段继续执行（本次目标）；
  resident/scheduled 单条目挂死只消耗自己的 60s 预算。

### 外部驱动点跟进（本轮修正）

- 拆分替换了监控循环的刷新 seam，直接驱动 `_background_monitor_loop` 的
  `tests/test_cleanup_background_lifetime.py` 用的是只实现旧
  `_refresh_session_statuses` 的残缺 fake：新循环首个刷新阶段即抛
  `AttributeError` 并被外层 backstop 每 5s 吞一次（旧 seam 抛的是
  `BaseException`，用于让本轮立刻结束）。已把该 fake 迁移到新 seam：
  `_monitor_refresh_sessions` 返回空、在 `_collect_session_agent_statuses`
  抛出同样的 sentinel `BaseException`，语义与旧测试一致。
- 其余对 `_refresh_session_statuses` 的引用都是调用保留的真实聚合方法，或在未启动
  后台监控的 manager 上打桩（start/dispatch/board 直接路径），不受影响。

## 验证

- `tests/test_workspace_monitor_isolation.py`：12 项通过（新增 per-workspace resident 超时、
  定时任务逐个隔离、成功驱逐 bucket、同步 guard、窗口后 suppressed 计数 flush，以及
  `_StepErrorLogState` 的 4 个单元测试）。
- 关联回归：scheduled_tasks、resident_agent、workspace_sessions、workspaces、task_lifecycle
  等 413 项（9 个文件）通过；补跑 `test_cleanup_background_lifetime.py`、
  `test_agent_stream_store_lifecycle.py` 共 19 项通过。
- `verify.sh backend-format`、`backend-types` 通过（为拆分方法补了 `_base.py` 抽象声明，
  并由 black 规范换行）。
- 默认范围 `verify.sh backend-tests` 全量结果见任务报告。

## 残留风险

- 每会话独立 60s 预算意味着会话很多且大量挂死时，单轮最坏耗时上升；正常路径是批量
  I/O 加少量本地/短等待，不受影响。
- 剪枝只按时间，不按实体是否仍存在；保留期内被删 scope 的 bucket 仍占内存到窗口过期，
  这是有意保留的（避免抖动实体的失败日志反复刷全量 traceback）。
