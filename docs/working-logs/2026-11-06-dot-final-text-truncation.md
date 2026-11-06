# 2026-11-06 Dot 最终回复被截断：干净 EOF 把半句前缀认证为完整答复

任务：`095beec9-8cd0-46e1-9de6-196bf7abeb88`（bc761902 重派）
分支：`codex/dot-final-text-truncation`（worktree `~/claude_hub_worktree/dot-final-text-truncation`），base `610bec50`。

## 现象

Dot（Hub Chat，`claude --print --input-format stream-json --output-format stream-json
--include-partial-messages`，端点 `https://any.s6o.org`，模型 `claude-opus-5-5`，
代理 `127.0.0.1:18082`）在多次工具调用后的长 markdown 最终回复，飞书与 journal
只收到开头几个字：

- 12:25（UTC 04:25）截到 `- 虚拟 iPhone 要`（56 字），完整回复约 400+ 字。
- 18:09（UTC 10:09）截到 `**抖音工`（5 字）。

journal（`~/.claude_hub/dot/journal/2026-11-06.md`）与飞书拿到的是同一份残缺文本，
因此问题在 Hub 采集/认证层，不在飞书渲染。

## 根因（原始证据，非猜测）

Hub 持久化流
`~/.claude_hub/workspaces/terminal-tabs/agent_streams/terminal-tab-3e0b2437-….jsonl`：

- 12:25 回合索引 695-697，18:09 回合索引 980-982：最终 `text_delta` 与
  `turn_completed` 同处 EOF 那一毫秒；`turn_completed.status=completed` 但
  **`usage` 为 null**（真实 `result` 必带 usage；当天完整回合均非空），
  `assistant_text` 与当回仅有的 text_delta 拼接逐字相等（56/5 字符），在 token
  中间被切断，前序各有 2 次工具调用。

真实 provider transcript（Dot 以 `CLAUDE_CONFIG_DIR=/Users/Apple/.claude-pai`
运行，会话目录
`~/.claude-pai/projects/-Users-Apple--claude-hub-dot`）证明模型/CLI 已生成完整、
闭合的最终答复：

- 12:25：assistant 记录 idx343，`stop_reason=end_turn`，全长 **583**，时间
  `04:24:59.601Z`。
- 18:09：`msg_011CgmVUz4zFJK2rmrSQN5Fy`，`stop_reason=end_turn`，全长 **373**，
  时间 `10:09:38.488Z`。

两者都比 Hub 的 EOF 合成（04:25:00.602 / 10:09:39.444）早约 1 秒。即：上游在
最终消息经 stdout 传完之前关闭管道（干净退出 exit 0），Hub 只读到前缀。

触发点是已验收任务 602bf59d 引入的「干净 EOF 合成完成」
（`runtime/watchdog.py::_synthesize_clean_eof_completion_locked`）。它的守卫用
`_final_text_after_tool` 表示「工具后必须有答复」，但该标志被**第一条**文本
delta（`native_consumer.py::_note_final_text`）提前清零，于是哪怕只流出 5 个字，
也通过认证，把前缀当成完整答复；飞书 `dispatch_tab_chat_and_wait` 与 journal
`sync_journal` 都消费这份前缀。

证据边界：六个 Dot 会话 transcript 的 `result`/`stream_event` 计数均为 0（SDK
日志不持久化它们），`claude_jsonl.py` 又吞掉 `message_stop`，因此无法从上述 jsonl
推断原始 `message_stop` 有无——该结论改由下列实时原始流实测确立。

## 动代码前的隔离原始流实测（评审 R1 要求）

独立 `HOME=/tmp/dot-trunc-probe/home`、`CLAUDE_CONFIG_DIR=/tmp/dot-trunc-probe/cfg`，
真实端点/模型/代理，不触碰 8173、默认 workspaces 或默认 tmux：

- 健康多工具 + 991 字中文长答复（产物 `healthy_multi.jsonl`）：干净退出前严格按
  `message_start → content_block_start → 207×text_delta → assistant 快照(991)
  → content_block_stop → message_delta → message_stop → result(success, usage,
  result=991)` 结束。证明**健康流在 result 之前必有 message_stop**。
