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
会短暂断连。确认框中的 **Continue interrupted Chats automatically** 默认勾选；
符合条件的普通 Chat 会在原对话中发送一次 `Please continue.`。正在执行的目标模式
会话也可沿用同一 Goal、对话和检查点继续，不会创建新目标。如果 Goal 原本在等待
普通回合结束，会先继续该普通回合，再恢复目标执行；普通回合被取消或失败时，Goal
保持暂停。

用户主动暂停、停止、等待确认、已结束或交付状态不确定的工作不会被批量唤醒。
飞书 Bot 发起的回合由 Bot 发送链路负责返回结果，目前不在自动续跑范围内；这类回合若被
重启中断，需要从网页或飞书手动继续。Dot 通过飞书收到的回合也遵循这一限制。
暂停、继续、完成或清除 Goal、归档、配置变化及已记录的新输入会撤销相应自动续跑。
修改目标内容仍按“下一步生效”处理；若编辑在恢复准备期间排队，会在当前恢复步骤之后应用。
只有同一 launcher 发起、保存了有效续跑记录的计划重启适用；普通崩溃和没有对应记录的旧版本
升级仍需手动恢复。取消勾选上述选项也会保留暂停状态。

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

### 每个 provider 的私有代理

原生 Claude / Codex / Cursor / TraeX 可以分别通过此部署的 `backend/.env`
配置 `CLAUDE_PROXY_URL`、`CODEX_PROXY_URL`、`CURSOR_PROXY_URL`、`TRAEX_PROXY_URL`。
每项接受 HTTP(S) CONNECT 代理 URL（可含认证信息）；未设置或空值沿用原有进程
代理，值 `direct` 清空该 provider 的 HTTP/HTTPS/ALL 代理。可选的
`CLAUDE_NO_PROXY`、`CODEX_NO_PROXY`、`CURSOR_NO_PROXY`、`TRAEX_NO_PROXY`
覆盖对应 provider 的绕过列表；不设置则继承进程 `NO_PROXY`，显式空值则清空列表。

这些字段由 Settings 读取，不需要 source `.env`，也不会全局修改 `os.environ`。
优先级为：会话/preset 显式代理 > 部署的 provider 默认值 > 进程通用代理；同名
provider 配置的进程变量仍优先于 `.env`。配置代理 URL 会同时替换大小写的
HTTP/HTTPS 代理并清空旧 ALL_PROXY，避免不同库选择不同出口。现有原生 Chat 的
`CLAUDE_HUB_PROVIDER_NETWORK_MODE` 路由策略继续生效；需要严格沿用代理环境时
选择 `inherit`，`auto` 仍可能选择直连，`NO_PROXY` 仍然生效。

例如，仅在远端部署填写 `CURSOR_PROXY_URL` 即可给远端 Cursor 指定代理；本机
留空即可保持原来的网络设置。不要把真实地址或认证信息复制到示例、源码、提交或
日志。`.env.example` 仅提供空字段；真实 `backend/.env` 应保持 Git 忽略并设置
`chmod 600 backend/.env`。`check-staged` 和 `checkout-safety` 会拒绝根目录或
backend 中的私有 `.env` 文件，即使用 `git add -f` 强制加入。

部署代理只在启动时读取。按部署流程重启后端后，新的原生 provider 子进程和
Cursor 模型目录探测都会读取同一份部署默认值；已有 Terminal agent 需重新启动
才会更新。只修改这些 `.env` 字段不需要更改 launcher 的环境。
Cursor 模型目录仍是实例级缓存，不读取单个会话的代理覆盖；不同出口的会话目录
可能不同。较慢出口可以在该部署设置 `CURSOR_MODELS_PROBE_TIMEOUT_SECONDS=15`
（默认 5，必须为正有限数），避免目录探测过早回退。

本地 Terminal agent 也使用上述默认值，并通过权限为 600 的启动文件传入，
不会回写到 tab/preset 或前端会话数据。Claude 的私有 launch settings 同样包含
其部署代理，以覆盖 CLI 用户 settings 中旧的代理。配置字段从序列化输出中排除，
所有 provider 的专用配置变量从子进程继承环境中移除，仅向目标 provider 传入
解析后的通用代理变量。远程 SSH Terminal 不会转发本机部署代理；应在远端 Hub
配置。MASO 是独立服务的 HTTP 通道，保持原来的直连行为，不使用这些 CLI 代理。

#### 部署必做：测试并填写 provider 代理

首次部署、迁移机器或更换出口时，以下步骤必须完成并记录结果，不能直接复制另一台
机器的代理地址或把“进程已启动”当作网络验收：

1. 核对目标部署的代码 SHA、运行用户、provider CLI 版本/登录状态，以及实际服务
   启动环境。先确认该代码版本支持上述 `.env` 字段，再填写；不要把新字段写给不
   兼容的旧后端。检查只输出是否配置，避免打印环境变量、认证 URL 或整个 `.env`。
2. 对要使用的每个 provider，分别测试直连和候选代理。使用目标运行用户、相同 CLI
   及凭据，在一次性子进程环境中测试；代理凭据从受保护文件读取，不放进命令参数、
   shell 历史或日志。Cursor 必须核对完整模型 ID 集合和期望模型，而非只检查数量。
