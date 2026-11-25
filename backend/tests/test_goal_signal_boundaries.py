"""Full-context Goal control recognition at the parser and its real consumers."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from claude_hub.models import (
    AgentType,
    ExecutionTarget,
    ManagedSession,
    ManagedSessionStatus,
    WorkspaceSessionRole,
)
from claude_hub.models.goal_run import GoalRunCreate, GoalRunStatus
from claude_hub.services.agent_stream.claude_jsonl import ClaudeJsonlAdapter
from claude_hub.services.agent_stream.native import ProviderSession
from claude_hub.services.agent_stream.tailer import SessionTailer
from claude_hub.services.goal_run import GoalRunController, GoalRunStore, StrictReviewOutcome
from claude_hub.services.goal_signals import GOAL_TAIL_SCAN_CHARS, parse_goal_signal

STATUS = '<goal-status state="complete">Verified</goal-status>'
CHECKPOINT = '<goal-checkpoint>{"remaining":["unfinished"]}</goal-checkpoint>'
PLAN = '<goal-plan>{"summary":"Verify boundaries","steps":[]}</goal-plan>'
MULTILINE_CHECKPOINT = '<goal-checkpoint>\n{"remaining":["unfinished"]}\n</goal-checkpoint>'
MULTILINE_PLAN = '<goal-plan>\n{"summary":"Verify boundaries","steps":[]}\n</goal-plan>'
ENVELOPE = PLAN + "\n" + CHECKPOINT + "\n" + STATUS
LONG_PADDING = "x\n" * 34817
NEGATIVE_CASES = [
    pytest.param("> " + CHECKPOINT + STATUS, id="quoted-adjacent-checkpoint"),
    pytest.param("> " + PLAN + STATUS, id="quoted-adjacent-plan"),
    pytest.param(
        '<goal-status state="continue">still working\n' + STATUS,
        id="unclosed-outer-status",
    ),
    pytest.param(
        '<goal-status state="continue">still working\n' + STATUS + "</goal-status>",
        id="closed-nested-status",
    ),
    pytest.param(
        '<goal-status state="continue">' + "x" * GOAL_TAIL_SCAN_CHARS + "\n" + STATUS,
        id="outer-status-before-window",
    ),
    *[
        pytest.param(
            "```xml\n" + "x" * (GOAL_TAIL_SCAN_CHARS + offset) + "\n" + STATUS,
            id=f"open-fence-window-{offset}",
        )
        for offset in (-len(STATUS) - 8, -len(STATUS), 0, 1)
    ],
    pytest.param("    " + STATUS, id="four-space-code"),
    pytest.param("\t" + STATUS, id="tab-code"),
    pytest.param(" \t" + STATUS, id="mixed-indent-code"),
    pytest.param(CHECKPOINT + "\n    " + STATUS, id="indented-after-checkpoint"),
    pytest.param(
        "> " + "x" * GOAL_TAIL_SCAN_CHARS + CHECKPOINT + STATUS,
        id="quoted-line-before-window",
    ),
    pytest.param("x" * GOAL_TAIL_SCAN_CHARS + STATUS, id="inline-before-window"),
    pytest.param("> unfinished\n" + CHECKPOINT + STATUS, id="lazy-blockquote-continuation"),
    pytest.param("    ```xml\n" + STATUS + "\n    ```", id="indented-fence-example"),
    pytest.param("<!-- example\n" + STATUS, id="open-html-comment"),
    pytest.param("> unfinished\u2028" + CHECKPOINT + STATUS, id="unicode-separator-is-not-newline"),
    *[
        pytest.param(prefix + "\n" + suffix + STATUS, id=f"{prefix_id}-{suffix_id}")
        for prefix_id, prefix in [
            ("multiline-checkpoint", MULTILINE_CHECKPOINT),
            ("multiline-plan", MULTILINE_PLAN),
            ("stray-status-closer", "</goal-status>"),
        ]
        for suffix_id, suffix in [
            ("open-fence", "```xml\n"),
            ("open-fence-blank", "```xml\nexample\n\n"),
            ("open-fence-window", "```xml\n" + "x" * 70000 + "\n"),
            ("lazy-quote", "> example\n"),
        ]
    ],
    *[
        pytest.param(
            f"Progress: <goal-{tag}{attributes}>still working\n" + STATUS,
            id=f"inline-outer-{tag}",
        )
        for tag, attributes in [
            ("status", ' state="continue"'),
            ("plan", ""),
            ("checkpoint", ""),
        ]
    ],
    pytest.param(
        'Progress: <goal-status state="continue">' + "x" * 70000 + "\n" + STATUS,
        id="inline-outer-before-window",
    ),
    pytest.param(PLAN + CHECKPOINT + "\nResult: " + STATUS, id="metadata-inline-status"),
    pytest.param(
        "\n".join("> " + line for line in ("```\n" + ENVELOPE + "\n```").split("\n")),
        id="protocol-fence-inside-quote",
    ),
    pytest.param(
        "\n".join("    " + line for line in ("```\n" + ENVELOPE + "\n```").split("\n")),
        id="protocol-fence-inside-indent",
    ),
    pytest.param(
        STATUS.replace("complete", "continue") + "\n\nExample:\n```\nProse.\n" + ENVELOPE + "\n```",
        id="no-fallback-before-fenced-prose",
    ),
    pytest.param("<goal-checkpoint>\n" + ENVELOPE + "\n</goal-checkpoint>", id="nested-tuple"),
    pytest.param("<goal-plan>\n" + LONG_PADDING + ENVELOPE, id="nested-tuple-before-window"),
]
POSITIVE_CASES = [
    pytest.param(STATUS + " \t\n", id="trailing-whitespace"),
    pytest.param(STATUS + "\n" * GOAL_TAIL_SCAN_CHARS, id="long-trailing-whitespace"),
    pytest.param("   " + STATUS, id="three-space-block"),
    pytest.param(PLAN + CHECKPOINT + STATUS, id="adjacent-controls"),
    pytest.param("```xml\n" + PLAN + CHECKPOINT + STATUS + "\n```", id="fenced-controls"),
    pytest.param(
        "```json\n" + CHECKPOINT + "\n```\n```xml\n" + STATUS + "\n```",
        id="separate-fences",
    ),
    pytest.param("~~~text\n" + STATUS + "\n~~~~", id="longer-tilde-closer"),
    pytest.param(
        "```xml\n" + "\n" * GOAL_TAIL_SCAN_CHARS + CHECKPOINT + STATUS + "\n```",
        id="closed-fence-before-window",
    ),
    pytest.param("x" * GOAL_TAIL_SCAN_CHARS + "\n" + STATUS, id="plain-after-window"),
    pytest.param("```xml\r\n" + CHECKPOINT + STATUS + "\r\n```", id="crlf-wrapper"),
    pytest.param(
        MULTILINE_PLAN + "\n" + MULTILINE_CHECKPOINT + "\n" + STATUS, id="multiline-controls"
    ),
    pytest.param(MULTILINE_CHECKPOINT + "\n```xml\n" + STATUS + "\n```", id="fence-after-metadata"),
    pytest.param(
        'Progress: <goal-status state="continue">old</goal-status>\n' + STATUS,
        id="closed-inline-example",
    ),
]


@pytest.mark.parametrize("text", NEGATIVE_CASES)
def test_parser_rejects_lexically_quoted_or_nested_completion(text):
    assert parse_goal_signal(text) is None


@pytest.mark.parametrize("text", POSITIVE_CASES)
def test_parser_preserves_valid_envelopes(text):
    signal = parse_goal_signal(text)
    assert signal is not None and signal.state == "complete"
    assert len(signal.protocol_text) <= GOAL_TAIL_SCAN_CHARS


@pytest.mark.parametrize(
    "text,state,has_plan,has_checkpoint",
    [
        pytest.param(
            ENVELOPE.replace("Verify boundaries", "Decoy").replace("unfinished", "decoy")
            + "\n\nEarlier example only.\n\n"
            + ENVELOPE.replace("complete", "continue")
            + " \n\t",
            "continue",
            True,
            True,
            id="new-tuple-after-example",
        ),
        pytest.param(
            "> " + STATUS + "\n\n```\n" + ENVELOPE.replace("complete", "needs_input") + "\n```",
            "needs_input",
            True,
            True,
            id="root-fence-after-quote",
        ),
        pytest.param(
            PLAN
            + CHECKPOINT
            + "\n\nEarlier example only.\n\n"
            + STATUS.replace("complete", "blocked"),
            "blocked",
            False,
            False,
            id="status-without-stale-metadata",
        ),
        pytest.param(
            "    " + PLAN + "\n\n" + CHECKPOINT + "\n" + STATUS,
            "complete",
            False,
            True,
            id="checkpoint-without-quoted-plan",
        ),
        pytest.param(
            "```\n" + LONG_PADDING + "```\n\n" + ENVELOPE,
            "complete",
            True,
            True,
            id="root-after-closed-long-example",
        ),
    ],
)
def test_interaction_matrix_preserves_only_the_authoritative_tuple(
    text, state, has_plan, has_checkpoint
):
    from claude_hub.services.goal_run.controller import _parse_checkpoint, _parse_plan

    signal = parse_goal_signal(text)
    assert signal is not None and signal.state == state
    plan = _parse_plan(signal.protocol_text)
    checkpoint = _parse_checkpoint(signal.protocol_text, "turn")
    assert (plan is not None) == has_plan
    assert (checkpoint is not None) == has_checkpoint
    if plan is not None:
        assert plan.summary == "Verify boundaries"
    if checkpoint is not None:
        assert checkpoint.remaining == ["unfinished"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True], ids=["ordinary", "strict"])
@pytest.mark.parametrize("text", NEGATIVE_CASES)
async def test_controller_repairs_instead_of_completing_quoted_output(tmp_path, text, strict):
    async def dispatch(goal, prompt):
        return goal.pending_step_id

    review = AsyncMock(
        return_value=StrictReviewOutcome(
            verdict="PASS", summary="Verified", findings=[], reviewer_provider="other"
        )
    )
    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=review
    )
    goal = await manager.create_and_start(
        "tab", GoalRunCreate(objective="Verify", client_request_id="new", strict_review=strict)
    )
    original_turn = goal.current_turn_id
    result = await manager.on_turn_completed("tab", original_turn, "completed", text)
    assert result.status == GoalRunStatus.ACTIVE
    assert result.current_turn_id != original_turn
    assert result.signal_recovery_attempts == 1
    assert result.completed_at is None
    assert result.strict_review_rounds == 0
    assert result.checkpoint is None and result.plan is None
    review.assert_not_awaited()
    result = await manager.on_turn_completed("tab", result.current_turn_id, "completed", text)
    assert result.status == GoalRunStatus.BLOCKED
    assert result.completed_at is None
    review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", POSITIVE_CASES)
async def test_controller_preserves_valid_completion_and_strict_review(tmp_path, text):
    async def dispatch(goal, prompt):
        return goal.pending_step_id

    review = AsyncMock(
        return_value=StrictReviewOutcome(
            verdict="PASS", summary="Verified", findings=[], reviewer_provider="other"
        )
    )
    manager = GoalRunController(
        GoalRunStore(tmp_path / "goals.json"), dispatch, strict_reviewer=review
    )
    goal = await manager.create_and_start(
        "tab", GoalRunCreate(objective="Verify", client_request_id="new", strict_review=True)
    )
    result = await manager.on_turn_completed("tab", goal.current_turn_id, "completed", text)
    assert result.status == GoalRunStatus.COMPLETE
    assert result.strict_review_rounds == 1
    review.assert_awaited_once()
    if CHECKPOINT in text or MULTILINE_CHECKPOINT in text:
        assert result.checkpoint.remaining == ["unfinished"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper",
    [
        "plain",
        "fenced",
        "quoted",
        "indented",
        "code-span",
        "separated-prose",
        "separated-code",
        "separated-quote",
    ],
)
async def test_checkpoint_and_plan_consume_only_validated_root_blocks(tmp_path, wrapper):
    async def dispatch(goal, prompt):
        return goal.pending_step_id

    manager = GoalRunController(GoalRunStore(tmp_path / "goals.json"), dispatch)
    goal = await manager.create_and_start(
        "tab", GoalRunCreate(objective="Verify", client_request_id="new", plan_first=True)
    )
    metadata = PLAN + CHECKPOINT
    if wrapper == "fenced":
        metadata = "```json\n" + metadata + "\n```"
    elif wrapper == "quoted":
        metadata = "> " + metadata
    elif wrapper == "indented":
        metadata = "    " + metadata
    elif wrapper == "code-span":
        metadata = "`example\n" + metadata + "\n`"
    elif wrapper == "separated-prose":
        metadata += "\nThese were earlier examples, not the final envelope."
    elif wrapper == "separated-code":
        metadata += "\n```text\nEarlier example ends here.\n```"
    elif wrapper == "separated-quote":
        metadata += "\n> Earlier example ends here."
    text = metadata + "\n\n" + STATUS.replace("complete", "needs_input")
    parsed = parse_goal_signal(text)
    assert parsed is not None
    result = await manager.on_turn_completed("tab", goal.current_turn_id, "completed", text)
    assert result.status == GoalRunStatus.BLOCKED
    if wrapper not in {"plain", "fenced"}:
        assert "<goal-plan>" not in parsed.protocol_text
        assert "<goal-checkpoint>" not in parsed.protocol_text
        assert result.plan is None and result.checkpoint is None
    else:
        assert result.plan.summary == "Verify boundaries"
        assert result.checkpoint.remaining == ["unfinished"]


class _OneShotTransport(ProviderSession):
    def _build_command(self) -> list[str]:
        return []

    async def _send_text(self, text: str) -> None:
        raise AssertionError("no real provider is contacted")


@pytest.fixture
async def watchdog():
    session = ManagedSession(
        id="boundary-session",
        workspace_id="boundary-workspace",
        tab_id="boundary-tab",
        role=WorkspaceSessionRole.WORKER,
        agent_type=AgentType.CLAUDE,
        status=ManagedSessionStatus.IDLE,
        title="boundary contract",
        workspace_path="/tmp",
        tmux_session="unused-boundary-tmux",
        target=ExecutionTarget.LOCAL,
        solo_mode=False,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    transport = _OneShotTransport(session)
    tailer = SessionTailer(
        workspace_id=session.workspace_id,
        session_id=session.id,
        adapter=ClaudeJsonlAdapter(),
        session_getter=lambda: session,
        native_transport=transport,
    )
    tailer._goal_protocol_turn = True
    tailer._active_turn_id = "boundary-turn"
    return tailer, transport


@pytest.mark.asyncio
@pytest.mark.parametrize("text", NEGATIVE_CASES)
async def test_watchdog_never_latches_or_synthesizes_quoted_completion(watchdog, text, monkeypatch):
    tailer, transport = watchdog
    publish, stop = AsyncMock(), AsyncMock()
    monkeypatch.setattr(tailer, "_publish", publish)
    monkeypatch.setattr(transport, "stop", stop)
    # Real deltas can straddle both the fence opener and the tail scan boundary.
    for end in sorted({1, min(8, len(text)), max(8, len(text) - len(STATUS)), len(text)}):
        tailer._assistant_text = text[:end]
        tailer._note_assistant_text(transport)
        assert tailer._goal_terminal_at is None
        assert not tailer._goal_terminal_grace_expired()
    await tailer._synthesize_goal_completion_locked(transport)
    publish.assert_not_awaited()
    stop.assert_not_awaited()
    assert tailer._active_turn_id == "boundary-turn"


@pytest.mark.parametrize("text", POSITIVE_CASES)
@pytest.mark.asyncio
async def test_watchdog_preserves_legal_completion(watchdog, text):
    tailer, transport = watchdog
    tailer._assistant_text = text
    tailer._note_assistant_text(transport)
    assert tailer._goal_terminal_at is not None


@pytest.mark.asyncio
async def test_watchdog_clears_latch_when_afterthought_pushes_status_out_of_window(watchdog):
    tailer, transport = watchdog
    tailer._assistant_text = STATUS
    tailer._note_assistant_text(transport)
    assert tailer._goal_terminal_at is not None
    tailer._assistant_text += "\n" + "afterthought" * GOAL_TAIL_SCAN_CHARS
    tailer._note_assistant_text(transport)
    assert tailer._goal_terminal_at is None
    assert not tailer._goal_terminal_grace_expired()
