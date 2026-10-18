"""Backward-compatible facade for the ``task`` command group."""

# ruff: noqa: F401

# Imports register Click commands in their established public help order.
# isort: off
from claude_hub.cli.task_commands.group import task
from claude_hub.cli.task_commands.read import (
    ACCEPTANCE_COLUMNS,
    ACK_FAILED_NOTE,
    GOAL_PACKET_FIELDS,
    REVIEW_COLUMNS,
    REVIEW_STATES,
    TASK_COLUMNS,
    TASK_DETAIL_FIELDS,
    TASK_EVENT_COLUMNS,
    TASK_STATUS_FIELDS,
    TASK_TREE_COLUMNS,
    _acceptance_check_items,
    _echo_call_id,
    _emit_task_events,
    _emit_task_tree,
    _find_task_board,
    _latest_acceptance_report,
    _latest_report,
    _print_task_status,
    _report_message,
    _resolve_call_id,
    _resolve_ws_id,
    _task_status_payload,
    task_get,
    task_list,
    task_report,
    task_review,
    task_status,
)
from claude_hub.cli.task_commands.execution_support import (
    REPORTER_KEY_DIR_ENV,
    _action_summary,
    _context_summary,
    _create_reporter_key,
    _default_reporter_key_file,
    _drop_superseded_default_key,
    _execution_ref_options,
    _execution_reference,
    _install_reporter_key,
    _prepare_reporter_key,
    _read_reporter_key,
    _registration_request_key,
    _reject_secret_task_fields,
    _reject_task_execution_fields,
    _reporter_key_dir,
    _safe_task_operation_error,
    _settle_staged_key,
    _source_reference,
    _staged_key_or_none,
    _staged_reporter_key_file,
    _task_in_board,
)
from claude_hub.cli.task_commands.creation import task_create, task_run
from claude_hub.cli.task_commands.lifecycle import (
    task_accept,
    task_cleanup,
    task_continue,
    task_start,
    task_update,
)
from claude_hub.cli.task_commands.observation import (
    task_abort,
    task_ack,
    task_delete,
    task_dispatch_decision,
    task_events,
    task_followup,
    task_request_review,
    task_send,
    task_spawn,
    task_tree,
    task_wait,
)
from claude_hub.cli.task_commands.feedback import task_feedback, task_feedback_reap
from claude_hub.cli.task_commands.execution import (
    _tab_context_task_ids,
    task_context,
    task_dispatch,
    task_handoff,
    task_progress,
    task_register,
)

# isort: on
