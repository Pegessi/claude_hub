# 2026-04-01 - 飞书认证与 Cloudflare Tunnel 实现记录

## 问题描述
实现 Claude Hub 的公网部署功能，包括：
1. 集成飞书 OAuth 认证
2. 使用 Cloudflare Tunnel 提供公网访问
3. 实现用户白名单控制（邮箱或 open_id）
4. 修复 WebSocket 认证问题

## 系统总览

### 架构
Claude Hub 是一个基于 Web 的持久化终端服务：
- **前端**: Vue 3 + TypeScript + Vite + Pinia
- **后端**: Python 3.11+ + FastAPI + WebSocket
- **终端**: ttyd + tmux 实现会话持久化
- **认证**: Feishu OAuth 2.0
- **公网访问**: Cloudflare Tunnel

### 认证工作流程
1. 用户点击前端的 "Login with Feishu"
2. 重定向到飞书授权页面
3. 用户授权后回调到 `/api/auth/callback`
4. 后端获取用户信息，创建 session
5. 设置 session cookie，重定向回前端
6. 前端通过 cookie 认证访问 API 和 WebSocket

### Cloudflare Tunnel 工作流程
1. 一键脚本启动后端、前端和 cloudflared
2. cloudflared 连接到 Cloudflare 边缘节点
3. 获取随机公网域名（如 `https://random-name.trycloudflare.com`）
4. 自动更新 `.env` 文件中的 `FRONTEND_URL` 和 `FEISHU_REDIRECT_URI`

## 模块设计

### 前端模块
- `App.vue`: 主应用组件，集成登录 UI 和多面板布局
- `types/index.ts`: 类型定义，新增 `User` 和 `AuthCheckResponse`
- `vite.config.ts`: 配置 `allowedHosts: true` 支持 Cloudflare Tunnel

### 后端模块
- `config.py`: 配置管理，新增 `auth_allowed_open_ids`
- `auth/dependencies.py`: 认证依赖，重点是 WebSocket 认证
- `auth/session.py`: Session 管理
- `api/auth.py`: 飞书 OAuth API 路由
- `services/ttyd_manager.py`: 终端管理（保持不变）

### 脚本模块
- `scripts/start-temp-tunnel.sh`: 一键启动临时隧道脚本
- `scripts/stop-all.sh`: 停止所有服务脚本
- `scripts/cloudflared-setup.sh`: Cloudflare Tunnel 配置脚本
- `scripts/cloudflared-run.sh`: Cloudflare Tunnel 运行脚本

## 关键问题/坑点

### 问题 1: Git 合并冲突
**场景**: 合并 `feat/feishu-auth-public-deploy` 分支到 main 时，`App.vue` 和 `types/index.ts` 出现冲突。

**原因**: main 分支新增了多面板布局功能，而 auth 分支有认证 UI。

**解决方案**: 手动合并代码，将认证 UI 集成到多面板布局中。

### 问题 2: WebSocket 连接失败（认证启用后）
**最初的实现**:
```python
async def get_current_user_ws(
    websocket: WebSocket,
    session_id: Optional[str] = Query(None),
) -> Optional[User]:
    # 只从 query 参数读取 session_id
```

**问题**: WebSocket 连接时，前端没有在 query 参数中传递 session_id，只在 cookie 中。

**第一次尝试修复**:
```python
async def get_current_user_ws(
    websocket: WebSocket,
    session_id: Optional[str] = Query(None),
    session_id_cookie: Optional[str] = Cookie(None, alias=settings.session_cookie_name),
) -> Optional[User]:
    effective_session_id = session_id or session_id_cookie
```

**新问题**: FastAPI 的 `Cookie` 装饰器在 WebSocket 端点中工作不稳定，有时无法正确读取 cookie。

**最终解决方案**（用户改进版）:
```python
async def get_current_user_ws(
    websocket: WebSocket,
    session_id: Optional[str] = Query(None),
) -> Optional[User]:
    import http.cookies
    effective_session_id = session_id
    if not effective_session_id:
        cookie_header = websocket.headers.get("cookie", "")
        if cookie_header:
            cookies = http.cookies.SimpleCookie()
            cookies.load(cookie_header)
            if settings.session_cookie_name in cookies:
                effective_session_id = cookies[settings.session_cookie_name].value
```

