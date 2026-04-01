# 2026-03-27 - Solo Mode 修复记录

## 问题描述
Solo Mode 功能没有按照预期工作。勾选 Solo Mode 复选框创建新 tab 时，没有以 `IS_SANDBOX=1 claude --dangerously-skip-permissions` 的方式启动 Claude。

## 系统总览

### 架构
Claude Hub 是一个基于 Web 的持久化终端服务：
- **前端**: Vue 3 + TypeScript + Vite + Pinia
- **后端**: Python 3.11+ + FastAPI + WebSocket
- **终端**: ttyd + tmux 实现会话持久化

### Solo Mode 工作流程
1. 用户在前端勾选 "Solo Mode" 复选框
2. 前端发送 POST `/api/tabs` 请求，携带 `solo_mode: true`
3. 后端创建新的终端会话
4. 以 `IS_SANDBOX=1 claude --dangerously-skip-permissions` 启动 Claude

## 模块设计

### 前端模块
- `TabBar.vue`: 创建 tab 的 UI，包含 Solo Mode 复选框
- `terminalStore.ts`: 状态管理，发送 API 请求

### 后端模块
- `api/tabs.py`: API 路由处理
- `services/ttyd_manager.py`: 核心终端管理逻辑
  - `TTYDProcess`: 管理单个 ttyd 进程
  - `TTYDManager`: 管理多个终端进程

## 关键问题/坑点

### 问题 1: tmux send-keys 用法错误
**最初的实现**:
```python
claude_cmd = "IS_SANDBOX=1 claude --dangerously-skip-permissions\n"
proc = await asyncio.create_subprocess_exec(
    "tmux", "send-keys", "-t", self.tmux_session, claude_cmd,
    ...
)
```

**问题**: `tmux send-keys` 需要将命令和 Enter 作为单独的参数传递，而不是把包含换行符的整个字符串作为一个参数。

### 问题 2: tmux session 未就绪就发送命令
**问题**: 尝试在 tmux session 完全创建之前就发送命令，导致 "can't find session" 错误。

**解决方案**: 增加等待和重试逻辑，但这增加了复杂度。

### 问题 3: 默认 shell 就是 claude
**根本问题**: 配置中的 `default_command = "claude"`，所以 tmux 启动时直接就进入了 Claude，没有机会在 shell 中发送命令。

### 最终解决方案（最简单直接）
直接修改启动命令，使用 `bash -c` 来包装：
```python
if self.solo_mode and not session_exists:
    user_shell = os.environ.get("SHELL", "/bin/bash")
    cmd.extend([
        user_shell, "-c",
        "IS_SANDBOX=1 claude --dangerously-skip-permissions; exec " + user_shell
    ])
```

这样：
1. 直接在启动时设置环境变量并运行 Claude
2. `exec bash` 确保 Claude 退出后还能回到 shell
3. 不需要任何 tmux send-keys 的复杂逻辑

## 其他改进
- 添加了文件日志功能，日志输出到 `~/.claude_hub/logs/backend.log`
- 添加了调试日志，便于排查问题

## 提交记录
- commit: `2e838c2` - fix: ensure solo mode correctly launches with IS_SANDBOX=1