3. 根据实测结果，在目标部署未跟踪的 `backend/.env` 填写对应 `*_PROXY_URL` 和
   必要的 `*_NO_PROXY`。确认能直连且需要禁用继承代理时填写 `direct`；继续继承
   已验证的进程代理时留空并记录这一选择。未测试的 provider 不要盲目套用新代理。
   代理与绕过列表均不能提交到 Git；文件权限必须为 600，并保留原有其他配置。
4. 用实际 Settings 和 provider 环境构建路径加载文件，验证只有目标 provider 改变，
   会话覆盖符合优先级，内网和 loopback 绕过规则正确。较慢 Cursor 出口需按实测
   填写 `CURSOR_MODELS_PROBE_TIMEOUT_SECONDS`，并在该期限内重复探测。
5. 在空的、一次性工作目录执行最小无工具模型请求，核对成功完成和非空预期回复。
   模型目录可见不等于推理成功；对期望使用的模型分别记录验收，不能据一项成功
   宣称整个目录可用。测试失败时保留脱敏原因并修复配置，不能标记部署验收通过。
6. 生效前检查该实例的活跃会话/任务，按授权的部署重启流程应用配置；重启后再次
   核对 `/health`、SPA、Cursor 模型选项和实际请求。已有 Terminal 需重启其 agent，
   并验证登录 shell 不会把代理覆盖回旧值。

验收记录应包含部署位置、代码 SHA、CLI 版本、目标 provider/模型、直连/代理
选择、目录集合比较、超时/重复探测结果、最小请求及重启后检查结果；仅记录代理
已配置或脱敏标识，不记录真实代理地址、绕过域名或认证信息。本机与远端分别测试、
分别填写，远端配置不应改变本机配置。

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

### 反向代理信任（重要）

`X-Forwarded-For` / `X-Forwarded-Proto` 是普通 HTTP 头，任何直连客户端都可以
伪造。后端只在**直连对端**（TCP 连接的 peer）属于可信代理名单时才解析这两个
头；直连对端不可信时完全忽略它们，直接以直连 IP 判定客户端身份。因此未配置
飞书鉴权时，外部客户端无法靠发送 `X-Forwarded-For: 127.0.0.1` 绕过本机/内网
豁免。

```env
# 可信直连代理的 IP/CIDR 逗号分隔名单，默认只信任环回：
CLAUDE_HUB_TRUSTED_PROXIES=127.0.0.0/8,::1
```

- ngrok、cloudflared、frpc、本机 nginx 等从环回地址连入的隧道/反代，使用默认值
  即可，转发头会被正确识别。
- 反向代理位于其它地址（如 `10.0.0.5` 的 nginx）时必须显式加入，例如
  `CLAUDE_HUB_TRUSTED_PROXIES=127.0.0.0/8,::1,10.0.0.5`；只信任代理本身所在的
  地址/网段，不要填 `0.0.0.0/0`。
- 可信名单会参与 XFF 链解析：从右向左逐个剥掉可信代理跳，取最左侧的不可信地址
  作为客户端；格式错误的条目会被忽略。Cookie 的 `Secure` 语义只在直连对端可信
  时才接受 `X-Forwarded-Proto`（仅取第一个 token，且只接受 `http`/`https`）。
- 终端 WebSocket 会校验浏览器握手的 `Origin`：缺省要求 Origin 的 host[:port] 与
  请求 `Host` 一致。没有 Origin 头的原生客户端（ttyd、websocat、curl）不受影响。
  若页面由其它源提供，用逗号分隔的完整 Origin 或 `host[:port]` 白名单放行：
  `CLAUDE_HUB_ALLOWED_WS_ORIGINS=https://hub.example.com`。

### 未启用鉴权时的监听保护

未配置 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 时，API 对本机与内网来源跳过登录。
此时若把服务绑定到 `0.0.0.0`、`::` 或其它非环回/非链路本地地址，任何能访问该
端口的人都能直接操作终端，因此启动器和后端都会拒绝启动，并提示以下三种修复方式：

1. 配置飞书 OAuth（及可选白名单）；
2. 绑定环回地址（如 `--host 127.0.0.1`，由隧道从本机回连）；
3. 确认网络路径已自行隔离后，显式接受风险：
   `CLAUDE_HUB_ALLOW_INSECURE_LISTEN=1`。

环回（`127.0.0.1`、`::1`、`localhost`）与链路本地地址（`169.254.0.0/16`、
`fe80::/10`）不告警、可直接启动；链路本地与既有的内网鉴权豁免保持一致（它不
能跨网段路由）。

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

当 Nginx 与后端不在同一主机/环回上时，必须把 Nginx 的直连地址加入
`CLAUDE_HUB_TRUSTED_PROXIES`，后端才会采纳它设置的 `X-Forwarded-For` 与
`X-Forwarded-Proto`（详见上文“反向代理信任”）；否则会以 Nginx 地址当作客户端，
HTTPS 的 Cookie `Secure` 判断也会失效。请确保 Nginx 自身覆写而非追加这两个
转发头，避免客户端伪造值穿透到可信链路。

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
