"""Compose the CSS and JavaScript injected into proxied ttyd HTML pages."""

from ._terminal_injection_bootstrap import render_terminal_bootstrap
from ._terminal_injection_runtime import render_terminal_runtime
from ._terminal_injection_style import TERMINAL_STYLE

FULL_HISTORY_LINES = 100000
INITIAL_AGENT_HISTORY_LINES = 5000
INITIAL_AGENT_REPLAY_MIN_LINES = 1000
INITIAL_TERMINAL_HISTORY_LINES = 20000


def render_terminal_injection(
    *,
    tab_id: str,
    agent_type_value: str,
    execution_target_value: str,
) -> str:
    """Render the custom style and history-replay script injected before ``</head>``."""
    return (
        TERMINAL_STYLE
        + render_terminal_bootstrap(
            tab_id=tab_id,
            agent_type_value=agent_type_value,
            execution_target_value=execution_target_value,
            full_history_lines=FULL_HISTORY_LINES,
            initial_agent_replay_min_lines=INITIAL_AGENT_REPLAY_MIN_LINES,
            initial_agent_history_lines=INITIAL_AGENT_HISTORY_LINES,
            initial_terminal_history_lines=INITIAL_TERMINAL_HISTORY_LINES,
        )
        + render_terminal_runtime()
    )


def inject_terminal_html(
    html: str,
    *,
    tab_id: str,
    agent_type_value: str,
    execution_target_value: str,
) -> str:
    """Inject custom styles and scripts before ``</head>`` when present."""
    if "</head>" not in html:
        return html
    injected = render_terminal_injection(
        tab_id=tab_id,
        agent_type_value=agent_type_value,
        execution_target_value=execution_target_value,
    )
    return html.replace("</head>", injected + "</head>")
