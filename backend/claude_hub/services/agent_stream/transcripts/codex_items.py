"""CodexItems with per-adapter state inherited from its typed owner."""

from __future__ import annotations

from typing import Any, Dict, List

from ....models import AgentStreamEvent, AgentStreamEventType
from ....models.agent_stream import normalize_provider_usage
from ..base import NormalizeContext
from ..fork_seed import strip_fork_seed_history
from ..native import codex_normalize_questions, strip_hub_runtime_guidance
from .codex_ownership import CodexOwnership
from .codex_values import (
    _COLLAB_MESSAGE_TOOLS,
    _COLLAB_SPAWN_TOOLS,
    _clean_thread_id,
    _codex_extract_text,
    _codex_parse_arguments,
    _codex_tool_args,
    _is_fernet_token,
)


class CodexItems(CodexOwnership):
    """Normalize this protocol layer without duplicating adapter state."""

    @staticmethod
    def _plan_message_id(item_id: Any, ctx: NormalizeContext) -> str:
        return (
            f"plan:{item_id if isinstance(item_id, str) and item_id else ctx.turn_id or 'proposal'}"
        )

    def _normalize_tool_item(
        self,
        item: Any,
        method: str,
        ctx: NormalizeContext,
        params_thread_id: Any = None,
    ) -> List[AgentStreamEvent]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return []
        kind = item.get("type")
        if kind == "plan":
            # The protocol explicitly says deltas need not match the final
            # plan. Publish an authoritative replacement, including empty
            # snapshots, instead of dropping it or appending it twice.
            if method != "item/completed" or not isinstance(item.get("text"), str):
                return []
            return [
                ctx.event(
                    AgentStreamEventType.TEXT_DELTA,
                    {
                        "text": item["text"],
                        "plan": True,
                        "plan_kind": "proposal",
                        "snapshot": True,
                    },
                    message_id=self._plan_message_id(item["id"], ctx),
                )
            ]
        if kind == "reasoning":
            # The current app-server emits ``item/started`` + ``item/completed``
            # for reasoning items but NO ``item/reasoning/textDelta`` — the
            # reasoning content/summary are empty and the actual reasoning is
            # encrypted server-side (only token counts are exposed via
            # ``thread/tokenUsage/updated``). Emit a truthful in-flight status
            # so the user sees process activity without fabricating reasoning
            # content. The stable message_id + snapshot lets the completion
            # update replace the start indicator in place.
            if method not in ("item/started", "item/completed"):
                return []
            text = "Thinking…" if method == "item/started" else "Done thinking"
            # Route to the owning child thread (a child's reasoning must not
            # appear on the main stream).
            sub_thread = self._resolve_sub_thread(
                {"threadId": params_thread_id} if isinstance(params_thread_id, str) else {},
                ctx,
                item["id"],
                item,
            )
            # Track in-flight reasoning so a cancelled/interrupted turn can
            # finalize the status instead of leaving "Thinking…" stale.
            turn_key = ctx.turn_id or ctx.session_id
            if method == "item/started":
                self._inflight_reasoning.setdefault(turn_key, {})[item["id"]] = sub_thread
            else:
                self._inflight_reasoning.get(turn_key, {}).pop(item["id"], None)
            return [
                ctx.event(
                    AgentStreamEventType.STATUS,
                    {
                        "text": text,
                        "provider_status": "reasoning",
                        "snapshot": True,
                    },
                    message_id=f"reasoning:{item['id']}",
                    sub_thread_id=sub_thread,
                )
            ]
        name: str
        args: Dict[str, Any]
        result: Any
        if kind == "commandExecution":
            name, args = "exec_command", {"cmd": item.get("command"), "cwd": item.get("cwd")}
            result = item.get("aggregatedOutput") or ""
        elif kind == "fileChange":
            name, args = "apply_patch", {"changes": item.get("changes", [])}
            result = args
        elif kind in {"mcpToolCall", "dynamicToolCall"}:
            name = str(item.get("tool") or kind)
            args = _codex_parse_arguments(item.get("arguments"))
            result = item.get("error") or item.get("result") or item.get("contentItems")
        elif kind == "webSearch":
            name, args = "web_search", {"query": item.get("query")}
            result = item.get("action")
        elif kind == "imageView":
            name, args = "view_image", {"path": item.get("path")}
            result = ""
        elif kind in {"collabAgentToolCall", "CollabAgentToolCall"}:
            name = str(item.get("tool") or "collabAgentToolCall")
            receivers = item.get("receiverThreadIds")
            if not isinstance(receivers, list):
                receivers = item.get("receiver_thread_ids")
            args = {
                "prompt": item.get("prompt"),
                "receiverThreadIds": receivers if isinstance(receivers, list) else [],
            }
            for extra_key, dst_key in (
                ("model", "model"),
                ("reasoningEffort", "reasoningEffort"),
                ("reasoning_effort", "reasoningEffort"),
                ("agentPath", "agentPath"),
                ("agent_path", "agentPath"),
            ):
                val = _clean_thread_id(item.get(extra_key))
                if val is not None:
                    args.setdefault(dst_key, val)
            result = item.get("agentsStates")
            if result is None:
                result = item.get("agents_states")
        elif kind in {"subAgentActivity", "SubAgentActivity"}:
            return self._normalize_subagent_activity_item(item, method, ctx)
        else:
            # Text/reasoning/plan items already arrive as deltas.
            return []
        call_id = item["id"]
        # Collab calls (spawnAgent/sendInput) are issued BY the main agent and
        # stay on the main stream as instruction cards; they also teach us the
        # receiver threads. Every other item that carries a sub-thread id (or a
        # ``code-mode-nested`` item id) is the child's own work and is nested.
        is_collab = kind in {"collabAgentToolCall", "CollabAgentToolCall"} and (
            name in _COLLAB_SPAWN_TOOLS
            or name in _COLLAB_MESSAGE_TOOLS
            or name == "collabAgentToolCall"
        )
        if is_collab:
            self._remember_collab_threads(name, args, ctx)
        sub_thread = None
        if not is_collab:
            sub_thread = self._resolve_sub_thread(
                {"threadId": params_thread_id} if isinstance(params_thread_id, str) else {},
                ctx,
                call_id,
                item,
            )
        if method == "item/started":
            return [
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    {
                        "tool_call_id": call_id,
                        "name": name,
                        "args": args,
                    },
                    call_id=call_id,
                    sub_thread_id=sub_thread,
                )
            ]
        failed = item.get("status") in {"failed", "declined"} or item.get("success") is False
        if kind == "commandExecution" and item.get("exitCode") not in (None, 0):
            failed = True
        payload = {
            "tool_call_id": call_id,
            "status": "failed" if failed else "completed",
            "result": _codex_extract_text(result),
        }
        if is_collab:
            # Spawn may receive its thread id only in item/completed. Preserve
            # the completed item's structured identity so the reducer can
            # recover the call from its temporary generic-tool presentation.
            payload.update({"name": name, "args": args})
        return [
            ctx.event(
                AgentStreamEventType.TOOL_CALL_COMPLETED,
                payload,
                call_id=call_id,
                sub_thread_id=sub_thread,
            )
        ]

    def _normalize_subagent_activity_item(
        self,
        item: Dict[str, Any],
        method: str,
        ctx: NormalizeContext,
    ) -> List[AgentStreamEvent]:
        """Normalize Codex ``SubAgentActivity`` / ``subAgentActivity`` items."""
        target_thread = _clean_thread_id(item.get("agentThreadId") or item.get("agent_thread_id"))
        if target_thread is None:
            return []
        agent_path = _clean_thread_id(item.get("agentPath") or item.get("agent_path"))
        main_id = _clean_thread_id(ctx.main_thread_id)
        if agent_path in ("/root", "root", "main") or (
            main_id is not None and target_thread == main_id
        ):
            return []
        short_name = (
            agent_path.rsplit("/", 1)[-1]
            if agent_path and agent_path not in ("/root", "root")
            else None
        )
        if agent_path:
            self._agent_path_threads.setdefault(ctx.session_id, {})[agent_path] = target_thread
            if short_name:
                self._agent_path_threads[ctx.session_id][short_name] = target_thread
        activity_kind = item.get("kind")
        if activity_kind == "completed":
            if method == "item/completed" and ctx.turn_id:
                return [self._child_lifecycle_event(ctx, target_thread, "completed")]
            return []
        if activity_kind not in ("started", "interacted"):
            return []
        call_id = str(item["id"])
        name = "spawnAgent" if activity_kind == "started" else "sendInput"
        default_prompt = (
            f"Spawned {short_name or target_thread}"
            if activity_kind == "started"
            else f"Follow-up to {short_name or target_thread}"
        )
        raw_prompt = _clean_thread_id(item.get("prompt"))
        saved_prompt = self._collab_call_prompts.get(ctx.session_id, {}).pop(call_id, None)
        explicit_prompt = (
            raw_prompt if raw_prompt and not _is_fernet_token(raw_prompt) else saved_prompt
        )
        args: Dict[str, Any] = {
            "prompt": explicit_prompt or default_prompt,
            "receiverThreadIds": [target_thread],
        }
        if not explicit_prompt:
            args["syntheticPrompt"] = True
        if short_name:
            args["subagentType"] = short_name
        if agent_path:
            args["agentPath"] = agent_path
        model = _clean_thread_id(item.get("model"))
        effort = _clean_thread_id(item.get("reasoningEffort") or item.get("reasoning_effort"))
        if model:
            args["model"] = model
        if effort:
            args["reasoningEffort"] = effort
        self._remember_collab_threads(name, args, ctx)
        events: List[AgentStreamEvent] = []
        started_set = self._started_collab_call_ids.setdefault(ctx.session_id, set())
        if method == "item/started":
            if call_id not in started_set:
                started_set.add(call_id)
                events.append(
                    ctx.event(
                        AgentStreamEventType.TOOL_CALL_STARTED,
                        {"tool_call_id": call_id, "name": name, "args": args},
                        call_id=call_id,
                    )
                )
        else:
            if call_id not in started_set:
                started_set.add(call_id)
                events.append(
                    ctx.event(
                        AgentStreamEventType.TOOL_CALL_STARTED,
                        {"tool_call_id": call_id, "name": name, "args": args},
                        call_id=call_id,
                    )
                )
            self._completed_collab_call_ids.setdefault(ctx.session_id, set()).add(call_id)
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_COMPLETED,
                    {
                        "tool_call_id": call_id,
                        "name": name,
                        "args": args,
                        "status": "completed",
                        "result": "",
                    },
                    call_id=call_id,
                )
            )
        if ctx.turn_id:
            events.append(self._child_lifecycle_event(ctx, target_thread, "active"))
        return events

    def _normalize_question(
        self, params: Dict[str, Any], ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        """Emit a tool call + approval card for a blocking user question.

        The app-server blocks the turn on ``requestUserInput``; the card lets
        the user answer, and the tailer routes the answer back as the JSON-RPC
        response (see ``CodexNativeSession.answer_pending_question``).
        """
        events: List[AgentStreamEvent] = []
        questions = codex_normalize_questions(params.get("questions"))
        if not questions:
            return events
        item_id = params.get("itemId")
        call_id = str(item_id) if item_id is not None else "request_user_input"
        events.append(
            ctx.event(
                AgentStreamEventType.TOOL_CALL_STARTED,
                {"name": "request_user_input", "args": params},
                call_id=call_id,
            )
        )
        events.append(
            ctx.event(
                AgentStreamEventType.APPROVAL_REQUIRED,
                {
                    "tool_call_id": call_id,
                    "kind": "ask_question",
                    "title": questions[0].get("prompt"),
                    "questions": questions,
                },
                call_id=call_id,
            )
        )
        return events

    def _normalize_event_msg(
        self, payload: Dict[str, Any], payload_type: str, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        if payload_type == "user_message":
            text = payload.get("message")
            if isinstance(text, str):
                # Strip the sentinel-wrapped Hub Chat runtime guidance the
                # transport prepends on the first turn so it never reaches the
                # persisted timeline or the UI (no-op on later turns).
                text = strip_hub_runtime_guidance(text)
                # Strip the one-shot fork-seed history block likewise.
                text = strip_fork_seed_history(text)
            if isinstance(text, str) and text.strip():
                events.append(ctx.event(AgentStreamEventType.TURN_STARTED, {"summary": text}))
        elif payload_type == "agent_reasoning":
            text = payload.get("text")
            if isinstance(text, str) and text:
                events.append(ctx.event(AgentStreamEventType.THINKING_DELTA, {"text": text}))
        elif payload_type in {"error", "stream_error"}:
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                detail = payload.get("error")
                if isinstance(detail, dict):
                    message = detail.get("message")
                elif isinstance(detail, str):
                    message = detail
            if isinstance(message, str) and message.strip():
                events.append(ctx.event(AgentStreamEventType.ERROR, {"message": message}))
        elif payload_type in {"item_started", "item_completed"}:
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") in {
                "SubAgentActivity",
                "subAgentActivity",
                "CollabAgentToolCall",
                "collabAgentToolCall",
            }:
                method = "item/started" if payload_type == "item_started" else "item/completed"
                events.extend(
                    self._normalize_tool_item(
                        item, method, ctx, payload.get("thread_id") or payload.get("threadId")
                    )
                )
        elif payload_type == "task_complete":
            completed: Dict[str, Any] = {"status": "completed"}
            summary = payload.get("last_agent_message")
            if isinstance(summary, str) and summary.strip():
                completed["summary"] = summary
            usage = normalize_provider_usage(payload, "codex")
            if usage is not None:
                completed["usage"] = usage
            events.append(ctx.event(AgentStreamEventType.TURN_COMPLETED, completed))
        return events

    def _normalize_response_item(
        self, payload: Dict[str, Any], payload_type: str, ctx: NormalizeContext
    ) -> List[AgentStreamEvent]:
        events: List[AgentStreamEvent] = []
        if payload_type == "message":
            role = payload.get("role")
            if role == "assistant":
                text = _codex_extract_text(payload.get("content"))
                if text:
                    events.append(ctx.event(AgentStreamEventType.TEXT_DELTA, {"text": text}))
        elif payload_type == "agent_message":
            author = _clean_thread_id(payload.get("author"))
            recipient = _clean_thread_id(payload.get("recipient"))
            sub_thread = (
                self._agent_path_threads.get(ctx.session_id, {}).get(author)
                if author and author not in ("/root", "root") and recipient in ("/root", "root")
                else None
            )
            if sub_thread:
                raw_text = _codex_extract_text(payload.get("content"))
                marker = "\nPayload:\n"
                report = (
                    raw_text.split(marker, 1)[1].strip() if marker in raw_text else raw_text.strip()
                )
                if report and not _is_fernet_token(report):
                    is_final = "Message Type: FINAL_ANSWER" in raw_text
                    events.append(
                        ctx.event(
                            AgentStreamEventType.TEXT_DELTA,
                            {"text": report, **({"final_report": True} if is_final else {})},
                            sub_thread_id=sub_thread,
                        )
                    )
        elif payload_type == "function_call":
            name = payload.get("name") or "unknown"
            call_id = payload.get("call_id")
            tool_call_id = call_id if isinstance(call_id, str) else None
            args = _codex_parse_arguments(payload.get("arguments"))
            if name in _COLLAB_SPAWN_TOOLS or name in _COLLAB_MESSAGE_TOOLS:
                target = _clean_thread_id(args.get("target") or args.get("task_name"))
                if target in ("/root", "root", "main"):
                    if tool_call_id:
                        self._completed_collab_call_ids.setdefault(ctx.session_id, set()).add(
                            tool_call_id
                        )
                    return events
                short_target = target.rsplit("/", 1)[-1] if target else None
                raw_msg = _clean_thread_id(args.get("prompt") or args.get("message"))
                if _is_fernet_token(args.get("message")):
                    fallback_msg = (
                        f"Spawned {short_target or 'sub-agent'}"
                        if name in _COLLAB_SPAWN_TOOLS
                        else f"Follow-up to {short_target or 'sub-agent'}"
                    )
                    args["message"] = fallback_msg
                    args.setdefault("prompt", fallback_msg)
                    args["syntheticPrompt"] = True
                elif raw_msg:
                    args.setdefault("prompt", raw_msg)
                    if tool_call_id:
                        self._collab_call_prompts.setdefault(ctx.session_id, {})[
                            tool_call_id
                        ] = raw_msg
                if short_target:
                    args.setdefault("subagentType", short_target)
                mapped = (
                    self._agent_path_threads.get(ctx.session_id, {}).get(target) if target else None
                )
                if mapped and "receiverThreadIds" not in args:
                    args["receiverThreadIds"] = [mapped]
                if tool_call_id:
                    self._started_collab_call_ids.setdefault(ctx.session_id, set()).add(
                        tool_call_id
                    )
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "args": args,
                    },
                    call_id=tool_call_id,
                )
            )
        elif payload_type == "function_call_output":
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id in self._completed_collab_call_ids.get(
                ctx.session_id, set()
            ):
                return events
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_COMPLETED,
                    {
                        "tool_call_id": call_id,
                        "status": "completed",
                        "result": _codex_extract_text(payload.get("output")),
                    },
                    call_id=call_id if isinstance(call_id, str) else None,
                )
            )
        elif payload_type == "custom_tool_call":
            name = payload.get("name") or "unknown"
            call_id = payload.get("call_id")
            tool_call_id = call_id if isinstance(call_id, str) else None
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_STARTED,
                    {
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "args": _codex_tool_args(payload.get("input")),
                    },
                    call_id=tool_call_id,
                )
            )
        elif payload_type == "custom_tool_call_output":
            call_id = payload.get("call_id")
            events.append(
                ctx.event(
                    AgentStreamEventType.TOOL_CALL_COMPLETED,
                    {
                        "tool_call_id": call_id,
                        "status": "completed",
                        "result": _codex_extract_text(payload.get("output")),
                    },
                    call_id=call_id if isinstance(call_id, str) else None,
                )
            )
        return events
