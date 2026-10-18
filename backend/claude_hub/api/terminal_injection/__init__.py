"""Composable terminal HTML injection fragments."""

from claude_hub.api.terminal_injection.bootstrap import render_bootstrap_prelude
from claude_hub.api.terminal_injection.buffered_capture import render_buffered_capture
from claude_hub.api.terminal_injection.history_actions import render_history_actions
from claude_hub.api.terminal_injection.history_replay import render_history_replay
from claude_hub.api.terminal_injection.history_resync import render_history_resync
from claude_hub.api.terminal_injection.input_controls import render_input_controls
from claude_hub.api.terminal_injection.style import TERMINAL_STYLE
from claude_hub.api.terminal_injection.terminal_hook import render_terminal_hook
from claude_hub.api.terminal_injection.terminal_setup import render_terminal_setup

__all__ = [
    "TERMINAL_STYLE",
    "render_bootstrap_prelude",
    "render_buffered_capture",
    "render_history_actions",
    "render_history_replay",
    "render_history_resync",
    "render_input_controls",
    "render_terminal_hook",
    "render_terminal_setup",
]
