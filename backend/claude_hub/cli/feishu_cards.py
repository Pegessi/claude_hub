"""Backward-compatible facade for Feishu interactive-card builders."""

# ruff: noqa: F401

from claude_hub.cli.feishu_cards_impl.contract import (
    ACTION_KEY,
    CARD_KINDS,
    DISPLAY_KINDS,
    INTERACTIVE_KINDS,
    TOKEN_KEY,
    _attr_or_key,
    action_to_cli_command,
    logger,
    parse_card_action,
)
from claude_hub.cli.feishu_cards_impl.interactive import (
    build_approval_card,
    build_needs_input_card,
    build_plan_confirm_card,
)
from claude_hub.cli.feishu_cards_impl.inventory import (
    _column,
    _count_by,
    _counts_text,
    _get,
    _item_rows,
    _reports_by_task,
    _row,
    _workspace_name,
    build_action_catalog_card,
    build_filesystem_card,
    build_network_card,
    build_remote_profiles_card,
    build_result_card,
    build_status_card,
    build_tab_status_card,
    build_tabs_card,
    build_workspaces_card,
)
from claude_hub.cli.feishu_cards_impl.primitives import (
    _action_value,
    _button,
    _header,
    _markdown,
    _note,
    _plain_text,
    _wrap,
)
from claude_hub.cli.feishu_cards_impl.simple import (
    build_lessons_card,
    build_task_card,
    build_terminal_card,
)
from claude_hub.cli.feishu_cards_impl.workspace import (
    _report_line,
    build_agents_card,
    build_overview_card,
    build_reports_card,
    build_task_detail_card,
)
