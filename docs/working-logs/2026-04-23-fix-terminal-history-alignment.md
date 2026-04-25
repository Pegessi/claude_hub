# 2026-04-23 - 修复终端历史消息错位与移动端无限滚动

## 问题描述

### Bug 1：历史消息与实时消息错位

终端在实时渲染时，用户滚动 web 端窗口会导致历史消息和实时消息错位显示，表现为滚动时在 scrollback 和可见屏幕的交界处出现内容重叠或缺口。

### Bug 2：移动端键盘弹出导致无限刷新滚动

在移动端点击输入框拉起虚拟键盘后，viewport 纵向缩短，历史记录多时终端产生无限刷新滚动的循环。

### Bug 3（修复 Bug 1 过程中引入）：历史丢失

修复 Bug 1 时写入全部 tmux capture 输出（包含可见屏幕），导致可见屏幕被重复写入（历史回放一份 + ttyd WebSocket 一份），xterm.js scrollback buffer 溢出，中间历史被挤出，只显示很早期和最新的内容。

---

## 根因分析

### Bug 1 根因：两个问题叠加

**问题 A：边界裁剪计算错误**

回放脚本用 `lines.slice(0, lines.length - rows)` 裁剪掉可见屏幕行，假设 `lines.length - rows` 能精确分割 scrollback 和可见屏幕。但 `tmux capture-pane -J` 会合并折行（一个视觉行可能跨多行），导致 `lines.length` 不等于视觉行数，裁剪位置偏移，历史和实时的交界处产生重叠或缺口。

**问题 B：写入竞争**

`term.write()` 在 xterm.js 中是异步的（数据进入队列后分块处理）。大量历史数据写入时：
1. `term.write(history)` 将历史数据入队但未同步处理完
2. ttyd 的 WebSocket 已连接 tmux 开始推流实时数据
3. ttyd 调用 `term.write(screen)` 写入实时屏幕数据
4. xterm.js 交替处理历史和实时数据块
5. 两个数据流在 buffer 中交叉混合

### Bug 2 根因：resize 反馈循环

```
键盘弹出 → viewport 高度缩短 → xterm.js 触发 resize
→ ttyd 发送 resize 给 tmux → tmux 用新尺寸重绘屏幕
→ xterm.js 收到重绘数据重新渲染 → 大量 scrollback 内容重排
→ 滚动位置跳动 → 可能再次触发 viewport 变化 → 循环...
```

移动端键盘动画过程中 `visualViewport.resize` 会连续触发多次，每次都引发完整的 resize → redraw → re-render 链路，在 100K 行 scrollback 下开销极大。

### Bug 3 根因：scrollback buffer 溢出

写入全部 `normalizedHistory`（scrollback + 可见屏幕）后，ttyd WebSocket 又推送了一份可见屏幕数据。xterm.js 的 scrollback 限制为 100K 行，重复内容占满 buffer 后把中间的 scrollback 历史挤出。

---

## 修复方案

### 修复 Bug 1 + Bug 3：准确的 scrollback-only 回放 + 写入缓冲

**文件**: `backend/claude_hub/services/ttyd_manager.py`

去掉 `capture-pane` 的 `-J` 标志。不带 `-J` 时，tmux 输出一行 = 一个视觉行，`lines.length - rows` 的裁剪计算才准确。

```python
# 改前
"tmux", "capture-pane", "-p", "-e", "-J", "-S", start, "-t", self.tmux_session,

# 改后
"tmux", "capture-pane", "-p", "-e", "-S", start, "-t", self.tmux_session,
```

**文件**: `backend/claude_hub/api/terminal.py` — 注入的回放脚本

1. **恢复裁剪逻辑**，但修复计算准确性：去掉 `-J` 后行数准确，并增加尾部空行清理（tmux 可能填充空行导致 rows 偏差）
2. **写入缓冲**：覆写 `term.write()`，历史写入期间实时数据暂存缓冲区，历史写完后（通过 `originalWrite(data, callback)` 回调确认）按序刷新
3. **5 秒安全超时**：防止回调不触发导致终端卡死

