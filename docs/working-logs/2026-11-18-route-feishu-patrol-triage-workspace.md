# Route Feishu Patrol Triage to an Agent Workspace & Filter Bot Notifications

- **Task ID**: `948d3c44-8d7d-40c1-a5b6-db483a8ce9b9`
- **Branch**: `feat/route-feishu-patrol-triage-to-an-948d3c44`

## Summary of Changes

1. **Workspace Chat Triage Routing (`PatrolConfig.workspace_id` & `diagnose_message`)**:
   - Added `workspace_id: str = DEFAULT_PATROL_WORKSPACE_ID` (`9b6725ac-68fa-4111-8ea9-b729764c6e97`, `evo_workspace`) with `patrol_workspace_id` alias support in `PatrolConfig` (`services/demand_patrol/models.py`), `DemandPatrolService.configure()` (`services/demand_patrol/service.py`), CLI (`cli/commands/patrol.py`), and frontend type definitions (`frontend/src/utils/dotApi.ts`).
   - Changed `diagnose_message()` (`services/demand_patrol/feishu_triage.py`) to provision an ephemeral Workspace Chat `ManagedSession` (`ensure_workspace_agent(..., bootstrap=False)`) in the configured workspace (falling back to the first available workspace if the configured ID is absent), execute the single read-only turn via `_run_workspace_triage_turn()`, and auto-archive the session in a `finally` block (`_archive_patrol_session()`).
   - Hardened error boundaries (Review Cycle 1 F2 & F3): normalized `DeliveryUncertain`, `ManagedChatDeliveryUncertain`, `WorkspaceAgentInitializationError`, and `RuntimeError` into per-candidate `CommandError` failures so one candidate's delivery or cleanup failure never aborts the patrol run or loses prior candidates' processed state, and ensured `_archive_patrol_session()` quarantines unarchived sessions and surfaces `CommandError` instead of swallowing archive failures.

2. **Automated Bot & System Notification Filtering (Review Cycle 1 F1)**:
   - Added sender metadata extraction (`_extract_sender_metadata`, `_is_automated_notification_fields`, `_is_automated_candidate`) in `feishu_triage.py` to filter out bot/app/system senders (`sender_type` in `app|bot|system|anonymous`, `id_type` in `app_id|bot_id`, `cli_*`/`app_*`/`bot_*` sender IDs, `open_bot_id`), known automated chats (`[redacted]`), exact bot names (`Codebase Assistant`, `[redacted]`, `[redacted]`, `[redacted]`, `[redacted]`, `[redacted]`, `定期提醒`), and standalone notification `<card>` payloads before LLM triage.
   - Preserved authoritative human senders (`sender_type == "user"` and `ou_*` open_id without bot flags) against fallback name/text heuristics so human colleagues with names like `Kanishka` or `张三（飞书团队）` or humans quoting permission-expiry notices are never dropped.

3. **Legacy Terminal `Patrol triage: *` Subagent Cleanup (Review Cycle 1 F4)**:
   - Added `SubagentService.purge_by_name_prefix("Patrol triage: ")` (`services/subagents.py`) invoked on startup to retire legacy `Patrol triage: *` tabs and remove their subagent records, retaining the record in durable state if `delete_tab()` fails transiently so subsequent startups retry tab deletion.
   - Deleted the 6 live `Patrol triage: om_*` tabs and removed their records from SQLite `subagents.json`.