**关键点**:
1. 手动从 `websocket.headers` 读取 `cookie` header
2. 使用 `http.cookies.SimpleCookie` 解析 cookie
3. 优先使用 query 参数，回退到 cookie

### 问题 3: OpenID 白名单支持
**需求**: 用户希望使用飞书 open_id 作为白名单（比邮箱更安全，因为邮箱可能变更）。

**实现**:
1. 在 `config.py` 新增 `auth_allowed_open_ids` 配置
2. 新增 `allowed_open_ids_list` 属性
3. 在认证逻辑中优先检查 open_id 白名单，再检查邮箱白名单

```python
# Check whitelist: open_id first, then email
allowed_open_ids = settings.allowed_open_ids_list
allowed_emails = [email.lower() for email in settings.allowed_emails_list]

access_granted = False

# Check open_id whitelist if configured
if allowed_open_ids:
    if session.user.open_id in allowed_open_ids:
        access_granted = True
# Check email whitelist if configured and open_id check not passed
elif allowed_emails:
    user_email_lower = session.user.email.lower() if session.user.email else ""
    if user_email_lower in allowed_emails:
        access_granted = True
# No whitelist configured: allow all authenticated users
else:
    access_granted = True
```

### 问题 4: Vite 开发服务器拒绝 Cloudflare Tunnel 请求
**问题**: 通过 Cloudflare Tunnel 访问前端时，Vite 开发服务器返回 `Invalid Host header` 错误。

**原因**: Vite 默认只允许 `localhost` 和 `127.0.0.1` 访问，防止 Host header 攻击。

**解决方案**: 在 `frontend/vite.config.ts` 中配置 `allowedHosts: true`：
```typescript
server: {
  host: '0.0.0.0',
  port: 5173,
  allowedHosts: true,  // 允许所有 Host header
  proxy: { ... }
}
```

### 问题 5: .env 文件位置问题
**问题**: 后端启动时无法读取到根目录的 `.env` 文件。

**原因**: 后端从当前工作目录读取 `.env`，而启动时工作目录是 `backend/`。

**解决方案**: 将 `.env` 文件复制到 `backend/` 目录下。

### 问题 6: 敏感信息脱敏
**需求**: 删除根目录的 `.env` 文件（包含真实的飞书密钥），只保留 `backend/.env`（正在使用）。

**处理**:
1. 删除根目录 `.env`
2. 保留 `backend/.env`（在使用中）
3. 确保 `.env.example` 只有占位符
4. 检查 README 和 DEPLOYMENT.md 没有泄露敏感信息

## 其他改进

### Cloudflare Tunnel 一键脚本
`scripts/start-temp-tunnel.sh` 功能：
1. 自动检查端口占用
2. 启动后端、前端、cloudflared
3. 等待隧道建立并提取公网 URL
4. 自动更新 `.env` 文件中的 `FRONTEND_URL` 和 `FEISHU_REDIRECT_URI`
5. 显示所有服务的 PID 和日志位置
6. Ctrl+C 时自动清理所有进程

### 文档更新
- `README.md`: 更新了完整的认证和隧道使用说明
- `docs/DEPLOYMENT.md`: 详细的部署指南
- `.env.example`: 添加了 `AUTH_ALLOWED_OPEN_IDS` 配置

## 提交记录
- `f5a2d6f` - fix: improve tmux server persistence and add gstack skills to CLAUDE.md
- `966c104` - docs: add AUTH_ALLOWED_OPEN_IDS to .env.example
- (以及中间的多个合并提交)

## 关键文件修改清单
- `frontend/src/App.vue` - 合并多面板布局和认证 UI
- `frontend/src/types/index.ts` - 新增 User 和 AuthCheckResponse 类型
- `frontend/vite.config.ts` - 添加 allowedHosts: true
- `backend/claude_hub/config.py` - 新增 auth_allowed_open_ids 配置
- `backend/claude_hub/auth/dependencies.py` - 改进 WebSocket 认证逻辑
- `backend/claude_hub/api/auth.py` - 添加 open_id 白名单检查
- `.env.example` - 添加 AUTH_ALLOWED_OPEN_IDS
- `scripts/start-temp-tunnel.sh` - 一键启动脚本
- `scripts/stop-all.sh` - 停止脚本
- `README.md` - 更新文档
- `docs/DEPLOYMENT.md` - 部署指南
