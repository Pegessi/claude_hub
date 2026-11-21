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

## Review cycle 2 fixes

Independent review (`claude-hub-reviewer-3-bbf7f5`, attempt 1) raised three
blocking findings; all are addressed.

1. **MASO discovery, strong-model selection and fallback.**
   - Replaced the hard-coded `DEFAULT_MASO_BASE_URL` with `discover_maso_service`
     (`resolve_maso_base_url`), honouring an explicit `MASO_BASE_URL` first and
     otherwise probing the local registry/loopback candidates.
   - Stage 2 now resolves a real strong model against the live catalog
     (`resolve_stage2_model`, default `gpt-6-astra_reasoning_high`, distinct from
     the DeepSeek-flash Stage 1) with a family-diverse `STAGE2_FALLBACK_MODELS`
     chain (`_call_stage2` tries the strong model then fallbacks, last resort the
     cheap Stage 1 model) so one unavailable model no longer aborts verification.
   - `_run_ephemeral_turn` now checks `TURN_COMPLETED` status and surfaces `ERROR`
     events instead of treating them as success; ephemeral-session archive is
     shielded (`_shield_archive`) so cleanup failures never mask the turn result.
2. **Dedupe-ledger poisoning.**
   - `dry_run=True` now records only the run (`ScoutingStore.append_run`) and
     never writes `seen_urls`/`seen_topics`, so previews do not suppress future
     real runs.
   - In live runs, high-potential candidates whose Stage 2 verification fails or
     whose `dot notify` delivery fails are withheld from the ledger (and retried
     later); only resolved outcomes (verified/silenced or notified) are committed.
3. **Active-tab handling and Chinese radar terms.**
   - `read_hub_tab_titles` excludes archived, terminal and internal
     `Dot scouting:`/`Patrol triage:` tabs and returns the **newest**
     `MAX_TAB_TITLES` active chat sessions (active tabs are stored oldest-first).
   - Added core Chinese terms to both interest lexicons and a CJK-aware matcher
     (`_is_cjk` substring matching, retaining token-boundary matching for Latin).
   - Re-ran the isolated live `--dry-run`: Stage 1 DeepSeek produced 8 candidates,
     Stage 2 ran `gpt-6-astra_reasoning_high` and returned parsed verdicts with
     zero notifications and an untouched ledger.

### Module split (line-ceiling)

The single module exceeded the 1500-line doc ceiling, so the deterministic core
moved to `services/dot/scouting_core.py` (config, untrusted-input Pydantic models,
fingerprints, JSON store, radar, prompts, defensive parsing, scoring, with an
explicit `__all__`). `services/dot/scouting.py` now holds only the live MASO
adapter, the fallback chain, `ScoutingEngine` and tab discovery, and re-exports
the core public API so `from ...scouting import X` keeps working. Line counts:
628 (scouting) + 1187 (scouting_core).

### Parser hardening for strong-model variance

Observed strong-model output shapes that dropped verdicts are now tolerated
without weakening validation: balanced-JSON extraction from surrounding prose,
truncation repair, string/boolean coercion and 0..1 score clamping, `null`/numeric
`published_at` and other text fields coerced to strings, and per-evidence
filtering so one bad link no longer invalidates a whole verdict. Unit tests grew
from 19 to 35, including new ledger-poisoning, fallback, tab-recency, Chinese-term
and coercion cases.
