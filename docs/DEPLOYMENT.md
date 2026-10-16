# Claude Hub 公网部署指南

本指南介绍如何将 Claude Hub 部署到公网，并配置飞书身份认证。

## 目录

1. [架构概述](#架构概述)
2. [飞书应用配置](#飞书应用配置)
3. [环境变量配置](#环境变量配置)
4. [内网穿透部署](#内网穿透部署)
5. [Nginx 反向代理](#nginx-反向代理)
6. [Cloudflare Tunnel（推荐）](#cloudflare-tunnel推荐)

---

## 架构概述

```
                    ┌─────────────────────────────────────┐
                    │          Public Internet              │
                    └──────────────────┬────────────────────┘
                                       │
                                       ▼
                    ┌─────────────────────────────────────┐
                    │     frp/ngrok 公网节点               │
                    │    (例如: xxx.ngrok.io)              │
                    └──────────────────┬────────────────────┘
                                       │
                    ┌──────────────────┴────────────────────┐
                    │      内网穿透隧道 (frp/ngrok)          │
                    └──────────────────┬────────────────────┘
                                       │
                    ┌──────────────────▼────────────────────┐
                    │         Nginx (可选)                    │
                    │    反向代理 + SSL 终止                  │
                    └──────┬──────────────────┬──────────────┘
                           │                  │
                           ▼                  ▼
               ┌───────────────────────────────────────────┐
               │        Claude Hub（生产模式：单源）          │
               │   FastAPI :8173 同时提供构建后的 SPA + API   │
               │   （开发模式下才拆成 vite :5173 + uvicorn）  │
               └───────────────────────────────────────────┘
```

> **部署模式（重要）**
>
> 本服务有两种运行模式，由 `start.sh` 控制：
>
> - **生产模式（默认，`./start.sh`）**：先 `pnpm build` 构建前端，然后用**单个
>   uvicorn 进程**在 `:8173` 同时提供 API 和构建后的 SPA。无 `--reload`、无
>   vite dev server、无 HMR WebSocket（HMR 断连会导致后台标签页整页重载）。
>   公网部署应使用此模式，隧道/反代统一指向 `:8173`。
> - **开发模式（`./start.sh --dev`）**：vite dev（`:5173`，HMR）+ uvicorn
>   `--reload`（`:8173`），用于本地开发热更新。
>
> 下文的隧道/反代示例默认按**生产模式**指向 `:8173`；若用开发模式，把端口换回
> `:5173` 即可。

### 从页面重启服务

生产启动脚本保留一个独立 launcher，由它管理单个 uvicorn 子进程。
桌面顶部 `⋯` → **Build and restart**；移动端在原有扩展菜单中使用同一入口。
确认后，launcher 会先同步后端依赖，再把前端构建到临时目录。准备全部成功后才会
中断正在执行的 Chat 和 Agent 任务、原子替换前端静态资源、停止旧后端、启动新
后端并检查新实例健康状态。准备失败时旧后端和旧前端继续提供服务。重启期间页面
会短暂断连；恢复后需手动续接被中断的工作，按钮不会自动发送“继续”消息。

首次部署此功能需要在外部终端用新版 `./start.sh` 启动一次；已运行的旧进程
不会自动获得 launcher。直接运行 uvicorn 或 `./start.sh --dev` 时该功能禁用。
`CLAUDE_HUB_HOST`、`CLAUDE_HUB_PORT` 可覆盖生产启动的监听地址和端口。

MASO 的服务地址可作为持久化启动配置写入 `backend/.env`：

```dotenv
MASO_BASE_URL=http://127.0.0.1:11004
```

上面端口只是示例，以本机验证结果为准。服务端现有 `Settings` 读取该文件，
进程环境中的同名变量可覆盖它，单个 Chat 的显式 URL 再覆盖服务默认值。
启动探测和 Chat 连接读取同一份配置；修改文件后需在下一次授权启动/重启时生效。

启动时会只读检查 MASO，打印实际服务 URL 或不可用原因。显式
`MASO_BASE_URL`（进程环境或配置文件）优先，其次是进程中的 `MASO_PORT`；未配置时，从本地实例登记和旧默认
端口发现唯一可用服务。多个可用实例时需指定 URL，不自动选择。检查不会启动
MASO 或调用模型；MASO 不可用不会阻止其他 Hub provider 启动。独立检查命令和
已有 Chat 的地址绑定限制见 [MASO HTTP Chat](MASO.md#startup-check)。

页面重启不拉取代码。停止旧后端前，launcher 会在 `backend/` 运行
`uv sync --locked --inexact`，按已提交的 `uv.lock` 补齐后端依赖且不删除额外
工具；随后在 `frontend/` 运行 `pnpm build`。新静态资源先写入同级临时目录，完整
产出 `index.html` 后才整树替换 `frontend/dist`，避免构建中暴露半成品。依赖同步失败、
前端构建失败或超时时，旧后端和旧 `dist` 继续运行，页面显示失败原因，不会进入
中断服务阶段。操作状态保存在当前 runtime home 下 `restart/state.json`，后端日志
在 `logs/backend.log`；该文件默认按 10 MiB 轮转并保留 5 份备份，可通过
`BACKEND_LOG_MAX_BYTES` 和 `BACKEND_LOG_BACKUP_COUNT` 调整。同步、构建或启动失败
还应查看启动终端输出。前端构建上限为 90 秒，页面等待超过 5 分钟只提示未确认
恢复，不会重复重启。

### Tab 状态文件升级与清理恢复

直接 Chat 和普通终端 tab 的删除使用当前 runtime home 下的 `tabs.json` 保存
待清理记录。文件删除失败后，tab 身份可能已经移除；同一 DELETE 请求返回
HTTP 503，并说明清理尚未完成。后端会在启动时和现有监控循环中重试，重复
DELETE 也可重试。只有文件清理和待清理记录的持久删除均完成后才返回 HTTP 204。
待清理期间不会复用对应 ID；遇到活动身份冲突或损坏的状态文件时，会保留记录
并停止相关写入或清理，不会通过删除记录来跳过检查。

新版兼容读取旧的 tab 列表，首次成功保存后改为包含版本号、活动 tab 和待清理
记录的对象。单纯导入模块或读取旧文件不会触发迁移。**旧程序不能安全读取或
覆盖新格式**。以后部署新版前，应先停止相关写入并备份一致的完整运行数据。
如需回退，必须另行确认状态文件及相关历史文件的一致性恢复方案；不要只修改
版本号、恢复单个旧 `tabs.json` 或丢弃待清理记录。

此机制仅清理 `terminal-tabs` 命名空间。它不扫描历史遗留文件，也不表示
Workspace 会话及整个 Workspace 的多文件删除已改为同一原子事务。
只合并代码不会迁移正在运行的共享服务数据，实际部署和重启仍需单独授权。

---

## 飞书应用配置

### 1. 创建飞书应用

1. 访问 [飞书开放平台](https://open.feishu.cn/)
2. 点击"创建应用" → 选择"企业自建应用"
3. 填写应用名称和描述，点击"创建"

### 2. 获取 App ID 和 App Secret

1. 在应用详情页 → "凭证与基础信息"
2. 复制 `App ID` 和 `App Secret`

### 3. 配置重定向 URL

1. 进入"安全设置" → "重定向 URL"
2. 添加你的回调地址，例如：
   ```
   https://your-domain.ngrok.io/api/auth/callback
   ```

### 4. 配置权限

进入"权限管理"，添加以下权限：
- `contact:user.id:readonly` - 获取用户 ID
- `contact:user.email:readonly` - 获取用户邮箱
- `contact:user.employee_id:readonly` - 获取用户信息

### 5. 发布应用

1. 进入"版本管理与发布"
2. 创建一个版本并发布
3. 如果是企业内部使用，可以申请"企业内部开发"权限

---

## 环境变量配置

服务端配置由 `backend/claude_hub/config.py` 中的 `Settings` 定义，支持进程环境
和 `backend/.env`（同名进程环境优先）。生产和开发启动脚本都会从 `backend/`
运行后端，所以应在该目录保存文件。首次配置时，从仓库根目录复制模板并填写；
已有文件应只修改需要的配置项，保留其他值：

```bash
cp .env.example backend/.env
```

监听地址/端口仍通过启动脚本的 `CLAUDE_HUB_HOST`、`CLAUDE_HUB_PORT` 进程环境
传入。CLI 的 `~/.config/claude-hub/config.toml` 配置客户端连接及默认 preset，
会话环境 preset 配置单个 Chat/Agent；它们分别由自己的组件管理。目前没有统一
的启动配置页面。

### 必需配置

```env
# 前端公网地址
FRONTEND_URL=https://your-domain.ngrok.io

# 飞书应用配置
FEISHU_APP_ID=cli_a1b2c3d4e5f6g7h8
FEISHU_APP_SECRET=abcdef1234567890abcdef1234567890
FEISHU_REDIRECT_URI=https://your-domain.ngrok.io/api/auth/callback
```

### Session 配置

```env
# 生成安全的密钥
# python -c "import secrets; print(secrets.token_urlsafe(32))"
SESSION_SECRET_KEY=your-secure-random-key-here

# Session 过期时间（天）
SESSION_EXPIRE_DAYS=7
```

### 访问白名单（可选）

```env
# 只允许指定邮箱的用户登录
AUTH_ALLOWED_EMAILS=user1@company.com,user2@company.com
```

---

## 内网穿透部署

### 方案一：使用 ngrok（快速测试）

1. 安装 ngrok：https://ngrok.com/download
2. 运行 ngrok：

```bash
# 转发服务端口（生产模式下 FastAPI 在 8173 同时提供前端和 API）
ngrok http 8173

# 或者使用自定义域名（付费版）
ngrok http --domain=your-custom-domain.ngrok.io 8173
```

3. 将 ngrok 提供的 URL 配置到 `.env` 的 `FRONTEND_URL` 和 `FEISHU_REDIRECT_URI`

### 方案二：使用 frp（自建服务器）

1. 在公网服务器上配置 frps（frp 服务端）

`frps.toml`:
```toml
bindPort = 7000
auth.token = "your-auth-token"
```

2. 在本地配置 frpc（frp 客户端）

复制 `docker/frp/frpc.toml.example` 到 `docker/frp/frpc.toml` 并修改：

```toml
serverAddr = "your-server-ip"
serverPort = 7000
auth.token = "your-auth-token"

[[proxies]]
name = "claude-hub"
type = "http"
localIP = "127.0.0.1"
localPort = 8173
customDomains = ["claude.your-domain.com"]
```

3. 启动 frpc：

```bash
frpc -c docker/frp/frpc.toml
```

### 方案三：使用 Cloudflare Tunnel（推荐）

Cloudflare Tunnel 是最简单、最安全的公网访问方案，无需公网服务器，免费且稳定。

#### 前置要求
- 一个 Cloudflare 账户
- 一个通过 Cloudflare 管理的域名

#### 快速开始（使用脚本）

项目提供了自动化脚本来简化设置过程：

```bash
# 1. 运行设置脚本（只需运行一次）
./scripts/cloudflared-setup.sh

# 2. 启动 Claude Hub（生产模式：构建前端并在 :8173 同时提供前端和 API）
./start.sh

# 3. 启动 Cloudflare Tunnel
./scripts/cloudflared-run.sh
```

#### 手动设置步骤

如果你想手动配置，按以下步骤操作：

##### 1. 安装 cloudflared

```bash
# macOS
brew install cloudflare/cloudflare/cloudflared

# 其他系统
# 访问 https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
```

##### 2. 认证 cloudflared

```bash
cloudflared tunnel login
```
这会打开浏览器，选择你要使用的域名。

##### 3. 创建隧道

```bash
cloudflared tunnel create claude-hub
```

记下输出中的隧道 ID。

##### 4. 配置 DNS 路由

```bash
cloudflared tunnel route dns claude-hub claude.your-domain.com
```

##### 5. 创建配置文件

复制 `docker/cloudflared/config.yml.example` 到 `docker/cloudflared/config.yml` 并修改：

```yaml
tunnel: your-tunnel-id-here
credentials-file: /Users/your-user/.cloudflared/your-tunnel-id-here.json

ingress:
  - hostname: claude.your-domain.com
    service: http://localhost:8173
  - service: http_status:404
```

##### 6. 更新环境变量

在 `.env` 文件中设置：

```env
FRONTEND_URL=https://claude.your-domain.com
FEISHU_REDIRECT_URI=https://claude.your-domain.com/api/auth/callback
```

##### 7. 启动隧道

```bash
cloudflared tunnel --config docker/cloudflared/config.yml run
```

#### Cloudflare Tunnel 的优势

- ✅ **免费** - 无需额外费用
- ✅ **安全** - 流量通过 Cloudflare 网络加密
- ✅ **稳定** - Cloudflare 的全球边缘网络
- ✅ **无需公网 IP** - 不需要开放防火墙端口
- ✅ **自动 HTTPS** - 自动配置 SSL 证书

---

## Nginx 反向代理

如果需要使用 Nginx 进行 SSL 终止或负载均衡：

### 使用 Docker Compose

在 `docker/` 目录下创建 `docker-compose.prod.yml`：

```yaml
version: '3.8'

services:
  backend:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    ports:
      - "8173:8173"
    environment:
      - FRONTEND_URL=https://your-domain.com
    env_file:
      - ../.env
    restart: unless-stopped

  frontend:
    build:
      context: ..
      dockerfile: docker/Dockerfile.frontend
    ports:
      - "5173:5173"
    depends_on:
      - backend
    restart: unless-stopped

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - ./nginx/ssl:/etc/nginx/ssl:ro
    depends_on:
      - backend
      - frontend
    restart: unless-stopped
```

此 Docker Compose 示例及仓库的 `docker/docker-compose.yml` 通过 `env_file`
读取根目录 `.env` 并注入容器进程；该路径由 Compose 配置管理。上面的
`backend/.env` 路径适用于直接使用 `start.sh` 的本地服务启动。

### SSL 证书配置（使用 Let's Encrypt）

1. 安装 certbot：

```bash
# Ubuntu/Debian
sudo apt-get install certbot

# macOS
brew install certbot
```

2. 获取证书：

```bash
certbot certonly --standalone -d your-domain.com
```

3. 将证书复制到 nginx 配置目录：

```bash
mkdir -p docker/nginx/ssl
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem docker/nginx/ssl/
cp /etc/letsencrypt/live/your-domain.com/privkey.pem docker/nginx/ssl/
```

4. 更新 nginx 配置以支持 SSL（参考 nginx 配置文档）

---

## 启动服务

### 生产模式 + Cloudflare Tunnel（推荐）

```bash
# 终端 1 - 启动服务（构建前端，并在 :8173 同时提供前端和 API）
./start.sh

# 终端 2 - 启动 Cloudflare Tunnel
./scripts/cloudflared-run.sh
```

### 生产模式（本地）

```bash
./start.sh
# 访问 http://localhost:8173
```

### 开发模式（热更新）

```bash
./start.sh --dev
# 前端（HMR）: http://localhost:5173
# 后端（--reload）: http://localhost:8173
```

### Docker Compose 部署

```bash
cd docker
docker-compose -f docker-compose.yml up -d
```

---

## 验证部署

1. 访问公网 URL
2. 应该会看到飞书登录页面
3. 点击登录，飞书授权后跳转回应用
4. 确认可以正常使用终端功能

---

## 故障排查

### 登录后回调失败

- 检查 `FEISHU_REDIRECT_URI` 是否与飞书开放平台配置一致
- 检查网络是否可以访问飞书 API
- 查看后端日志 `~/.claude_hub/logs/backend.log`

### WebSocket 连接断开

- 检查 Nginx/Web 服务器的 WebSocket 配置
- 确认 proxy_read_timeout 设置足够长

### Session 过期太快

- 调整 `SESSION_EXPIRE_DAYS` 配置
- 检查 Cookie 的 `max_age` 设置

---

## 安全建议

1. **始终启用认证** - 不要在公网暴露未认证的服务
2. **使用邮箱白名单** - 限制只有公司内部人员可以访问
3. **使用 HTTPS** - 始终使用 SSL 加密传输
4. **定期轮换密钥** - 定期更新 `SESSION_SECRET_KEY`
5. **监控日志** - 定期检查访问日志和后端日志
