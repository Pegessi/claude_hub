# Codex/TraeX Chat network access — fix loopback `Errno 1` (A2)

Date: 2026-09-25
Branch: `fix/codex-chat-network-access` (base `main` 71236cd)
Scope: structured **Chat** transports only (`agent_stream/native.py`); Terminal
launch paths are untouched. Local fix; no push/merge.

## Symptom

In a Hub structured Chat, a Codex (or TraeX) agent that needs to reach the
local Hub — e.g. `claude-hub schedule create --kind chat_turn …`, reading a
session, or any HTTP call to `127.0.0.1:<hub-port>` — fails inside the agent's
shell tool with:

```
OSError: [Errno 1] Operation not permitted
```

…even with solo mode on. Claude Chat is unaffected (it could always connect).

## Root cause (verified)

Stock Codex's `CodexNativeSession._thread_config()` / `_turn_config()` returned
`{}`. After the params merge, `thread/start` and `turn/start` therefore carried
**no** `sandboxPolicy` / `approvalPolicy` / `networkAccess`. The Hub solo/plan
toggles only wired Claude (`--dangerously-skip-permissions`) and TraeX
(`sandboxPolicy`); for stock Codex they were a no-op. Codex then fell back to
its built-in default sandbox = `workspace-write` with **network disabled**.

The codex app-server on macOS (0.156.1) installs a seatbelt profile whose base
policy is `deny default`; an outbound-network allow is appended only when
network is explicitly enabled. Under plain `workspace-write` — and even under
`approval=never` — there is no network allow, so **every** outbound TCP
`connect(2)` is denied, loopback included, returning `EPERM` (errno 1).

TraeX already sent a `sandboxPolicy` but it never carried `networkAccess`, so
its `workspace-write` tier had the same loopback ban.

## Empirical protocol findings (real codex app-server 0.156.1)

Driven over newline-delimited JSON-RPC stdio with an isolated `CODEX_HOME`
(`/tmp/codex-net-probe`, trusted cwd, copied auth) so the live `~/.codex` and
the live Hub were never touched.

### 1. `sandboxPolicy.networkAccess` is a strict **boolean** — no loopback tier

`thread/start` does **not** validate the policy (it accepts even a bogus
`type`); deserialization/validation happens at `turn/start`. Probing
`turn/start` serde:

- `sandboxPolicy.type` must be one of
  **`dangerFullAccess | readOnly | externalSandbox | workspaceWrite`**
  (`bogus` → `unknown variant … expected one of …`).
- `networkAccess` accepts **only booleans**:
  - `true` / `false` → accepted.
  - `"enabled"`, `"restricted"`, `"disabled"`, `"none"`, `"loopback"`,
    `"local"` → `invalid type: string …, expected a boolean`.
  - integer / map → rejected likewise.
- Unknown granular keys (`allowedDomains`, a nested `network` object,
  `writableRoots`) are **silently ignored** — there is no per-turn
  domain/loopback scoping channel.

So the binary's `restricted`/`enabled` network-level strings seen in its
symbols are **not** reachable through the per-turn `sandboxPolicy.networkAccess`
field; at this protocol surface the only knob is full-outbound (`true`) vs
none (`false`). **There is no loopback-only tier.**

### 2. Real-turn seatbelt behavior (actual model turns, real TCP listeners)

Listeners bound on `0.0.0.0`; the agent ran a self-identifying connect probe
to both `127.0.0.1:<port>` and the LAN IP `<port>`. Authoritative signal = the
listener's connection ledger (immune to the read-only file-write block).

| `sandboxPolicy` | approval | loopback | LAN IP |
| --- | --- | --- | --- |
| `{type: workspaceWrite}` (stock default) | never | **FAIL errno=1 EPERM** | **FAIL errno=1 EPERM** |
| `{type: dangerFullAccess}` (solo) | never | **OK** | **OK** |
| `{type: workspaceWrite, networkAccess: true}` | never | **OK** | **OK** |
| `{type: readOnly, networkAccess: true}` | n/a | no connect attempted | no connect attempted |

`readOnly` (plan): the model emits **no `commandExecution` item at all** —
shell commands do not execute, so there is structurally no egress path even
when `networkAccess: true` is present. The flag is therefore not attached to
plan.

Important: `networkAccess: true` lifts the ban for **full outbound**, not
loopback only — the LAN-IP connect succeeding alongside loopback proves it.

## Fix

`backend/claude_hub/services/agent_stream/native.py`

