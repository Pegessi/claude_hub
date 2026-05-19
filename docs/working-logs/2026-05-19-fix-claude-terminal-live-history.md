# 2026-05-19 - 修复 Claude/Codex 终端实时输出与历史浏览错位

## 问题描述

Claude/Codex tab 在持续生成内容时，前端 terminal 偶发不显示最新内容，或者用户浏览历史时可见区域被新输出改写。典型表现：

1. 内容更新到一半停在某段中间内容，页面/scrollback 继续向下增长，需要手动滑动或刷新 tab 才看到最新内容。
2. Claude 固定底部输入框/status 区时，上方输出区继续滚动；Web 端把这类 TUI redraw 当普通 append-only 输出处理后，历史中段会出现重复、错位或丢几行。
3. 用户滚到历史中段时，上半屏历史固定、下半屏被 live redraw 刷新，像是中间历史被吞掉。

这次最终落地提交：`5d690f3 fix: keep dynamic terminal output pinned`。

## 背景与关键结论

Claude/Codex 不是普通 “一行一行追加” 的 shell 输出。它们是 TUI：

- 底部输入框和状态区固定；
- 上方回答区滚动；
- 通过 ANSI cursor movement / relative redraw 更新局部屏幕；
- ttyd 发给 xterm 的是屏幕操作流，不是结构化的 “新增日志行”。

因此 terminal replay 逻辑必须区分两类 tab：

| 类型 | 处理原则 |
|------|----------|
| 普通 terminal / cursor | 可以用 tmux snapshot 做 idle resync，补齐 wrapped/快速输出导致的 scrollback 缺口 |
| Claude / Codex TUI | 不能在工作中随意重放 tmux snapshot；必须保护 cursor-driven live screen 和用户正在看的历史视口 |

## 根因分层

### 1. 自动 history replay 对 Claude/Codex 不安全

之前引入的 idle history replay 对普通 terminal 有价值：ttyd/tmux 在快速 wrapped 输出下可能只推送最终屏幕，idle 后从 tmux 重新抓完整历史可以补齐缺口。

但 Claude/Codex TUI 使用相对 cursor 操作维护固定输入/status 区。自动重放纯文本 tmux snapshot 会破坏当前 cursor state，导致状态行残留、历史衔接不连续，或者 tab 切回来时显示旧内容。

结论：自动 idle/activation history replay 只适合 plain/cursor terminal，不适合 Claude/Codex。

### 2. Phase B 初始 replay 期间 ttyd initial frames 与 tmux snapshot 竞争

终端重连时，Phase B 会清空 xterm buffer，然后写入 tmux capture 的完整内容（scrollback + visible screen）。与此同时 ttyd WebSocket 仍可能迟到一批 initial-screen frame。

如果直接写入这些 held frames，会把可见屏幕重复写进 xterm，造成 scrollback seam 重复或断裂。

如果为了避免重复而整段丢弃 held frames，又会误删 Claude 在 hold 窗口里真实新生成的几行内容，表现为 “刷新后能恢复，但 live 时少几行”。

最终策略：只过滤已经包含在 replay snapshot 里的重复 initial frame；保留不在 snapshot 里的真实 live frame。

### 3. 底部跟随与输入延迟是同一个链路上的权衡

为了让 live output 始终显示最新内容，不能简单地频繁 `refresh()` 或无条件 scroll-to-bottom。Claude 输入框 echo 本身也是 live redraw；过重的 refresh/scroll 会让输入有明显延迟。

最终策略：

- 只有当写入前确实在底部时，写入后才调度 bottom-follow；
- bottom-follow 等 xterm render 后执行；
- live 路径不做 refresh-heavy 操作，只在必要时 scroll；
- 真实用户 wheel/touch 滚动会取消 bottom-follow，但 xterm 内部 scroll event 不会被误判为用户滚动。

### 4. 用户浏览历史时，Claude live redraw 不能写入当前历史视口

最后一层问题来自 Claude 的固定底部输入区。用户滚到历史中段时，如果继续把 Claude 的 live redraw 写进同一个 xterm buffer，可见历史区域就会被 ANSI 屏幕操作改写，出现 “上半部分固定、下半部分刷新”。

最终策略：

