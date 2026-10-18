"""Compose the injected terminal runtime script fragments."""

from claude_hub.api.terminal_injection import (
    render_history_actions,
    render_history_resync,
    render_input_controls,
    render_terminal_hook,
)


def render_terminal_runtime() -> str:
    """Render the runtime half of the shared terminal script scope."""
    return (
        render_history_resync()
        + render_history_actions()
        + render_input_controls()
        + render_terminal_hook()
    )