- Stock Codex now emits the same policy shape TraeX does, computed once and
  shared by both providers:
  - `plan`  → `approvalPolicy: on-request`, `sandboxPolicy.type: readOnly`
  - `solo`  → `approvalPolicy: never`,     `sandboxPolicy.type: dangerFullAccess`
  - default → `approvalPolicy: on-request`,`sandboxPolicy.type: workspaceWrite`
  - `_thread_config`/`_turn_config` both carry it (thread/start now also pins
    the policy for stock Codex instead of relying on codex's defaults).
- TraeX keeps its kebab-case **thread** field (`sandbox: read-only |
  workspace-write | danger-full-access`); its **turn** `sandboxPolicy` is now
  built by the shared helper so Codex/TraeX stay identical.
- `networkAccess: true` is attached **only** to the `workspaceWrite` tier and
  **only** when the explicit env opt-in `HUB_CHAT_ALLOW_NETWORK` is truthy
  (`1/true/yes/on/y`, case/space insensitive). It is never attached to
  `readOnly` (plan) or `dangerFullAccess` (solo — already full egress).

Helpers: `HUB_CHAT_NETWORK_ENV`, `_env_flag`, `_sandbox_plan_solo`,
`_sandbox_type`, `_network_access_enabled`, `_sandbox_policy`,
`_permission_config`.

## Security decision (why not default-open)

Because the engine exposes **no loopback-only tier**, enabling network in the
default `workspace-write` tier means granting **full outbound** access. We do
not silently open every Chat to the network:

- **Solo Chat (the managed-worker default and the reported failure):** fixed
  with **no new flag** via `dangerFullAccess`. This matches the pre-existing
  trust model — solo already maps to Claude `--dangerously-skip-permissions`
  and TraeX `danger-full-access`; stock Codex was the inconsistent outlier.
- **Non-solo Chat:** network stays **off by default** (status quo). Operators
  grant it deliberately per session/env-preset with
  `HUB_CHAT_ALLOW_NETWORK=1`, understanding it is full egress.
- **Plan:** never network-capable.

`HUB_RUNTIME_GUIDANCE` was intentionally not edited: the default behaviour for
a non-solo Chat is unchanged (network still off), and the only default change
is solo Codex aligning to the existing solo trust semantics.

## Verification

- Unit tests: `backend/tests/test_chat_network_access.py` — 45 cases covering
  env-flag parsing, the full plan/solo/default × flag matrix for **both**
  Codex and TraeX turn configs, thread config (incl. TraeX kebab vs camelCase),
  end-to-end `turn/start` params with/without the flag, and a Claude
  `_build_command` test proving Claude is byte-for-byte unaffected.
- Targeted suite: `pytest tests/test_chat_network_access.py
  test_agent_stream_native.py test_traex_agent.py
  test_provider_question_protocol.py test_chat_fork_seed_history.py`
  → **227 passed**.
- `black --check`, `isort --check-only`, `mypy` clean on `native.py` and the
  new test file.
- Production-factory end-to-end (drives `create_native_session`, the exact Hub
  code path, against real codex 0.156.1 + real listener):
  - solo (`dangerFullAccess`) → loopback **OK (Errno1 gone)**;
  - non-solo + `HUB_CHAT_ALLOW_NETWORK=1` → loopback **OK**;
  - non-solo no flag → loopback **still errno=1** (opt-in honored).
- Probe artifacts lived under `/tmp/codex-net-probe` with an isolated
  `CODEX_HOME`; the live Hub (8173), the live `~/.codex`, and the default tmux
  server were never touched. Probe app-servers self-terminate; a pre-existing
  Hub-managed codex `app-server` (started 14:18, reparented to launchd) was
  identified as not-ours and left running.

## Regression surface

- **Claude:** no code path changed; test pins identical `_build_command`.
- **plan/readOnly:** no shell execution, no network — unchanged security
  posture; now also aligned for stock Codex.
- **remote agents:** policy is built identically regardless of target; the
  remote codex seatbelt governs actual egress. Remote stdin/PTY launch is
  untouched.
- **default non-solo Codex:** now explicitly sends `approvalPolicy:
  on-request` + `workspaceWrite` on thread/start instead of leaving them
  implicit. Values equal codex's documented defaults, so behaviour is
  equivalent, but it is the one wire-shape change beyond solo; covered by the
  targeted suite.
- Out of scope (per task): B (per-command approval chain — no value for solo,
  version-fragile) and C (Hub stdio MCP tool).

## Follow-up (not in this branch)

A first-class UI/env-preset toggle labelled honestly ("allow full outbound
network for non-solo Chat agents") would surface `HUB_CHAT_ALLOW_NETWORK`;
today it is set via the session/env-preset environment like `CODEX_MODEL`.
