# Dot proactive interest radar + two-stage scout funnel (MVP)

Date: 2026-11-22
Task: `3f01f8bc-0485-48a4-b625-232dbac282f2`
Branch: `feat/deepseek-mvp-3f01f8bc`

## What was built

A self-contained proactive scouting pipeline for Dot that mirrors the
"Jarvis-style" high-signal, rarely-interrupts pattern: dynamically derive the
user's evolving technical interests, fan a cheap model across primary public
sources, then spend a strong model only on the few high-potential leads, and
speak (Feishu) only past a high Speak-Bar threshold.

### New module: `backend/claude_hub/services/dot/scouting.py`

- **State on demand, not in resident memory.** All durable state lives in
  `<dot home>/scouting.json` (atomic writes, in-process lock): the evolving
  `profile`, a `seen_urls` ledger (canonical-URL SHA-256), a `seen_topics`
  ledger (salient-token content fingerprint) and bounded `runs` history. It is
  never injected into `session_pack`/`turn_pack`, so Dot's per-turn prompt does
  not grow.
- **Deterministic interest radar.** `build_interest_profile` scans only local
  bounded text — recent Dot journal entries, memory cards, focus items and the
  active Hub chat tab titles (`runtime/tabs.json`) — and maintains two explicit
  interest lines: LLM training/inference engineering features, and frontier
  Agent tooling / harness architecture. Terms carry weight, source and a 0.8
  decay so the radar evolves. No network or model call is used to build it.
- **Two-stage, cost-controlled funnel.**
  - Stage 1 defaults to MASO `deepseek-v41-flash-reasoning-high`
    (`CLAUDE_HUB_SCOUT_STAGE1_MODEL` overrides; Stage 2 model is configurable via
    `CLAUDE_HUB_SCOUT_STAGE2_MODEL`). Candidates are re-scored deterministically
    from profile-term overlap, primary-source credibility and the dedupe ledger;
    model self-scores are only a 0.2-weight hint.
  - When nothing clears `STAGE1_PASS_SCORE` (or every item is already seen), the
    run short-circuits with **zero Stage 2 calls and zero notifications**. At
    most `MAX_SPEAK_CANDIDATES = 2` leads reach Stage 2 per run.
  - Stage 2 verifies the primary URL/date and ties the item to current work.
    Only `verified && primary_source && speak_score >= SPEAK_BAR_SCORE (0.75)`
    reaches the notifier; everything else is filed silently.
- **Untrusted-input discipline.** All model/web content is read-only data:
  Pydantic models with `extra="forbid"`, absolute-HTTP(S)-only URL validation,
  bounded lengths, defensive JSON parsing (including a truncation-salvage
  parser for token-cut replies), and the verdict URL is re-anchored to the
  Stage 1 candidate so a model cannot redirect trust. External text can never
  drive a write or tool call.
- **Injection seams.** `ModelCaller` and `Notifier` are async callables, so the
  gating/dedupe logic is fully unit-tested without a live provider or Feishu.
  The default adapters reuse the read-only ephemeral Workspace Chat pattern from
  `demand_patrol.feishu_triage` (solo, ephemeral, archived after the turn) with
  one fresh-session retry for transient startup/empty-turn flakiness.

### Minimal routing (low merge-conflict surface)

- `cli/commands/dot.py`: `claude-hub dot scout profile` and
  `claude-hub dot scout run [--dry-run/--no-dry-run]` (dry-run is the default).
- `api/dot.py`: `GET /api/dot/scout/profile` and
  `POST /api/dot/scout/run` (`{"dry_run": true}` default).

## Validation

- New unit tests: `backend/tests/test_dot_scouting.py` (19 tests) covering
  profile extraction/decay/persistence, canonical-URL and topic-fingerprint
  dedupe, the low-score Stage 1 short-circuit (zero Stage 2, zero notify),
  seen-item filtering, Stage 2 Speak-Bar message format, dry-run zero-notify,
  sub-bar silencing, untrusted/non-HTTP input rejection and truncation salvage.
- `verify.sh backend-format`, `backend-types` (677 files, 0 issues), `docs`,
  `checkout-safety` all PASS.
- Focused backend run: 147 passed across the dot + patrol suites. Full backend
  run: 7070 passed; the only failures are pre-existing/environmental
  (`test_ci_health.py` fails identically with these changes stashed;
  `test_terminal_replay.py` / `test_terminal_input_latency_perf.py` need a
  Chromium binary not installed here) and reference no Dot/scouting code.
- Real `--dry-run` end-to-end against live MASO (`deepseek-v41-flash-reasoning-high`)
  in an **isolated** `CLAUDE_HUB_DOT_HOME` with a recording notifier: Stage 1
  returned 8 on-radar primary-source candidates (vLLM release, arXiv training /
  attention papers, MCP/Anthropic agent-harness posts), deterministic scoring
  selected the top two for Stage 2, a verified vLLM v0.34.0 verdict produced a
  bounded Speak-Bar message (primary link + hit reason + next-step handle), and
  the notifier was invoked **0 times** while the ledger recorded 8 URLs/topics.
  A hallucinated/sparse arXiv candidate was correctly rejected
  (`verified=false`, low score) rather than pushed.

## Safety / boundaries

- Production `~/.claude_hub/dot` and the live Feishu binding were never touched:
  all live runs used an isolated Dot home and an injected recording notifier.
- `dry_run=True` never calls the notifier even when the Speak-Bar passes.
- `services/demand_patrol/` is unchanged; no FeedbackLesson content is auto-loaded
  into any brief.

## Residual notes

- MASO exposes session-based streaming only (no stateless completions endpoint),
  so the ephemeral-Chat adapter is the correct model-call mechanism; rapid
  repeated provisioning is occasionally flaky, hence the single retry and the
  non-fatal error capture (a failed stage is recorded, never notified).
