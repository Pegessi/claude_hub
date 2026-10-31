# Agent CLI 安装指引

Claude Hub 通过第三方命令行工具（CLI）运行 Agent。本文列出需要单独安装的工具，
以及安装、登录和接入 Hub 的检查步骤；不涉及模型权重下载或桌面编辑器安装。

只安装你准备使用的 provider（Agent 提供方），不需要把所有 CLI 都装上。
`./setup.sh`、`uv sync` 和 `pnpm install` 不会安装或登录这些工具；
`claude-hub` 是 Hub 的管理客户端，也不能代替它们。

## 依赖清单

| Hub 中的 provider | 第三方工具 | Hub 实际调用的命令 | 安装后检查 |
| --- | --- | --- | --- |
| `claude` | Anthropic Claude Code | `claude` | `claude --version` |
| `codex` | OpenAI Codex CLI | `codex` | `codex --version` |
| `cursor` | Cursor Agent CLI | `agent` | `agent --version` |
| `antigravity` | Google Antigravity CLI | `agyx`，由部署包装官方 `agy` | `agy --version`、`command -v agyx` |

命令名以仓库的 [ProviderSpec 注册表](../backend/claude_hub/services/agent_stream/providers/spec.py)
为准。尤其要区分 Cursor 的桌面命令 `cursor` 与 Agent CLI，以及 Antigravity 的
官方命令 `agy` 与部署包装程序 `agyx`。

其他入口：TraeX 需要部署方另行提供 `traex`，请使用所在环境的安装说明，不要从
同名第三方包猜测安装来源。MASO 使用独立 HTTP 服务，见 [MASO 配置](MASO.md)，
不依赖本页四个 CLI。普通 Shell Terminal 不要求安装 Agent CLI。

## 安装前准备

- 以下命令面向 Linux/macOS 的 Bash 或 Zsh。CLI 的具体系统版本、架构和账号资格
  要求以各自官方文档为准；本文不声明 Hub 在原生 Windows 上的支持情况。
- 在**实际执行 Agent 的机器上，以运行 Agent 的系统账号**安装和登录。本地 Chat
  通常就是 Hub 后端所在机器及其运行账号；SSH Terminal 则在远端准备。只在打开
  Hub 网页的电脑上安装，不能满足服务器的依赖。
- 安装器需要 `curl` 和联网条件。先确认官方地址及脚本内容符合部署安全要求，再
  执行下面的下载脚本；已有可用安装时先检查版本，不要重复安装或覆盖私有包装。
- CLI 登录、模型访问权限及计费由各提供方管理。Hub 的飞书登录不授予模型权限。
  不要把 API key、登录链接、认证文件或完整账号信息写入仓库、截图或共享日志。

用户级安装常放在 `~/.local/bin`。若安装器提示 PATH 未生效，可在当前 shell 中设置：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

同时让 **Hub 服务的启动环境**包含对应目录。只修改交互 shell 的 `.bashrc`、
定义 alias，或在另一个终端执行 `export`，不能保证已运行的后端能找到命令。
使用 npm/Homebrew 安装时，应确认它们实际使用的 bin 目录也在服务 PATH 中。
需要调整已运行服务时，按[部署流程](DEPLOYMENT.md)安排授权重启，不要为检查安装
直接中断共享服务。

## 1. Claude Code

