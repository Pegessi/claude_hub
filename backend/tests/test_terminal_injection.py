"""Focused contracts for proxied terminal HTML injection."""

import hashlib
import json

from claude_hub.api._terminal_injection import inject_terminal_html, render_terminal_injection


def test_terminal_injection_json_encodes_dynamic_values() -> None:
    tab_id = 'tab-"雪\\path\nline'
    agent_type = "claude"
    execution_target = "remote"

    rendered = render_terminal_injection(
        tab_id=tab_id,
        agent_type_value=agent_type,
        execution_target_value=execution_target,
    )

    assert f"const TAB_ID = {json.dumps(tab_id)};" in rendered
    assert f"const AGENT_TYPE = {json.dumps(agent_type)};" in rendered
    assert f"const EXECUTION_TARGET = {json.dumps(execution_target)};" in rendered
    assert "function setupHistoryResync(term)" in rendered
    assert rendered.count("<style>") == 1
    assert rendered.count("<script>") == 1


def test_terminal_injection_remains_byte_identical_after_fragment_split() -> None:
    rendered = render_terminal_injection(
        tab_id='tab"</script>',
        agent_type_value="claude",
        execution_target_value="remote",
    )

    assert len(rendered) == 104455
    assert hashlib.sha256(rendered.encode("utf-8")).hexdigest() == (
        "48f286cf570e082e4261af6858fc66eac6c8f760c2e57423136c01c3937c6bb1"
    )


def test_terminal_injection_preserves_pages_without_lowercase_head_close() -> None:
    for html in ("<html><body>no head</body></html>", "<html><HEAD></HEAD></html>"):
        assert (
            inject_terminal_html(
                html,
                tab_id="tab-1",
                agent_type_value="terminal",
                execution_target_value="local",
            )
            == html
        )


def test_terminal_injection_replaces_every_lowercase_head_close() -> None:
    html = "<head></head><template></head></template>"
    rendered = render_terminal_injection(
        tab_id="tab-1",
        agent_type_value="terminal",
        execution_target_value="local",
    )

    injected = inject_terminal_html(
        html,
        tab_id="tab-1",
        agent_type_value="terminal",
        execution_target_value="local",
    )

    assert injected.count(rendered) == 2
    assert injected.count("</head>") == 2