- Claude/Codex tab 不在最底部时，暂停把 live redraw 写入当前 xterm 视口；
- tmux 仍然继续记录真实输出，数据不丢；
- 用户回到底部后，从 tmux 做一次受控快照恢复最新内容。

## 修复内容

### backend/claude_hub/api/terminal.py

1. 注入 `AGENT_TYPE`，按 tab 类型分流 terminal replay 行为。
2. `AUTO_HISTORY_REPLAY_ENABLED` / `AUTO_HISTORY_RESYNC_ENABLED` 只对 `cursor` 打开，Claude/Codex 禁用自动 idle resync。
3. Phase B replay 的 buffer flush 改为重复帧过滤：
   - strip ANSI/control chars 后与 replay snapshot 比对；
   - 重复 initial-screen frame 丢弃；
   - 新的 Claude/Codex live frame 保留。
4. bottom-follow 改为 render 后按需贴底，live 路径避免 refresh-heavy 操作。
5. 增加 Claude/Codex 历史视口保护：
   - `PROTECT_AGENT_HISTORY_VIEW = AGENT_TYPE === 'claude' || AGENT_TYPE === 'codex'`;
   - 不在底部时，live redraw 不写入当前 xterm；
   - 回到底部后触发 `agent-return-bottom` tmux snapshot 恢复。

### frontend/src/components/TerminalView.vue

1. 桌面 tab activation 只对 cursor/plain terminal 自动刷新历史。
2. Claude/Codex tab activation 改为 scroll-to-bottom，不自动重放 tmux snapshot。
3. 移动端/缓存 iframe 激活路径复用相同原则，避免 agent TUI 被隐式 snapshot replay 破坏。

### backend/tests/test_terminal_replay.py

新增/强化 E2E 覆盖：

- live output 在底部时保持贴底；
- xterm 内部 scroll event 不会取消 bottom-follow；
- Claude/Codex agent tab 不会在 live write 或 activation 后自动 history resync；
- Phase B 初始 replay 不丢 hold 窗口里的真实 live frame；
- Claude/Codex 滚到历史中段时，live redraw 不能改写当前可见历史视口；
- 手动 history refresh 仍可恢复到最新状态；
- 普通 wrapped live output 的完整 history resync 仍然工作。

## 验证记录

最终验证：

```bash
uv run black --check claude_hub/api/terminal.py tests/test_terminal_replay.py
git diff --check
pnpm -C frontend build
HOME=/tmp/claude_hub_test_home_terminal_dynamic_full5 \
TTYD_BASE_PORT=13600 \
PLAYWRIGHT_BROWSERS_PATH=/Users/bytedance/Library/Caches/ms-playwright \
CLAUDE_HUB_TEST_BACKEND_URL=http://127.0.0.1:8187 \
uv run pytest tests/test_terminal_replay.py -q
```

结果：

```text
17 passed in 161.36s
```

实机验证：

- 开 5174 前端预览，后端指向 8175；
- 使用真实 Claude tab；
- 验证持续生成内容时底部跟随正常；
- 验证输入延迟消失；
- 验证滚到历史中段时，不再出现上半屏固定、下半屏刷新吞历史；
- 验证回到底部后能恢复到最新内容。

## 经验沉淀

1. 不要把 Agent TUI 当 append-only terminal 日志处理。
2. tmux snapshot 是恢复历史的源头，但不是 Claude/Codex 工作中可随时重放的 UI 状态。
3. ttyd initial-screen frame 是 replay 里最容易制造重复/断裂的来源；不能简单全放，也不能简单全丢。
4. 用户是否在底部是 terminal live 渲染策略的核心分支。
5. “保护历史浏览” 和 “显示最新 live output” 不能同时在同一个 xterm 视口里完成；Claude/Codex 离底时应保护历史，回底时再从 tmux 恢复最新。
6. 新 terminal 修复必须配套 Playwright/xterm buffer 级测试，单靠视觉手测很容易只修到其中一层。

## 后续注意事项

- 新增 agent 类型时，需要明确它是 append-only terminal 还是 cursor-driven TUI。
- 如果未来要支持更实时的离底提示，可以在外层 UI 显示 “new output available”，而不是在离底 xterm 视口里继续写 Claude/Codex redraw。
- 对 Claude/Codex 的自动刷新逻辑要保持保守：手动刷新、回到底部受控刷新可以；idle 自动刷新要避免。
