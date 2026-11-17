# Fix Feishu `@mention` Proactive Patrol Pipeline (RC1–RC6)

- **Task ID**: `87287524-c457-4d34-b358-26563bc7f4a3`
- **Branch**: `feat/fix-feishu-mention-proactive-pat-87287524`
- **Diagnosis reference**: `/tmp/feishu-patrol-diagnosis-20261118.md` (`85b83afc-c1e5-4054-b836-0a54d4e5af11`)

## Summary of Fixes (RC1–RC6 & Review Cycle 1 Findings F1–F4)

1. **RC1 & F4 — Second-precision ISO timestamps, minute-floored search start, and fail-closed/redacted CLI error details**:
   - `run_feishu_triage()` formats `--start` and `--end` as `astimezone(ZoneInfo(state.config.timezone)).isoformat(timespec="seconds")` so Feishu's `/open-apis/im/v1/messages/search` never rejects microsecond ISO strings with `code=99992402` (`invalid_parameters: field validation failed`).
   - `_search_start()` floors the computed start timestamp to the minute (`replace(second=0, microsecond=0)`) so minute-precision `create_time` strings returned by `lark-cli` are never dropped at the lower boundary of `start <= item.created_at <= now`.
   - `run_command()` (`commands.py`) captures bounded `stderr` and `extract_cli_error_detail()` surfaces only recognized error envelopes (`error` dict/string or `ok: false` message), failing closed (`""`) on unrecognized JSON/structured dumps and redacting quoted/unquoted sensitive keys (`access_token`, `appSecret`, `password`, `api_key`, etc.), bearer/token prefixes, and URL userinfo (`https://user:password@host` → `https://[redacted]@host`).
2. **RC2 — Naive local `YYYY-MM-DD HH:MM` timestamp parsing**:
   - `_timestamp()` attaches `ZoneInfo(tz)` (configured `state.config.timezone`, defaulting to `"Asia/Shanghai"`) when `datetime.fromisoformat()` produces a naive `datetime` from `lark-cli im +messages-search`.
3. **RC3, F1, F3 & F5 — Bounded scoped discovery (`--is-at-me`, `--chat-type p2p`, `--sender <uid>`, and `+threads-messages-list --thread <thread_id>`)**:
   - `run_feishu_triage()` replaces the single unfiltered all-chat search with bounded scoped queries:
     1. `--is-at-me` (all explicit `@me` mentions across all chats),
     2. `--chat-type p2p` (all direct P2P messages, preserving unmentioned P2P questions and replies),
     3. `--sender <uid>` (all user-authored messages in `[start, now]`, unconditionally establishing own-reply coverage across all chats/threads even when self-mentions exist elsewhere),
     4. `lark-cli im +threads-messages-list --thread <thread_id>` for each exact `thread_id` where the user posted a group thread message in `[start, now]` (propagating `chat_id`, `chat_name`, and `chat_type` defaults from the user's seed thread message), preserving unmentioned follow-ups in participated threads while completely ignoring >500 unrelated messages in other threads or non-threaded chatter of the same busy group.
4. **RC4 & F2 — Broadened `_ACTIONABLE` regex and strict conversation-anchored reply matching**:
   - `_ACTIONABLE` matches common Chinese triage phrasings (`看看`, `看下`, `瞅瞅`, `检查`, `问题`, `异常`, `不对`, `不正常`, `不行`, `是不是`, `能否`, `能不能`, `吗`) and internal URLs (`larkoffice.com/(?:docx|wiki|sheets|base)`, `seed.byteintl.net`, `megavision.byteintl.net`).
   - `_is_answered_by()` requires matching conversation evidence: a reply with `reply_to` only answers `message.message_id` (or shares the same non-empty `message.reply_to` parent), a threaded reply only answers the same `thread_id`, and non-threaded group `@mentions` are never suppressed by unanchored group messages or replies anchored to a different question from the same sender.
5. **RC5 — MASO reasoning-effort model discovery**:
   - `_has_maso_model()` matches `_PREFERRED_MODEL` (`gemini-4-argon_reasoning_high`) against `option.id`, `option.provider_model_id`, and `option.supported_reasoning_efforts[].provider_model_id`.
6. **RC6 — Thread/reply metadata in `_diagnosis_prompt()` and relaxed `reply_draft` length**:
   - `FeishuCandidate` preserves `reply_to` (`reply_to` or `parent_id`), and `_diagnosis_prompt()` includes `chat_id`, `thread_id`, and `reply_to` in `<untrusted_feishu_evidence>`.
   - `TriageDiagnosis.reply_draft` bounds are relaxed from `80..150` to `20..600` characters while keeping the prompt's 80–150 character guidance.

## Real Read-Only `lark-cli` Dry-Run Evidence

Executed `run_feishu_triage(..., dry_run=True, notify=False)` against `/home/tiger/.local/bin/lark-cli` in an isolated temporary `CLAUDE_HUB_HOME` with a fail-on-call notifier:
- **Morning window (`2026-11-17T09:25:00+08:00` → `2026-11-17T12:15:42.123456+08:00`)**: `errors=[]`, `candidates=20`, `answered_skipped=36` (extracting `@mention` candidates including `MMPE-debug群` `om_x100b608f1e868d3cdd1b702a922c851` and `coding for visual outputs加速` `om_x100b608f13d15480b4bed0720298f8f`, plus participated-thread follow-ups such as `OneEval M15.3 10B` `om_x100b608f042d6cb0dc1409d9afa98c7`).
- **Busy window (`2026-11-17T18:12:00+08:00` [4-hour bound from `22:12:10.418596+08:00`] → `2026-11-17T22:12:10.418596+08:00`)**: `errors=[]`, `candidates=30`, `answered_skipped=50` (zero `has_more` truncation errors, extracting group `@mentions`, P2P questions, and participated-thread follow-ups via `+threads-messages-list`).