- 在最终消息生成中途 SIGTERM 杀掉 one-shot（`kill_mid.py`，产物
  `killmid.jsonl`）：捕获 8 个最终 text_delta / 41 字半句前缀，最终消息有
  `message_start` 但**没有它自己的 message_stop、也没有 result**（此前工具消息的
  message_stop 存在）。这要求用**逐消息**的未闭合计数，而非全局布尔。

## 修复（三态）

1. 适配器按 `(session, run_epoch, sub_thread)` 维护助手消息生命周期
   （`base.py::_TurnAccumulator.saw_message_scope/open_assistant_messages`）：
   `message_start` +1、`message_stop` -1、`result`/清 turn 清零；只读查询
   `provider_message_lifecycle(ctx)` 供消费者在 EOF 判定。子代理消息计入其
   sub-thread，不影响主线程。
2. `watchdog.py::_synthesize_clean_eof_completion_locked` 三态：
   - 见到消息作用域且仍有未闭合消息 + 工具后已流出最终文本 → **不合成 completed**，
     走 `_clean_eof_final_message_truncated` → 单个有界、可重试失败
     （`_fail_active_turn(retryable=True)`，payload 仅 `status=failed/retryable/
     error_message`，**不带** assistant_text/final_text），前缀绝不外发/落 journal
     （journal 对 failed 只写 `(turn failed)`；dispatch 对非 completed 抛错而非
     返回前缀）。桥接/用户回「重发」恢复。本任务不做自动 kill+relaunch/resume
     （方案 A，工具重放/幂等风险，另立评审任务）。
   - 所有消息均闭合、仅缺 result → 维持合成 completed（保住 602bf59d 健康情形）。
   - 整回无消息作用域（final-only/backfill/synthetic）→ 维持旧的按缓冲文本合成。
   保留 `_final_text_after_tool` 守卫的另一职责：工具运行后、最终答复**尚未开始**
     时，工具前注释不是答案（既有测试 :5982 依赖）。
3. AC4 投递长度兜底：`result.result`（成功记录里的权威全文，原先被丢弃）经私有键
   `_authoritative_result_text` 下传，完成归一化时比较：权威 ≥80 字、待投递 ≤50%、
   绝对差 ≥40 字（可由 `CLAUDE_HUB_FINAL_TEXT_MIN_AUTHORITATIVE_CHARS /
   _MIN_DELIVERY_RATIO / _MIN_GAP_CHARS` 配置）则 `logger.warning` 并写结构化
   `final_text_mismatch={authoritative_len,delivered_len,ratio,source:"result"}`，
   同时改用权威全文（纯前缀/空缓冲替换 assistant_text；含工具前注释则只替换
   final_text）。私有键在持久化/redaction 前弹出。

## 验证

- 新增 6 个离线回归测试（`backend/tests/test_agent_stream.py`，AC6 a-e）：未闭合
  最终消息干净 EOF → 单个 failed/retryable 且无前缀；闭合但缺 result → completed
  全文；无消息作用域 → 仍 completed；权威 result 显著更长 → 替换+marker；阈值以下
  → 不替换；逐消息生命周期计数隔离与 result 清零。
- `tests/test_agent_stream.py` 186 passed（基线 180 + 新增 6）；
  coalescer/native/merge 209 passed；`-k 'dot or journal or feishu or clean_eof or
  final_text'` 860 passed。
- black、isort、mypy（全后端 607 文件）均通过。
- 未做（8173 硬边界，需操作员协调）：服务重启后的真实飞书 ≥300 字、含工具调用
  长回复端到端验证。

## 残留风险 / 超范围

- 上游 relay/18082 代理为何在最终消息传完前关流属基础设施问题，未改。
- 方案 B 失败当回已消耗一次模型生成；以「绝不发半句 + 可重发」为优先；自动续跑
  （方案 A）留待独立评审任务。
- AC4 只能在 Hub 确实收到权威全文（result/最终快照）时补全；最终消息未闭合 EOF
  时 Hub 未收到的文本无法补回，由三态门 fail-closed 处理。
- 实测证据保留在 `/tmp/dot-trunc-probe/work/`（含密钥的临时 env 文件已删除；
  仓库 git 干净）。
