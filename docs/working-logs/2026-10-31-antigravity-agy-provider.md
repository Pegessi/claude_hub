# Antigravity (agy) provider integration — 2026-10-31

Branch: `codex/antigravity-cli-provider` (worktree `~/claude_hub_worktree/antigravity-cli-provider`).

## What was added

A new `antigravity` agent type that drives the installed Antigravity CLI
(`agy 1.4.x`) through the local `agyx` wrapper:

- **Spec** (`services/agent_stream/providers/spec.py`): CLI_ONESHOT /
  STDOUT_STREAM_JSON transport, binary `agyx`, resume
  `verified_flag=--conversation`, solo `bypass_flag`, static model catalog,
  no images/goals/quota/auth-endpoint (the wrapper owns network + login).
  Surfaces: direct Chat, subagent (`agent run`), terminal tab only —
  deliberately no managed/resident/scheduled/Dot and no remote.
- **Transport** (`providers/antigravity.py`): per-turn
  `agyx --input-format stream-json --output-format stream-json
  --disable-slash-commands [--dangerously-skip-permissions|--mode plan]
  [--model M] [--conversation <id>]`, stdin one NDJSON
  `{"event":"user","message":{"content":...}}`; conversation id captured from
  the top-level `init` record. `_build_provider_env` skips Hub's direct/proxy
  endpoint selector (unknown to it; agyx pins its own proxy + SSL_CERT_FILE).
- **Adapter** (`antigravity_events.py`): stdout-only (no transcript file,
  like MASO SSE). `step_update.step_type=agent_response|tool`, state
  ACTIVE/DONE; assistant text fragments arrive on BOTH states and are appended
  in order into the per-turn accumulator; the terminal `result.response` then
  reconciles to an empty suffix (no duplication). Tool ACTIVE→DONE pairs share
  `agy:{turn}:{step_index}`; `"Command failed"` maps to status failed; ERROR
  result maps to error + failed turn_completed.
- **Terminal TUI**: tmux/ttyd launch branch, `get_agent_command → agyx`,
  foreground idle command set (`agyx`/`agy` since the wrapper execs the real
  binary), live-env switch, IS_AGENT_TUI injection.
- **Frontend**: AgentType union, SessionCreator option/hint, avatar, model
  channel `ANTIGRAVITY_MODEL`, switch-policy gating, workspace status color.

## Bugs hit while wiring

1. `normalize_provider_usage` is exported from `models.agent_stream`, not the
   `models` package root.
2. agy nests step data: records are `{"event":"step_update","step_update":{…}}`
   (the fields are not top-level).
3. Assistant text is streamed as an ACTIVE prefix fragment plus a DONE suffix
   fragment — mapping only DONE silently dropped the prefix.
4. The generic network selector rejects providers without a configured
   endpoint; the session overrides `_build_provider_env` to skip selection.
5. `_build_command()` is also called with no args (capabilities binary probe);
   `provider_env` defaults to None.
6. Direct Chat delivery is `POST /api/workspaces/tabs/{id}/stream/send` with
   `{client_turn_id,text,delivery,attachments}` and the turn is driven/observed
   by `POST …/stream/wait` — the cross-tab `…/messages` inbox only enqueues and
   never starts a native direct Chat turn.

## Verification

- `tests/test_antigravity_native.py` (12): argv/resume/solo/plan/stdin/init
  capture, tool timeline, streaming fragment dedup, error mapping.
- Golden `provider_spec_matrix.json` regenerated (additive: antigravity only),
  byte-identical terminal-injection hash recomputed, frontend style golden
  updated. Backend targeted/remaining suites: ~5.9k passed; one pre-existing
  `test_maso_preflight` failure reproduces on clean main (live local MASO
  service on this host). Frontend typecheck clean, 1139 unit tests pass.
- Live: two-turn Chat through the native transport (init capture →
  `--conversation` resume recalls prior turn) and a full HTTP API smoke
  (create tab → stream/send → stream/wait) against the real Google backend,
  `status=completed` with the expected reply.

## Boundary

Local-host, personal-account use of the official CLI; not wrapped as a
multi-tenant provider, no API-key/ADC billing. Depends on the `agyx` wrapper
and its deployment egress proxy being available.
