# 2026-06-29 - Claude Hub CLI full-capability gap analysis

## Bottom line

The current `claude-hub` CLI does **not** have every Claude Hub capability in a
typed, first-class command. It is close for backend REST workflows because it
has typed groups for the major REST domains plus `api raw METHOD PATH` as an
HTTP JSON/query escape hatch. `api raw` is not equivalent coverage for binary
downloads, multipart uploads, WebSockets, redirect/header-preserving auth
flows, or streaming. It is not full product coverage for three important
classes:

1. WebSocket terminal live IO: attach/input/resize is browser/WebSocket-only.
2. Hub-hosted Feishu/Hermes runtime: the CLI only builds/parses card JSON; it
   does not send cards, receive callbacks, persist correlation, or poll results.
3. Frontend-local state: theme, app mode, split layout, active workspace, report
   language, and custom env presets live in browser `localStorage`, not backend
   REST.

So the accurate claim is: **the CLI covers most backend REST control-plane
capabilities, but it does not cover all Claude Hub product capabilities.**

## Coverage matrix

| Domain | Typed CLI coverage | `api raw` coverage | True gaps / limitations | Missing-piece class |
| --- | --- | --- | --- | --- |
| Auth / system | `auth check`, `auth me`, `auth logout`, `auth login-url`, `auth callback`; `system network-access`; global `--token`, `--cookie`, `--base-url`, config-file resolution. | Can call simple JSON/query `/api/auth/*` and `/api/system/*` routes. | No browser auth automation, OAuth browser opening, cookie capture, redirect/header-preserving login flow, or typed CLI config CRUD. Login/callback commands expose redirects but do not complete an interactive browser session. | Browser-only flow for cookie capture; CLI-local config limitation; no backend config CRUD API. |
| Terminal / tabs | `tab list/status/create/get/update/delete/duplicate/order`; `terminal history`; `terminal proxy-url`; tab create/update support local/remote targets, env, cwd, solo mode. | Can call JSON/query tab and terminal history/proxy routes. | No terminal attach, stdin injection, resize, or live output streaming. `api raw` cannot open `/api/terminal/ws/{tab_id}` or ttyd proxy WebSockets. A future attach command would still not duplicate browser ttyd iframe, replay, theme, or split-layout UI parity. | WebSocket gap for live IO; frontend-local/UI parity gap for browser terminal experience. |
| Local filesystem | `filesystem list`, `filesystem home`, alias `fs`; `clipboard image` for clipboard upload. | Can call JSON/query filesystem routes. | Filesystem support is directory/home browsing, not full file CRUD. Clipboard image is typed, but generic raw does not provide multipart upload coverage. | Backend REST limitation if full file CRUD is desired; typed convenience gap for non-JSON uploads beyond existing command. |
| Remote filesystem / env | `remote profiles`, `remote list`; workspace/agent/tab commands can set remote profile, remote cwd, reconnect, and launch env. | Can call JSON/query `/api/remote/*` routes. | Remote profile CRUD is not exposed as typed CLI and appears read/discovery oriented. Custom env preset management is frontend `localStorage`, so the CLI cannot list or reuse browser-defined presets. | Backend REST limitation for remote profile CRUD; frontend-local state for env presets. |
| Workspace orchestration | `workspace list/create/update/board/status/dispatch/artifact-preview/attachment-get`. | Can call JSON/query workspace routes, including future REST routes not yet typed. | No typed delete workspace command if that becomes a product need. Artifact preview is typed; attachment binary download is typed through `workspace attachment-get` and needs attachment id only. | Typed convenience gap; backend REST gap only if delete/config APIs are absent. |
| Tasks | `task list/get/report/review/create/start/continue/update/accept/send/abort/request-review/delete/spawn/dispatch-decision`; `task feedback reap`; task modes include `direct`, `reviewed`, `autonomous`. | Can call JSON/query task routes and pass raw JSON bodies for fields not promoted to flags. | Attachments require repeated `--attachment-json`; there is no path-to-attachment convenience for task create/update/continue/send. | Typed convenience gap over existing backend REST. |
| Agents / sessions | `agent list/create`; `session list/logs/send/delete/report`. Reports include typed bilingual fields and raw payload support for `goal_packet`, `acceptance_check`, `artifact_refs`, and other structured fields. | Can call JSON/query session/agent REST endpoints. | `session logs` is terminal-history polling, not live attach. `session send` attachments have the same JSON-only convenience gap. | WebSocket gap for live session terminal; typed convenience gap for attachment paths. |
| Review / autonomous mode | Task create/update expose task mode, execution complexity, review profiles, request review, review timeline, human accept, spawn, dispatch decision, feedback reap; session reports can submit review states and structured review payloads. | Can call JSON/query review/autonomous REST routes and pass detailed JSON. | No dedicated high-level "review loop runner" CLI; autonomous behavior is backend/orchestrator driven. This is mostly covered as control-plane operations, not as an evaluator runtime. | Typed workflow convenience gap; backend/orchestrator runtime remains server-side. |
| Reports / artifacts / attachments | `session report`, `task report`, `task get --reports`, `workspace artifact-preview`, `workspace attachment-get`. | Can call JSON/query report and artifact preview routes. Attachment binary download is covered by typed `workspace attachment-get`, not generic `api raw`. | Attachment upload is JSON payload only in task/session commands; no `--attachment-file path --mime type` helper. Attachment download needs attachment id, not a path or task-scoped selector. | Typed convenience gap over existing backend REST. |
| Lessons / feedback | `lessons list/get/create/summarize/delete`; `task feedback reap`. | Can call JSON/query lesson and feedback REST routes. | No obvious coverage gap for current REST surface. Higher-level lesson curation policies remain backend/workflow behavior. | No current CLI gap beyond workflow convenience. |
| Feishu / Hermes collaboration | `feishu build-card` and `feishu parse-action`; card kinds include approval, needs input, plan confirm, status/task/workspaces/overview/agents/reports/terminal/lessons/tabs/tab status/network/filesystem/remote profiles/remote filesystem/result/action catalog. | `api raw` has no extra Feishu value because there are no Hub Feishu REST endpoints in this scenario. | No Hub-hosted Feishu bot, sender, callback receiver, token/result store, chat binding, long poll, or Hermes job integration. The external agent owns Feishu sending, callback handling, and correlation. | Feishu/Hermes runtime gap; possible backend REST gap only if Hub should become the bot. |
| Frontend-only UI state | None except backend-backed concepts exposed elsewhere, such as tabs/workspaces. | Cannot access browser `localStorage`. | App mode, theme, split layout, active workspace, report language, floating status panel preferences, and custom env preset visibility/editing are browser-local. | Frontend-local state; requires backend persistence APIs before CLI can cover it. |