官方说明：[安装与系统要求](https://docs.anthropic.com/en/docs/claude-code/setup)、
[认证](https://docs.anthropic.com/en/docs/claude-code/authentication)。

```bash
# 官方原生安装器（Linux/macOS）
curl -fsSL https://claude.ai/install.sh | bash

claude --version
claude auth login
claude auth status
```

也可以直接运行 `claude`，按首次启动提示完成浏览器登录和初始化。使用具有相应
权限的 Claude 账号或 Claude Console 账号；API key、Bedrock、Vertex 等认证方式
按官方认证文档配置。只安装 Claude Desktop 不能代替这里的 `claude` 可执行文件。

如果所在环境统一用 npm 管理，也可选择 `npm install -g @anthropic-ai/claude-code`
替代原生安装器；该方式需要 Node.js 18+，不要与原生安装混用或使用 `sudo npm install`。

## 2. OpenAI Codex CLI

官方说明：[Codex CLI 安装](https://developers.openai.com/codex/cli)、
[认证](https://developers.openai.com/codex/auth)。

```bash
# 官方独立安装器（Linux/macOS）
curl -fsSL https://chatgpt.com/codex/install.sh | sh

codex --version
codex login
codex login status
```

也可选择 `npm install -g @openai/codex` 或 macOS 的 `brew install --cask codex`
替代独立安装器。只选一种方式，避免 PATH 指向旧版本。

首次运行 `codex` 也会提示登录，可使用有权限的 ChatGPT 账号或 API key。
无浏览器服务器可按官方认证文档使用 `codex login --device-auth`；该方式需要账号
允许设备码登录。若使用已通过安全方式注入环境的 API key，可通过标准输入登录，
不要把密钥字面值放进命令参数或 shell 历史：

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

Hub Chat 使用 `codex app-server`，因此安装后还应确认 `codex app-server --help`
可用；只安装 Codex 网页端或 IDE 扩展不满足该要求。

## 3. Cursor Agent CLI

官方说明：[CLI 安装](https://cursor.com/docs/cli/installation)、
[CLI 认证](https://cursor.com/docs/cli/reference/authentication)。

```bash
curl https://cursor.com/install -fsS | bash

agent --version
agent login
agent status
```

浏览器流程使用 Cursor 账号登录。自动化环境也可使用官方 CLI 认证文档中的
`CURSOR_API_KEY`，由部署方安全注入，避免使用带明文密钥的 `--api-key` 参数。
Cursor 编辑器中配置模型供应商 API key 的功能，与 CLI 登录配置不同。

部分官方旧文档使用 `cursor-agent` 命令名；**当前 Hub 固定调用 `agent`**。
如果 `cursor-agent --version` 成功但 `agent --version` 失败，先按官方安装说明
核对版本和 PATH。确需兼容旧安装时，由部署方提供指向 Cursor Agent 的真实
`agent` 符号链接或可执行包装；不要覆盖其他同名程序，也不要只定义 shell alias。
`cursor --version` 只能检查桌面编辑器命令，不能作为 Agent CLI 的安装证据。

## 4. Google Antigravity CLI

官方说明：[CLI 入门与安装](https://antigravity.google/docs/cli/getting-started)、
[CLI 命令参考](https://antigravity.google/docs/cli/reference)。

### 安装官方 `agy`

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash

agy --version
agy --help
```

官方安装器默认把 Linux/macOS 的可执行文件放在 `~/.local/bin/agy`。在准备使用的
工作目录运行 `agy`，按交互界面完成首次设置、Google 账号登录及工作目录信任。
已有专用 `agyx` 包装的部署，应通过 `agyx` 执行这些交互步骤，见下一节。
当前 Hub 接入按 Google 账号登录方式使用，不提供 API key 或 Application Default
Credentials（ADC，Google 应用默认凭据）计费接入；不要把其他 Gemini CLI 的安装
或认证命令套用到这里。接入时核对的 `agy 1.4.11` 帮助中没有 `login`/`auth` 子命令，
登录通过交互界面完成，不需要执行 `agy login`。

### 为 Hub 准备 `agyx`

**当前 Hub 的 Chat 和 Terminal 都调用 `agyx`，只安装 `agy` 还不够。**
`agyx` 是部署维护的包装程序，不是 Google 官方命令，也没有随本仓库分发。
已有部署的包装程序可能负责网络代理、传输层安全（TLS）证书和登录所需环境，应保留并由部署方
维护；不要覆盖它或复制另一台机器的私有代理和认证配置。若部署依赖该包装提供的
网络环境，首次登录和后续检查也应通过 `agyx` 进行，而非绕过包装直接运行 `agy`。

如果是在新环境安装，且已确认直接运行 `agy` 能正常登录和使用，不需要额外网络
配置，可以在**没有现有 `agyx` 文件或命令**的前提下，新建 `~/.local/bin/agyx`，
使用最小转发脚本：

```sh
#!/bin/sh
exec agy "$@"
```

```bash
chmod 700 "$HOME/.local/bin/agyx"
command -v agyx
agyx --version
```

这只是适配 Hub 所需的命令名，不会安装代理、修复证书或替你登录。如果 `agy`
本身还不可用，应先解决安装、账号或网络问题。自定义包装必须原样转发参数、
标准输入输出及退出状态，不向 stdout 打印额外提示，因为 Hub 需要读取 JSON 流。

当前支持范围为本机直接 Chat、一次性 subagent 调用（`claude-hub agent run`）、
Dot 和普通 Terminal tab；不支持 Workspace 管理的 Agent/任务、Resident、通用
定时任务或远程 SSH 执行。Dot 自身的唤醒和整理计划随其 Chat 一起运行。
安装成功不会改变这些限制。集成细节见
[Antigravity 接入记录](working-logs/2026-10-31-antigravity-agy-provider.md) 和
[Dot 后端接入记录](working-logs/2026-10-31-dot-antigravity-backend.md)。

## 代理与证书配置提示

**是否需要代理，要在安装时按目标机器、按 provider 分别检查。** 代理是可选的环境
配置。同一个 provider 在当前机器可能需要代理，在另一台机器可能可以直连；同一台
机器上的不同 provider 也可能使用不同网络配置。不要照搬本文接入记录或其他机器的
代理设置，也不要因为某个 provider 需要代理，就给所有 provider 配置同一个代理。

首次安装、迁移机器或更换网络出口时：

1. 使用实际运行 Agent 的账号和服务环境，核对已有进程代理、CLI 用户配置及部署
   网络要求；保留已验证的配置，不为检查而修改共享服务。
2. 在网络策略允许的前提下，分别验证各 provider 的直连登录、模型查询（CLI 支持时）
   和获准后的最小模型请求。注意继承的代理，不能把经代理成功的请求记为直连成功。
   **直连正常就不需要额外配置代理**；已有强制代理要求的环境按其规定验证。
3. 只有确认该机器访问某个 provider 需要代理时，才为它选择并验证代理及证书配置。
   网络失败时先区分连通性、账号权限和额度问题，不把所有失败都归因于缺少代理。
4. 为每个 provider 记录“直连 / 使用代理 / 未验证”及检查结果。其他机器重新检查，
   不把本机结论当成通用安装要求。

CLI 安装成功不代表网络验证通过。下面只列配置入口，不提供任何部署的真实地址、
端口、账号或证书内容。

| Provider | Hub 的部署配置入口 | 需要特别注意 |
| --- | --- | --- |
| Claude Code | `CLAUDE_PROXY_URL`、`CLAUDE_NO_PROXY` | 本地 Terminal 和原生 Chat 均支持部署默认值；同时核对 CLI 用户配置中是否还有旧代理 |
| Codex | `CODEX_PROXY_URL`、`CODEX_NO_PROXY` | 已运行的 `app-server` 不会因修改外部环境自动更新；应用配置后使用新会话检查 |
| Cursor | `CURSOR_PROXY_URL`、`CURSOR_NO_PROXY` | 模型目录探测也使用部署默认值；慢出口可按实测调整 `CURSOR_MODELS_PROBE_TIMEOUT_SECONDS`（默认 5 秒） |
| Antigravity | 部署方维护的 `agyx` 及其私有配置 | Hub 要求 `agyx` 命令；是否配置代理和 `SSL_CERT_FILE` 由目标机器的检查结果决定，没有 `ANTIGRAVITY_PROXY_URL` 配置 |

### Claude、Codex、Cursor 的代理

- 确认需要指定代理时，把真实 `*_PROXY_URL` 值写入部署中 **未跟踪的 `backend/.env`**，
  权限设为 `600`。支持 HTTP(S) CONNECT 代理 URL；留空/未设置表示继承进程代理，
  `direct` 表示清除继承代理。没有继承代理且直连已验证时，可以不填代理字段。
  不要把留空误认为强制直连。已有文件只修改所需字段，不要整份覆盖。
- `*_NO_PROXY` 未设置时继承进程 `NO_PROXY`，显式空值会清空绕过列表。根据部署
  保留 loopback 和实际内网目标的绕过规则，避免把本机 Hub 或内部服务送到外部代理。
- 会话/preset 的显式代理优先于部署默认值，部署默认值优先于进程通用代理。
  **原生 Chat 的 `auto` 网络模式仍可能选择直连**；需要沿用已验证的代理环境时，
  在对应 Chat/preset 环境中设置 `CLAUDE_HUB_PROVIDER_NETWORK_MODE=inherit`，
  并检查 `NO_PROXY`，不要以为填了代理 URL 就一定经过代理。
- `backend/.env` 中的这些字段由 Hub 读取，手工运行 `claude auth login`、
  `codex login`、`agent login` 不会自动读取它们。登录测试也要使用部署方提供的
  相同代理/证书环境或受保护的启动包装，不要把私有配置粘贴进命令行。
- 部署默认值在 Hub 启动时读取；修改后按授权部署流程重启，并检查新建的 Chat
  和 Terminal。远程 SSH Terminal 不继承本机部署代理，需在远端单独准备。

### Antigravity 的专用包装与证书

接入记录中的部署使用 `agyx` 连接专用本地代理和上游出口，并指定 `SSL_CERT_FILE`。
这是该机器的网络配置，不是 Antigravity 在所有机器上的安装要求。其他机器如果
直接运行 `agy` 已验证可用，`agyx` 可以仅使用上文的最小转发脚本，无需额外代理。

只有确认目标机器也需要专用代理时，才检查本地代理是否可达、上游出口是否可用、
证书文件是否存在且服务账号可读。不要直接复制原机器的包装配置；仅复制包装文件
或创建同名 alias，也不能迁移代理和证书依赖。Hub 不负责安装或修复这些可选依赖。

Antigravity 跳过 Hub 通用的直连/代理选择，不能用 `CLAUDE_HUB_PROVIDER_NETWORK_MODE`
或其他 provider 的 `*_PROXY_URL` 替代 `agyx` 配置。需要专用代理的部署不能使用
上文仅转发 `agy` 的最小脚本；该脚本只适用于已验证直接调用可用的环境。

如果代理使用私有受信任根证书，应按 CLI 文档配置证书文件，而不是关闭 TLS 验证。
例如 Codex 支持 `CODEX_CA_CERTIFICATE`，未设置时回退到 `SSL_CERT_FILE`，见
[Codex 认证与证书说明](https://developers.openai.com/codex/auth)。各 CLI 的证书配置
并不通用；浏览器能够登录，也不证明后端进程信任同一证书。

完整配置语义与验收步骤见[每个 provider 的私有代理](DEPLOYMENT.md#每个-provider-的私有代理)。
记录“直连 / 使用代理 / 未验证”、CLI 版本及脱敏错误，不公开真实代理地址或认证信息。

## 接入 Hub 前的检查

只对已选择的 provider 执行对应检查，不要求安装所有工具。

1. **命令可见**：在与 Hub 启动方式一致的账号和环境中执行 `command -v claude`、
   `command -v codex`、`command -v agent` 或 `command -v agyx`。Hub 后端需要能从
   PATH 找到真正的可执行文件；一个交互 shell 中的 alias/function 不够。
2. **版本及协议可用**：检查 `--version` 和 `--help`。Claude/Cursor 的帮助应有
   `--print`、`--output-format`；Codex 应有 `app-server`；Antigravity 应有
   `--input-format`、`--output-format` 的 `stream-json` 模式。CLI 升级后重新检查，
   不把“版本较新”直接当作 Hub 兼容性保证。
3. **认证有效**：用上述状态命令或交互界面确认登录。验证所用账号与服务运行账号
   一致；若会话指定了不同 HOME、配置目录或环境 preset，也要检查对应配置。
4. **实际请求可用**：确认允许消耗模型额度后，在空的测试目录创建该 provider 的
   Hub Chat（Antigravity 使用本机直接 Chat），发送“只回复 OK，不调用工具”，确认
   有非空回复且回合正常结束。对需要使用 Terminal 的 provider，另行检查 Terminal
   启动及交互。此步骤会调用模型，前三步不能替代它。

常见问题：

| 现象 | 优先检查 |
| --- | --- |
| `provider binary not found on PATH` / `command not found` | 安装位置、运行账号、服务启动 PATH；Cursor 要找 `agent`，Antigravity 要找 `agyx` |
| 终端可用，Hub 不可用 | 后端与交互 shell 的 PATH、HOME、认证目录及环境 preset 是否一致 |
| 需要登录、401/403、没有可用模型 | 对应 CLI 的登录状态、账号资格、权限和额度；不要只重新安装 |
| 网络超时或 TLS 错误 | 部署的网络出口、代理和受信任证书；不要通过关闭 TLS 验证解决 |
| CLI 能回复，但 Hub 无法解析或恢复会话 | CLI 版本和协议参数是否匹配；包装程序是否污染 stdout；记录脱敏错误后排查 |

本文安装来源于 2026-10-31 核对的官方文档；命令名和 Hub 支持范围以当前仓库实现
为准。官方安装器会更新，部署时请再次核对官方说明，并记录实际 CLI 版本及验证结果。
