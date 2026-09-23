# Chat sub-agent card (frontend MVP) — 2026-09-23

Structured Chat rendered every provider tool call as one generic collapsible
`tool-card`. When the model delegated work to a sub-agent (Claude Code `Agent`,
Cursor `Task`, TraeX `spawnAgent`), that spawn showed up as another collapsed
tool row indistinguishable from `Bash`/`Read`. This change renders confirmed
sub-agent spawns as dedicated Codex-style cards. Frontend-only: the backend
normalized stream protocol (`tool_call_started`/`tool_call_completed`) is
unchanged and the sub-agent's *internal* activity is not streamed.

## Real-event evidence (the rules come from data, not guessed names)

Tallied every `tool_call_started` payload across the 129 persisted
`~/.claude_hub/workspaces/**/agent_streams/*.jsonl` files and read the args +
the matching `tool_call_completed`:

| provider | sub-agent tool | n | identifying args | result shape |
| --- | --- | --- | --- | --- |
| Claude Code (`claude`) | `Agent` | 63 | `description`, `prompt`, `subagent_type` (snake_case); optional `run_in_background`, `model` | string (the child's report) |
| Cursor CLI (`cursor`) | `Task` | 5 | `description`, `prompt`, **`subagentType`**, **`agentId`** (camelCase), `mode`, … | dict `{success:{agentId,isBackground,durationMs,…}}` |
| TraeX (`traex`) | `spawnAgent` | 32 | `prompt` + **`receiverThreadIds: string[]`**; no type/description | stringified `{threadId:{status,message}}` |
| Codex | — | 0 | no sub-agent tool appears in captured history (`apply_patch`/`exec_command`/`web_search` are the tool set) | — |

The brief guessed Claude's tool would be `Task`; real data shows Claude Code now
uses **`Agent`**, and `Task` is Cursor's tool. TraeX lifecycle helper
`closeAgent` (also `receiverThreadIds`) is deliberately **not** a spawn.

## Conservative matching (`frontend/src/utils/subagentTool.ts`)

`parseSubagent(name, args)` requires BOTH the exact name AND that provider's
argument signature:

- `Agent` needs a non-empty `prompt` and (`subagent_type` **or** `description`).
- `Task` needs a non-empty `prompt` and (`subagentType` **or** `agentId`) — the
  camelCase fields distinguish it from a hypothetical same-named tool.
- `spawnAgent` needs a non-empty `prompt` and a non-empty `receiverThreadIds`.

A name alone never matches; missing/non-object args and same-stem task tools
(`TaskCreate`/`TaskUpdate`/`TaskOutput`/`TaskStop`, `closeAgent`) fall back to
the ordinary tool card. `isSubagentTool` is the boolean wrapper.

## Rendering / data flow

- `agentStreamTimeline.ts` parses the sub-agent view at the `tool_call_started`
  event (the only event carrying args) and stores it on `TimelineTool.subagent`.
  A sub-agent is pushed as its own `part.kind: 'subagent'` instead of into the
  current `tool_group`, so it never merges with neighbours and naturally splits
  the ordinary groups before/after it. The completed event still mutates the
  same `TimelineTool`, so status/result flow into the card.
- `subagent` is treated as a process part (`isProcessPart`,
  `countProcessSteps`), so it folds with the working region and counts as one
  action in the folded label.
- `StructuredPane.vue` renders `.subagent-card`: identity chip
  (`Claude/Cursor/TraeX 子代理`), sub-agent type (or a TraeX `线程 <id8>`
  target), the `description` (TraeX falls back to the prompt's first line), a
  localized running/completed/failed/cancelled badge reusing the existing
  `tool-status` color modifiers, and an expandable curated prompt + result. A
  `border-left: 2px solid var(--ch-color-accent)` plus indentation give the
  nested feel; all colors/radii reuse existing `--ch-*` tokens.

## Compatibility

- Ordinary tool grouping/collapse, edit-resend, fork (replays normalized
  events, not part kinds), approval cards (`Ask*` excluded as before), and
  images are untouched. The card body renders only the curated prompt/result
  projections — never the raw args blob or `receiverThreadIds` — so a
  whole-turn copy cannot surface internal JSON.
- A same-named `Agent`/`Task`/`spawnAgent` call that lacks the signature stays
  in `tool_group` (`subagent === undefined`), guaranteeing fail-closed.

## Validation (local, this machine)

- `pnpm exec vue-tsc --noEmit` → 0 errors.
- `pnpm lint:check` → 0 errors / 0 warnings.
- `node --test tests/*.test.mjs` → 477 tests, 474 pass, 3 fail. The 3 failures
  are the pre-existing, environment-only `tests/forkFromTurn.test.mjs`
  (`localStorage.getItem is not a function` under Node 25), failing on main
  independent of this change.
- `pnpm build` → success (188 modules; the >500 kB chunk-size notice predates
  this change).
- New: `tests/subagentTool.test.mjs` (7), reducer coverage appended to
  `agentStreamTimelineReducer.test.mjs` (4), `structuredPaneSubagentCard.test.mjs`
  (7). The three timeline-transpiling test loaders now also concatenate the
  dependency-free `subagentTool.ts`.

## Follow-up (explicitly out of MVP scope)

Stream the sub-agent's internal thinking/tool events. That needs a backend
protocol addition (child-call events correlated to the parent `tool_call_id`,
e.g. a `parent_call_id` + nesting depth), after which the card could gain a live
indented inner timeline instead of just prompt/result.