## Priority recommendations

1. **Do not market the CLI as "all Claude Hub capabilities."** Use the narrower
   wording: "typed coverage for most backend REST control-plane workflows, plus
   `api raw` for HTTP JSON endpoints."
2. **Add a WebSocket terminal command next if full remote operation matters.**
   A useful first slice would be `terminal attach <tab-id>` with live output,
   stdin passthrough, and resize propagation. This closes the largest product
   capability gap for CLI operability, but it would not provide browser
   ttyd/iframe, replay, theme, or split-layout UI parity.
3. **Add attachment path helpers before adding more endpoint wrappers.** The
   backend already accepts attachment objects, but operators need
   `--attachment-file`, MIME inference/override, and consistent support across
   task create/update/continue/send and session send.
4. **Decide whether Hub should own Feishu/Hermes runtime or keep Scenario A.**
   If Scenario A is intentional, document the boundary clearly. If the product
   needs Hub-hosted collaboration, add backend APIs/runtime for send, callback,
   correlation/result storage, and then add typed CLI commands over those APIs.
5. **Only add CLI coverage for theme/layout/env presets after backend
   persistence exists.** Today those values are browser-local, so a CLI command
   would either be misleading or would need to edit browser storage out of band.
6. **Add typed config commands as local CLI ergonomics, not product coverage.**
   `config get/set/unset` would reduce setup friction, but it does not close a
   Claude Hub backend capability gap.

## Evidence checked

- `git -C /Users/bytedance/claude_hub-cli-feishu-collab status --short --branch`
  confirmed branch `feat/cli-feishu-collab-85b2b765`.
- `uv run claude-hub --help` confirmed typed groups: `auth`, `system`, `tab`,
  `terminal`, `filesystem`/`fs`, `remote`, `clipboard`, `workspace`, `agent`,
  `task`, `session`, `lessons`, `feishu`, and `api`.
- `uv run claude-hub terminal --help`, `feishu --help`, `task --help`, and
  `session --help` confirmed no live terminal attach command and only two
  Feishu helper commands.
- CLI source checked: `backend/claude_hub/cli/main.py`,
  `backend/claude_hub/cli/client.py`, `backend/claude_hub/cli/commands/rest.py`,
  `backend/claude_hub/cli/commands/workspaces.py`,
  `backend/claude_hub/cli/commands/tasks.py`,
  `backend/claude_hub/cli/commands/sessions.py`,
  `backend/claude_hub/cli/commands/lessons.py`,
  `backend/claude_hub/cli/commands/feishu.py`,
  `backend/claude_hub/cli/feishu_cards.py`.
- Backend route inventory checked with
  `rg -n "@router\\.(get|post|put|patch|delete)|websocket|WebSocket|APIRouter" backend/claude_hub/api/*.py`.
- Frontend-local state checked with
  `rg -n "localStorage|theme|layout|mode|preset|remoteProfiles|env" frontend/src`.
- Historical design notes checked:
  `docs/working-logs/2026-06-15-claude-hub-cli.md` and
  `docs/working-logs/2026-06-16-feishu-card-cli.md`.
