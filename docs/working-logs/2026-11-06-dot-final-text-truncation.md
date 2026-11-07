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

## 实现评审 cycle-4 修复（@9ca91f85 review_failed）

- **B1（阻塞，并发）**：retryable fail 路径原本在 `_send_lock` 外调用
  `_fail_active_turn`，随后的 `_active_turn_id=None` / `acknowledge_turn_complete`
  也无锁；并发手动 Stop / `stop()` teardown（`_cancel_active_turn_locked`，仅按
  `turn_in_flight/active_turn_id` 判定、不复查 `_turn_completed_seen`）会在两次
  await `_publish` 之间插入，给同一 turn 持久化第二个终态（failed+cancelled）。
  修复：把 retryable 与「通用缺 result 失败」（N1，同样无锁，既有缺陷）的
  `_fail_active_turn` + id/guard 释放合并为 `_send_lock` 内单原子段，并在锁内复查
  `_active_turn_id is not None`；通知在锁外。新增确定性测试
  `test_truncated_clean_eof_concurrent_cancel_persists_one_terminal`：在 retryable
  终态 publish 处注入并发 `cancel_turn()`，断言恰好 1 个 turn_completed；对旧提交
  9ca91f85 运行该测试确证得到 2 个终态（失败），修复后为 1 个（通过）。
- **B2（CHANGELOG）**：恢复误删的标题
  `### feat(chat): replace the bottom sub-agent list with an Agents dock`，其 4 条
  不再悬挂。`verify.sh docs` 通过。
- **N2（顺手修）**：无 `result` 的终态（干净 EOF 合成 completed、failed/retryable
  EOF）适配器每 turn 累加器原本不清（result 才清）。新增
  `AgentStreamAdapter.clear_turn_state(ctx)` 公共方法，在合成完成 helper 与 EOF
  失败锁段（turn 已终态后）调用；测试断言下一个 turn 的 lifecycle 为 `(False,0)`。
- 非阻塞 N3（工具前注释 + 截断的混合回合 journal 留注释+前缀、飞书得全文）与 N4
  （任何 delta 之前被切 → 普通不可重试 failed，本就无前缀）经评审确认可接受，保持。

## Codex 交叉验证 P1 修复（用户拍板 A，cycle-5）

Codex 任务 2cf145bd（卡片 w-result-dot-26052392-main-8eb5fa）发现 AC4 兜底比错对象：
回合流出 520 字工具前说明 + 工具 + 仅 4 字最终回复（`**结论`）并正常闭合，`result`
带 175 字权威答复。旧逻辑用整段 `assistant_text`（524 字）与权威 175 字比，差值为负
→ 跳过替换/告警，于是 4 字回复仍以 completed 外发。

- 改为比较**实际投递的最终答复**：有 post-tool `final_text` 时以其长度为准，否则用
  `assistant_text`；阈值仍 80/0.5/40。命中时：有 commentary 则 `final_text` 置权威、
  且 `assistant_text` 重建为 `commentary + authoritative`（journal 也得到修正、不丢
  说明）；无 commentary 则直接整体替换。`final_text_mismatch.delivered_len` 记实际
  投递长度。
- 新增确定性回归 `test_result_backstop_compares_delivered_final_not_whole_with_commentary`
  （520 commentary + 4 字 final vs 175 权威，断言 final_text/assistant_text 均为权威、
  commentary 保留）；对 26052392 运行确证 `final_text=='**结论'`（失败），修复后通过。
- 复跑（单条前台阻塞命令）：test_agent_stream.py 188 passed；coalescer/native/
  merge_contract 209 passed；mypy/black/isort 通过。

## Codex 第二轮交叉验证 P1 修复（cycle-6）

卡片 w-result-dot-26052392 第二轮：工具后最终答复 **0 个增量且无快照**时，
`_final_text_after_tool=True` 却未清旧的 `_final_assistant_text`（仅在首段文本到来时
于 `_note_final_text` 清），于是完成块把工具前 260 字说明当成 `final_text`；兜底用
260 对权威 175 得负差而提前返回，Dot 实际投递 260 字旧说明且标 completed。

- TOOL_CALL_STARTED 处即把 `_final_assistant_text` 清空并保持
  `_final_text_after_tool=True`（边界从"有工具"推进到"工具后是否已有文本"，避免
  陈旧说明被当作答案）。
- `_apply_authoritative_result_text(event, post_tool_final_absent=...)`：工具后无最终
  文本时**忽略长度阈值**，直接以成功的权威 result 作为最终答复：`final_text=权威`，
  且 `assistant_text = 工具前说明 + 权威`（保留说明、修正 journal）；marker 增
  `post_tool_final_absent=true, delivered_len=0`。其余情形仍走 80/0.5/40 阈值。