```javascript
// 裁剪可见屏幕行（修复后的准确计算）
const rows = Number(term.rows) || 24;
const lines = normalizedHistory.replace(/\r/g, '').split('\n');
while (lines.length > 0 && lines[lines.length - 1] === '') {
  lines.pop(); // 清理 tmux 填充的尾部空行
}
if (lines.length <= rows) return;
const scrollbackLines = lines.slice(0, lines.length - rows);
const replayText = scrollbackLines.join('\r\n');

// 缓冲实时写入，等历史写完再按序放行
const buffer = [];
let historyDone = false;
const originalWrite = term.write.bind(term);

term.write = function(data, cb) {
  if (historyDone) return originalWrite(data, cb);
  buffer.push({ data, cb });
};

function flushBuffer() {
  if (historyDone) return;
  historyDone = true;
  term.write = originalWrite;
  for (const item of buffer) originalWrite(item.data, item.cb);
  buffer.length = 0;
}

const safetyTimer = setTimeout(flushBuffer, 5000);
originalWrite(replayText + '\r\n', function() {
  clearTimeout(safetyTimer);
  flushBuffer();
});
```

### 修复 Bug 2：移动端 resize 三层防护

**文件**: `backend/claude_hub/api/terminal.py` — 注入的 CSS + JavaScript

**层 1 — CSS `100lvh`**：使用 Large Viewport Height，键盘弹出时终端容器高度不变，从根源减少 resize 触发。

```css
html, body {
  height: 100lvh;
}
```

**层 2 — xterm.js `onResize` 防抖**：150ms debounce，只把最终稳定尺寸转发给 tmux，吞掉中间态 resize。

```javascript
term.onResize = function(cols, rows) {
  lastArgs = [cols, rows];
  if (pending) return; // 吞掉中间 resize
  pending = true;
  timer = setTimeout(function() {
    pending = false;
    origOnResize.apply(term, lastArgs);
  }, 150);
};
```

**层 3 — `visualViewport` 键盘状态检测**：只在键盘完全弹出/收起时触发一次 `fit()`，键盘动画过程中的瞬态抖动全部忽略。

```javascript
window.visualViewport.addEventListener('resize', function() {
  clearTimeout(vvTimer);
  vvTimer = setTimeout(function() {
    const nowKeyboard = (vv.height < window.innerHeight * 0.8);
    if (nowKeyboard !== keyboardVisible) {
      keyboardVisible = nowKeyboard;
      term.fitAddon.fit(); // 只在状态切换时触发一次
    }
    // 状态未变 = 瞬态抖动，忽略
  }, 150);
});
```

---

## 修改文件汇总

| 文件 | 改动 |
|------|------|
| `backend/claude_hub/api/terminal.py` | 回放脚本：恢复裁剪 + 缓冲写入 + 安全超时；CSS `100lvh`；resize 防抖 + visualViewport 键盘检测 |
| `backend/claude_hub/services/ttyd_manager.py` | `capture-pane` 去掉 `-J` 标志 |

## Commits

| Commit | 说明 |
|--------|------|
| `1cc28ed` | fix: prevent history/realtime terminal message misalignment on scroll |
| `9015dac` | fix: debounce mobile keyboard resize to prevent infinite scroll loop |
| `1c153f0` | fix: restore scrollback-only replay to prevent history loss |

## 验证方法

1. 创建终端 tab，执行命令产生历史输出
2. 滚动终端到历史区域再滚回，确认历史/实时交界处无错位
3. 刷新浏览器页面，确认 scrollback 完整（无早期+最新的断层）
4. 在实时输出期间滚动，确认历史和实时消息不交叉
5. 移动端：点击输入拉起键盘，确认终端不会无限刷新滚动
6. 移动端：键盘收起后，确认终端尺寸恢复正常
