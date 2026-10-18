"""TraexSession implementation; compatibility dependencies are explicit."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ....models import ManagedSession
from .codex_rpc import CodexProtocolSession


class TraexSession(CodexProtocolSession):
    """TraeX 0.205.1: shared JSON-RPC framing, explicit v2 turn contracts.

    Model overrides use Hub's CODEX_MODEL key. TraeX requires turn/interrupt
    with provider IDs, and command/file approvals use decision responses.
    """

    adapter_id = "traex-native"

    def _build_command(self) -> List[str]:
        # traex has no ``--stdio`` flag; stdio:// is the default listener.
        return ["traex", "app-server"]

    def __init__(
        self,
        session: ManagedSession,
        conversation_id_persist: Optional[Callable[[str], None]] = None,
        seed_history: Optional[str] = None,
        on_seed_consumed: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        super().__init__(
            session,
            conversation_id_persist=conversation_id_persist,
            seed_history=seed_history,
            on_seed_consumed=on_seed_consumed,
        )
        self._discard_turn_id: Optional[str] = None
        self._interrupted = asyncio.Event()
        self._pending_permissions: Dict[Any, Dict[str, str]] = {}

    def _thread_config(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {"cwd": self._cwd, **self._permission_config()}
        model = self._selected_model_override()
        if model:
            config["model"] = model
        return config

    # Thread/start and turn/start use different sandbox spellings.
    _THREAD_SANDBOX = {
        "readOnly": "read-only",
        "workspaceWrite": "workspace-write",
        "dangerFullAccess": "danger-full-access",
    }

    def _permission_config(self) -> Dict[str, Any]:
        plan, solo = self._sandbox_plan_solo()
        return {
            "approvalPolicy": "never" if solo else "on-request",
            "sandbox": self._THREAD_SANDBOX[self._sandbox_type(plan, solo)],
        }

    def _turn_config(self) -> Dict[str, Any]:
        config = self._permission_config()
        plan, solo = self._sandbox_plan_solo()
        config.pop("sandbox")  # kebab thread field; turn/start uses sandboxPolicy
        return {**config, "sandboxPolicy": self._sandbox_policy(plan, solo)}

    @staticmethod
    def _notification_turn_id(record: Dict[str, Any]) -> Optional[str]:
        params = record.get("params", {})
        if not isinstance(params, dict):
            return None
        turn = params.get("turn")
        value = turn.get("id") if isinstance(turn, dict) else params.get("turnId")
        return value if isinstance(value, str) else None

    async def _handle_notification(self, record: Dict[str, Any]) -> None:
        if not self.accepts_notification(record):
            if record.get("method") == "turn/completed":
                self._interrupted.set()
            return
        await super()._handle_notification(record)

    def accepts_notification(self, record: Dict[str, Any]) -> bool:
        return not (
            self._discard_turn_id and self._notification_turn_id(record) == self._discard_turn_id
        )

    async def cancel_active_turn(self) -> None:
        if not self._turn_in_flight:
            return
        self._discard_turn_id = self._provider_turn_id
        self._interrupted.clear()
        # Discard already queued output too: the tailer has published the
        # cancelled state and must never stamp old deltas with the next turn.
        retained = []
        while not self._notification_queue.empty():
            record = self._notification_queue.get_nowait()
            if record is not None and self._notification_turn_id(record) == self._discard_turn_id:
                if record.get("method") == "turn/completed":
                    self._interrupted.set()
            else:
                retained.append(record)
        for record in retained:
            self._notification_queue.put_nowait(record)
        try:
            if not self._interrupted.is_set():
                if not self._thread_id or not self._provider_turn_id:
                    raise RuntimeError("TraeX did not return an active turn id")
                await self._send_request(
                    "turn/interrupt",
                    {"threadId": self._thread_id, "turnId": self._provider_turn_id},
                )
                await asyncio.wait_for(
                    self._interrupted.wait(), timeout=self._runtime.startup_grace_s()
                )
        except Exception:
            # An unconfirmed interrupt must actually stop execution, but simply
            # killing the app-server used to fail the session closed (the EOF
            # surfaced "Retry" and the tab could neither send nor resume). Kill
            # and relaunch instead: the fresh app-server thread/resumes the same
            # conversation, so the next message works.
            self._runtime.logger().exception(
                "TraeX interrupt failed; restarting app-server to resume the thread"
            )
            try:
                await self.restart_for_recovery()
            except Exception:
                self._runtime.logger().exception(
                    "TraeX app-server restart after failed interrupt failed for thread %s",
                    self._thread_id,
                )
        finally:
            self._pending_questions.clear()
            self._pending_permissions.clear()
            self._clear_staged_images()
            self._cleanup_images(self._inflight_images)
            self._inflight_images = []
            self._end_turn()

    async def restart_for_recovery(self) -> None:
        """Restart the app-server and forget the interrupted turn's filters.

        ``_discard_turn_id`` belongs to the dead process's turn; in the resumed
        process it would otherwise suppress unrelated notifications whose
        turn-id happens to compare equal.
        """
        await super().restart_for_recovery()
        self._discard_turn_id = None
        self._interrupted.clear()

    async def stop(self) -> None:
        self._pending_permissions.clear()
        await super().stop()

    async def _handle_server_request(self, record: Dict[str, Any]) -> None:
        method = record.get("method")
        if not self.accepts_notification(record):
            # A permission request already in transit when Stop was pressed
            # must not reopen a card or leave the interrupted turn blocked.
            await self._send_jsonrpc_response(
                record.get("id"), error={"code": -32600, "message": "turn interrupted"}
            )
            return
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            await super()._handle_server_request(record)
            return
        req_id = record.get("id")
        params = record.get("params", {})
        if not isinstance(params, dict):
            await self._send_jsonrpc_response(req_id, result={"decision": "decline"})
            return
        decisions = params.get("availableDecisions")
        allowed = decisions if isinstance(decisions, list) else ["accept", "decline"]
        choices = {
            label: decision
            for label, decision in [
                ("Allow once", "accept"),
                ("Reject", "decline"),
                ("Cancel turn", "cancel"),
            ]
            if decision in allowed
        }
        if not choices:
            await self._send_jsonrpc_response(req_id, result={"decision": "cancel"})
            return
        question_id = f"permission:{req_id}"
        self._pending_permissions[req_id] = choices
        command = params.get("command") or "Apply file changes"
        reason = params.get("reason") or "Approval required"
        await self._notification_queue.put(
            {
                "method": self._runtime.question_methods()[0],
                "params": {
                    "itemId": question_id,
                    "turnId": params.get("turnId"),
                    "questions": [
                        {
                            "id": question_id,
                            "header": "Permission",
                            "question": f"{reason}\n{command}",
                            "options": [{"label": label, "description": ""} for label in choices],
                        }
                    ],
                },
            }
        )

    async def answer_pending_question(
        self,
        answers: List[Dict[str, Any]],
        *,
        expected_call_id: Optional[str] = None,
    ) -> bool:
        selected = {
            a["questionId"]: a.get("selected")
            for a in answers
            if isinstance(a, dict) and isinstance(a.get("questionId"), str)
        }
        responses: List[Tuple[Any, Dict[str, Any]]] = []
        for req_id, choices in list(self._pending_permissions.items()):
            question_id = f"permission:{req_id}"
            if expected_call_id is not None and question_id != expected_call_id:
                continue
            if answers and question_id not in selected:
                continue
            values = selected.get(question_id)
            decision = (
                choices.get(values[0], "decline")
                if isinstance(values, list) and len(values) == 1 and isinstance(values[0], str)
                else "decline"
            )
            self._pending_permissions.pop(req_id, None)
            responses.append((req_id, {"decision": decision}))
        for req_id, params in list(self._pending_questions.items()):
            item_id = params.get("itemId")
            call_id = str(item_id) if item_id is not None else "request_user_input"
            if expected_call_id is not None and call_id != expected_call_id:
                continue
            questions = self._runtime.normalize_questions(params.get("questions"))
            ids = {question["id"] for question in questions}
            if answers and not ids <= selected.keys():
                continue
            result = {
                question_id: {"answers": [value for value in values if isinstance(value, str)]}
                for question_id, values in selected.items()
                if question_id in ids and isinstance(values, list)
            }
            self._pending_questions.pop(req_id, None)
            responses.append((req_id, {"answers": result}))
        # Claim the entire response batch before yielding to concurrent clicks.
        for req_id, result in responses:
            await self._send_jsonrpc_response(req_id, result=result)
        return bool(responses)
