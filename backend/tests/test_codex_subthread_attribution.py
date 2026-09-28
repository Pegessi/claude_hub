"""Sub-agent thread attribution for the Codex/TraeX app-server adapter.

These tests pin the real collab shape captured in tab 4ed6b70c (TraeX): a main
``spawnAgent`` / ``sendInput`` call carries ``receiverThreadIds``; the child's
work arrives as ``item/agentMessage/delta`` / ``item/reasoning/textDelta`` /
``item/started|completed`` with a ``threadId`` (or a ``code-mode-nested:…``
item id). The adapter stamps child records with ``payload.subagent_thread``
and a thread-scoped ``message_id`` while keeping the main agent's own records
on the main stream.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter, TraexJsonlAdapter
from claude_hub.services.agent_stream.store import AgentStreamStore

MAIN = "thread-main"
F6 = "01a0e2f6-ef37-7031-b97e-fecf618eef5a"
FEC = "01a0e2ec-54c9-74f3-bcc0-369634ac31de"


def _ctx(main_thread_id: str | None = MAIN, session_id: str = "s1") -> NormalizeContext:
    return NormalizeContext(
        session_id=session_id,
        tab_id="t1",
        agent_type=AgentType.TRAEX,
        run_epoch=1,
        turn_id="turn-1",
        main_thread_id=main_thread_id,
    )


def _types(events: List[AgentStreamEvent]) -> List[AgentStreamEventType]:
    return [e.type for e in events]


def _spawn(adapter: CodexJsonlAdapter, thread: str = F6) -> None:
    adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "threadId": MAIN,
                "item": {
                    "type": "collabAgentToolCall",
                    "id": f"call-spawn-{thread[:6]}",
                    "tool": "spawnAgent",
                    "prompt": "develop kernel",
                    "receiverThreadIds": [thread],
                },
            },
        },
        _ctx(),
    )


def test_spawn_agent_stays_on_main_stream_and_carries_no_thread() -> None:
    adapter = TraexJsonlAdapter()
    events = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "threadId": MAIN,
                "item": {
                    "type": "collabAgentToolCall",
                    "id": "call-spawn",
                    "tool": "spawnAgent",
                    "prompt": "go",
                    "receiverThreadIds": [F6],
                },
            },
        },
        _ctx(),
    )
    assert _types(events) == [AgentStreamEventType.TOOL_CALL_STARTED]
    assert events[0].payload["name"] == "spawnAgent"
    assert "subagent_thread" not in events[0].payload
    assert events[0].payload["args"]["receiverThreadIds"] == [F6]


def test_child_deltas_are_thread_stamped_main_deltas_are_not() -> None:
    adapter = TraexJsonlAdapter()
    ctx = _ctx()

    main_think = adapter.normalize_line(
        {"method": "item/reasoning/textDelta", "params": {"threadId": MAIN, "delta": "main think"}},
        ctx,
    )
    child_think = adapter.normalize_line(
        {"method": "item/reasoning/textDelta", "params": {"threadId": F6, "delta": "child think"}},
        ctx,
    )
    main_text = adapter.normalize_line(
        {"method": "item/agentMessage/delta", "params": {"threadId": MAIN, "delta": "main answer"}},
        ctx,
    )
    child_text = adapter.normalize_line(
        {"method": "item/agentMessage/delta", "params": {"threadId": F6, "delta": "child report"}},
        ctx,
    )

    assert "subagent_thread" not in main_think[0].payload
    assert main_think[0].message_id == "turn-1:thinking"
    assert "subagent_thread" not in main_text[0].payload
    assert main_text[0].message_id == "turn-1:assistant"

    assert child_think[0].payload["subagent_thread"] == F6
    assert child_think[0].message_id == f"turn-1:sub:{F6}:thinking"
    assert child_text[0].payload["subagent_thread"] == F6
    assert child_text[0].message_id == f"turn-1:sub:{F6}:assistant"


def test_nested_code_mode_item_without_thread_id_falls_back_to_spawned_thread() -> None:
    adapter = TraexJsonlAdapter()
    _spawn(adapter, F6)

    # Real shape: nested worker items carry only the code-mode-nested id and no
    # params.threadId. With exactly one spawned thread they attribute to it.
    events = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "id": "code-mode-nested:29:call_x:exec-1",
                    "command": ["ls"],
                }
            },
        },
        _ctx(),
    )
    assert events[0].payload["name"] == "exec_command"
    assert events[0].payload["subagent_thread"] == F6

    done = adapter.normalize_line(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "id": "code-mode-nested:29:call_x:exec-1",
                    "exitCode": 0,
                    "aggregatedOutput": "ok",
                }
            },
        },
        _ctx(),
    )
    assert done[0].payload["subagent_thread"] == F6
    assert done[0].payload["status"] == "completed"


def test_two_threads_are_distinguished_by_explicit_thread_id() -> None:
    adapter = TraexJsonlAdapter()
    ctx = _ctx()
    _spawn(adapter, F6)

    # sendInput to a SECOND, pre-existing peer thread (not spawned here).
    adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "threadId": MAIN,
                "item": {
                    "type": "collabAgentToolCall",
                    "id": "call-si",
                    "tool": "sendInput",
                    "prompt": "status?",
                    "receiverThreadIds": [FEC],
                },
            },
        },
        ctx,
    )
    # The sendInput call itself stays main; the second thread's report does not.
    f6 = adapter.normalize_line(
        {"method": "item/agentMessage/delta", "params": {"threadId": F6, "delta": "f6"}}, ctx
    )
    fec = adapter.normalize_line(
        {"method": "item/agentMessage/delta", "params": {"threadId": FEC, "delta": "fec"}}, ctx
    )
    assert f6[0].payload["subagent_thread"] == F6
    assert fec[0].payload["subagent_thread"] == FEC


def test_ambiguous_nested_item_uses_stable_synthetic_group_not_main() -> None:
    adapter = TraexJsonlAdapter()
    ctx = _ctx()
    # Two distinct spawned threads: a bare nested item cannot be assigned to a
    # specific receiver, but must still stay OFF the main agent's stream.
    _spawn(adapter, F6)
    _spawn(adapter, FEC)
    events = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "id": "code-mode-nested:29:call_y:exec-9",
                    "command": ["pwd"],
                }
            },
        },
        ctx,
    )
    thread = events[0].payload["subagent_thread"]
    assert thread and thread not in (MAIN, F6, FEC)
    assert thread.startswith("code-mode-")


def test_without_main_thread_context_explicit_child_is_not_misattributed() -> None:
    # One-shot / transcript paths have no known main thread id. An explicit
    # differing threadId then cannot be judged, so deltas fail closed to main;
    # only the explicit code-mode-nested marker still nests (registry fallback).
    adapter = TraexJsonlAdapter()
    ctx = _ctx(main_thread_id=None)
    _spawn(adapter, F6)

    delta = adapter.normalize_line(
        {"method": "item/agentMessage/delta", "params": {"threadId": F6, "delta": "x"}}, ctx
    )
    assert "subagent_thread" not in delta[0].payload

    nested = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "id": "code-mode-nested:29:call_z:exec-2",
                    "command": ["ls"],
                }
            },
        },
        ctx,
    )
    assert nested[0].payload["subagent_thread"] == F6


def test_empty_or_blank_thread_id_is_treated_as_main() -> None:
    adapter = CodexJsonlAdapter()
    ctx = _ctx()
    for blank in ("", "   "):
        out = adapter.normalize_line(
            {"method": "item/agentMessage/delta", "params": {"threadId": blank, "delta": "d"}},
            ctx,
        )
        assert "subagent_thread" not in out[0].payload


def test_multi_spawn_nested_items_attribute_by_learned_host_owner() -> None:
    # Real tab 4ed6b70c turn 9ffdd6dd: three spawnAgent calls in one turn run
    # three parallel code-mode workers. The host emits SOME records (deltas /
    # first items) with an explicit ``threadId`` and later item/started|completed
    # records with only the ``code-mode-nested:29:<host>:…`` id. The adapter
    # must learn host -> child thread and never collapse the three into the
    # synthetic ``code-mode-29`` bucket.
    adapter = TraexJsonlAdapter()
    ctx = _ctx()
    t1 = "01a0e38b-b3c6-7642-93b0-37b8c4445674"
    t2 = "01a0e38b-c4a2-7d10-bf5d-70b73ec27dc4"
    t3 = "01a0e38b-d768-70f3-926a-5b18bd203db2"
    for thread in (t1, t2, t3):
        _spawn(adapter, thread)

    # A reasoning delta on the host carries the owner thread id and teaches the
    # host-token mapping even before that host's tool items arrive.
    learn = adapter.normalize_line(
        {
            "method": "item/reasoning/textDelta",
            "params": {
                "threadId": t2,
                "itemId": "code-mode-nested:29:call_HOST2:reasoning-1",
                "delta": "reviewing",
            },
        },
        ctx,
    )
    assert learn[0].payload["subagent_thread"] == t2

    def nested(host: str, exec_id: str, thread_id: str | None) -> dict:
        params: Dict[str, Any] = {
            "item": {
                "type": "commandExecution",
                "id": f"code-mode-nested:29:{host}:exec-{exec_id}",
                "command": ["ls"],
            }
        }
        if thread_id is not None:
            params["threadId"] = thread_id
        return {"method": "item/started", "params": params}

    # HOST1: the item/started itself carries the owner thread id.
    e1 = adapter.normalize_line(nested("call_HOST1", "111", t1), ctx)
    assert e1[0].payload["subagent_thread"] == t1
    # HOST2 owner was learned from the delta; the bare item must follow.
    e2 = adapter.normalize_line(nested("call_HOST2", "222", None), ctx)
    assert e2[0].payload["subagent_thread"] == t2
    # HOST3: learned from one threaded exec, the completion arrives bare.
    adapter.normalize_line(nested("call_HOST3", "333", t3), ctx)
    done3 = adapter.normalize_line(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "id": "code-mode-nested:29:call_HOST3:exec-333",
                    "exitCode": 0,
                    "aggregatedOutput": "ok",
                }
            },
        },
        ctx,
    )
    assert done3[0].payload["subagent_thread"] == t3
    # More exec items on an already-known host stay on that same child even
    # though three spawned threads exist (the old len(spawned)==1 bug path).
    e1b = adapter.normalize_line(nested("call_HOST1", "112", None), ctx)
    assert e1b[0].payload["subagent_thread"] == t1
    # A genuinely unknown host cannot be guessed: synthetic bucket, not main.
    unknown = adapter.normalize_line(nested("call_OTHER", "999", None), ctx)
    bucket = unknown[0].payload["subagent_thread"]
    assert bucket not in (MAIN, t1, t2, t3) and bucket.startswith("code-mode-")


def test_nested_owner_is_also_learned_from_item_level_thread_field() -> None:
    # Some protocol versions put the owner on the item itself
    # (``senderThreadId``) rather than the notification params.
    adapter = TraexJsonlAdapter()
    ctx = _ctx()
    t1 = "01a0e38b-b3c6-7642-93b0-37b8c4445674"
    t2 = "01a0e38b-c4a2-7d10-bf5d-70b73ec27dc4"
    _spawn(adapter, t1)
    _spawn(adapter, t2)
    first = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "id": "code-mode-nested:29:call_H:exec-1",
                    "senderThreadId": t1,
                    "command": ["pwd"],
                }
            },
        },
        ctx,
    )
    assert first[0].payload["subagent_thread"] == t1
    second = adapter.normalize_line(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "id": "code-mode-nested:29:call_H:exec-1",
                    "exitCode": 0,
                }
            },
        },
        ctx,
    )
    assert second[0].payload["subagent_thread"] == t1


def test_registry_is_scoped_per_session() -> None:
    adapter = TraexJsonlAdapter()
    _spawn(adapter, F6)  # learned under session s1 via _ctx()
    # A different session must not inherit s1's spawned thread registry.
    other = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "id": "code-mode-nested:29:call_q:exec-3",
                    "command": ["ls"],
                }
            },
        },
        _ctx(session_id="s2"),
    )
    # s2 has no spawned thread and no explicit thread id → synthetic group,
    # never s1's F6.
    assert other[0].payload["subagent_thread"] != F6


# ── persistence + cold reopen ────────────────────────────────────────────────
#
# TraeX/Codex Chat is a NATIVE session: on reopen the UI history is replayed
# from the flat persisted ``AgentStreamStore`` JSONL — there is no raw-rollout
# re-normalization for native sessions (``TraexJsonlAdapter.discover_source``
# returns None and the native poll loop returns before ``_tail_file``). So the
# multi-spawn grouping only survives a tab reopen if ``subagent_thread`` is
# persisted verbatim into that flat stream and survives history compaction.
# These tests pin that end-to-end contract (adapter -> store -> cold replay).


T1 = "01a0e38b-b3c6-7642-93b0-37b8c4445674"
T2 = "01a0e38b-c4a2-7d10-bf7d-70b73ec27dc4"
T3 = "01a0e38b-d768-70f3-926a-5b18bd203db2"


def _multi_spawn_events(
    adapter: CodexJsonlAdapter, ctx: NormalizeContext
) -> List[AgentStreamEvent]:
    """Live JSON-RPC shape for one turn with three parallel spawned workers."""
    events: List[AgentStreamEvent] = []

    def _spawn(thread: str) -> None:
        events.extend(
            adapter.normalize_line(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": MAIN,
                        "item": {
                            "type": "collabAgentToolCall",
                            "id": f"sp-{thread[:6]}",
                            "tool": "spawnAgent",
                            "prompt": "work",
                            "receiverThreadIds": [thread],
                        },
                    },
                },
                ctx,
            )
        )

    def _nested(host: str, exec_id: str, thread_id: str | None) -> None:
        params: Dict[str, Any] = {
            "item": {
                "type": "commandExecution",
                "id": f"code-mode-nested:29:{host}:exec-{exec_id}",
                "command": ["ls"],
            }
        }
        if thread_id is not None:
            params["threadId"] = thread_id
        events.extend(adapter.normalize_line({"method": "item/started", "params": params}, ctx))

    for thread in (T1, T2, T3):
        _spawn(thread)
    events.extend(
        adapter.normalize_line(
            {"method": "item/agentMessage/delta", "params": {"threadId": MAIN, "delta": "MAIN"}},
            ctx,
        )
    )
    # T2 owner learned from a reasoning delta carrying its host token.
    events.extend(
        adapter.normalize_line(
            {
                "method": "item/reasoning/textDelta",
                "params": {
                    "threadId": T2,
                    "itemId": "code-mode-nested:29:H2:r-1",
                    "delta": "think a",
                },
            },
            ctx,
        )
    )
    events.extend(
        adapter.normalize_line(
            {
                "method": "item/reasoning/textDelta",
                "params": {
                    "threadId": T2,
                    "itemId": "code-mode-nested:29:H2:r-1",
                    "delta": " think b",
                },
            },
            ctx,
        )
    )
    events.extend(
        adapter.normalize_line(
            {"method": "item/agentMessage/delta", "params": {"threadId": T1, "delta": "t1 report"}},
            ctx,
        )
    )
    _nested("H1", "1", T1)  # explicit owner
    _nested("H2", "2", None)  # bare; learned from the T2 reasoning delta
    _nested("H3", "3", T3)
    events.extend(
        adapter.normalize_line(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "id": "code-mode-nested:29:H3:exec-3",
                        "exitCode": 0,
                        "aggregatedOutput": "ok",
                    }
                },
            },
            ctx,
        )
    )
    return events


async def test_subthread_attribution_survives_persistence_and_cold_reopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    wm_pkg = importlib.import_module("claude_hub.services.workspace_manager")
    monkeypatch.setattr(wm_pkg, "STATE_ROOT", Path(tempfile.mkdtemp()))

    adapter = TraexJsonlAdapter()
    ctx = _ctx()
    live_store = AgentStreamStore("ws1", "s1")
    for event in _multi_spawn_events(adapter, ctx):
        await live_store.append(event)

    # Simulate a backend/tab restart: a fresh store instance reads ONLY the
    # persisted flat JSONL (the path the SSE history endpoint replays), with
    # history compaction enabled.
    cold_store = AgentStreamStore("ws1", "s1")
    events = list((await cold_store.read_since(-1, limit=500, compact=True)).events)

    def _thread(event: AgentStreamEvent) -> object:
        return event.payload.get("subagent_thread")

    # ① multi-spawn nested tools keep their OWN child thread id (no collapse).
    nested = {
        str(e.call_id): _thread(e)
        for e in events
        if e.type == AgentStreamEventType.TOOL_CALL_STARTED
        and str(e.call_id).startswith("code-mode-nested")
    }
    assert nested == {
        "code-mode-nested:29:H1:exec-1": T1,
        "code-mode-nested:29:H2:exec-2": T2,
        "code-mode-nested:29:H3:exec-3": T3,
    }
    # The H3 completion arrived bare (no threadId) but still follows its host.
    completed = {
        str(e.call_id): _thread(e)
        for e in events
        if e.type == AgentStreamEventType.TOOL_CALL_COMPLETED
    }
    assert completed["code-mode-nested:29:H3:exec-3"] == T3

    # spawnAgent instruction cards are the main agent's own: never stamped.
    spawn_cards = [
        e
        for e in events
        if e.type == AgentStreamEventType.TOOL_CALL_STARTED
        and e.payload.get("name") == "spawnAgent"
    ]
    assert spawn_cards and all(_thread(e) is None for e in spawn_cards)

    # ② the main turn's text contains only the main agent's own content.
    main_text = "".join(
        e.payload.get("text", "")
        for e in events
        if e.type == AgentStreamEventType.TEXT_DELTA and _thread(e) is None
    )
    assert "MAIN" in main_text
    assert "t1 report" not in main_text and "think" not in main_text

    child_text = {
        _thread(e): e.payload.get("text", "")
        for e in events
        if e.type == AgentStreamEventType.TEXT_DELTA and _thread(e) is not None
    }
    assert child_text.get(T1) == "t1 report"

    # The two T2 thinking deltas compact into ONE child row (thread-scoped
    # message id keeps it off the main ``turn-1:thinking`` row).
    thinking = {
        _thread(e): e.payload.get("text", "")
        for e in events
        if e.type == AgentStreamEventType.THINKING_DELTA
    }
    assert thinking == {T2: "think a think b"}
    assert not any(
        e.type == AgentStreamEventType.THINKING_DELTA and _thread(e) is None for e in events
    )


def test_reasoning_item_emits_truthful_thinking_status() -> None:
    """A reasoning item with no text still surfaces a process indicator.

    The current app-server emits ``item/started`` + ``item/completed`` for
    reasoning items but NO ``item/reasoning/textDelta`` (content/summary are
    empty; the reasoning is encrypted server-side). The adapter must emit a
    truthful in-flight status instead of dropping the item, so the user sees
    process activity without fabricated reasoning content.
    """
    adapter = CodexJsonlAdapter()
    rs_id = "rs_0b197a71508c0c82016aba900f82dc87d097031c4f3d2b55d4"

    started = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "threadId": MAIN,
                "item": {"type": "reasoning", "id": rs_id, "summary": [], "content": []},
            },
        },
        _ctx(),
    )
    assert len(started) == 1
    assert started[0].type == AgentStreamEventType.STATUS
    assert started[0].payload["text"] == "Thinking…"
    assert started[0].payload["snapshot"] is True
    assert started[0].message_id == f"reasoning:{rs_id}"

    completed = adapter.normalize_line(
        {
            "method": "item/completed",
            "params": {
                "threadId": MAIN,
                "item": {"type": "reasoning", "id": rs_id, "summary": [], "content": []},
            },
        },
        _ctx(),
    )
    assert len(completed) == 1
    assert completed[0].type == AgentStreamEventType.STATUS
    # Same stable message_id + snapshot: the frontend replaces the start
    # indicator in place rather than appending a second status.
    assert completed[0].payload["text"] == "Done thinking"
    assert completed[0].message_id == f"reasoning:{rs_id}"
    assert completed[0].payload["snapshot"] is True


def test_child_reasoning_status_routed_to_subthread() -> None:
    """A child's reasoning STATUS must carry the subthread id, not appear on main.

    Regression for review defect (1): the reasoning branch previously returned
    before ``_resolve_sub_thread``, so the STATUS got no ``subagent_thread``
    and appeared in the main stream.
    """
    adapter = CodexJsonlAdapter()
    _spawn(adapter, F6)
    rs_id = "rs_child_001"

    started = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "threadId": F6,
                "item": {"type": "reasoning", "id": rs_id, "summary": [], "content": []},
            },
        },
        _ctx(),
    )
    assert len(started) == 1
    assert started[0].type == AgentStreamEventType.STATUS
    assert started[0].payload["text"] == "Thinking…"
    # The STATUS must be routed to the child thread.
    assert started[0].payload.get("subagent_thread") == F6
    assert started[0].message_id == f"reasoning:{rs_id}"

    completed = adapter.normalize_line(
        {
            "method": "item/completed",
            "params": {
                "threadId": F6,
                "item": {"type": "reasoning", "id": rs_id, "summary": [], "content": []},
            },
        },
        _ctx(),
    )
    assert completed[0].payload["text"] == "Done thinking"
    assert completed[0].payload.get("subagent_thread") == F6


def test_cancelled_turn_finalizes_inflight_thinking_status() -> None:
    """A cancelled/interrupted turn must not leave a stale Thinking… status.

    Regression for review defect (2): when the turn terminates without
    ``item/completed`` for an in-flight reasoning item, the "Thinking…" status
    stayed stale. The turn/completed handler must finalize it in place.
    """
    adapter = CodexJsonlAdapter()
    rs_id = "rs_cancel_001"

    # Start reasoning (in-flight).
    started = adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "threadId": MAIN,
                "item": {"type": "reasoning", "id": rs_id, "summary": [], "content": []},
            },
        },
        _ctx(),
    )
    assert started[0].payload["text"] == "Thinking…"

    # Turn is interrupted (cancelled) WITHOUT item/completed for reasoning.
    completed = adapter.normalize_line(
        {
            "method": "turn/completed",
            "params": {
                "turn": {"id": "turn-1", "status": "interrupted"},
            },
        },
        _ctx(),
    )
    # The turn_completed event plus a final status that replaces the stale
    # "Thinking…" in place (same message_id + snapshot).
    status_events = [e for e in completed if e.type == AgentStreamEventType.STATUS]
    assert len(status_events) == 1
    assert status_events[0].payload["text"] == "Thinking interrupted"
    assert status_events[0].message_id == f"reasoning:{rs_id}"
    assert status_events[0].payload["snapshot"] is True


def test_cancelled_child_turn_finalizes_inflight_thinking_status() -> None:
    """A cancelled child turn finalizes the child's in-flight Thinking status."""
    adapter = CodexJsonlAdapter()
    _spawn(adapter, F6)
    rs_id = "rs_child_cancel_001"

    adapter.normalize_line(
        {
            "method": "item/started",
            "params": {
                "threadId": F6,
                "item": {"type": "reasoning", "id": rs_id, "summary": [], "content": []},
            },
        },
        _ctx(),
    )

    completed = adapter.normalize_line(
        {
            "method": "turn/completed",
            "params": {
                "turn": {"id": "turn-1", "status": "interrupted"},
            },
        },
        _ctx(),
    )
    status_events = [e for e in completed if e.type == AgentStreamEventType.STATUS]
    assert len(status_events) == 1
    assert status_events[0].payload["text"] == "Thinking interrupted"
    # The final status stays routed to the child thread.
    assert status_events[0].payload.get("subagent_thread") == F6
    assert status_events[0].message_id == f"reasoning:{rs_id}"