- 新增零增量回归 `test_no_post_tool_final_uses_authoritative_result_keeping_commentary`
  （260 说明 + 工具 + 0 最终增量 + 175 result → final_text=175、assistant_text=
  260+175）；对 91efbaf8 运行确证失败，修复后通过。
- 复跑（单条前台阻塞）：test_agent_stream.py 189 passed；coalescer/native/
  merge_contract 209；mypy（全后端，仅既有 check_verify_runtime 3.11 报错）、black、
  isort 通过。说明：test_native_goal_transport.py 两个 stop-handoff 用例在 pristine
  base 610bec50 上同样失败（已知时序 flake，非本次回归）。

## Codex 第三轮交叉验证——按『类』统一修复（cycle-7）

同类问题第三次：工具后只流出**纯空白**（空格/换行/tab/NBSP/窄 NBSP，含分块）也会
清掉 `_final_text_after_tool`；strip 后最终文本为空却仍传 `post_tool_final_absent=False`，
兜底用整段 560 字说明与权威 175 比、负差跳过，外发 560 字旧说明且 completed。

按类统一（不调阈值）：
- 唯一判定 `_has_substantive_post_tool_final()` = `_final_text_after_tool and
  _final_assistant_text.strip()`（`str.strip()` 覆盖 ASCII + Unicode 空白/NBSP）。
- `_note_final_text` 改为只追加；缓冲在每次 TOOL_CALL_STARTED 清空，标志位语义固定为
  「本回合运行过工具、最终答案起算点」，不再被（含空白的）首段 delta 清掉。多工具时
  每个工具后重置，避免误把中间工具的文本当最终答复。
- 完成归一化只用这一个谓词计算 `post_tool_final_absent`（无增量 / 纯空白 / 分块空白 /
  空白后真答复 共用）；无实质答复且成功 result 在场 → 以权威 result 为 final_text、
  `assistant_text = 前置说明 + 权威`；有实质但过短 → 仍走 80/0.5/40 阈值。
- 干净 EOF 截断检测：最终消息「未闭合」即生成未完成（无论 0/空白/部分前缀），
  有实质前缀→可重试 failed；0/空白的未闭合消息→普通 failed（同样不外发）。
- 对抗自查回归（11 个相关用例，参数化）：无增量；单空白；分块空白（空格/换行/tab/
  NBSP/窄 NBSP）；空白后 4 字真答复（走阈值，175 对照）；多工具；未闭合+仅空白 EOF→
  retryable 且无 assistant/final；完整实质答复→不替换、无 marker；失败 result +
  工具后空白→failed、不采用错误文本。包含 Codex 三轮全部反例（4 字/524 字、0 增量/
  260 字、纯空白/560 字）。
- 验证（单条前台阻塞）：test_agent_stream.py 197 passed（+8）；coalescer/native/
  merge_contract 209；相关宽集（剔除 2 个 base 上既有的 goal stop-handoff 时序 flake）
  2248 passed、0 failed；mypy/black/isort 干净。

## Codex 第七轮——整个『不可见答案』类别收口（cycle-8）

第四例：工具后仅零宽/格式字符（U+200B、U+FEFF 等 Unicode Cf，非 White_Space，
`str.strip()` 不去）也被当实质答案；干净 EOF（已 message_stop 缺 result）同样只看
strip，于是合成 completed 外发 1 个不可见字符。

按类一次性收口（阈值 80/0.5/40 不变）：
- 新增唯一共享谓词 `agent_stream/base.py::is_substantive_answer(text)`：逐字符跳过
  `isspace()` 与 Unicode 类别 Cc/Cf/Zs/Zl/Zp，仍有任意字符才算实质；仅判存在、不改文
  本，故正文中间的零宽字符保留。另加 `trim_invisible_edges()` 仅用于拼接说明+权威答案
  时去掉边界空白/不可见。
- 全部答案存在判定只走这一个函数（grep 复核无残留裸 strip）：
  * consumer `_has_substantive_post_tool_final`、完成块 final_text 仅在实质时设置、
    `post_tool_final_absent`（无工具时缓冲即整答，统一覆盖）；
  * watchdog 干净 EOF 合成守卫、合成 final_text、权威兜底的权威/投递存在判定；
  * dispatch `dispatch_tab_chat_and_wait` 的 assistant_text/final_text 选择；
  * dot journal `sync_journal` 是否记录 dot 答复。
- 无实质（无/空白/零宽/BOM）+ 成功权威 result → 以权威为 final_text，
  assistant_text = trim(说明) + 权威（短 result 同样采用，绕过长度门槛）；已闭合缺
  result 的不可见答复 → 不合成 completed；失败 result 永不采用；正文内嵌零宽的正常
  长答复照常完成、逐字投递、无 marker。
