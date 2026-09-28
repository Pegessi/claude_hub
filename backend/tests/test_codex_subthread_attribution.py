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

from typing import Any, Dict, List

from claude_hub.models import AgentStreamEvent, AgentStreamEventType, AgentType
from claude_hub.services.agent_stream.base import NormalizeContext
from claude_hub.services.agent_stream.codex_jsonl import CodexJsonlAdapter, TraexJsonlAdapter

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
