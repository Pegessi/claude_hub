# 2026-04-10 - 修复终端文本选择功能

## 问题描述

用户在浏览器中无法通过鼠标拖拽选择终端中的文本。此前已修复了右键菜单问题（浏览器默认右键菜单被阻止），但文本选择始终无法工作。经过多次尝试（移除干扰 CSS、简化注入脚本等），问题仍然存在。

## 根因分析

### 事件流路径

```
用户鼠标拖拽 → 浏览器 iframe → ttyd HTML 页面 → xterm.js → WebSocket → tmux
```

### 根本原因：tmux `mouse on` 拦截鼠标事件

`ttyd_manager.py` 中配置了 `tmux set -g mouse on`，这导致：

1. tmux 通过发送转义序列（`\e[?1000h` / `\e[?1002h` / `\e[?1006h`）告诉 xterm.js 进入"鼠标报告模式"
2. xterm.js 收到这些序列后，将所有鼠标事件（包括拖拽）编码为转义序列，通过 WebSocket 发回给 tmux
3. xterm.js 不再将鼠标拖拽处理为文本选择，而是作为鼠标位置报告
4. tmux 收到鼠标事件后进入自己的 copy-mode，但这是 tmux 内部的选择机制，不是浏览器原生文本选择

### 次要问题

- `TerminalView.vue` 中用 `capture: true` 拦截了所有 `contextmenu` 事件，导致选中文本后也无法通过右键菜单复制
- `terminal.py` 中注入的 touch JS handler（`touchstart` 使用 `{ passive: false }`）对移动端文本选择有潜在干扰

### 排除的因素

| 因素 | 是否影响 |
|------|---------|
| 注入的 CSS（terminal.py） | 无影响 |
| iframe 嵌入方式 | 无影响（同源代理） |
| ttyd `-t` 启动参数 | 无影响 |
| `terminal-overrides xterm*:smcup@:rmcup@` | 无影响 |
| tmux `history-limit`、`mode-keys`、`status` | 无影响 |

## 修复方案

### 修改 1：关闭 tmux 鼠标模式（根因修复）

**文件**: `backend/claude_hub/services/ttyd_manager.py`

```python
# 改前
["set", "-g", "mouse", "on"],

# 改后
["set", "-g", "mouse", "off"],
```

关闭 tmux mouse 后：
- xterm.js 恢复原生文本选择（鼠标拖拽选中、Cmd/Ctrl+C 复制）
- 鼠标滚轮滚动改由 xterm.js 处理（`scrollback=10000` 已配置，功能不受影响）
- 失去 tmux 鼠标点击切 pane 功能（项目中每个 tab 只有一个 tmux pane，无影响）

### 修改 2：选中文本时允许浏览器原生右键菜单

**文件**: `frontend/src/components/TerminalView.vue`

```javascript
// 改前：无条件阻止右键菜单
document.addEventListener('contextmenu', function(e) {
  e.preventDefault();
  e.stopPropagation();
  return false;
}, true);

// 改后：选中文本时放行，允许复制
document.addEventListener('contextmenu', function(e) {
  var selection = window.getSelection();
  if (selection && selection.toString().length > 0) {
    return; // 允许原生右键菜单
  }
  e.preventDefault();
  e.stopPropagation();
  return false;
}, true);
```

### 修改 3：移除不必要的 touch JS handler

**文件**: `backend/claude_hub/api/terminal.py`

移除了注入到 ttyd HTML 页面中的 `touchstart`/`touchmove` JavaScript 监听器，将动态注入的 `.xterm-viewport` CSS 改为静态 CSS 直接写在 `<style>` 块中。简化代码的同时消除 `{ passive: false }` 对移动端选择的潜在干扰。

## 验证结果

- 鼠标拖拽可以正常选中终端中的文本
- 选中文本后右键可以看到浏览器原生复制菜单
- 鼠标滚轮可以正常滚动终端内容
- 前端类型检查（vue-tsc）通过
- 后端测试（pytest）通过
