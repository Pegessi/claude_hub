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
               ┌───────────▼──┐          ┌──▼───────────┐
               │  Frontend    │          │   Backend     │
               │  (Vue 5173)  │          │  (FastAPI 8000)│
               └──────────────┘          └───────────────┘
```

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

复制 `.env.example` 到 `.env` 并填写配置：

```bash
cp .env.example .env
```

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
# 转发前端端口（如果前端和后端在同一端口）
ngrok http 5173

# 或者使用自定义域名（付费版）
ngrok http --domain=your-custom-domain.ngrok.io 5173
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
localPort = 5173
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

# 2. 启动 Claude Hub 后端和前端（在两个不同的终端中）
cd backend && uv run uvicorn claude_hub.main:app --reload
cd frontend && pnpm dev

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
    service: http://localhost:5173
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
      - "8000:8000"
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

### 开发模式 + Cloudflare Tunnel（推荐）

```bash
# 终端 1 - 启动后端
cd backend
uv run uvicorn claude_hub.main:app --reload

# 终端 2 - 启动前端
cd frontend
pnpm dev

# 终端 3 - 启动 Cloudflare Tunnel
./scripts/cloudflared-run.sh
```

### 开发模式（本地测试）

```bash
# 后端
cd backend
uv run uvicorn claude_hub.main:app --reload

# 前端（新终端）
cd frontend
pnpm dev
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
