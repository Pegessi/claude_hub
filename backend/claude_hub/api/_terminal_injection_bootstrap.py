"""Compose the injected terminal bootstrap script fragments."""

from claude_hub.api.terminal_injection import (
    render_bootstrap_prelude,
    render_buffered_capture,
    render_history_replay,
    render_terminal_setup,
)


def render_terminal_bootstrap(
    *,
    tab_id: str,
    agent_type_value: str,
    execution_target_value: str,
    full_history_lines: int,
    initial_agent_replay_min_lines: int,
    initial_agent_history_lines: int,
    initial_terminal_history_lines: int,
) -> str:
    """Render the bootstrap half of the shared terminal script scope."""
    return (
        render_bootstrap_prelude(
            tab_id=tab_id,
            agent_type_value=agent_type_value,
            execution_target_value=execution_target_value,
            full_history_lines=full_history_lines,
            initial_agent_replay_min_lines=initial_agent_replay_min_lines,
            initial_agent_history_lines=initial_agent_history_lines,
            initial_terminal_history_lines=initial_terminal_history_lines,
        )
        + render_buffered_capture()
        + render_history_replay()
        + render_terminal_setup()
    )
