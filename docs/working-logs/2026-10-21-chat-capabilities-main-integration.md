# 历史优先加载与能力缓存的主分支集成

## 固定版本与授权

- 用户授权合入本地 `main`，未要求推送、部署或重启服务。
- 合入前本地 `main`：`bac48c3bd6c42ba8ecc02f55855cb54cc1ab26ab`。
- 已验证功能候选：`1c947fad333235d99937f815fa4755a85290ec74`。
- 共同基线和本次 fetch 后的 `origin/main`：`34dc7c8d1c1c05540af2cceb2012e71c1471670f`。
- 固定合并：`92b07ff4e6a14ae3d976d3f6c118d2c50dac5bd6`。
- 专用 worktree：`~/claude_hub_worktree/chat-capabilities-main-1c947fa`。

本地 main 包含远端引用，已有未推送提交全部保留。本次只将验证后的集成结果快进到本地 main。

## 合并处理与审查

只有 `CHANGELOG.md` 存在文本冲突，保留双方的全部条目，将本次功能条目放在最前。业务代码使用 Git 的自动合并结果，没有额外改写。

独立集成审查检查了双方都修改的六个文件及必要调用链：更新日志、`api/agent_stream.py`、原生执行测试、`StructuredPane.vue` 和两个浏览器测试文件。未发现集成缺陷或阻塞问题。

核对结果包括：

- 主分支的持久消息收件箱和消息路由仍完整保留；能力缓存参数和持久历史参数正常传递。
- `StructuredPane` 只有一个 `currentTab` 定义，消息收件箱、历史就绪状态和配置变化处理同时保留。
- 能力观察相关的单方模块与功能父提交完全相同；消息收件箱相关的单方模块与主分支父提交完全相同。
- 测试没有重复定义，浏览器请求处理条件未相互遮挡。

固定合并的冲突处理记录见同目录的 `2026-10-21-chat-capabilities-merge-decisions.json`。该记录只证明所列处理与固定合并一致，不能代替语义审查。

## 验证

以下检查针对固定合并 `92b07ff` 执行，使用独立运行目录和锁定依赖：

| 检查 | 结果 |
| --- | --- |
| `verify.sh all` | 通过 |
| 后端测试 | 5,012 通过，9 跳过 |
| 前端测试 | 1,117 通过 |
| 文档、格式、类型、lint、构建、checkout safety | 通过 |
| `verify.sh browser-mocks` | 14 个场景全部通过 |
| `verify.sh terminal-hmr` | 12 项通过 |
| 固定合并决策检查 | 通过，唯一处理路径为 CHANGELOG.md |

浏览器测试使用模拟 API；没有调用真实执行后端。专用 Vite 进程正常退出，端口已关闭。测试框架未给出整个子进程树退出的证明。

证据目录：
`/home/tiger/claude_hub_workspace/chat-capabilities-cache/integration-validation/`。

主要日志为 `verify-all.log`、`verify-browser.log`、`verify-terminal.log`、`verify-merge-decisions.log`，浏览器明细位于 `claude-hub-browser-mocks.fD4Jd7/run/summary.json`。

固定合并之后只补充本记录和冲突处理记录，不改变已验证业务代码。补充文档另行执行文档和暂存区检查。主分支快进前再次核对其 SHA 与工作区状态；不推送，不重启共享服务。