- 并入 Codex round7 三个失败用例为正式测试（参数化 zwsp/bom×短长 result、format-only
  干净 EOF）+ 谓词单测 + 内嵌 ZWSP 正常长答对照。旧提交 74904e61 上恰为 4 个失败
  （3 format_only + format_eof）、内嵌对照通过；修复后全过。
- 验证（单条前台阻塞）：test_agent_stream.py 203 passed；core 412；dot/dispatch/
  feishu 89；相关宽集（剔除 2 个 base 既有 goal flake）2321 passed、0 failed；
  mypy/black/isort 干净。

## Codex 第八轮——孤立修饰符(P1-A) 与 双方无答案(P1-B)（cycle-9）

- **P1-A**：`is_substantive_answer` 把孤立 Mn/Me（VS16 U+FE0F、VS15 U+FE0E、CGJ
  U+034F、补充 VS U+E0100）当有效答案。改为：逐字符扫描时维护 `has_base`，遇到 Mn/Me
  仅在其前已有真实基字符时才算实质；无基的孤立修饰符一律忽略；任何其它真实字形
  立即算实质。于是 e+U+0301、键帽 1️⃣、ZWJ 👩‍💻、正文内嵌 ZWSP/VS 全部原样保留
  （谓词只判存在、不改文本）。`trim_invisible_edges` 同步：边界只剥「无基」修饰符，
  附着在基字符后的标记（如 é 结尾）保留。
- **P1-B**：工具后无有效 final，且 success result 为空/ZWSP/BOM/空白/缺字段时，旧逻辑
  把工具前 commentary 当答案返回。修法：①adapter 生成 `_authoritative_result_text`
  的存在性也改走 `is_substantive_answer`（空/不可见 result 直接不带权威键）；
  ②consumer 完成块在 backstop 之后做最终存在性 fail-closed——仅当**本回合运行过工具、
  工具后无实质 final、backstop 也未能给出实质 final_text** 时，把 `completed` 降级为
  单个 retryable failed，并剥除 final_text/assistant_text（commentary 永不当答案）。
  保留：有效流式 final + 空 authority → 仍投递 final；无工具无文本的空 success 不改
  provider 的 completed 状态（由 dispatch「completed without assistant text」raise）。
- 测试：并入 round8 6 个失败（VS16/VS15/CGJ/补充VS × 短/长 authority、仅 VS16 已闭合
  EOF、empty/zwsp/bom/spaces/missing authority）+ 6 个对照（长组合/键帽/ZWJ/内嵌 ZWSP
  正常长答、谓词单测、有效 final+空 authority、无 plan 无答案）。cda59f0a 上新测 12 个
  失败（含谓词单测）、3 个对照通过；新提交全过。
- 验证（单条前台阻塞）：test_agent_stream.py 218 passed；coalescer/native/merge 209；
  dot/dispatch/feishu 89；相关宽集（剔除 2 个 base 既有 goal flake）2336 passed、0
  failed；mypy（改动 5 文件及全后端，除既有 harness 报错）/black/isort 干净。

## Codex 第九轮——降级状态守卫 + 字形簇安全修剪（cycle-10）

- **P1（回归）**：cycle-9 的 P1-B 降级块没检查原 status，把 provider 显式
  `result(subtype=cancelled)`（工具后无 final）改写成 failed/retryable。修复：降级条件
  增加 `event.payload.get("status") == "completed"` 守卫——只对 provider 的成功
  completed 无答案做降级；cancelled/failed 的原状态与诊断原样保留（加测试
  no-text 与 with-text-control）。
- **P2（journal 污染）**：`trim_invisible_edges` 只看前一个码点，commentary 以
  base+多个 Mn（e+U+0301+U+0327）或键帽（1+VS16+U+20E3，U+20E3 是 Me）结尾时会误删
  一个合法组合符。改为**字形簇安全**：尾随先回退整段连续 Mn/Me 标记栈，若栈直接接在
  真实基字符上则保留整簇（绝不拆散），仅当标记无基（前为空白/格式）才连同其前的空白
  /格式一并剥除；前导只剥纯空白/控制/格式，不动标记（标记附后随基字符）。权威采纳时
  journal 的 commentary 因此逐字保留（多 Mn、键帽、ZWJ emoji 均覆盖）。
- 并入 round9 3 个失败（explicit cancelled no-text、multiple-mn、keycap）+ 7 个通过
  对照（padded selector/marks、open 短中文/emoji+有效 result、无工具空 result 两种、
  cancelled with-text、trim 单测）。ee8ef1e1 上恰 3 失败、8 对照通过；新提交全过。
- 验证（单条前台阻塞）：test_agent_stream.py 229 passed；coalescer/native/merge 209；
  宽集 2405 passed（唯一失败 test_provider_process_group 依赖 Linux /proc，macOS 平台
  不兼容，与本改动无关）；black/isort/mypy（改动文件）干净。
