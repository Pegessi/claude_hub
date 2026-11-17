# Fix Feishu `@mention` Proactive Patrol Pipeline (RC1–RC6)

- **Task ID**: `87287524-c457-4d34-b358-26563bc7f4a3`
- **Branch**: `feat/fix-feishu-mention-proactive-pat-87287524`
- **Diagnosis reference**: `/tmp/feishu-patrol-diagnosis-20261118.md` (`85b83afc-c1e5-4054-b836-0a54d4e5af11`)

## Summary of Fixes (RC1–RC6)

1. **RC1 — Second-precision ISO timestamps, minute-floored search start, and surfaced CLI error details**:
   - `run_feishu_triage()` formats `--start` and `--end` as `astimezone(ZoneInfo(state.config.timezone)).isoformat(timespec="seconds")` so Feishu's `/open-apis/im/v1/messages/search` never rejects microsecond ISO strings with `code=99992402` (`invalid_parameters: field validation failed`).
   - `_search_start()` floors the computed start timestamp to the minute (`replace(second=0, microsecond=0)`) so minute-precision `create_time` strings returned by `lark-cli` are never dropped at the lower boundary of `start <= item.created_at <= now`.
   - `run_command()` (`commands.py`) captures bounded `stderr` and `extract_cli_error_detail()` surfaces structured JSON error fields (`subtype`, `code`, `message`) or sanitized single-line `stderr` details (with token/secret redaction) on `CommandError`.
2. **RC2 — Naive local `YYYY-MM-DD HH:MM` timestamp parsing**:
   - `_timestamp()` attaches `ZoneInfo(tz)` (configured `state.config.timezone`, defaulting to `"Asia/Shanghai"`) when `datetime.fromisoformat()` produces a naive `datetime` from `lark-cli im +messages-search`.
3. **RC3 — Scoped `--is-at-me` search and targeted reply check**:
   - `run_feishu_triage()` passes `--is-at-me` to the primary `lark-cli im +messages-search` query so high-volume group chats cannot overflow the 500-message cap and deadlock `feishu_watermark_iso`.
   - When unanswered candidates are found and no user-authored messages are present in the initial `--is-at-me` batch, a targeted second search (`--sender <uid> --chat-id <candidate_chat_ids>`) fetches the user's own replies in those chats and re-evaluates `filter_unanswered()`.
4. **RC4 — Broadened `_ACTIONABLE` regex and accurate non-threaded group reply matching**:
   - `_ACTIONABLE` now matches common Chinese triage phrasings (`看看`, `看下`, `瞅瞅`, `检查`, `问题`, `异常`, `不对`, `不正常`, `不行`, `是不是`, `能否`, `能不能`, `吗`) and internal URLs (`larkoffice.com/(?:docx|wiki|sheets|base)`, `seed.byteintl.net`, `megavision.byteintl.net`).
   - `_is_answered_by()` ensures non-threaded group `@mentions` are only marked answered when the user replies in the same thread, replies via `reply_to`, or explicitly `@mentions` the sender in a later message in that chat—preventing unrelated non-threaded group chatter from suppressing earlier `@mentions`.
5. **RC5 — MASO reasoning-effort model discovery**:
   - `_has_maso_model()` matches `_PREFERRED_MODEL` (`gemini-4-argon_reasoning_high`) against `option.id`, `option.provider_model_id`, and `option.supported_reasoning_efforts[].provider_model_id`.
6. **RC6 — Thread/reply metadata in `_diagnosis_prompt()` and relaxed `reply_draft` length**:
   - `FeishuCandidate` preserves `reply_to` (`reply_to` or `parent_id`), and `_diagnosis_prompt()` includes `chat_id`, `thread_id`, and `reply_to` in `<untrusted_feishu_evidence>`.
   - `TriageDiagnosis.reply_draft` bounds are relaxed from `80..150` to `20..600` characters while keeping the prompt's 80–150 character guidance.
